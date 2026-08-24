"""In-memory audio clip: float32 samples shaped (frames, channels)."""
from dataclasses import dataclass

import numpy as np

from audio.channels import ChannelLayout, layout_for_channel_count


@dataclass
class AudioClip:
    samples: np.ndarray
    samplerate: int
    layout: ChannelLayout

    def __post_init__(self) -> None:
        samples = np.asarray(self.samples)
        if samples.ndim == 1:
            samples = samples.reshape(-1, 1)
        if samples.ndim != 2:
            raise ValueError("AudioClip samples must be 1-D or 2-D")
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)
        self.samples = np.ascontiguousarray(samples)
        if self.layout.count != self.samples.shape[1]:
            self.layout = layout_for_channel_count(self.samples.shape[1])

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])

    @property
    def channels(self) -> int:
        return int(self.samples.shape[1])

    @property
    def duration_ms(self) -> float:
        if self.samplerate <= 0:
            return 0.0
        return self.frames / self.samplerate * 1000.0
