"""Stamp lyric timestamps while the track plays, matching the former web editor."""
from dataclasses import dataclass

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QMouseEvent, QPainter, QPalette, QResizeEvent, QTextOption
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from audio.clip import AudioClip
from gui.messages import silent_message
from gui.style import em_px, format_clock_ms, size_lyrics_editor
from gui.widgets.audio_player import AudioPlayerWidget
from workspaces.transcript import Cue

_DEFAULT_LINE_MS = 4_000
_WRAP_FLAGS = (
    Qt.AlignmentFlag.AlignLeft
    | Qt.AlignmentFlag.AlignTop
    | Qt.TextFlag.TextWordWrap
    | Qt.TextFlag.TextWrapAnywhere
)


def _wrapped_text_height(widget: QWidget, text: str, width: int) -> int:
    width = max(1, width)
    return max(
        widget.fontMetrics().height(),
        widget.fontMetrics()
        .boundingRect(0, 0, width, 0, int(_WRAP_FLAGS), text or " ")
        .height(),
    )


@dataclass
class _Line:
    text: str
    start_ms: int | None


class _ClickLabel(QLabel):
    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to stamp or clear the timestamp")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return _wrapped_text_height(self, self.text(), width)

    def sizeHint(self) -> QSize:
        width = self.width() if self.width() > 0 else 256
        return QSize(max(width, 1), self.heightForWidth(max(width, 1)))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().height())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.contentsRect(), int(_WRAP_FLAGS), self.text())


class _LineEditor(QPlainTextEdit):
    cancelled = pyqtSignal()
    editingFinished = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.document().setDocumentMargin(2)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.clearFocus()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.editingFinished.emit()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        margins = self.contentsMargins()
        frame = self.frameWidth() * 2
        inner = max(
            1,
            width - margins.left() - margins.right() - frame,
        )
        return (
            _wrapped_text_height(self, self.toPlainText(), inner)
            + margins.top()
            + margins.bottom()
            + frame
            + round(self.document().documentMargin() * 2)
        )

    def sizeHint(self) -> QSize:
        width = self.width() if self.width() > 0 else 256
        return QSize(max(width, 1), self.heightForWidth(max(width, 1)))

    def minimumSizeHint(self) -> QSize:
        margins = self.contentsMargins()
        return QSize(
            0,
            self.fontMetrics().height()
            + margins.top()
            + margins.bottom()
            + self.frameWidth() * 2
            + 8,
        )


def _action_button(label: str, tooltip: str) -> QPushButton:
    button = QPushButton(label)
    button.setAutoDefault(False)
    button.setDefault(False)
    button.setToolTip(tooltip)
    return button


class _LyricRow(QFrame):
    stamp_requested = pyqtSignal()
    text_changed = pyqtSignal(str)
    add_requested = pyqtSignal()
    delete_requested = pyqtSignal()

    def __init__(self, line: _Line, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._line = line
        self._editing = False
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        self._label = _ClickLabel()
        self._label.clicked.connect(self._on_stamp)
        self._editor = _LineEditor()
        self._editor.setVisible(False)
        self._editor.editingFinished.connect(self._commit_edit)
        self._editor.cancelled.connect(self._cancel_edit)
        self._editor.textChanged.connect(self._sync_height)
        self._edit_btn = _action_button("Edit", "Edit this line")
        self._edit_btn.clicked.connect(self.begin_edit)
        self._add_btn = _action_button("Add", "Insert a line below")
        self._add_btn.clicked.connect(self.add_requested.emit)
        self._delete_btn = _action_button("Delete", "Delete this line")
        self._delete_btn.clicked.connect(self.delete_requested.emit)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._editor, 1)
        for button in (self._edit_btn, self._add_btn, self._delete_btn):
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            layout.addWidget(button)
            layout.setAlignment(button, Qt.AlignmentFlag.AlignTop)
        self._refresh()
        self.set_active(False)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        layout = self.layout()
        margins = layout.contentsMargins() if layout is not None else self.contentsMargins()
        spacing = layout.spacing() if layout is not None else 0
        if spacing < 0:
            spacing = 0
        buttons_w = (
            self._edit_btn.sizeHint().width()
            + self._add_btn.sizeHint().width()
            + self._delete_btn.sizeHint().width()
            + spacing * 3
        )
        inner = max(
            1,
            width
            - margins.left()
            - margins.right()
            - buttons_w
            - self.frameWidth() * 2,
        )
        if self._editing:
            text_h = self._editor.heightForWidth(inner)
        else:
            text_h = self._label.heightForWidth(inner)
        button_h = self._edit_btn.sizeHint().height()
        return (
            margins.top()
            + margins.bottom()
            + max(text_h, button_h)
            + self.frameWidth() * 2
        )

    def sizeHint(self) -> QSize:
        width = self.width() if self.width() > 0 else super().sizeHint().width()
        return QSize(max(width, 1), self.heightForWidth(max(width, 1)))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(max(self.width(), 1)))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._sync_height()

    def _sync_height(self) -> None:
        width = self.width()
        if width <= 1:
            return
        target = max(1, self.heightForWidth(width))
        if self.minimumHeight() != target or self.maximumHeight() != target:
            self.setFixedHeight(target)

    def set_line(self, line: _Line) -> None:
        self._line = line
        if not self._editing:
            self._refresh()

    def set_active(self, active: bool) -> None:
        font = self._label.font()
        font.setBold(active)
        self._label.setFont(font)
        self._label.setForegroundRole(
            QPalette.ColorRole.WindowText if active else QPalette.ColorRole.PlaceholderText
        )
        self._sync_height()

    def _refresh(self) -> None:
        text = self._line.text
        if self._line.start_ms is None:
            self._label.setText(text or "New line")
        else:
            self._label.setText(f"{format_clock_ms(self._line.start_ms)}  {text}")
        self._sync_height()

    def _on_stamp(self) -> None:
        if not self._editing:
            self.stamp_requested.emit()

    def begin_edit(self) -> None:
        if self._editing:
            self._editor.setFocus()
            return
        self._editing = True
        self._label.setVisible(False)
        self._editor.setVisible(True)
        self._editor.setPlainText(self._line.text)
        self._editor.setFocus()
        self._editor.selectAll()
        self._sync_height()
        QTimer.singleShot(0, self._sync_height)

    def _commit_edit(self) -> None:
        if not self._editing:
            return
        text = self._editor.toPlainText()
        self._editing = False
        self._editor.setVisible(False)
        self._label.setVisible(True)
        self._sync_height()
        if text != self._line.text:
            self.text_changed.emit(text)
        else:
            self._refresh()

    def _cancel_edit(self) -> None:
        if not self._editing:
            return
        self._editor.setPlainText(self._line.text)
        self._editor.clearFocus()


class LyricsEditorDialog(QDialog):
    def __init__(
        self,
        cues: list[Cue],
        clip: AudioClip,
        *,
        title: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Synchronize Lyrics")
        self._lines = [_Line(text=cue.text, start_ms=int(cue.start_ms)) for cue in cues]
        self._rows: list[_LyricRow] = []
        self._duration_ms = max(0, round(clip.duration_ms))

        layout = QVBoxLayout(self)
        self.player = AudioPlayerWidget()
        self.player.set_mix_button_visible(False)
        self.player.place_reset_rate_inline()
        layout.addWidget(self.player)

        seek_row = QHBoxLayout()
        self._clock = QLabel()
        self._clock.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._clock.setMinimumWidth(em_px(self, 16))
        self._seek = QSlider(Qt.Orientation.Horizontal)
        self._seek.setRange(0, max(self._duration_ms, 1))
        self._seek.setSingleStep(100)
        self._seek.setPageStep(1000)
        self._seek.setToolTip("Seek")
        self._seek.valueChanged.connect(self._on_seek_changed)
        self._seek.sliderReleased.connect(self._on_seek_released)
        seek_row.addWidget(self._clock)
        seek_row.addWidget(self._seek, 1)
        layout.addLayout(seek_row)

        form = QFormLayout()
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-3_600.0, 3_600.0)
        self.offset.setDecimals(1)
        self.offset.setSingleStep(0.1)
        self.offset.setValue(0.0)
        self.offset.setSuffix(" s")
        self.offset.setToolTip(
            "Added to the playhead when a line is stamped. Negative stamps earlier."
        )
        form.addRow("Offset:", self.offset)
        layout.addLayout(form)
        hint = QLabel(
            "If offset is −0.5 s, clicking a lyric at time t stamps t − 0.5 s. "
            "Click a stamped line to clear its timestamp. Untimed lines are omitted when you save."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.StyledPanel)
        host = QWidget()
        self._list = QVBoxLayout(host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(4)
        self._empty_add = _action_button("Add line", "Add a lyric line")
        self._empty_add.clicked.connect(self._append_line)
        self._list.addWidget(self._empty_add)
        self._list.addStretch(1)
        for line in self._lines:
            self._insert_row(len(self._rows), line)
        self._sync_empty_add()
        self._scroll.setWidget(host)
        layout.addWidget(self._scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.player.position_changed.connect(self._on_position)
        self.player.set_clip(clip, title)
        self._duration_ms = max(0, self.player.duration_ms())
        self._seek.blockSignals(True)
        self._seek.setRange(0, max(self._duration_ms, 1))
        self._seek.blockSignals(False)
        self._set_clock(0)
        self._highlight(0)
        size_lyrics_editor(self)

    def cues(self) -> list[Cue]:
        timed = [line for line in self._lines if line.start_ms is not None]
        timed.sort(key=lambda line: int(line.start_ms or 0))
        cues = [
            Cue(
                start_ms=int(line.start_ms),
                end_ms=int(line.start_ms) + 1,
                text=line.text.strip() or " ",
            )
            for line in timed
            if line.start_ms is not None
        ]
        for i, cue in enumerate(cues):
            if i + 1 < len(cues):
                cue.end_ms = max(cues[i + 1].start_ms, cue.start_ms + 1)
            elif self._duration_ms > cue.start_ms:
                cue.end_ms = self._duration_ms
            else:
                cue.end_ms = max(cue.end_ms, cue.start_ms + _DEFAULT_LINE_MS)
        return cues

    def accept(self) -> None:
        if not self.cues():
            silent_message(
                self,
                "warn",
                "Save Lyrics",
                "Stamp at least one line before saving. Untimed lines are omitted.",
            )
            return
        super().accept()

    def done(self, result: int) -> None:
        self.player.stop()
        super().done(result)

    def _make_row(self, line: _Line) -> _LyricRow:
        row = _LyricRow(line)
        row.stamp_requested.connect(lambda r=row: self._toggle_stamp(self._index_of(r)))
        row.text_changed.connect(lambda text, r=row: self._set_text(self._index_of(r), text))
        row.add_requested.connect(lambda r=row: self._insert_after(self._index_of(r)))
        row.delete_requested.connect(lambda r=row: self._delete_row(self._index_of(r)))
        return row

    def _index_of(self, row: _LyricRow) -> int:
        return self._rows.index(row)

    def _insert_row(self, index: int, line: _Line) -> _LyricRow:
        row = self._make_row(line)
        self._rows.insert(index, row)
        self._list.insertWidget(index, row)
        return row

    def _append_line(self) -> None:
        self._insert_after(len(self._rows) - 1)

    def _insert_after(self, index: int) -> None:
        line = _Line(text="", start_ms=None)
        insert_at = index + 1
        self._lines.insert(insert_at, line)
        row = self._insert_row(insert_at, line)
        self._sync_empty_add()
        self._highlight(self.player.position_ms())
        self._scroll.ensureWidgetVisible(row)
        row.begin_edit()

    def _delete_row(self, index: int) -> None:
        row = self._rows.pop(index)
        del self._lines[index]
        self._list.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._sync_empty_add()
        self._highlight(self.player.position_ms())

    def _sync_empty_add(self) -> None:
        self._empty_add.setVisible(not self._rows)

    def _toggle_stamp(self, index: int) -> None:
        line = self._lines[index]
        if line.start_ms is not None:
            line.start_ms = None
        else:
            offset_ms = round(self.offset.value() * 1000)
            stamped = self.player.position_ms() + offset_ms
            line.start_ms = min(max(stamped, 0), self._duration_ms)
        self._rows[index].set_line(line)
        self._highlight(self.player.position_ms())

    def _set_text(self, index: int, text: str) -> None:
        self._lines[index].text = text
        self._rows[index].set_line(self._lines[index])

    def _on_position(self, position_ms: int) -> None:
        if not self._seek.isSliderDown():
            self._seek.blockSignals(True)
            self._seek.setValue(min(max(position_ms, 0), self._seek.maximum()))
            self._seek.blockSignals(False)
            self._set_clock(position_ms)
            self._highlight(position_ms)

    def _on_seek_changed(self, value: int) -> None:
        self._set_clock(value)
        self._highlight(value)
        if not self._seek.isSliderDown():
            self.player.set_position(value)

    def _on_seek_released(self) -> None:
        self.player.set_position(self._seek.value())

    def _set_clock(self, position_ms: int) -> None:
        self._clock.setText(
            f"{format_clock_ms(position_ms)} / {format_clock_ms(self._duration_ms)}"
        )

    def _highlight(self, position_ms: int) -> None:
        active_index = self._active_index(position_ms)
        for index, row in enumerate(self._rows):
            row.set_active(index == active_index)
            if index == active_index:
                self._scroll.ensureWidgetVisible(row)

    def _active_index(self, position_ms: int) -> int | None:
        timed = [
            (index, start)
            for index, line in enumerate(self._lines)
            if (start := line.start_ms) is not None
        ]
        if not timed:
            return None
        for i, (index, start) in enumerate(timed):
            end = timed[i + 1][1] if i + 1 < len(timed) else self._duration_ms
            if start <= position_ms < max(end, start + 1):
                return index
        last_index, last_start = timed[-1]
        if position_ms >= last_start:
            return last_index
        return None
