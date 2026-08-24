"""Named channel layouts and conversion / pan / routing."""



from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ChannelLayout:
    name: str
    channels: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.channels)


MONO = ChannelLayout("mono", ("C",))
STEREO = ChannelLayout("stereo", ("L", "R"))
SURROUND_5_1 = ChannelLayout("5.1", ("L", "R", "C", "LFE", "Ls", "Rs"))

LAYOUTS: dict[str, ChannelLayout] = {
    MONO.name: MONO,
    STEREO.name: STEREO,
    SURROUND_5_1.name: SURROUND_5_1,
}


def layout_by_name(name: str) -> ChannelLayout:
    try:
        return LAYOUTS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown channel layout: {name}") from exc


def layout_for_channel_count(count: int) -> ChannelLayout:
    if count == 1:
        return MONO
    if count == 2:
        return STEREO
    if count == 6:
        return SURROUND_5_1
    labels = tuple(f"ch{i}" for i in range(count))
    return ChannelLayout(name=f"{count}ch", channels=labels)


def _as_2d(samples: np.ndarray) -> np.ndarray:
    if samples.ndim == 1:
        return samples.reshape(-1, 1)
    return samples


def convert_layout(
    samples: np.ndarray,
    source: ChannelLayout,
    target: ChannelLayout,
    routing: dict[str, int] | None = None,
) -> np.ndarray:
    samples = _as_2d(samples).astype(np.float32, copy=False)
    if source.name == target.name and routing is None:
        if samples.shape[1] == target.count:
            return samples
    frames = samples.shape[0]
    out = np.zeros((frames, target.count), dtype=np.float32)
    src_index = {label: i for i, label in enumerate(source.channels)}

    if routing:
        for label, src_col in routing.items():
            if label in target.channels and 0 <= src_col < samples.shape[1]:
                out[:, target.channels.index(label)] = samples[:, src_col]
        return out

    pair = (source.name, target.name)
    if pair == ("mono", "stereo"):
        out[:, 0] = samples[:, 0]
        out[:, 1] = samples[:, 0]
        return out
    if pair == ("stereo", "mono"):
        left = samples[:, 0] if samples.shape[1] > 0 else 0
        right = samples[:, 1] if samples.shape[1] > 1 else left
        out[:, 0] = 0.5 * (left + right)
        return out
    if pair == ("stereo", "5.1") or (source.name == "stereo" and target.name == "5.1"):
        if samples.shape[1] >= 2:
            out[:, 0] = samples[:, 0]
            out[:, 1] = samples[:, 1]
        elif samples.shape[1] == 1:
            out[:, 0] = samples[:, 0]
            out[:, 1] = samples[:, 0]
        return out
    if pair == ("mono", "5.1"):
        out[:, 2] = samples[:, 0]
        return out
    if pair == ("5.1", "stereo"):
        def col(label: str) -> np.ndarray:
            idx = src_index.get(label)
            if idx is None or idx >= samples.shape[1]:
                return np.zeros(frames, dtype=np.float32)
            return samples[:, idx]

        coef = np.sqrt(0.5)
        out[:, 0] = col("L") + coef * col("C") + coef * col("Ls")
        out[:, 1] = col("R") + coef * col("C") + coef * col("Rs")
        return out
    if pair == ("5.1", "mono"):
        stereo = convert_layout(samples, source, STEREO)
        return convert_layout(stereo, STEREO, MONO)

    for i, label in enumerate(target.channels):
        idx = src_index.get(label)
        if idx is not None and idx < samples.shape[1]:
            out[:, i] = samples[:, idx]
        elif target.count == 1:
            out[:, 0] = samples.mean(axis=1)
    return out


def equal_power_gains(pan: float) -> tuple[float, float]:
    """pan in [-1, 1] -> (left_gain, right_gain) using equal-power pan law."""
    pan = np.clip(pan, -1.0, 1.0)
    angle = (pan + 1.0) * (np.pi / 4.0)
    return np.cos(angle), np.sin(angle)


def pan_stereo(samples: np.ndarray, pan: float) -> np.ndarray:
    samples = _as_2d(samples).astype(np.float32, copy=True)
    gain_l, gain_r = equal_power_gains(pan)
    if samples.shape[1] == 1:
        left = samples[:, 0] * gain_l
        right = samples[:, 0] * gain_r
        return np.stack([left, right], axis=1)
    if samples.shape[1] >= 2:
        samples[:, 0] *= gain_l
        samples[:, 1] *= gain_r
        return samples
    return samples
