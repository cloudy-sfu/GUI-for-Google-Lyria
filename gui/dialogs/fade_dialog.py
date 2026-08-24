"""Fade in or fade out duration and curve."""
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)


class FadeDialog(QDialog):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.duration = QLineEdit("500")
        self.duration.setValidator(QIntValidator(1, 3_600_000, self))
        self.shape = QComboBox()
        self.shape.addItem("Linear", "linear")
        self.shape.addItem("Equal-power / exponential", "exp")
        form.addRow("Duration (ms):", self.duration)
        form.addRow("Shape:", self.shape)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[int, str]:
        return int(self.duration.text() or 500), str(self.shape.currentData())
