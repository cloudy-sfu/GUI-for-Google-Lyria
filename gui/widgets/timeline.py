"""Mix timeline: drag clips sideways to set offset, up or down to reorder tracks.

Time maps to pixels through a view window (`_view_start_ms` plus a visible span)
so Ctrl+scroll can zoom. A span of zero means "fit the whole mix", which is the
default and keeps the widget self-scaling as tracks are added. A scroll bar
along the bottom pans that window once the span is narrower than the mix.
"""
from dataclasses import dataclass

from PyQt6.QtCore import QEvent, QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QHelpEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QWheelEvent,
)
from PyQt6.QtWidgets import QScrollBar, QSizePolicy, QToolTip, QVBoxLayout, QWidget

from audio.channels import layout_for_channel_count
from audio.render import estimate_mix_duration_ms, estimate_track_duration_ms
from gui.style import (
    em_px,
    format_clock,
    format_clock_ms,
    height_for_lines,
    wheel_time_delta_ms,
    wheel_zoom_notches,
)
from workspaces.models import Mix, Track

_DRAG_SLOP = 4
# Ruler labels are second-resolution, so a one-second floor keeps ticks unique.
_MIN_SPAN_MS = 1_000
_ZOOM_PER_NOTCH = 1.25
_GUTTER_EM = 8

TIMELINE_HELP = (
    "Drag a clip left or right to change its start time.\n"
    "Drag a clip up or down to reorder tracks.\n"
    "Click the ruler, or click an empty part of a lane, to move the playhead.\n"
    "Scroll to move the playhead (1 second per scroll line).\n"
    "Ctrl+scroll to zoom in or out around the pointer.\n"
    "Use the horizontal scroll bar at the bottom to pan while zoomed in.\n"
    "Right-click a clip for rename, start, gain, mute, duplicate and the "
    "source edit operations.\n"
    "Delete removes the selected track (Undo restores it).\n"
    "\n"
    "The label at the start of each row shows the track name, the source file "
    "format, and where the audio came from.\n"
    "The label inside a clip shows, on the first line, the rendered duration, "
    "the speed factor and the mix gain; and on the second line, the fade in, "
    "the fade out, the channel layout and the start time.\n"
    "Lines that do not fit inside the clip are hidden; hover the clip to see "
    "all of them in a tooltip."
)


@dataclass
class _ClipVisual:
    track_id: str
    name: str
    offset_ms: int
    duration_ms: int
    gain_db: float
    mute: bool
    lines: tuple[str, str]
    row: int
    rect: QRect


class TimelineWidget(QWidget):
    clip_changed = pyqtSignal(str, int)
    track_moved = pyqtSignal(str, int)
    track_selected = pyqtSignal(str)
    # Deliberately picked with a left click, as opposed to merely selected so a
    # context menu has a target. Only this one is meant to reach the transport.
    track_activated = pyqtSignal(str)
    seek_requested = pyqtSignal(int)
    # Global position plus the mute state of the clicked track, so the menu can
    # offer "Mute" or "Unmute" without reaching back into the project.
    context_menu_requested = pyqtSignal(QPoint, bool)
    delete_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self._tracks: list[Track] = []
        self._mix = Mix()
        self._selected_id = ""
        self._position_ms = 0
        self._duration_ms = 1
        self._view_start_ms = 0
        self._view_span_ms = 0
        self._gutter = em_px(self, _GUTTER_EM)
        self._ruler = height_for_lines(self, 1) + 8
        self._row_h = height_for_lines(self, 2) + 10
        self._clips: list[_ClipVisual] = []
        self._drag: _ClipVisual | None = None
        self._drag_origin = QPoint()
        self._drag_offset0 = 0
        self._drag_row0 = 0
        self._drag_axis = ""
        self._dragging = False
        self._syncing_scroll = False
        self._hscroll = QScrollBar(Qt.Orientation.Horizontal, self)
        self._hscroll.setToolTip("Drag to pan the visible time range.")
        self._hscroll.valueChanged.connect(self._on_hscroll)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(self._gutter, 0, 0, 0)
        layout.addStretch(1)
        layout.addWidget(self._hscroll)
        self.setMinimumHeight(height_for_lines(self, 8) + self._hscroll.sizeHint().height())
        self._sync_scrollbar()

    def reload(self, tracks: list[Track], mix: Mix) -> None:
        self._tracks = list(tracks)
        self._mix = mix
        self._rebuild()
        self.update()

    def set_selected(self, track_id: str) -> None:
        if self._selected_id == track_id:
            return
        self._selected_id = track_id
        self.update()

    def selected_track_id(self) -> str:
        return self._selected_id

    def set_position(self, position_ms: int) -> None:
        self._position_ms = max(0, position_ms)
        self._scroll_into_view(self._position_ms)
        self.update()

    def position_ms(self) -> int:
        return self._position_ms

    def duration_ms(self) -> int:
        return self._duration_ms

    def _rebuild(self) -> None:
        clips: list[_ClipVisual] = []
        for row, track in enumerate(self._tracks):
            mix_clip = self._mix.clip_for_track(track.id)
            offset = mix_clip.offset_ms if mix_clip else 0
            gain = mix_clip.gain_db if mix_clip else 0.0
            mute = bool(mix_clip and mix_clip.mute)
            duration = max(1, estimate_track_duration_ms(track))
            clips.append(
                _ClipVisual(
                    track_id=track.id,
                    name=track.name,
                    offset_ms=offset,
                    duration_ms=duration,
                    gain_db=gain,
                    mute=mute,
                    lines=clip_label_lines(track, duration, offset, gain),
                    row=row,
                    rect=QRect(),
                )
            )
        self._duration_ms = max(
            estimate_mix_duration_ms(self._tracks, self._mix), self._position_ms, 1
        )
        self._clips = clips
        self._clamp_view()
        self._layout_clips()

    def _apply_gutter(self) -> None:
        gutter = em_px(self, _GUTTER_EM)
        if gutter == self._gutter:
            return
        self._gutter = gutter
        layout = self.layout()
        if layout is not None:
            layout.setContentsMargins(self._gutter, 0, 0, 0)
        self._layout_clips()
        self.update()

    def _lane_width(self) -> int:
        return max(1, self.width() - self._gutter - 8)

    def _span_ms(self) -> int:
        if self._view_span_ms <= 0:
            return self._duration_ms
        return max(_MIN_SPAN_MS, min(self._view_span_ms, self._duration_ms))

    def _clamp_view(self) -> None:
        slack = max(0, self._duration_ms - self._span_ms())
        self._view_start_ms = min(max(self._view_start_ms, 0), slack)

    def _x_for_ms(self, ms: int) -> int:
        lane = self._lane_width()
        return self._gutter + int((ms - self._view_start_ms) * lane / self._span_ms())

    def _layout_clips(self) -> None:
        # Clip rects are clamped to one lane width outside the view so extreme
        # zoom cannot produce huge rects (or visible rounded corners) off-screen.
        lane = self._lane_width()
        left_bound = self._gutter - lane
        right_bound = self._gutter + 2 * lane
        for clip in self._clips:
            x = self._x_for_ms(clip.offset_ms)
            w = max(24, int(clip.duration_ms * lane / self._span_ms()))
            left = max(x, left_bound)
            right = min(x + w, right_bound)
            y = self._ruler + clip.row * self._row_h + 4
            clip.rect = QRect(left, y, max(0, right - left), self._row_h - 8)
        self._sync_scrollbar()

    def _sync_scrollbar(self) -> None:
        span = self._span_ms()
        slack = max(0, self._duration_ms - span)
        self._syncing_scroll = True
        self._hscroll.setRange(0, slack)
        self._hscroll.setPageStep(span)
        self._hscroll.setSingleStep(max(1, span // 20))
        self._hscroll.setValue(self._view_start_ms)
        self._syncing_scroll = False
        self._hscroll.setEnabled(slack > 0)

    def _on_hscroll(self, value: int) -> None:
        if self._syncing_scroll or value == self._view_start_ms:
            return
        self._view_start_ms = value
        self._clamp_view()
        self._layout_clips()
        self.update()

    def _content_height(self) -> int:
        """Height above the scroll bar; valid before the layout has run."""
        return max(0, self.height() - self._hscroll.sizeHint().height())

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._apply_gutter()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            clip = self._hit_clip(event.pos())
            if clip is not None:
                QToolTip.showText(
                    event.globalPos(), f"{clip.name}\n" + "\n".join(clip.lines), self
                )
                return True
            row = self._row_at(event.pos().y())
            if event.pos().x() < self._gutter and row is not None:
                QToolTip.showText(event.globalPos(), self._tracks[row].labeled_name(), self)
                return True
        return super().event(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_clips()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.color(QPalette.ColorRole.Base))
        self._paint_ruler(painter, palette)
        self._paint_lanes(painter, palette)
        painter.save()
        painter.setClipRect(self._lane_rect())
        for clip in self._clips:
            self._paint_clip(painter, palette, clip)
        self._paint_playhead(painter, palette)
        painter.restore()

    def _lane_rect(self) -> QRect:
        return QRect(self._gutter, 0, self.width() - self._gutter, self._content_height())

    def _paint_ruler(self, painter: QPainter, palette: QPalette) -> None:
        painter.fillRect(0, 0, self.width(), self._ruler, palette.color(QPalette.ColorRole.Window))
        painter.setPen(palette.color(QPalette.ColorRole.Mid))
        painter.drawLine(0, self._ruler, self.width(), self._ruler)
        span = self._span_ms()
        step = _nice_tick(span, self._lane_width())
        painter.save()
        painter.setClipRect(self._lane_rect())
        painter.setPen(palette.color(QPalette.ColorRole.Text))
        end = self._view_start_ms + span
        t = self._view_start_ms - self._view_start_ms % step
        while t <= end:
            x = self._x_for_ms(t)
            painter.drawLine(x, self._ruler - 6, x, self._ruler)
            painter.drawText(x + 4, self._ruler - 10, format_clock(t))
            t += step
        painter.restore()

    def _paint_lanes(self, painter: QPainter, palette: QPalette) -> None:
        for row, track in enumerate(self._tracks):
            y = self._ruler + row * self._row_h
            painter.setPen(palette.color(QPalette.ColorRole.Mid))
            painter.drawLine(0, y + self._row_h, self.width(), y + self._row_h)
            painter.setPen(palette.color(QPalette.ColorRole.Text))
            painter.save()
            painter.setClipRect(QRect(0, y, self._gutter, self._row_h))
            painter.drawText(
                QRect(6, y, self._gutter - 8, self._row_h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                track.labeled_name(),
            )
            painter.restore()

    def _paint_clip(self, painter: QPainter, palette: QPalette, clip: _ClipVisual) -> None:
        if clip.rect.isEmpty():
            return
        selected = clip.track_id == self._selected_id
        fill = palette.color(QPalette.ColorRole.Highlight)
        if clip.mute:
            fill = palette.color(QPalette.ColorRole.Mid)
        elif not selected:
            fill = palette.color(QPalette.ColorRole.Button)
        painter.setPen(palette.color(QPalette.ColorRole.Highlight if selected else QPalette.ColorRole.Mid))
        painter.setBrush(fill)
        painter.drawRoundedRect(clip.rect, 4, 4)
        text_color = palette.color(
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.ButtonText
        )
        if clip.mute:
            text_color = palette.color(QPalette.ColorRole.PlaceholderText)
        painter.setPen(text_color)
        _draw_lines(painter, clip.rect.adjusted(6, 0, -6, 0), list(clip.lines))

    def _paint_playhead(self, painter: QPainter, palette: QPalette) -> None:
        x = self._x_for_ms(self._position_ms)
        color = QColor(palette.color(QPalette.ColorRole.Highlight))
        painter.setPen(color)
        painter.drawLine(x, 0, x, self._content_height())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = event.position().toPoint()
        if point.y() < self._ruler:
            ms = self._ms_at(point.x())
            if ms is not None:
                self.seek_requested.emit(ms)
            return
        clip = self._hit_clip(point)
        if clip is None:
            row = self._row_at(point.y())
            if row is not None:
                track = self._tracks[row]
                self._selected_id = track.id
                self.track_selected.emit(track.id)
                self.track_activated.emit(track.id)
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self.update()
            ms = self._ms_at(point.x())
            if ms is not None:
                self.seek_requested.emit(ms)
            return
        self._drag = clip
        self._drag_origin = event.position().toPoint()
        self._drag_offset0 = clip.offset_ms
        self._drag_row0 = clip.row
        self._drag_axis = ""
        self._dragging = False
        self._selected_id = clip.track_id
        self.track_selected.emit(clip.track_id)
        self.track_activated.emit(clip.track_id)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag is None:
            super().mouseMoveEvent(event)
            return
        delta = event.position().toPoint() - self._drag_origin
        if not self._dragging and abs(delta.x()) < _DRAG_SLOP and abs(delta.y()) < _DRAG_SLOP:
            return
        if not self._dragging:
            self._drag_axis = "x" if abs(delta.x()) >= abs(delta.y()) else "y"
            self._dragging = True
        if self._drag_axis == "x":
            dx_ms = int(delta.x() * self._span_ms() / self._lane_width())
            self._drag.offset_ms = max(0, self._drag_offset0 + dx_ms)
            self._duration_ms = max(
                self._duration_ms, self._drag.offset_ms + self._drag.duration_ms, 1
            )
            self._layout_clips()
        else:
            self._move_to_row(self._drag, self._drag_row0 + round(delta.y() / self._row_h))
        self.update()

    def _move_to_row(self, clip: _ClipVisual, target_row: int) -> None:
        """Reorder the live track list so the drag previews its drop position."""
        target = min(max(target_row, 0), len(self._tracks) - 1)
        if target == clip.row:
            return
        track = next((item for item in self._tracks if item.id == clip.track_id), None)
        if track is None:
            return
        self._tracks.remove(track)
        self._tracks.insert(target, track)
        order = {item.id: index for index, item in enumerate(self._tracks)}
        for visual in self._clips:
            visual.row = order[visual.track_id]
        self._layout_clips()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        del event
        clip = self._drag
        self._drag = None
        if clip is None or not self._dragging:
            return
        self._dragging = False
        if self._drag_axis == "x":
            self.clip_changed.emit(clip.track_id, clip.offset_ms)
        elif clip.row != self._drag_row0:
            self.track_moved.emit(clip.track_id, clip.row)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Delete and not event.modifiers() and self._selected_id:
            self.delete_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            notches = wheel_zoom_notches(event)
            if notches == 0.0:
                event.ignore()
                return
            self._zoom_at(event.position().toPoint().x(), notches)
            event.accept()
            return
        delta_ms = wheel_time_delta_ms(event)
        if delta_ms == 0:
            event.ignore()
            return
        new_pos = min(max(self._position_ms + delta_ms, 0), self._duration_ms)
        if new_pos != self._position_ms:
            self._position_ms = new_pos
            self._scroll_into_view(new_pos)
            self.update()
            self.seek_requested.emit(new_pos)
        event.accept()

    def _zoom_at(self, x: int, notches: float) -> None:
        """Scale the visible span, holding the time under the pointer in place."""
        span = self._span_ms()
        anchor_ms = self._ms_at_clamped(x)
        fraction = min(max((anchor_ms - self._view_start_ms) / span, 0.0), 1.0)
        new_span = round(span / _ZOOM_PER_NOTCH**notches)
        new_span = max(_MIN_SPAN_MS, min(new_span, self._duration_ms))
        if new_span == span:
            return
        self._view_span_ms = 0 if new_span >= self._duration_ms else new_span
        self._view_start_ms = round(anchor_ms - fraction * new_span)
        self._clamp_view()
        self._layout_clips()
        self.update()

    def _scroll_into_view(self, ms: int) -> None:
        span = self._span_ms()
        if ms < self._view_start_ms:
            self._view_start_ms = ms
        elif ms > self._view_start_ms + span:
            self._view_start_ms = ms - span
        else:
            return
        self._clamp_view()
        self._layout_clips()

    def _on_context_menu(self, pos: QPoint) -> None:
        clip = self._hit_clip(pos)
        if clip is None:
            row = self._row_at(pos.y())
            if row is None:
                return
            clip = next((item for item in self._clips if item.row == row), None)
            if clip is None:
                return
        self._selected_id = clip.track_id
        self.track_selected.emit(clip.track_id)
        self.update()
        self.context_menu_requested.emit(self.mapToGlobal(pos), clip.mute)

    def _hit_clip(self, point: QPoint) -> _ClipVisual | None:
        for clip in self._clips:
            if clip.rect.contains(point):
                return clip
        return None

    def _row_at(self, y: int) -> int | None:
        if y < self._ruler:
            return None
        row = (y - self._ruler) // self._row_h
        if 0 <= row < len(self._tracks):
            return row
        return None

    def _ms_at(self, x: int) -> int | None:
        if x < self._gutter:
            return None
        return self._ms_at_clamped(x)

    def _ms_at_clamped(self, x: int) -> int:
        offset = max(0, x - self._gutter) * self._span_ms() / self._lane_width()
        return min(max(self._view_start_ms + int(offset), 0), self._duration_ms)


def clip_label_lines(
    track: Track, duration_ms: int, offset_ms: int, gain_db: float
) -> tuple[str, str]:
    """Two summary lines painted inside a clip, in the order the UI expects."""
    speed = 1.0
    fade_in = 0
    fade_out = 0
    layout = layout_for_channel_count(track.original.channels).name
    for spec in track.operations:
        op = spec.get("op")
        if op == "speed":
            ratio = float(spec.get("ratio", 1.0))
            if ratio > 0:
                speed *= ratio
        elif op == "fade_in":
            fade_in = int(spec.get("duration_ms", 0))
        elif op == "fade_out":
            fade_out = int(spec.get("duration_ms", 0))
        elif op == "channels":
            layout = str(spec.get("target_layout") or layout)
    line_1 = f"{format_clock_ms(duration_ms)}  ×{speed:.2f}  {gain_db:+.1f}dB"
    line_2 = f"<{fade_in}ms  >{fade_out}ms  c={layout}  s={format_clock_ms(offset_ms)}"
    return line_1, line_2


def _draw_lines(painter: QPainter, rect: QRect, lines: list[str]) -> None:
    """Draw stacked lines centred in `rect`, eliding each to the available width."""
    if rect.width() <= 0:
        return
    metrics = painter.fontMetrics()
    line_h = metrics.height()
    visible = [text for text in lines if text]
    while len(visible) > 1 and len(visible) * line_h > rect.height():
        visible.pop()
    if not visible:
        return
    y = rect.top() + (rect.height() - len(visible) * line_h) // 2
    for text in visible:
        painter.drawText(
            QRect(rect.left(), y, rect.width(), line_h),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, rect.width()),
        )
        y += line_h


def _nice_tick(span_ms: int, width_px: int) -> int:
    target = 90 * span_ms / max(1, width_px)
    for step in (1_000, 2_000, 5_000, 10_000, 15_000, 30_000, 60_000, 120_000, 300_000):
        if step >= target:
            return step
    return 600_000
