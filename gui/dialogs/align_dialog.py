"""Place tracks on the mix timeline by anchoring them to another track."""
from dataclasses import dataclass

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from gui.style import format_clock

START = "start"
END = "end"
GAP_LIMIT_MS = 60 * 60 * 1000
_PREVIEW_ROWS = 4


@dataclass(frozen=True)
class AlignTrack:
    """A track as the timeline sees it: where it starts and how long it plays."""

    track_id: str
    name: str
    offset_ms: int
    duration_ms: int

    def anchor_ms(self, anchor: str) -> int:
        return self.offset_ms + (self.duration_ms if anchor == END else 0)


def aligned_offset_ms(
    moving: AlignTrack,
    moving_anchor: str,
    reference: AlignTrack,
    reference_anchor: str,
    gap_ms: int = 0,
) -> int:
    target = reference.anchor_ms(reference_anchor) + gap_ms
    if moving_anchor == END:
        target -= moving.duration_ms
    return max(0, target)


class AlignDialog(QDialog):
    def __init__(
        self,
        moving: list[AlignTrack],
        references: list[AlignTrack],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Align to Track")
        self._moving = list(moving)
        self._references = list(references)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self._moving_summary()))
        self.moving_anchor = QComboBox()
        self.reference_anchor = QComboBox()
        for combo in (self.moving_anchor, self.reference_anchor):
            combo.addItem("start", START)
            combo.addItem("end", END)
        # End-to-start is the sequential case, the one people reach for most.
        self.reference_anchor.setCurrentIndex(1)
        self.reference = QComboBox()
        self.reference.setSizePolicy(
            QSizePolicy.Policy.Expanding, self.reference.sizePolicy().verticalPolicy()
        )
        for track in self._references:
            self.reference.addItem(
                f"{track.name} ({format_clock(track.duration_ms)})", track.track_id
            )
        self.gap = QSpinBox()
        self.gap.setRange(-GAP_LIMIT_MS, GAP_LIMIT_MS)
        self.gap.setSingleStep(100)
        self.gap.setSuffix(" ms")
        self.gap.setToolTip(
            "Positive leaves silence after the anchor point; negative overlaps the two tracks."
        )

        # One sentence over three rows, with the two anchor pickers in the same
        # column so both halves read the same way.
        grid = QGridLayout()
        grid.addWidget(QLabel("Move the"), 0, 0)
        grid.addWidget(self.moving_anchor, 0, 1)
        grid.addWidget(QLabel("of this track"), 0, 2, 1, 2)
        grid.addWidget(QLabel("to the"), 1, 0)
        grid.addWidget(self.reference_anchor, 1, 1)
        grid.addWidget(QLabel("of track"), 1, 2)
        grid.addWidget(self.reference, 1, 3)
        grid.addWidget(QLabel("with a gap of"), 2, 0)
        grid.addWidget(self.gap, 2, 1)
        grid.setColumnStretch(3, 1)
        layout.addLayout(grid)

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.moving_anchor.currentIndexChanged.connect(self._refresh_preview)
        self.reference_anchor.currentIndexChanged.connect(self._refresh_preview)
        self.reference.currentIndexChanged.connect(self._refresh_preview)
        self.gap.valueChanged.connect(self._refresh_preview)
        self._refresh_preview()
        # A combo box asks for far less than its content when it can shrink, so
        # pin the natural width to keep whole track names readable.
        self.setMinimumWidth(self.sizeHint().width())

    def offsets(self) -> dict[str, int]:
        """New timeline start for every moving track, keyed by track id."""
        reference = self._selected_reference()
        if reference is None:
            return {}
        moving_anchor = str(self.moving_anchor.currentData())
        reference_anchor = str(self.reference_anchor.currentData())
        gap = int(self.gap.value())
        return {
            track.track_id: aligned_offset_ms(
                track, moving_anchor, reference, reference_anchor, gap
            )
            for track in self._moving
        }

    def _moving_summary(self) -> str:
        if len(self._moving) == 1:
            return f"Moving: {self._moving[0].name}"
        names = ", ".join(track.name for track in self._moving)
        return f"Moving {len(self._moving)} tracks: {names}"

    def _selected_reference(self) -> AlignTrack | None:
        track_id = self.reference.currentData()
        for track in self._references:
            if track.track_id == track_id:
                return track
        return None

    def _refresh_preview(self) -> None:
        offsets = self.offsets()
        lines = []
        for track in self._moving[:_PREVIEW_ROWS]:
            offset = offsets.get(track.track_id, track.offset_ms)
            lines.append(f"{track.name} starts at {offset} ms ({format_clock(offset)})")
        remaining = len(self._moving) - _PREVIEW_ROWS
        if remaining > 0:
            lines.append(f"and {remaining} more")
        self.preview.setText("\n".join(lines))
