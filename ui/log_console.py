"""
ui/log_console.py
──────────────────
Thread-safe log console widget.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget


class _QtLogBridge(QObject):
    message = pyqtSignal(str, int)


class QtLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self._bridge = _QtLogBridge()
        self.message = self._bridge.message

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self._bridge.message.emit(msg, record.levelno)


class LogConsole(QWidget):
    log_message = pyqtSignal(str)

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
        self.log_message.connect(self._append_plain)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._edit = QPlainTextEdit()
        self._edit.setReadOnly(True)
        self._edit.setMaximumBlockCount(2000)
        self._edit.setFont(QFont("Consolas", 10))
        # Styles handled centrally
        layout.addWidget(self._edit)

    def connect_handler(self, handler: QtLogHandler) -> None:
        handler.message.connect(self.append_record)

    def append_record(self, msg: str, levelno: int = logging.INFO) -> None:
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
