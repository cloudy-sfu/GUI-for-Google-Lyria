"""STFT, ISTFT, and phase vocoder. Attribution: see audio/librosa/LICENSE.md."""



import numpy as np
from scipy.fft import irfft, rfft
from scipy.signal import get_window


def _pad_center(data: np.ndarray, size: int) -> np.ndarray:
    n = data.shape[-1]
    lpad = int((size - n) // 2)
    if lpad < 0:
        raise ValueError(f"Target size ({size}) must be at least input size ({n})")
    return np.pad(data, (lpad, int(size - n - lpad)))


def _fix_length(data: np.ndarray, size: int) -> np.ndarray:
    n = data.shape[-1]
    if n > size:
        return data[..., :size]
    if n < size:
        return np.pad(data, [(0, 0)] * (data.ndim - 1) + [(0, size - n)])
    return data


def _frame(y: np.ndarray, frame_length: int, hop_length: int) -> np.ndarray:
    n_frames = 1 + (y.shape[-1] - frame_length) // hop_length
    if n_frames < 1:
        raise ValueError(
            f"Input is too short (n={y.shape[-1]}) for frame_length={frame_length}"
        )
    shape = (frame_length, n_frames)
    strides = (y.strides[-1], hop_length * y.strides[-1])
    return np.lib.stride_tricks.as_strided(y, shape=shape, strides=strides)


def stft(
    y: np.ndarray,
    *,
    n_fft: int = 2048,
    hop_length: int | None = None,
    win_length: int | None = None,
    window: str = "hann",
    center: bool = True,
    pad_mode: str = "constant",
) -> np.ndarray:
    y = np.asarray(y)
    if win_length is None:
        win_length = n_fft
    if hop_length is None:
        hop_length = int(win_length // 4)

    fft_window = np.asarray(get_window(window, win_length, fftbins=True), dtype=y.dtype)
    fft_window = _pad_center(fft_window, n_fft)

    if center:
        y = np.pad(y, (n_fft // 2, n_fft // 2), mode=pad_mode)
    if y.shape[-1] < n_fft:
        y = np.pad(y, (0, n_fft - y.shape[-1]))

    frames = _frame(y, n_fft, hop_length)
    spec = rfft(fft_window[:, None] * frames, axis=0)
    return np.asarray(spec, dtype=np.result_type(y.dtype, np.complex64))


def istft(
    stft_matrix: np.ndarray,
    *,
    hop_length: int | None = None,
    win_length: int | None = None,
    n_fft: int | None = None,
    window: str = "hann",
    center: bool = True,
    dtype: np.dtype | type[np.floating] | None = None,
    length: int | None = None,
    pad_mode: str = "constant",
) -> np.ndarray:
    del pad_mode  # accepted so time_stretch can forward stft kwargs
    if n_fft is None:
        n_fft = 2 * (stft_matrix.shape[-2] - 1)
    if win_length is None:
        win_length = n_fft
    if hop_length is None:
        hop_length = int(win_length // 4)
    if dtype is None:
        dtype = np.float32 if np.iscomplexobj(stft_matrix) else stft_matrix.dtype

    ifft_window = np.asarray(get_window(window, win_length, fftbins=True), dtype=dtype)
    ifft_window = _pad_center(ifft_window, n_fft)

    n_frames = stft_matrix.shape[-1]
    expected_len = n_fft + hop_length * int(np.maximum(n_frames - 1, 0))
    y = np.zeros(expected_len, dtype=dtype)
    frames = np.asarray(irfft(stft_matrix, n=n_fft, axis=-2), dtype=dtype)
    frames *= ifft_window[:, None]

    n = int(np.minimum(n_fft, frames.shape[0]))
    for frame in range(n_frames):
        sample = frame * hop_length
        end = int(np.minimum(n, y.shape[-1] - sample))
        if end <= 0:
            break
        y[sample : sample + end] += frames[:end, frame]

    wss = np.zeros(expected_len, dtype=dtype)
    win_sq = ifft_window * ifft_window
    for frame in range(n_frames):
        sample = frame * hop_length
        end = int(np.minimum(n_fft, y.shape[-1] - sample))
        if end <= 0:
            break
        wss[sample : sample + end] += win_sq[:end]

    nonzero = wss > np.finfo(np.dtype(dtype)).tiny
    y[nonzero] /= wss[nonzero]

    if center:
        y = y[n_fft // 2 :]
    if length is not None:
        y = _fix_length(y, length)
    return y


def phase_vocoder(
    D: np.ndarray,
    *,
    rate: float,
    hop_length: int | None = None,
    n_fft: int | None = None,
) -> np.ndarray:
    if n_fft is None:
        n_fft = 2 * (D.shape[-2] - 1)
    if hop_length is None:
        hop_length = int(n_fft // 4)

    time_steps = np.arange(0, D.shape[-1], rate, dtype=np.float64)
    d_stretch = np.zeros(D.shape[:-1] + (len(time_steps),), dtype=D.dtype)
    phi_advance = hop_length * np.fft.rfftfreq(n_fft, d=1.0 / (2.0 * np.pi)).astype(
        np.float64
    )
    if D.shape[-1] == 0:
        return d_stretch

    phase_acc = np.angle(D[..., 0])
    padding = [(0, 0)] * D.ndim
    padding[-1] = (0, 2)
    D = np.pad(D, padding, mode="constant")

    for t, step in enumerate(time_steps):
        columns = D[..., int(np.floor(step)) : int(np.floor(step + 2))]
        alpha = np.mod(step, 1.0)
        mag = (1.0 - alpha) * np.abs(columns[..., 0]) + alpha * np.abs(columns[..., 1])
        d_stretch[..., t] = (mag * np.exp(1j * phase_acc)).astype(D.dtype, copy=False)
        dphase = np.angle(columns[..., 1]) - np.angle(columns[..., 0]) - phi_advance
        dphase = dphase - 2.0 * np.pi * np.round(dphase / (2.0 * np.pi))
        phase_acc = phase_acc + phi_advance + dphase

    return d_stretch
