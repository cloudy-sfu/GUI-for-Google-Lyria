"""Font stylesheet, screen-relative window sizing, height helpers, and label formatting."""
from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QFontInfo, QFontMetrics, QIcon, QPainter, QPalette, QPixmap, QWheelEvent
from PyQt6.QtWidgets import QAbstractItemView, QApplication, QHeaderView, QStyle, QTableView, QWidget

# Qt reports 120 eighth-degrees per mouse-wheel notch. Windows then
# multiplies by the system "lines per notch" (QApplication.wheelScrollLines).
_WHEEL_NOTCH = 120
_MS_PER_SCROLL_LINE = 1000
_PIXELS_PER_NOTCH = 50


def fit_interactive_columns(table: QTableView) -> None:
    """Fit each column to contents, then leave widths user-adjustable.

    Used by the material table and by tables that should match it.
    """
    table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    for col in range(table.columnCount()):
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
    header.resizeSections(QHeaderView.ResizeMode.ResizeToContents)
    for col in range(table.columnCount()):
        width = header.sectionSize(col)
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        table.setColumnWidth(col, width)


def size_main_window(window: QWidget) -> None:
    screen = window.screen().availableGeometry()
    width = round(min(0.75 * screen.width(), 1.6 * screen.height()))
    window.resize(QSize(width, round(width / 1.6)))


def size_chat_window(window: QWidget) -> None:
    screen = window.screen().availableGeometry()
    width = round(min(0.62 * screen.width(), 1.35 * screen.height()))
    height = round(min(0.78 * screen.height(), width / 1.15))
    window.resize(QSize(width, height))


def em_px(widget: QWidget, ems: float) -> int:
    """Pixels for a CSS-style ``em`` count, tied to the widget font size.

    Qt stylesheets define 1em as the font pixel size; ``QFontInfo.pixelSize``
    matches that even when the font is specified in points.
    """
    return max(1, round(QFontInfo(widget.font()).pixelSize() * ems))


def icon_size(widget: QWidget, scale: float = 1.25) -> QSize:
    """Square icon edge derived from the widget font, so icons track the font size."""
    edge = round(QFontMetrics(widget.font()).height() * scale)
    return QSize(edge, edge)


def themed_standard_icon(
    widget: QWidget,
    pixmap: QStyle.StandardPixmap,
    size: QSize,
    role: QPalette.ColorRole = QPalette.ColorRole.ButtonText,
) -> QIcon:
    """A standard icon recolored to a palette role.

    Qt ships these icons as fixed dark artwork, so they vanish against a dark
    theme; repainting the glyph keeps them legible whatever the palette is.
    """
    source = widget.style().standardIcon(pixmap).pixmap(size, widget.devicePixelRatioF())
    if source.isNull():
        return QIcon()
    tinted = QPixmap(source.size())
    tinted.setDevicePixelRatio(source.devicePixelRatio())
    tinted.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tinted)
    painter.drawPixmap(0, 0, source)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(tinted.rect(), widget.palette().color(role))
    painter.end()
    return QIcon(tinted)


def height_for_lines(widget: QWidget, n_lines: int) -> int:
    line_height = QFontMetrics(widget.font()).height()
    frame_width = widget.frameWidth() if hasattr(widget, "frameWidth") else 0
    screen = QApplication.primaryScreen()
    zoom = screen.devicePixelRatio() if screen is not None else 1.0
    return int((line_height * n_lines + frame_width * 2) * zoom)


def wheel_time_delta_ms(event: QWheelEvent) -> int:
    """Playhead delta for a wheel event: one document scroll line = 1 second.

    Scroll down (later content) moves time forward; scroll up moves it back.
    """
    delta = event.angleDelta().y()
    if delta != 0:
        lines = delta / _WHEEL_NOTCH * max(1, QApplication.wheelScrollLines())
    else:
        pixel = event.pixelDelta().y()
        if pixel == 0:
            return 0
        lines = pixel / (QFontMetrics(QApplication.font()).height() or 1)
    return -round(lines * _MS_PER_SCROLL_LINE)


def wheel_zoom_notches(event: QWheelEvent) -> float:
    """Zoom notches for a wheel event: positive zooms in, negative zooms out.

    Unlike scrolling, zooming ignores the system lines-per-notch setting so one
    wheel notch is always one zoom step.
    """
    delta = event.angleDelta().y()
    if delta != 0:
        return delta / _WHEEL_NOTCH
    pixel = event.pixelDelta().y()
    if pixel == 0:
        return 0.0
    return pixel / _PIXELS_PER_NOTCH


def format_clock(ms: float | int) -> str:
    """Clock-style duration formatting for UI labels."""
    minutes, seconds = divmod(max(0, int(ms)) // 1000, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_clock_ms(ms: float | int) -> str:
    """Clock formatting that keeps milliseconds, e.g. ``01:23.456``."""
    total = max(0, round(ms))
    millis = total % 1000
    minutes, seconds = divmod(total // 1000, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def parse_clock_ms(text: str) -> int | None:
    """Inverse of :func:`format_clock_ms`; also accepts a bare millisecond count."""
    parts = text.strip().split(":")
    if not text.strip() or len(parts) > 3:
        return None
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    if any(number < 0 for number in numbers):
        return None
    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return round(total * (1000.0 if len(parts) > 1 else 1.0))
