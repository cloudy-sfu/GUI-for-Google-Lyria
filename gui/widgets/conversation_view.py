"""Left column: session warnings, rolling transcript, and the chat launcher."""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
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
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        layout = QHBoxLayout(self)
        header = QLabel()
        size = icon_size(self)
        header.setPixmap(
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
            .pixmap(size, self.devicePixelRatioF())
        )
        header.setFixedSize(size)
        header.setToolTip("Warning")
        header.setAccessibleName("Warning")
        body = QLabel(text)
        body.setWordWrap(True)
        body.setMinimumWidth(0)
        body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(header)
        layout.addWidget(body)


class ConversationView(QWidget):
    chat_requested = pyqtSignal()
    language_changed = pyqtSignal(str)
    cue_seek_requested = pyqtSignal(int)
    import_requested = pyqtSignal()
    export_requested = pyqtSignal()
    translate_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._warning_texts: list[str] = []
        self._transcript: Transcript | None = None
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.warning_host = QWidget()
        self.warning_host.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        self.warning_list = QVBoxLayout(self.warning_host)
        self.warning_list.setContentsMargins(0, 0, 0, 0)
        self.warning_scroll = QScrollArea()
        self.warning_scroll.setWidgetResizable(True)
        self.warning_scroll.setWidget(self.warning_host)
        self.warning_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.warning_scroll.setMaximumHeight(height_for_lines(self.warning_scroll, 8))
        self.warning_scroll.setVisible(False)
        layout.addWidget(self.warning_scroll)

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
            "Translate lyrics with the model set in Preferences"
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
        if text in self._warning_texts:
            return
        self._warning_texts.append(text)
        self.warning_list.addWidget(WarningBubble(text))
        self.warning_scroll.setVisible(True)

    def clear_warnings(self) -> None:
        self._warning_texts.clear()
        while self.warning_list.count():
            item = self.warning_list.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self.warning_scroll.setVisible(False)

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
