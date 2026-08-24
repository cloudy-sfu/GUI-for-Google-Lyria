"""Right column: compact player above the mix timeline."""

from PyQt6.QtCore import QPoint, pyqtSignal
from PyQt6.QtWidgets import QMenu, QVBoxLayout, QWidget

from gui.widgets.audio_player import AudioPlayerWidget
from gui.widgets.timeline import TimelineWidget

CLIP_ACTIONS = (
    ("rename", "Rename…"),
    ("start", "Start…"),
    ("gain", "Gain…"),
    ("mute", "Mute"),
    ("duplicate", "Duplicate"),
    ("align", "Align to track…"),
    ("cut", "Cut…"),
    ("fade_in", "Fade in…"),
    ("fade_out", "Fade out…"),
    ("speed", "Speed…"),
    ("reverse", "Reverse"),
    ("channels", "Channels…"),
    None,
    ("clear_edits", "Clear modifications"),
)


class EditingArea(QWidget):
    edit_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.player = AudioPlayerWidget()
        self.timeline = TimelineWidget()
        layout.addWidget(self.player)
        layout.addWidget(self.timeline, 1)
        self.timeline.context_menu_requested.connect(self._show_clip_menu)

    def _show_clip_menu(self, global_pos: QPoint, muted: bool) -> None:
        menu = QMenu(self)
        for entry in CLIP_ACTIONS:
            if entry is None:
                menu.addSeparator()
                continue
            key, label = entry
            if key == "mute":
                label = "Unmute" if muted else "Mute"
            action = menu.addAction(label)
            if action is not None:
                action.setData(key)
        chosen = menu.exec(global_pos)
        if chosen is not None and chosen.data():
            self.edit_requested.emit(str(chosen.data()))
