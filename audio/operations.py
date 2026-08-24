"""Pure AudioClip transforms. Also used by the named effect registry."""
from dataclasses import dataclass

import numpy as np

from audio.channels import (
    ChannelLayout,
    convert_layout,
    layout_by_name,
    pan_stereo,
)
from audio.clip import AudioClip
from audio.librosa.timestretch import time_stretch

import numpy as np
import soxr

from audio.librosa.spectrum import _fix_length


def _ms_to_frame(ms: float, samplerate: int) -> int:
    return int(np.round(ms / 1000.0 * samplerate))


def db_to_gain(gain_db: float) -> float:
    return float(10.0 ** (float(gain_db) / 20.0))


def resample(clip: AudioClip, target_rate: int) -> AudioClip:
    if clip.samplerate == target_rate:
        return clip
    if target_rate <= 0:
        raise ValueError("Target samplerate must be positive.")
    try:
        orig_sr = clip.samplerate
        target_sr = target_rate
        if orig_sr == target_sr:
            stretched = np.asarray(clip.samples.T)
        else:
            y = np.asarray(clip.samples.T)
            ratio = float(target_sr) / float(orig_sr)
            n_samples = int(np.ceil(y.shape[-1] * ratio))
            if y.ndim == 1:
                y_hat = soxr.resample(y, orig_sr, target_sr, quality="HQ")
            else:
                frames = np.moveaxis(y, -1, 0)
                flat = np.reshape(frames, (frames.shape[0], -1))
                y_hat = soxr.resample(flat, orig_sr, target_sr, quality="HQ")
                y_hat = np.reshape(y_hat, (-1,) + frames.shape[1:])
                y_hat = np.moveaxis(y_hat, 0, -1)
            y_hat = _fix_length(np.asarray(y_hat), n_samples)
            stretched = np.asarray(y_hat, dtype=y.dtype)
        samples = np.ascontiguousarray(stretched.T, dtype=np.float32)
    except Exception:
        duration = clip.frames / clip.samplerate
        new_frames = int(np.maximum(1, np.round(duration * target_rate)))
        old_idx = np.linspace(0.0, 1.0, clip.frames, endpoint=False)
        new_idx = np.linspace(0.0, 1.0, new_frames, endpoint=False)
        samples = np.column_stack(
            [np.interp(new_idx, old_idx, clip.samples[:, ch]) for ch in range(clip.channels)]
        ).astype(np.float32)
    return AudioClip(samples=samples, samplerate=target_rate, layout=clip.layout)


def match_layout(clip: AudioClip, layout: ChannelLayout) -> AudioClip:
    if clip.layout.name == layout.name and clip.channels == layout.count:
        return clip
    samples = convert_layout(clip.samples, clip.layout, layout)
    return AudioClip(samples=samples, samplerate=clip.samplerate, layout=layout)


def reconcile(clips: list[AudioClip], samplerate: int, layout: ChannelLayout) -> list[AudioClip]:
    return [match_layout(resample(clip, samplerate), layout) for clip in clips]


def cut(clip: AudioClip, start_ms: float, end_ms: float, mode: str = "keep") -> AudioClip:
    start = int(np.maximum(0, _ms_to_frame(start_ms, clip.samplerate)))
    end = int(np.minimum(clip.frames, _ms_to_frame(end_ms, clip.samplerate)))
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


def concat(clips: list[AudioClip], samplerate: int | None = None, layout: ChannelLayout | None = None) -> AudioClip:
    if not clips:
        raise ValueError("Cannot concatenate an empty list of clips.")
    samplerate = samplerate or clips[0].samplerate
    layout = layout or clips[0].layout
    aligned = reconcile(clips, samplerate, layout)
    samples = np.concatenate([item.samples for item in aligned], axis=0)
    return AudioClip(samples=samples, samplerate=samplerate, layout=layout)


def volume(clip: AudioClip, gain_db: float) -> AudioClip:
    gain = db_to_gain(gain_db)
    return AudioClip(samples=clip.samples * np.float32(gain), samplerate=clip.samplerate, layout=clip.layout)


def _fade_curve(frames: int, shape: str, reverse: bool) -> np.ndarray:
    if frames <= 0:
        return np.ones(0, dtype=np.float32)
    t = np.linspace(0.0, 1.0, frames, dtype=np.float32)
    if shape == "exp":
        curve = np.sin(t * (np.pi / 2.0)).astype(np.float32)
    else:
        curve = t
    if reverse:
        curve = curve[::-1]
    return curve


def fade_in(clip: AudioClip, duration_ms: float, shape: str = "linear") -> AudioClip:
    n = int(np.minimum(clip.frames, np.maximum(1, _ms_to_frame(duration_ms, clip.samplerate))))
    samples = clip.samples.copy()
    samples[:n] *= _fade_curve(n, shape, reverse=False)[:, None]
    return AudioClip(samples=samples, samplerate=clip.samplerate, layout=clip.layout)


def fade_out(clip: AudioClip, duration_ms: float, shape: str = "linear") -> AudioClip:
    n = int(np.minimum(clip.frames, np.maximum(1, _ms_to_frame(duration_ms, clip.samplerate))))
    samples = clip.samples.copy()
    samples[-n:] *= _fade_curve(n, shape, reverse=True)[:, None]
    return AudioClip(samples=samples, samplerate=clip.samplerate, layout=clip.layout)


def reverse(clip: AudioClip) -> AudioClip:
    return AudioClip(
        samples=np.flip(clip.samples, axis=0).copy(),
        samplerate=clip.samplerate,
        layout=clip.layout,
    )


def speed(clip: AudioClip, ratio: float, preserve_pitch: bool = True) -> AudioClip:
    ratio = np.round(ratio, 2)
    if not 0.01 <= ratio <= 10.00:
        raise ValueError("Speed ratio must be between 0.01 and 10.00.")
    if np.abs(ratio - 1.0) < 1e-9:
        return clip
    if not preserve_pitch:
        new_frames = int(np.maximum(1, np.round(clip.frames / ratio)))
        old_idx = np.linspace(0.0, 1.0, clip.frames, endpoint=False)
        new_idx = np.linspace(0.0, 1.0, new_frames, endpoint=False)
        samples = np.column_stack(
            [np.interp(new_idx, old_idx, clip.samples[:, ch]) for ch in range(clip.channels)]
        ).astype(np.float32)
        return AudioClip(samples=samples, samplerate=clip.samplerate, layout=clip.layout)
    stretched = _time_stretch(clip.samples, ratio)
    return AudioClip(samples=stretched, samplerate=clip.samplerate, layout=clip.layout)


def _time_stretch(samples: np.ndarray, ratio: float) -> np.ndarray:
    stretched_channels = [
        np.asarray(
            time_stretch(samples[:, ch].astype(np.float32), rate=ratio),
            dtype=np.float32,
        )
        for ch in range(samples.shape[1])
    ]
    return np.column_stack(stretched_channels)


def channels_op(
    clip: AudioClip,
    target_layout: str,
    routing: dict[str, int] | None = None,
    pan: float | None = None,
) -> AudioClip:
    layout = layout_by_name(target_layout)
    samples = convert_layout(clip.samples, clip.layout, layout, routing=routing)
    result = AudioClip(samples=samples, samplerate=clip.samplerate, layout=layout)
    if pan is not None and layout.name == "stereo":
        panned = pan_stereo(result.samples, pan)
        return AudioClip(samples=panned, samplerate=clip.samplerate, layout=layout)
    return result


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
        offset = int(np.maximum(0, _ms_to_frame(item.offset_ms, samplerate)))
        aligned.append((clip, offset, db_to_gain(item.gain_db)))
        last_frame = int(np.maximum(last_frame, offset + clip.frames))
    out = np.zeros((last_frame, layout.count), dtype=np.float32)
    for clip, offset, gain in aligned:
        end = offset + clip.frames
        out[offset:end] += clip.samples * np.float32(gain)
    peak = np.max(np.abs(out)) if out.size else 0.0
    if peak > 1.0:
        if clip_protection == "limiter":
            out = np.tanh(out).astype(np.float32)
        else:
            out *= np.float32(0.99 / peak)
    return AudioClip(samples=out, samplerate=samplerate, layout=layout)
