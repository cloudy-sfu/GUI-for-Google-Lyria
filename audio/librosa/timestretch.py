"""Time-stretch via STFT phase vocoder. Attribution: see audio/librosa/LICENSE.md."""


import numpy as np

from audio.librosa.spectrum import istft, phase_vocoder, stft


def time_stretch(y: np.ndarray, *, rate: float, **kwargs) -> np.ndarray:
    if rate <= 0:
        raise ValueError("rate must be a positive number")
    y = np.asarray(y)
    stretched = stft(y, **kwargs)
    stretched = phase_vocoder(
        stretched,
        rate=rate,
        hop_length=kwargs.get("hop_length"),
        n_fft=kwargs.get("n_fft"),
    )
    length = int(np.round(y.shape[-1] / rate))
    return istft(stretched, dtype=y.dtype, length=length, **kwargs)
