"""Non-destructive render: apply a track operation chain, then mix placements."""
import json
from pathlib import Path

from audio.channels import layout_by_name
from audio.clip import AudioClip
from audio.io import load
from audio.operations import OPERATIONS, Placement, mix as mix_clips
from workspaces.models import Mix, Track


def _chain_key(track: Track) -> str:
    return json.dumps(
        {"media": track.media_id, "ops": track.operations},
        sort_keys=True,
        separators=(",", ":"),
    )


class RenderCache:
    """Per-track cache of the rendered operation chain, keyed by that chain."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, AudioClip]] = {}

    def invalidate(self, track_id: str | None = None) -> None:
        if track_id is None:
            self._entries.clear()
        else:
            self._entries.pop(track_id, None)

    def get(self, track_id: str, key: str) -> AudioClip | None:
        entry = self._entries.get(track_id)
        return entry[1] if entry is not None and entry[0] == key else None

    def put(self, track_id: str, key: str, clip: AudioClip) -> None:
        self._entries[track_id] = (key, clip)


def apply_operations(clip: AudioClip, operations: list[dict]) -> AudioClip:
    for spec in operations:
        func = OPERATIONS.get(spec.get("op", ""))
        if func is None:
            raise ValueError(f"Unknown audio operation: {spec.get('op')}")
        clip = func(clip, **{key: value for key, value in spec.items() if key != "op"})
    return clip


def render_track(
    track: Track,
    project_root: Path,
    cache: RenderCache | None = None,
) -> AudioClip:
    key = _chain_key(track)
    if cache is not None:
        cached = cache.get(track.id, key)
        if cached is not None:
            return cached
    clip = apply_operations(load(project_root / track.original.path), track.operations)
    if cache is not None:
        cache.put(track.id, key, clip)
    return clip


def render_mix(
    tracks: list[Track],
    mix: Mix,
    project_root: Path,
    samplerate: int,
    clip_protection: str = "headroom",
    cache: RenderCache | None = None,
) -> AudioClip:
    by_id = {track.id: track for track in tracks}
    placements = [
        Placement(
            clip=render_track(by_id[mix_clip.track_id], project_root, cache=cache),
            offset_ms=mix_clip.offset_ms,
            gain_db=mix_clip.gain_db,
            mute=mix_clip.mute,
        )
        for mix_clip in mix.clips
        if mix_clip.track_id in by_id
    ]
    return mix_clips(
        placements,
        layout=layout_by_name(mix.channel_layout),
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
            span = end - start
            duration = span if spec.get("mode", "keep") == "keep" else duration - span
            duration = max(0.0, duration)
        elif op == "speed":
            ratio = float(spec.get("ratio", 1.0))
            if ratio > 0:
                duration /= ratio
    return round(duration)


def estimate_mix_duration_ms(tracks: list[Track], mix: Mix) -> int:
    """Latest clip end on the mix timeline, matching what the ruler shows."""
    offsets = {clip.track_id: clip.offset_ms for clip in mix.clips}
    ends = [
        offsets.get(track.id, 0) + max(1, estimate_track_duration_ms(track))
        for track in tracks
    ]
    return max(ends, default=1)
