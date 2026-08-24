"""Non-destructive render: apply a track operation chain, then mix placements."""



import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from audio.channels import ChannelLayout, layout_by_name
from audio.clip import AudioClip
from audio.io import load
from audio.operations import Placement, mix as mix_clips
from app_context import Registry
from workspaces.models import Mix, Track


def _chain_key(track: Track) -> str:
    payload = json.dumps(
        {"id": track.id, "media": track.media_id, "ops": track.operations},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RenderCache:
    def __init__(self) -> None:
        self._clips: dict[str, AudioClip] = {}
        self._keys: dict[str, str] = {}

    def invalidate(self, track_id: str | None = None) -> None:
        if track_id is None:
            self._clips.clear()
            self._keys.clear()
            return
        self._clips.pop(track_id, None)
        self._keys.pop(track_id, None)

    def get(self, track_id: str, key: str) -> AudioClip | None:
        if self._keys.get(track_id) == key:
            return self._clips.get(track_id)
        return None

    def put(self, track_id: str, key: str, clip: AudioClip) -> None:
        self._keys[track_id] = key
        self._clips[track_id] = clip


def apply_operations(
    clip: AudioClip,
    operations: list[dict[str, Any]],
    effects: Registry,
) -> AudioClip:
    for spec in operations:
        op_name = spec.get("op")
        if not op_name:
            raise ValueError("Operation is missing 'op'.")
        params = {key: value for key, value in spec.items() if key != "op"}
        params.pop("rubberband_path", None)
        try:
            effect = effects.create(op_name)
        except KeyError as exc:
            raise ValueError(f"Unknown audio operation: {op_name}") from exc
        clip = effect.apply(clip, params)
    return clip


def render_track(
    track: Track,
    project_root: Path,
    effects: Registry,
    cache: RenderCache | None = None,
) -> AudioClip:
    key = _chain_key(track)
    if cache is not None:
        cached = cache.get(track.id, key)
        if cached is not None:
            return cached
    path = project_root / track.original.path
    clip = load(path)
    clip = apply_operations(clip, track.operations, effects)
    if cache is not None:
        cache.put(track.id, key, clip)
    return clip


def render_mix(
    tracks: list[Track],
    mix: Mix,
    project_root: Path,
    effects: Registry,
    samplerate: int,
    clip_protection: str = "headroom",
    cache: RenderCache | None = None,
) -> AudioClip:
    by_id = {track.id: track for track in tracks}
    layout: ChannelLayout = layout_by_name(mix.channel_layout)
    placements: list[Placement] = []
    for mix_clip in mix.clips:
        track = by_id.get(mix_clip.track_id)
        if track is None:
            continue
        rendered = render_track(
            track,
            project_root,
            effects,
            cache=cache,
        )
        placements.append(
            Placement(
                clip=rendered,
                offset_ms=mix_clip.offset_ms,
                gain_db=mix_clip.gain_db,
                mute=mix_clip.mute,
            )
        )
    return mix_clips(
        placements,
        layout=layout,
        samplerate=samplerate,
        clip_protection=clip_protection,
    )


def estimate_track_duration_ms(track: Track) -> int:
    duration = float(track.original.duration_ms)
    for spec in track.operations:
        op = spec.get("op")
        if op == "cut":
            start = float(spec.get("start_ms", 0))
            end = float(spec.get("end_ms", duration))
            mode = spec.get("mode", "keep")
            if mode == "keep":
                duration = np.maximum(0.0, end - start)
            else:
                duration = np.maximum(0.0, duration - (end - start))
        elif op == "speed":
            ratio = float(spec.get("ratio", 1.0))
            if ratio > 0:
                duration = duration / ratio
    return int(np.round(duration))


def estimate_mix_duration_ms(tracks: list[Track], mix: Mix) -> int:
    """Latest clip end on the mix timeline, matching what the ruler shows."""
    end = 1
    for track in tracks:
        mix_clip = mix.clip_for_track(track.id)
        offset = mix_clip.offset_ms if mix_clip else 0
        duration = int(np.maximum(1, estimate_track_duration_ms(track)))
        end = int(np.maximum(end, offset + duration))
    return int(np.maximum(end, 1))
