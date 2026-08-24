"""Pitch-preserving speed change, 0.01x to 10.00x."""



from PyQt6.QtGui import QDoubleValidator
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

import numpy as np


class SpeedDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Adjust Speed")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.ratio = QLineEdit("1.00")
        validator = QDoubleValidator(0.01, 10.00, 2, self)
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        self.ratio.setValidator(validator)
        self.preserve = QCheckBox("Preserve pitch")
        self.preserve.setChecked(True)
        form.addRow("Speed ratio (0.01–10.00):", self.ratio)
        form.addRow("", self.preserve)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[float, bool]:
        ratio = np.round(float(self.ratio.text() or 1.0), 2)
        return ratio, self.preserve.isChecked()
