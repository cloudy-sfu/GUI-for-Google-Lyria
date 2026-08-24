"""Trim or remove a time range on the selected track."""



from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

import numpy as np


class CutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, duration_ms: int = 0) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cut")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.start_edit = QLineEdit("0")
        self.end_edit = QLineEdit(str(int(np.maximum(0, duration_ms))))
        validator = QIntValidator(0, int(np.maximum(duration_ms, 1_000_000)), self)
        self.start_edit.setValidator(validator)
        self.end_edit.setValidator(validator)
        form.addRow("Start (ms):", self.start_edit)
        form.addRow("End (ms):", self.end_edit)
        layout.addLayout(form)
        self.keep = QRadioButton("Keep this range")
        self.remove = QRadioButton("Remove this range")
        self.keep.setChecked(True)
        layout.addWidget(self.keep)
        layout.addWidget(self.remove)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[int, int, str]:
        start = int(self.start_edit.text() or 0)
        end = int(self.end_edit.text() or 0)
        mode = "keep" if self.keep.isChecked() else "remove"
        return start, end, mode
