"""QStyle standard-icon message boxes, matching the JSON schema editor helpers."""
from PyQt6.QtWidgets import QApplication, QMessageBox, QStyle, QWidget

_LEVEL_ICONS = {
    "info": QStyle.StandardPixmap.SP_MessageBoxInformation,
    "warn": QStyle.StandardPixmap.SP_MessageBoxWarning,
    "critical": QStyle.StandardPixmap.SP_MessageBoxCritical,
    "question": QStyle.StandardPixmap.SP_MessageBoxQuestion,
}


def _message_box(parent: QWidget | None, level: str, title: str, text: str) -> QMessageBox:
    style = QApplication.style()
    size = style.pixelMetric(QStyle.PixelMetric.PM_MessageBoxIconSize)
    message = QMessageBox(parent)
    message.setIconPixmap(style.standardIcon(_LEVEL_ICONS[level]).pixmap(size, size))
    message.setWindowTitle(title)
    message.setText(text)
    return message


def silent_message(parent: QWidget | None, level: str, title: str, text: str) -> None:
    _message_box(parent, level, title, text).exec()


def ask_yes_no(parent: QWidget | None, title: str, text: str) -> bool:
    message = _message_box(parent, "question", title, text)
    message.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    message.setDefaultButton(QMessageBox.StandardButton.No)
    return message.exec() == QMessageBox.StandardButton.Yes


def ask_save_discard_cancel(parent: QWidget | None, title: str, text: str) -> str:
    message = _message_box(parent, "question", title, text)
    message.setStandardButtons(
        QMessageBox.StandardButton.Save
        | QMessageBox.StandardButton.Discard
        | QMessageBox.StandardButton.Cancel
    )
    clicked = message.exec()
    if clicked == QMessageBox.StandardButton.Save:
        return "save"
    if clicked == QMessageBox.StandardButton.Discard:
        return "discard"
    return "cancel"
