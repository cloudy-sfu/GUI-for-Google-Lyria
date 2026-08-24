"""Scrollable explanation of the mix timeline, opened from Help → Time line."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui.style import height_for_lines
from gui.widgets.timeline import TIMELINE_HELP


class TimelineHelpDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Time line")
        layout = QVBoxLayout(self)
        text = QLabel(TIMELINE_HELP)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(text)
        area.setMinimumHeight(height_for_lines(self, 14))
        layout.addWidget(area, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.resize(height_for_lines(self, 26), height_for_lines(self, 18))
