"""Pure AudioClip transforms, plus the name -> transform table used by rendering."""
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import soxr

from audio.channels import (
    ChannelLayout,
    convert_layout,
    layout_by_name,
    pan_stereo,
)
from audio.clip import AudioClip
from audio.librosa.spectrum import _fix_length
from audio.librosa.timestretch import time_stretch


def _ms_to_frame(ms: float, samplerate: int) -> int:
    return int(round(ms / 1000.0 * samplerate))


def db_to_gain(gain_db: float) -> float:
    return float(10.0 ** (float(gain_db) / 20.0))


def _linear_resample(samples: np.ndarray, n_frames: int) -> np.ndarray:
    old_idx = np.linspace(0.0, 1.0, samples.shape[0], endpoint=False)
    new_idx = np.linspace(0.0, 1.0, n_frames, endpoint=False)
    return np.column_stack(
        [np.interp(new_idx, old_idx, samples[:, ch]) for ch in range(samples.shape[1])]
    ).astype(np.float32)


def resample(clip: AudioClip, target_rate: int) -> AudioClip:
    if clip.samplerate == target_rate:
        return clip
    if target_rate <= 0:
        raise ValueError("Target samplerate must be positive.")
    n_frames = int(np.ceil(clip.frames * target_rate / clip.samplerate))
    try:
        resampled = soxr.resample(clip.samples, clip.samplerate, target_rate, quality="HQ")
        samples = _fix_length(np.asarray(resampled).T, n_frames).T
    except Exception:
        samples = _linear_resample(clip.samples, max(1, n_frames))
    return AudioClip(
        samples=np.ascontiguousarray(samples, dtype=np.float32),
        samplerate=target_rate,
        layout=clip.layout,
    )


def match_layout(clip: AudioClip, layout: ChannelLayout) -> AudioClip:
    if clip.layout.name == layout.name and clip.channels == layout.count:
        return clip
    samples = convert_layout(clip.samples, clip.layout, layout)
    return AudioClip(samples=samples, samplerate=clip.samplerate, layout=layout)


def cut(clip: AudioClip, start_ms: float, end_ms: float, mode: str = "keep") -> AudioClip:
    start = max(0, _ms_to_frame(start_ms, clip.samplerate))
    end = min(clip.frames, _ms_to_frame(end_ms, clip.samplerate))
    if end < start:
        start, end = end, start
    if mode == "keep":
        samples = clip.samples[start:end]
    elif mode == "remove":
        samples = np.concatenate([clip.samples[:start], clip.samples[end:]], axis=0)
    else:
        raise ValueError(f"Unknown cut mode: {mode}")
    if samples.shape[0] == 0:
        samples = np.zeros((1, clip.channels), dtype=np.float32)
    return AudioClip(samples=samples, samplerate=clip.samplerate, layout=clip.layout)


def volume(clip: AudioClip, gain_db: float) -> AudioClip:
    gain = np.float32(db_to_gain(gain_db))
    return AudioClip(samples=clip.samples * gain, samplerate=clip.samplerate, layout=clip.layout)


def _fade_curve(frames: int, shape: str, reverse: bool) -> np.ndarray:
    t = np.linspace(0.0, 1.0, frames, dtype=np.float32)
    curve = np.sin(t * (np.pi / 2.0), dtype=np.float32) if shape == "exp" else t
    return curve[::-1] if reverse else curve


def _faded(clip: AudioClip, duration_ms: float, shape: str, out: bool) -> AudioClip:
    n = min(clip.frames, max(1, _ms_to_frame(duration_ms, clip.samplerate)))
    samples = clip.samples.copy()
    curve = _fade_curve(n, shape, reverse=out)[:, None]
    if out:
        samples[-n:] *= curve
    else:
        samples[:n] *= curve
    return AudioClip(samples=samples, samplerate=clip.samplerate, layout=clip.layout)


def fade_in(clip: AudioClip, duration_ms: float, shape: str = "linear") -> AudioClip:
    return _faded(clip, duration_ms, shape, out=False)


def fade_out(clip: AudioClip, duration_ms: float, shape: str = "linear") -> AudioClip:
    return _faded(clip, duration_ms, shape, out=True)


def reverse(clip: AudioClip) -> AudioClip:
    return AudioClip(
        samples=np.flip(clip.samples, axis=0).copy(),
        samplerate=clip.samplerate,
        layout=clip.layout,
    )


def speed(clip: AudioClip, ratio: float, preserve_pitch: bool = True) -> AudioClip:
    ratio = round(float(ratio), 2)
    if not 0.01 <= ratio <= 10.00:
        raise ValueError("Speed ratio must be between 0.01 and 10.00.")
    if ratio == 1.0:
        return clip
    if preserve_pitch:
        samples = np.column_stack(
            [
                np.asarray(time_stretch(clip.samples[:, ch], rate=ratio), dtype=np.float32)
                for ch in range(clip.channels)
            ]
        )
    else:
        samples = _linear_resample(clip.samples, max(1, round(clip.frames / ratio)))
    return AudioClip(samples=samples, samplerate=clip.samplerate, layout=clip.layout)


def channels_op(
    clip: AudioClip,
    target_layout: str,
    routing: dict[str, int] | None = None,
    pan: float | None = None,
) -> AudioClip:
    layout = layout_by_name(target_layout)
    samples = convert_layout(clip.samples, clip.layout, layout, routing=routing)
    if pan is not None and layout.name == "stereo":
        samples = pan_stereo(samples, pan)
    return AudioClip(samples=samples, samplerate=clip.samplerate, layout=layout)


OPERATIONS: dict[str, Callable[..., AudioClip]] = {
    "cut": cut,
    "fade_in": fade_in,
    "fade_out": fade_out,
    "volume": volume,
    "speed": speed,
    "reverse": reverse,
    "channels": channels_op,
}


@dataclass
class Placement:
    clip: AudioClip
    offset_ms: float = 0.0
    gain_db: float = 0.0
    mute: bool = False


def mix(
    placements: list[Placement],
    layout: ChannelLayout,
    samplerate: int,
    clip_protection: str = "headroom",
) -> AudioClip:
    active = [item for item in placements if not item.mute]
    if not active:
        return AudioClip(
            samples=np.zeros((1, layout.count), dtype=np.float32),
            samplerate=samplerate,
            layout=layout,
        )
    aligned: list[tuple[AudioClip, int, float]] = []
    last_frame = 1
    for item in active:
        clip = match_layout(resample(item.clip, samplerate), layout)
        offset = max(0, _ms_to_frame(item.offset_ms, samplerate))
        aligned.append((clip, offset, db_to_gain(item.gain_db)))
        last_frame = max(last_frame, offset + clip.frames)
    out = np.zeros((last_frame, layout.count), dtype=np.float32)
    for clip, offset, gain in aligned:
        out[offset : offset + clip.frames] += clip.samples * np.float32(gain)
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 1.0:
        if clip_protection == "limiter":
            out = np.tanh(out).astype(np.float32)
        else:
            out *= np.float32(0.99 / peak)
    return AudioClip(samples=out, samplerate=samplerate, layout=layout)
