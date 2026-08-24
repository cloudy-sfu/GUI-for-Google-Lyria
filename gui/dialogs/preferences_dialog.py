"""App-level settings: credentials, composition model, export format."""



from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from app_context import (
    DEFAULT_COMPOSITION_MODEL,
    DEFAULT_TRANSLATION_MODEL,
    Settings,
)
from audio.io import EXPORT_FORMATS


class PreferencesDialog(QDialog):
    def __init__(
        self,
        settings: Settings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self._settings = settings
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.api_key = QLineEdit(settings.gemini_api_key or "")
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Google AI Studio API key")
        form.addRow("Gemini API key:", self.api_key)
        self.composition_model = QLineEdit(
            settings.composition_model or DEFAULT_COMPOSITION_MODEL
        )
        self.composition_model.setPlaceholderText(DEFAULT_COMPOSITION_MODEL)
        form.addRow("Composition model:", self.composition_model)
        self.translation_model = QLineEdit(
            settings.translation_model or DEFAULT_TRANSLATION_MODEL
        )
        self.translation_model.setPlaceholderText(DEFAULT_TRANSLATION_MODEL)
        form.addRow("Translation model:", self.translation_model)
        self.export_format = QComboBox()
        self.export_format.addItems(list(EXPORT_FORMATS))
        self.export_format.setCurrentText(settings.export_format)
        form.addRow("Default export format:", self.export_format)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        hint = self.sizeHint()
        self.resize(int(hint.width() * 1.5), hint.height())

    def apply_to(self, settings: Settings) -> None:
        settings.gemini_api_key = self.api_key.text().strip() or None
        settings.composition_model = (
            self.composition_model.text().strip() or DEFAULT_COMPOSITION_MODEL
        )
        settings.translation_model = (
            self.translation_model.text().strip() or DEFAULT_TRANSLATION_MODEL
        )
        settings.export_format = self.export_format.currentText()
