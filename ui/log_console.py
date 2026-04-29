"""
ui/log_console.py
──────────────────
Thread-safe log console widget backed by a QPlainTextEdit.
A custom logging.Handler emits a Qt signal so worker threads can log safely.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget


# ── Qt-bridge logging handler ─────────────────────────────────────────────────

class _QtLogBridge(QObject):
    message = pyqtSignal(str, int)   # text, levelno


class QtLogHandler(logging.Handler):
    """
    Python logging.Handler that forwards records to a Qt signal.
    Must be connected to LogConsole.append_record() in the main thread.
    """

    def __init__(self) -> None:
        super().__init__()
        self._bridge = _QtLogBridge()
        self.message = self._bridge.message   # expose signal directly

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self._bridge.message.emit(msg, record.levelno)


# ── Widget ────────────────────────────────────────────────────────────────────

class LogConsole(QWidget):
    """
    Read-only log console with colour-coded log levels.
    Call log_message(str) from any thread via the Qt signal.
    """

    log_message = pyqtSignal(str)   # generic text signal (from workers)

    # level → colour
    _COLOURS = {
        logging.DEBUG:    QColor("#8e949a"),
        logging.INFO:     QColor("#d4d4d4"),
        logging.WARNING:  QColor("#e5c07b"),
        logging.ERROR:    QColor("#e06c75"),
        logging.CRITICAL: QColor("#c678dd"),
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build_ui()

        # Connect generic text signal (from worker threads)
        self.log_message.connect(self._append_plain)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._edit = QPlainTextEdit()
        self._edit.setReadOnly(True)
        self._edit.setMaximumBlockCount(5000)

        font = QFont("Consolas", 9)
        font.setFixedPitch(True)
        self._edit.setFont(font)

        # Dark background
        self._edit.setStyleSheet(
            "QPlainTextEdit { background: #1e1e1e; color: #d4d4d4; "
            "border: 1px solid #3a3a3a; }"
        )
        layout.addWidget(self._edit)

    # ── public API ────────────────────────────────────────────────────────────

    def connect_handler(self, handler: QtLogHandler) -> None:
        """Wire a QtLogHandler to this console."""
        handler.message.connect(self.append_record)

    def append_record(self, msg: str, levelno: int = logging.INFO) -> None:
        """Append a coloured line. Thread-safe via Qt signal."""
        colour = self._COLOURS.get(levelno, self._COLOURS[logging.INFO])
        cursor = self._edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt = QTextCharFormat()
        fmt.setForeground(colour)
        cursor.insertText(msg + "\n", fmt)
        self._edit.setTextCursor(cursor)
        self._edit.ensureCursorVisible()

    def clear(self) -> None:
        self._edit.clear()

    def _append_plain(self, msg: str) -> None:
        self.append_record(msg, logging.INFO)
