"""Named channel-layout conversion and stereo pan."""



from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QSlider,
    QVBoxLayout,
    QWidget,
    QLabel,
)

import numpy as np

from audio.channels import LAYOUTS


class ChannelsDialog(QDialog):
    def __init__(self, current: str = "stereo", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Channels")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.layout_combo = QComboBox()
        for name in LAYOUTS:
            label = {
                "mono": "Make mono",
                "stereo": "Make stereo",
                "5.1": "Make 5.1",
            }.get(name, name)
            self.layout_combo.addItem(label, name)
        index = int(np.maximum(0, list(LAYOUTS).index(current) if current in LAYOUTS else 1))
        self.layout_combo.setCurrentIndex(index)
        self.pan = QSlider(Qt.Orientation.Horizontal)
        self.pan.setRange(-100, 100)
        self.pan.setValue(0)
        self.pan_label = QLabel("Center")
        self.pan.valueChanged.connect(self._update_pan_label)
        form.addRow("Layout:", self.layout_combo)
        form.addRow("Pan (stereo):", self.pan)
        form.addRow("", self.pan_label)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_pan_label(self, value: int) -> None:
        if value == 0:
            self.pan_label.setText("Center")
        elif value < 0:
            self.pan_label.setText(f"Left {np.abs(value)}%")
        else:
            self.pan_label.setText(f"Right {value}%")

    def values(self) -> tuple[str, float]:
        return str(self.layout_combo.currentData()), self.pan.value() / 100.0
