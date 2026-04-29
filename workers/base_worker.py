"""
workers/base_worker.py
──────────────────────
Abstract QThread-based worker with a standard signal set.
All background tasks subclass this to keep the UI responsive.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from models.resource import CloudResource


class BaseWorker(QThread):
    """
    Base class for all background QThread workers.

    Signals
    -------
    progress(int)              0-100 percent progress
    log_message(str)           log text to display in the console
    resource_found(object)     a CloudResource discovered during scan
    finished(object)           final result (type varies per subclass)
    error(str)                  human-readable error description
    """

    progress      = pyqtSignal(int)
    log_message   = pyqtSignal(str)
    resource_found = pyqtSignal(object)   # CloudResource
    finished      = pyqtSignal(object)   # varies per subclass
    error         = pyqtSignal(str)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._cancelled: bool = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def cancel(self) -> None:
        """Request a graceful cancellation."""
        self._cancelled = True

    # ── helpers ───────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        self.log_message.emit(msg)

    def _check_cancelled(self) -> bool:
        return self._cancelled
