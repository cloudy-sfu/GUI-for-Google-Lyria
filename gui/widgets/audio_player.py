"""Transport, volume, and pitch-preserving playback rate.

Transcript lives in the left column; seek is on the timeline.
"""

import math
import threading

import numpy as np
from scipy.signal.windows import hann
from PyQt6.QtCore import QEvent, QIODevice, QMargins, Qt, QTimer, pyqtSignal
from PyQt6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from audio.channels import MONO, STEREO, convert_layout
from audio.clip import AudioClip
from audio.operations import resample
from gui.style import em_px, format_clock, icon_size, themed_standard_icon

# Live listen-rate, not the Speed edit (phase vocoder, 0.01–10). A centered
# log slider from 0.25× to 4.00× matches common player UIs and stays cheap.
_RATE_MIN = 0.25
_RATE_MAX = 4.00
_RATE_SLIDER_MAX = 1000
_SLIDER_EM = 12.0
_SLIDER_GAP_EM = 2.0
_RATE_LABEL_EM = 4.5
_OLA_GRAIN = 2048
_OLA_HOP = 1024


class AudioPlayerWidget(QWidget):
    position_changed = pyqtSignal(int)
    mixed_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sink: QAudioSink | None = None
        self._reader: _RatePlaybackDevice | None = None
        self._format = QAudioFormat()
        self._duration_ms = 0
        self._position_ms = 0
        self._samplerate = 48000
        self._playing = False
        self._source_name = "No audio loaded"
        self._clip: AudioClip | None = None
        self._offset_ms = 0
        self._timeline_ms = 0
        self._playback_rate = 1.0
        self._rate_anchor_source_ms = 0
        self._rate_anchor_usecs = 0
        self._transport_icons: dict[QPushButton, QStyle.StandardPixmap] = {}
        self._play_pause_status = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        transport = QHBoxLayout()
        self.play_pause_btn = self._transport_button(
            QStyle.StandardPixmap.SP_MediaPlay, "Play"
        )
        self.stop_btn = self._transport_button(QStyle.StandardPixmap.SP_MediaStop, "Stop")
        self.play_pause_btn.clicked.connect(self.toggle_play_pause)
        self.stop_btn.clicked.connect(self.stop)
        self.time_label = QLabel("00:00 / 00:00")
        self.mixed_btn = QPushButton("Mixed")
        self.mixed_btn.setEnabled(False)
        self.mixed_btn.setToolTip("Load the whole mix and play it")
        self.mixed_btn.clicked.connect(self.mixed_requested.emit)
        transport.addWidget(self.play_pause_btn)
        transport.addWidget(self.stop_btn)
        transport.addWidget(self.time_label)
        transport.addWidget(self.mixed_btn)
        transport.addStretch(1)
        layout.addLayout(transport)

        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel("Volume"))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setToolTip("Master volume")
        self.volume.valueChanged.connect(self._on_volume)
        self._slider_gap = QWidget()
        vol_row.addWidget(self.volume)
        vol_row.addWidget(self._slider_gap)
        vol_row.addWidget(QLabel("Playback"))
        self.playback = QSlider(Qt.Orientation.Horizontal)
        self.playback.setRange(0, _RATE_SLIDER_MAX)
        self.playback.setValue(_slider_from_rate(1.0))
        self.playback.setPageStep(50)
        self.playback.setSingleStep(5)
        self.playback.setToolTip(
            "Listen faster or slower (0.25×–4.00×). Pitch is always preserved."
        )
        self.playback.valueChanged.connect(self._on_playback_rate)
        self.speed_label = QLabel("1.00x")
        self.speed_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.reset_rate_btn = QPushButton("1x")
        self.reset_rate_btn.setToolTip("Set playback rate to 1.00×")
        self.reset_rate_btn.setAccessibleName("Set playback rate to 1x")
        self.reset_rate_btn.clicked.connect(self._reset_playback_rate)
        vol_row.addWidget(self.playback)
        vol_row.addWidget(self.speed_label)
        vol_row.addWidget(self.reset_rate_btn)
        vol_row.addStretch(1)
        layout.addLayout(vol_row)
        self._apply_em_sizes()

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._poll_position)

    def _transport_button(
        self, pixmap: QStyle.StandardPixmap, label: str
    ) -> QPushButton:
        button = QPushButton(self)
        size = icon_size(self)
        button.setIconSize(size)
        button.setFixedSize(size.grownBy(QMargins(8, 6, 8, 6)))
        button.setToolTip(label)
        button.setAccessibleName(label)
        self._transport_icons[button] = pixmap
        button.setIcon(themed_standard_icon(self, pixmap, size))
        return button

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self._retint_transport_icons()
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._apply_em_sizes()

    def _apply_em_sizes(self) -> None:
        width = em_px(self, _SLIDER_EM)
        self.volume.setFixedWidth(width)
        self.playback.setFixedWidth(width)
        self._slider_gap.setFixedWidth(em_px(self, _SLIDER_GAP_EM))
        self.speed_label.setMinimumWidth(em_px(self, _RATE_LABEL_EM))

    def _retint_transport_icons(self) -> None:
        for button, pixmap in self._transport_icons.items():
            button.setIcon(themed_standard_icon(self, pixmap, button.iconSize()))

    def _refresh_play_pause(self, status: str | None = None) -> None:
        if status is not None:
            self._play_pause_status = status
        label = "Pause" if self._playing else "Play"
        pixmap = (
            QStyle.StandardPixmap.SP_MediaPause
            if self._playing
            else QStyle.StandardPixmap.SP_MediaPlay
        )
        self._transport_icons[self.play_pause_btn] = pixmap
        self.play_pause_btn.setIcon(
            themed_standard_icon(self, pixmap, self.play_pause_btn.iconSize())
        )
        self.play_pause_btn.setAccessibleName(label)
        self.play_pause_btn.setToolTip(self._play_pause_status or label)

    def set_mixed_enabled(self, enabled: bool) -> None:
        self.mixed_btn.setEnabled(enabled)

    def set_clip(
        self,
        clip: AudioClip | None,
        label: str = "",
        *,
        offset_ms: int = 0,
        timeline_ms: int = 0,
    ) -> None:
        offset_ms = max(0, offset_ms)
        timeline_ms = max(0, timeline_ms)
        if clip is not None and clip is self._clip and self._duration_ms > 0:
            # The render cache hands back the same clip when nothing that feeds
            # it changed; reloading it would needlessly restart the sink.
            self._source_name = label or self._source_name
            self._apply_placement(offset_ms, timeline_ms)
            return
        was_playing = self._playing
        resume_at = self._position_ms if was_playing else 0
        self.stop()
        self._clip = clip
        self._offset_ms = offset_ms
        self._timeline_ms = timeline_ms
        if clip is None or clip.frames <= 0:
            self._close_reader()
            self._duration_ms = 0
            self._position_ms = 0
            self._source_name = "No audio loaded"
            self._set_position_display(0)
            return
        if not self._load_pcm(clip):
            self._close_reader()
            self._duration_ms = 0
            self._position_ms = 0
            self._source_name = label or "Audio"
            self._refresh_play_pause(
                f"{self._source_name} (could not open audio device)"
            )
            self._set_position_display(0)
            return
        self._source_name = label or "Audio"
        self._refresh_play_pause("")
        self._position_ms = 0
        self._set_position_display(0)
        if was_playing and self._duration_ms > 0:
            self.set_position(min(resume_at, self._duration_ms))
            self.play()

    def position_ms(self) -> int:
        if self._playing:
            self._sync_position_from_sink()
        return int(self._position_ms)

    def duration_ms(self) -> int:
        return int(self._duration_ms)

    def set_position(self, position_ms: int) -> None:
        pos = max(0, position_ms)
        if self._duration_ms > 0:
            pos = min(pos, self._duration_ms)
        resume = self._playing
        if resume:
            self._stop_sink()
            self._playing = False
            self._timer.stop()
        self._position_ms = pos
        if self._reader is not None:
            self._reader.seek_source_frame(self._frame_from_ms(pos))
        self._set_position_display(pos)
        if resume:
            self.play()
            if not self._playing:
                self._refresh_play_pause()

    def toggle_play_pause(self) -> None:
        if self._playing:
            self.pause()
            return
        if self._duration_ms > 0 and self._position_ms >= self._duration_ms:
            self.set_position(0)
        self.play()

    def play(self) -> None:
        if self._duration_ms <= 0 or self._reader is None:
            return
        if self._playing:
            return
        # Seeking to the end parks the playhead there; only the transport button
        # rewinds, so scrolling past the tail cannot loop back to the start.
        if self._position_ms >= self._duration_ms:
            return
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            self._refresh_play_pause("No audio output device")
            return
        self._stop_sink()
        self._sink = QAudioSink(device, self._format, self)
        self._sink.setVolume(self.volume.value() / 100.0)
        self._sink.stateChanged.connect(self._on_sink_state)
        self._reader.set_rate(self._playback_rate)
        self._reader.seek_source_frame(self._frame_from_ms(self._position_ms))
        self._rate_anchor_source_ms = self._position_ms
        self._rate_anchor_usecs = 0
        self._sink.start(self._reader)
        if self._sink.error() != QAudio.Error.NoError and self._clip is not None:
            self._stop_sink()
            fallback = device.preferredFormat()
            if fallback.sampleRate() > 0 and self._load_pcm(self._clip, fallback):
                self._sink = QAudioSink(device, self._format, self)
                self._sink.setVolume(self.volume.value() / 100.0)
                self._sink.stateChanged.connect(self._on_sink_state)
                self._reader.set_rate(self._playback_rate)
                self._reader.seek_source_frame(self._frame_from_ms(self._position_ms))
                self._rate_anchor_source_ms = self._position_ms
                self._rate_anchor_usecs = 0
                self._sink.start(self._reader)
        if self._sink is None or self._sink.error() != QAudio.Error.NoError:
            self._stop_sink()
            self._refresh_play_pause(
                f"{self._source_name} (could not open audio device)"
            )
            return
        self._playing = True
        self._timer.start()
        self._refresh_play_pause("")

    def _load_pcm(self, clip: AudioClip, fmt: QAudioFormat | None = None) -> bool:
        samples, chosen = _align_for_device(clip, fmt)
        if samples.size == 0:
            return False
        self._format = chosen
        self._samplerate = max(1, chosen.sampleRate())
        self._close_reader()
        self._reader = _RatePlaybackDevice(samples, chosen, self)
        self._reader.set_rate(self._playback_rate)
        self._sync_reader_placement()
        return int(samples.shape[0]) > 0

    def _apply_placement(self, offset_ms: int, timeline_ms: int) -> None:
        self._offset_ms = max(0, offset_ms)
        self._timeline_ms = max(0, timeline_ms)
        self._sync_reader_placement()
        if self._duration_ms > 0 and self._position_ms > self._duration_ms:
            self.set_position(self._duration_ms)
            return
        self._set_position_display(self._position_ms)

    def _sync_reader_placement(self) -> None:
        if self._reader is None:
            self._duration_ms = 0
            return
        self._reader.set_placement_ms(self._offset_ms, self._timeline_ms)
        self._duration_ms = self._reader.duration_ms()

    def pause(self) -> None:
        if not self._playing:
            return
        self._sync_position_from_sink()
        self._stop_sink()
        self._playing = False
        self._timer.stop()
        self._refresh_play_pause()
        self._set_position_display(self._position_ms)

    def stop(self) -> None:
        self._stop_sink()
        self._playing = False
        self._timer.stop()
        self._position_ms = 0
        if self._reader is not None:
            self._reader.seek_source_frame(0.0)
        self._refresh_play_pause()
        self._set_position_display(0)

    def _on_volume(self, value: int) -> None:
        if self._sink is not None:
            self._sink.setVolume(value / 100.0)

    def _reset_playback_rate(self) -> None:
        self.playback.setValue(_slider_from_rate(1.0))

    def _on_playback_rate(self, value: int) -> None:
        rate = _rate_from_slider(value)
        self.speed_label.setText(f"{rate:.2f}x")
        if rate == self._playback_rate:
            return
        if self._playing:
            self._sync_position_from_sink()
            self._rate_anchor_source_ms = self._position_ms
            if self._sink is not None:
                self._rate_anchor_usecs = int(self._sink.processedUSecs())
        self._playback_rate = rate
        if self._reader is not None:
            self._reader.set_rate(rate)

    def _on_sink_state(self, state: QAudio.State) -> None:
        if not self._playing or self._sink is None:
            return
        if state == QAudio.State.IdleState:
            self._finish()
            return
        if state == QAudio.State.StoppedState:
            error = self._sink.error()
            if error not in (QAudio.Error.NoError, QAudio.Error.UnderrunError):
                self._playing = False
                self._timer.stop()
                self._refresh_play_pause(
                    f"{self._source_name} (could not open audio device)"
                )

    def _finish(self) -> None:
        self._stop_sink()
        self._playing = False
        self._timer.stop()
        self._position_ms = self._duration_ms
        self._refresh_play_pause()
        self._set_position_display(self._duration_ms)

    def _poll_position(self) -> None:
        if not self._playing:
            return
        self._sync_position_from_sink()
        self._set_position_display(self._position_ms)
        if self._duration_ms > 0 and self._position_ms >= self._duration_ms:
            self._finish()

    def _sync_position_from_sink(self) -> None:
        if self._sink is None:
            return
        elapsed_ms = (int(self._sink.processedUSecs()) - self._rate_anchor_usecs) / 1000.0
        pos = int(self._rate_anchor_source_ms + elapsed_ms * self._playback_rate)
        self._position_ms = min(max(pos, 0), self._duration_ms)

    def _set_position_display(self, position_ms: int) -> None:
        self.time_label.setText(
            f"{format_clock(position_ms)} / {format_clock(self._duration_ms)}"
        )
        self.position_changed.emit(int(position_ms))

    def _frame_from_ms(self, position_ms: int) -> float:
        return max(0.0, position_ms * self._samplerate / 1000.0)

    def _close_reader(self) -> None:
        if self._reader is None:
            return
        self._reader.close()
        self._reader.deleteLater()
        self._reader = None

    def _stop_sink(self) -> None:
        if self._sink is None:
            return
        try:
            self._sink.stateChanged.disconnect(self._on_sink_state)
        except TypeError:
            pass
        self._sink.stop()
        self._sink.deleteLater()
        self._sink = None


class _RatePlaybackDevice(QIODevice):
    """Pull PCM for QAudioSink, time-scaled with overlap-add so pitch is kept.

    Rate 1.00× copies source frames directly. Other rates place windowed grains
    closer or farther apart (OLA), which changes tempo without resampling.
    """

    def __init__(
        self, samples: np.ndarray, fmt: QAudioFormat, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._samples = np.ascontiguousarray(samples, dtype=np.float32)
        self._nframes = int(self._samples.shape[0])
        self._channels = int(self._samples.shape[1])
        self._fmt = QAudioFormat(fmt)
        self._rate = 1.0
        self._src_pos = 0.0
        self._offset_frames = 0
        self._timeline_frames = self._nframes
        self._pending = bytearray()
        self._ola = np.zeros((_OLA_GRAIN, self._channels), dtype=np.float32)
        self._window = hann(_OLA_GRAIN, sym=False).astype(np.float32)[:, None]
        self._bytes_per_frame = self._channels * _bytes_per_sample(fmt.sampleFormat())
        self._samplerate = max(1, fmt.sampleRate())
        self._lock = threading.Lock()
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def set_placement_ms(self, offset_ms: int, timeline_ms: int) -> None:
        """Place the clip on a longer timeline; frames outside it are silence."""
        offset_frames = round(max(0, offset_ms) / 1000.0 * self._samplerate)
        timeline_frames = round(max(0, timeline_ms) / 1000.0 * self._samplerate)
        with self._lock:
            self._offset_frames = offset_frames
            self._timeline_frames = max(
                1, timeline_frames, self._offset_frames + self._nframes
            )
            self._src_pos = float(min(self._src_pos, self._timeline_frames))

    def duration_ms(self) -> int:
        with self._lock:
            return round(self._timeline_frames / self._samplerate * 1000.0)

    def isSequential(self) -> bool:
        return True

    def set_rate(self, rate: float) -> None:
        clamped = min(max(rate, _RATE_MIN), _RATE_MAX)
        with self._lock:
            self._rate = clamped

    def seek_source_frame(self, frame: float) -> None:
        with self._lock:
            self._src_pos = float(min(max(frame, 0.0), self._timeline_frames))
            self._pending.clear()
            self._ola.fill(0.0)

    def atEnd(self) -> bool:
        with self._lock:
            return self._src_pos >= self._timeline_frames and not self._pending

    def bytesAvailable(self) -> int:
        with self._lock:
            remain = max(
                0, int((self._timeline_frames - self._src_pos) / max(self._rate, 1e-9))
            )
            extra = len(self._pending)
        return remain * self._bytes_per_frame + extra + super().bytesAvailable()

    def readData(self, maxlen: int) -> bytes:
        needed = max(0, maxlen)
        if needed < self._bytes_per_frame:
            return b""
        needed -= needed % self._bytes_per_frame
        cap = self._bytes_per_frame * max(_OLA_HOP, self._samplerate // 10)
        needed = min(needed, cap)
        with self._lock:
            while len(self._pending) < needed and self._src_pos < self._timeline_frames:
                chunk = self._next_pcm()
                if not chunk:
                    break
                self._pending.extend(chunk)
            if self._src_pos >= self._timeline_frames and len(self._pending) < needed:
                tail = self._flush_ola()
                if tail:
                    self._pending.extend(tail)
            out = bytes(self._pending[:needed])
            del self._pending[:needed]
        return out

    def writeData(self, _data: bytes) -> int:
        return 0

    def _next_pcm(self) -> bytes:
        if self._rate == 1.0:
            start = int(self._src_pos)
            if start >= self._timeline_frames:
                return b""
            end = min(self._timeline_frames, start + _OLA_HOP)
            frames = self._gather(start, end)
            self._src_pos = float(end)
            return _pcm_bytes(frames, self._fmt)
        start = round(self._src_pos)
        grain = self._gather(start, start + _OLA_GRAIN)
        if grain.shape[0] < _OLA_GRAIN:
            padded = np.zeros((_OLA_GRAIN, self._channels), dtype=np.float32)
            padded[: grain.shape[0]] = grain
            grain = padded
        grain *= self._window
        self._ola += grain
        ready = self._ola[:_OLA_HOP].copy()
        self._ola = np.roll(self._ola, -_OLA_HOP, axis=0)
        self._ola[-_OLA_HOP:] = 0
        self._src_pos += _OLA_HOP * self._rate
        if self._src_pos > self._timeline_frames:
            self._src_pos = float(self._timeline_frames)
        return _pcm_bytes(ready, self._fmt)

    def _gather(self, start: int, end: int) -> np.ndarray:
        """Timeline frames [start, end), silent outside the placed clip."""
        end = max(start, end)
        n = end - start
        out = np.zeros((n, self._channels), dtype=np.float32)
        if n == 0:
            return out
        clip_start = self._offset_frames
        clip_end = clip_start + self._nframes
        a = max(start, clip_start)
        b = min(end, clip_end)
        if b > a:
            out[a - start : b - start] = self._samples[a - clip_start : b - clip_start]
        return out

    def _flush_ola(self) -> bytes:
        if not np.any(self._ola):
            return b""
        ready = self._ola[:_OLA_HOP].copy()
        self._ola.fill(0.0)
        return _pcm_bytes(ready, self._fmt)


def _rate_from_slider(value: int) -> float:
    half = _RATE_SLIDER_MAX / 2.0
    rate = round(_RATE_MAX ** ((value - half) / half), 2)
    return min(max(rate, _RATE_MIN), _RATE_MAX)


def _slider_from_rate(rate: float) -> int:
    rate = min(max(rate, _RATE_MIN), _RATE_MAX)
    return round(_RATE_SLIDER_MAX / 2.0 * (1.0 + math.log(rate, _RATE_MAX)))


def _bytes_per_sample(sample_format: QAudioFormat.SampleFormat) -> int:
    if sample_format == QAudioFormat.SampleFormat.Float:
        return 4
    if sample_format == QAudioFormat.SampleFormat.Int32:
        return 4
    if sample_format == QAudioFormat.SampleFormat.UInt8:
        return 1
    return 2


def _pcm_bytes(frames: np.ndarray, fmt: QAudioFormat) -> bytes:
    if frames.size == 0:
        return b""
    pcm = np.ascontiguousarray(np.clip(frames, -1.0, 1.0), dtype=np.float32)
    sample_format = fmt.sampleFormat()
    if sample_format == QAudioFormat.SampleFormat.Float:
        data = pcm.astype("<f4", copy=False)
    elif sample_format == QAudioFormat.SampleFormat.Int32:
        data = (pcm * 2147483647.0).astype("<i4")
    elif sample_format == QAudioFormat.SampleFormat.UInt8:
        data = ((pcm + 1.0) * 127.5).astype(np.uint8)
    else:
        data = (pcm * 32767.0).astype("<i2")
    return data.tobytes(order="C")


def _align_for_device(
    clip: AudioClip, fmt: QAudioFormat | None = None
) -> tuple[np.ndarray, QAudioFormat]:
    device = QMediaDevices.defaultAudioOutput()
    if fmt is None or fmt.sampleRate() <= 0:
        fmt = _choose_format(clip, device)
    else:
        fmt = QAudioFormat(fmt)
    rate = max(1, fmt.sampleRate())
    channels = 2 if fmt.channelCount() >= 2 else 1
    fmt.setSampleRate(rate)
    fmt.setChannelCount(channels)
    if channels == 1:
        fmt.setChannelConfig(QAudioFormat.ChannelConfig.ChannelConfigMono)
    else:
        fmt.setChannelConfig(QAudioFormat.ChannelConfig.ChannelConfigStereo)
    aligned = clip
    if aligned.samplerate != rate:
        aligned = resample(aligned, rate)
    target = STEREO if channels >= 2 else MONO
    samples = aligned.samples
    if aligned.layout.name != target.name or aligned.channels != channels:
        samples = convert_layout(samples, aligned.layout, target)
    samples = np.ascontiguousarray(np.clip(samples, -1.0, 1.0), dtype=np.float32)
    sample_format = fmt.sampleFormat()
    if sample_format not in (
        QAudioFormat.SampleFormat.Float,
        QAudioFormat.SampleFormat.Int32,
        QAudioFormat.SampleFormat.UInt8,
        QAudioFormat.SampleFormat.Int16,
    ):
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return samples, fmt


def _choose_format(clip: AudioClip, device) -> QAudioFormat:
    channels = 2 if clip.channels >= 2 else 1
    candidates: list[QAudioFormat] = []

    def add(rate: int, count: int, sample_format: QAudioFormat.SampleFormat) -> None:
        if rate <= 0 or count <= 0:
            return
        fmt = QAudioFormat()
        fmt.setSampleRate(int(rate))
        fmt.setChannelCount(int(count))
        fmt.setSampleFormat(sample_format)
        if count == 1:
            fmt.setChannelConfig(QAudioFormat.ChannelConfig.ChannelConfigMono)
        else:
            fmt.setChannelConfig(QAudioFormat.ChannelConfig.ChannelConfigStereo)
        candidates.append(fmt)

    if not device.isNull():
        preferred = device.preferredFormat()
        pref_channels = preferred.channelCount()
        play_channels = 2 if pref_channels >= 2 else 1 if pref_channels == 1 else channels
        pref_format = preferred.sampleFormat()
        if pref_format == QAudioFormat.SampleFormat.Unknown:
            pref_format = QAudioFormat.SampleFormat.Float
        add(preferred.sampleRate(), play_channels, pref_format)
        add(preferred.sampleRate(), play_channels, QAudioFormat.SampleFormat.Float)
        add(preferred.sampleRate(), play_channels, QAudioFormat.SampleFormat.Int16)
    add(clip.samplerate, channels, QAudioFormat.SampleFormat.Float)
    add(clip.samplerate, channels, QAudioFormat.SampleFormat.Int16)
    add(48000, channels, QAudioFormat.SampleFormat.Float)
    add(44100, channels, QAudioFormat.SampleFormat.Int16)
    for fmt in candidates:
        if device.isNull() or device.isFormatSupported(fmt):
            return fmt
    return candidates[0] if candidates else QAudioFormat()
