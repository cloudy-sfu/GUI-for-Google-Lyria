"""Shortcuts table dialog, mirroring the JSON schema editor TableDialog pattern."""
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.style import fit_interactive_columns

SHORTCUTS = [
    ("New Project", "Ctrl+N"),
    ("Open Project", "Ctrl+O"),
    ("Save", "Ctrl+S"),
    ("Save As…", "Ctrl+Shift+S"),
    ("Exit", "Ctrl+Q"),
    ("Undo", "Ctrl+Z"),
    ("Redo", "Ctrl+Y"),
    ("Toggle Transcript panel", "Ctrl+T"),
    ("Chat with Lyria", "Ctrl+L"),
    ("New conversation", "Ctrl+N (chat window)"),
    ("Rename conversation", "F2 (chat window)"),
    ("Delete conversation", "Del (chat list)"),
    ("Delete selected track", "Del (timeline)"),
    ("Shortcuts", "F1"),
]


class ShortcutsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Shortcuts")
        layout = QVBoxLayout(self)
        table = QTableWidget(len(SHORTCUTS), 2)
        table.setHorizontalHeaderLabels(["Action", "Shortcut"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, (action, shortcut) in enumerate(SHORTCUTS):
            table.setItem(row, 0, QTableWidgetItem(action))
            table.setItem(row, 1, QTableWidgetItem(shortcut))
        fit_interactive_columns(table)
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
