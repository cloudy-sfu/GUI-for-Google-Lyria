"""Audio file I/O: soundfile for WAV/FLAC/OGG, PyAV (bundled FFmpeg libraries) for MP3/AAC/M4A."""
import io
from pathlib import Path

import av
import numpy as np
import soundfile as sf

from audio.channels import layout_for_channel_count
from audio.clip import AudioClip

_SOUNDFILE_SUFFIXES = {".wav", ".flac", ".ogg", ".oga"}
EXPORT_FORMATS = ("wav", "flac", "m4a", "aac", "mp3")
_EXPORT_FILTER_LABELS = {
    "wav": "WAV",
    "flac": "FLAC",
    "m4a": "M4A",
    "aac": "AAC",
    "mp3": "MP3",
}
_AV_EXPORT = {
    "mp3": ("mp3", "libmp3lame"),
    "aac": ("adts", "aac"),
    "m4a": ("mp4", "aac"),
}


def export_file_filter() -> str:
    return ";;".join(
        f"{_EXPORT_FILTER_LABELS[fmt]} (*.{fmt})" for fmt in EXPORT_FORMATS
    )


def load(path: str | Path) -> AudioClip:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")
    if path.suffix.lower() in _SOUNDFILE_SUFFIXES:
        return _load_soundfile(str(path))
    return _load_av(str(path))


def load_bytes(data: bytes, mime: str | None = None) -> AudioClip:
    if (mime or "").lower() in {"audio/wav", "audio/x-wav", "audio/wave"} or data[:4] == b"RIFF":
        return _load_soundfile(io.BytesIO(data))
    return _load_av(io.BytesIO(data))


def _load_soundfile(source: str | io.BytesIO) -> AudioClip:
    samples, samplerate = sf.read(source, dtype="float32", always_2d=True)
    return AudioClip(
        samples=samples,
        samplerate=int(samplerate),
        layout=layout_for_channel_count(samples.shape[1]),
    )


def _load_av(source: str | io.BytesIO) -> AudioClip:
    try:
        container = av.open(source)
    except Exception as exc:
        raise RuntimeError(f"Could not decode audio: {exc}") from exc
    try:
        stream = next((item for item in container.streams if item.type == "audio"), None)
        if stream is None:
            raise ValueError("No audio stream in file.")
        chunks: list[np.ndarray] = []
        samplerate = int(stream.rate or 0)
        channels = int(stream.channels or 0)
        for frame in container.decode(audio=0):
            samplerate = int(frame.sample_rate or samplerate)
            planar = _frame_to_planar_float(frame)
            channels = planar.shape[0]
            chunks.append(planar)
        if not chunks:
            raise ValueError("No audio frames decoded.")
        samples = np.ascontiguousarray(np.concatenate(chunks, axis=1).T, dtype=np.float32)
        if samplerate <= 0:
            samplerate = 44100
        return AudioClip(
            samples=samples,
            samplerate=samplerate,
            layout=layout_for_channel_count(channels or samples.shape[1]),
        )
    finally:
        container.close()


def _frame_to_planar_float(frame) -> np.ndarray:
    """Return float32 array shaped (channels, samples)."""
    arr = frame.to_ndarray()
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    channels = getattr(getattr(frame, "layout", None), "nb_channels", None) or arr.shape[0]
    if arr.shape[0] == 1 and channels > 1 and arr.size % channels == 0:
        arr = arr.reshape(-1, channels).T
    elif arr.shape[0] not in {1, channels} and arr.shape[1] in {1, channels}:
        arr = arr.T
    if np.issubdtype(arr.dtype, np.floating):
        return np.ascontiguousarray(arr, dtype=np.float32)
    return np.ascontiguousarray(arr.astype(np.float32) / np.iinfo(arr.dtype).max)


def save(clip: AudioClip, path: str | Path, fmt: str | None = None, mp3_quality: str = "2") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (fmt or path.suffix.lstrip(".")).lower()
    if fmt in {"wav", "flac", "ogg"}:
        sf.write(str(path), clip.samples, clip.samplerate, format=fmt.upper())
        return
    if fmt in _AV_EXPORT:
        _save_av(clip, path, fmt, mp3_quality)
        return
    raise ValueError(f"Unsupported export format: {fmt}")


def _save_av(clip: AudioClip, path: Path, fmt: str, mp3_quality: str) -> None:
    container_format, codec = _AV_EXPORT[fmt]
    channels = 2 if clip.channels >= 2 else 1
    layout = "stereo" if channels == 2 else "mono"
    pcm = np.clip(clip.samples[:, :channels], -1.0, 1.0).astype(np.float32)
    label = _EXPORT_FILTER_LABELS[fmt]
    try:
        output = av.open(str(path), mode="w", format=container_format)
    except Exception as exc:
        raise OSError(f"Could not create {label} file: {exc}") from exc
    try:
        stream = output.add_stream(codec, rate=clip.samplerate)
        stream.layout = layout
        if fmt == "mp3":
            try:
                stream.codec_context.qscale = np.clip(mp3_quality, 0, 9, dtype=int)
            except (TypeError, ValueError):
                stream.bit_rate = 192_000
        else:
            stream.bit_rate = 192_000
        frame_size = int(stream.frame_size or (1152 if fmt == "mp3" else 1024))
        offset = 0
        while offset < pcm.shape[0]:
            chunk = pcm[offset : offset + frame_size]
            if chunk.shape[0] < frame_size:
                padded = np.zeros((frame_size, channels), dtype=np.float32)
                padded[: chunk.shape[0]] = chunk
                chunk = padded
            frame = av.AudioFrame.from_ndarray(
                np.ascontiguousarray(chunk.T),
                format="fltp",
                layout=layout,
            )
            frame.sample_rate = clip.samplerate
            for packet in stream.encode(frame):
                output.mux(packet)
            offset += frame_size
        for packet in stream.encode(None):
            output.mux(packet)
    except Exception as exc:
        raise RuntimeError(f"{label} encode failed: {exc}") from exc
    finally:
        output.close()


def probe(path: str | Path) -> tuple[int, int, int]:
    clip = load(path)
    return clip.samplerate, clip.channels, round(clip.duration_ms)
