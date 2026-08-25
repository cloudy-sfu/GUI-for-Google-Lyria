"""Left column: session warnings, rolling transcript, and the chat launcher."""
from PyQt6.QtCore import QSize, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QPalette, QResizeEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from gui.style import format_clock, height_for_lines, icon_size
from workspaces.transcript import Transcript


class WarningBubble(QFrame):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.text = text
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        self._header = QLabel()
        size = icon_size(self)
        self._header.setPixmap(
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
            .pixmap(size, self.devicePixelRatioF())
        )
        self._header.setFixedSize(size)
        self._header.setToolTip("Warning")
        self._header.setAccessibleName("Warning")
        self._body = QLabel(text)
        self._body.setWordWrap(True)
        self._body.setMinimumWidth(0)
        self._body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._header, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._body, 1)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        layout = self.layout()
        margins = layout.contentsMargins() if layout is not None else self.contentsMargins()
        spacing = layout.spacing() if layout is not None else 0
        if spacing < 0:
            spacing = 0
        inner = max(
            1,
            width
            - margins.left()
            - margins.right()
            - self._header.sizeHint().width()
            - spacing
            - self.frameWidth() * 2,
        )
        metrics = self._body.fontMetrics()
        body_margins = self._body.contentsMargins()
        text_width = max(1, inner - body_margins.left() - body_margins.right())
        text_h = metrics.boundingRect(
            0, 0, text_width, 0, Qt.TextFlag.TextWordWrap, self._body.text()
        ).height()
        body_h = text_h + body_margins.top() + body_margins.bottom()
        content = max(self._header.sizeHint().height(), body_h)
        return (
            margins.top()
            + margins.bottom()
            + content
            + self.frameWidth() * 2
        )

    def sizeHint(self) -> QSize:
        width = self.width() if self.width() > 0 else super().sizeHint().width()
        return QSize(max(width, 1), self.heightForWidth(max(width, 1)))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(max(self.width(), 1)))


class WarningStrip(QWidget):
    """Warning stack that shrinks to the remaining messages instead of keeping old height."""

    def __init__(self, max_lines: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._max_lines = max_lines
        self._texts: list[str] = []
        self._syncing = False
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._list = QVBoxLayout(self)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(4)
        self.setVisible(False)
        self.setFixedHeight(0)

    def add_warning(self, text: str) -> None:
        if text in self._texts:
            return
        self._texts.append(text)
        self._list.addWidget(WarningBubble(text))
        self.setVisible(True)
        self._schedule_sync()

    def clear_warnings(self) -> None:
        self._texts.clear()
        while self._list.count():
            item = self._list.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.setVisible(False)
        self.setFixedHeight(0)
        self.updateGeometry()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._texts and event.oldSize().width() != event.size().width():
            self._schedule_sync()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        if not self._texts:
            return 0
        return self._preferred_height(width)

    def sizeHint(self) -> QSize:
        if not self._texts:
            return QSize(0, 0)
        width = self.width() if self.width() > 0 else 256
        return QSize(width, self._preferred_height(width))

    def minimumSizeHint(self) -> QSize:
        if not self._texts:
            return QSize(0, 0)
        width = self.width() if self.width() > 0 else 256
        return QSize(0, self._preferred_height(width))

    def _schedule_sync(self) -> None:
        QTimer.singleShot(0, self._sync_height)

    def _sync_height(self) -> None:
        if self._syncing or not self._texts:
            return
        width = max(self.width(), 1)
        self._syncing = True
        for index in range(self._list.count()):
            item = self._list.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is None or widget.isHidden():
                continue
            widget.setFixedHeight(widget.heightForWidth(width))
        target = self._preferred_height(width)
        if self.minimumHeight() != target or self.maximumHeight() != target:
            self.setFixedHeight(target)
            self.updateGeometry()
            parent = self.parentWidget()
            if parent is not None and parent.layout() is not None:
                parent.layout().activate()
        self._syncing = False

    def _preferred_height(self, width: int) -> int:
        cap = height_for_lines(self, self._max_lines)
        return max(0, min(self._content_height(max(width, 1)), cap))

    def _content_height(self, width: int) -> int:
        height = self._list.contentsMargins().top() + self._list.contentsMargins().bottom()
        visible = 0
        spacing = self._list.spacing()
        if spacing < 0:
            spacing = 0
        for index in range(self._list.count()):
            item = self._list.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is None or widget.isHidden():
                continue
            if visible:
                height += spacing
            height += widget.heightForWidth(width)
            visible += 1
        return height


class ConversationView(QWidget):
    chat_requested = pyqtSignal()
    language_changed = pyqtSignal(str)
    cue_seek_requested = pyqtSignal(int)
    import_requested = pyqtSignal()
    export_requested = pyqtSignal()
    translate_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._transcript: Transcript | None = None
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.warnings = WarningStrip(5)
        layout.addWidget(self.warnings)

        trans_row = QHBoxLayout()
        trans_row.addWidget(QLabel("Transcript"))
        self.language = QComboBox()
        self.language.setMinimumWidth(0)
        self.language.currentTextChanged.connect(self.language_changed)
        trans_row.addWidget(self.language)
        self.import_btn = QPushButton("Import")
        self.export_btn = QPushButton("Export")
        self.translate_btn = QPushButton("Translate")
        self.import_btn.setToolTip("Import LRC lyrics for the selected track")
        self.export_btn.setToolTip("Export the current lyrics as LRC")
        self.translate_btn.setToolTip(
            "Translate lyrics with the model set in Settings"
        )
        self.import_btn.clicked.connect(self.import_requested.emit)
        self.export_btn.clicked.connect(self.export_requested.emit)
        self.translate_btn.clicked.connect(self.translate_requested.emit)
        trans_row.addWidget(self.import_btn)
        trans_row.addWidget(self.export_btn)
        trans_row.addWidget(self.translate_btn)
        trans_row.addStretch()
        layout.addLayout(trans_row)
        self._can_import = False
        self._can_export = False
        self._can_translate = False
        self._busy = False
        self.set_transcript_actions_enabled(can_import=False, can_export=False, can_translate=False)
        self.cues = QListWidget()
        self.cues.setMinimumWidth(0)
        self.cues.itemClicked.connect(self._on_cue_clicked)
        layout.addWidget(self.cues, 1)

        self.chat_button = QPushButton("Chat with Lyria")
        self.chat_button.clicked.connect(self.chat_requested.emit)
        layout.addWidget(self.chat_button)

    def add_warning(self, text: str) -> None:
        self.warnings.add_warning(text)

    def clear_warnings(self) -> None:
        self.warnings.clear_warnings()

    def set_transcript(
        self,
        transcript: Transcript | None,
        languages: list[str],
        preferred: str | None = None,
    ) -> None:
        self._transcript = transcript
        current = preferred or self.language.currentText()
        self.language.blockSignals(True)
        self.language.clear()
        self.language.addItems(languages)
        if current in languages:
            self.language.setCurrentText(current)
        elif languages:
            self.language.setCurrentIndex(0)
        self.language.blockSignals(False)
        self._reload_cues()

    def set_transcript_actions_enabled(
        self,
        *,
        can_import: bool,
        can_export: bool,
        can_translate: bool,
    ) -> None:
        self._can_import = can_import
        self._can_export = can_export
        self._can_translate = can_translate
        self._apply_action_enabled()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._apply_action_enabled()

    def _apply_action_enabled(self) -> None:
        idle = not self._busy
        self.import_btn.setEnabled(idle and self._can_import)
        self.export_btn.setEnabled(idle and self._can_export)
        self.translate_btn.setEnabled(idle and self._can_translate)
        self.language.setEnabled(idle)
        self.translate_btn.setText("Translating…" if self._busy else "Translate")

    def highlight_at(self, position_ms: int) -> None:
        if self._transcript is None:
            return
        palette = self.palette()
        active_color = palette.color(QPalette.ColorRole.Text)
        idle_color = palette.color(QPalette.ColorRole.PlaceholderText)
        for row in range(self.cues.count()):
            item = self.cues.item(row)
            start, end = item.data(Qt.ItemDataRole.UserRole)
            active = start <= position_ms < end
            font = item.font()
            font.setBold(active)
            item.setFont(font)
            item.setForeground(active_color if active else idle_color)
            if active:
                self.cues.scrollToItem(item)

    def _reload_cues(self) -> None:
        self.cues.clear()
        if self._transcript is None:
            return
        palette = self.palette()
        idle = palette.color(QPalette.ColorRole.PlaceholderText)
        for cue in self._transcript.cues:
            item = QListWidgetItem(
                f"{format_clock(cue.start_ms)}  {cue.text.replace(chr(10), ' ')}"
            )
            item.setData(Qt.ItemDataRole.UserRole, (cue.start_ms, cue.end_ms))
            item.setForeground(idle)
            self.cues.addItem(item)

    def _on_cue_clicked(self, item: QListWidgetItem) -> None:
        start, _end = item.data(Qt.ItemDataRole.UserRole)
        self.cue_seek_requested.emit(int(start))
