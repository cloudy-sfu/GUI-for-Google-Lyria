"""QStyle standard-icon message boxes, matching the JSON schema editor helpers."""



from PyQt6.QtWidgets import QApplication, QMessageBox, QStyle, QWidget

_LEVEL_ICONS = {
    "info": QStyle.StandardPixmap.SP_MessageBoxInformation,
    "warn": QStyle.StandardPixmap.SP_MessageBoxWarning,
    "critical": QStyle.StandardPixmap.SP_MessageBoxCritical,
    "question": QStyle.StandardPixmap.SP_MessageBoxQuestion,
}


def _icon_pixmap(icon: QStyle.StandardPixmap):
    style = QApplication.style()
    size = style.pixelMetric(QStyle.PixelMetric.PM_MessageBoxIconSize)
    return style.standardIcon(icon).pixmap(size, size)


def silent_message(parent: QWidget | None, level: str, title: str, text: str) -> None:
    icon = _LEVEL_ICONS.get(level)
    if icon is None:
        raise ValueError(f"unsupported message level: {level}")
    message = QMessageBox(parent)
    message.setIconPixmap(_icon_pixmap(icon))
    message.setWindowTitle(title)
    message.setText(text)
    message.exec()


def icon_message(
    parent: QWidget | None,
    title: str,
    text: str,
    icon: QStyle.StandardPixmap | None = None,
) -> None:
    message = QMessageBox(parent)
    if icon is not None:
        message.setIconPixmap(_icon_pixmap(icon))
    message.setWindowTitle(title)
    message.setText(text)
    message.exec()


def ask_yes_no(parent: QWidget | None, title: str, text: str) -> bool:
    message = QMessageBox(parent)
    message.setIconPixmap(_icon_pixmap(QStyle.StandardPixmap.SP_MessageBoxQuestion))
    message.setWindowTitle(title)
    message.setText(text)
    message.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    message.setDefaultButton(QMessageBox.StandardButton.No)
    return message.exec() == QMessageBox.StandardButton.Yes


def ask_save_discard_cancel(parent: QWidget | None, title: str, text: str) -> str:
    message = QMessageBox(parent)
    message.setIconPixmap(_icon_pixmap(QStyle.StandardPixmap.SP_MessageBoxQuestion))
    message.setWindowTitle(title)
    message.setText(text)
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
