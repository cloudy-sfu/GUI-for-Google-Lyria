"""Run synchronous core work off the Qt GUI thread."""
from collections.abc import Callable

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal


class WorkerSignals(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)


class FnWorker(QRunnable):
    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__()
        self._fn = fn
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:
            self.signals.error.emit(str(exc))
        else:
            self.signals.finished.emit(result)
