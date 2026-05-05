"""
ui/filter_bar.py
────────────────
Filter toolbar placed above the Resource Tree.
Supports tag-key/value filter, age filter, and minimum cost filter.
All filters are applied client-side on the already-populated tree.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QWidget,
)


class FilterBar(QWidget):
    """
    Emits `filters_changed` whenever any input changes.
    The resource tree connects to this signal to refresh its visible items.
    """

    filters_changed = pyqtSignal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(300)
        self._debounce_timer.timeout.connect(self._emit_filters)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)

        # ── Search filter ─────────────────────────────────────────────────────
        layout.addWidget(QLabel("Search:"))
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Name or ID...")
        self._search_input.setFixedWidth(150)
        self._search_input.textChanged.connect(self._debounce_timer.start)
        layout.addWidget(self._search_input)
        layout.addSpacing(10)

        # ── Tag filter ────────────────────────────────────────────────────────
        layout.addWidget(QLabel("Tag key:"))
        self._tag_key = QLineEdit()
        self._tag_key.setPlaceholderText("e.g. Owner")
        self._tag_key.setFixedWidth(100)
        layout.addWidget(self._tag_key)

        layout.addWidget(QLabel("="))
        self._tag_value = QLineEdit()
        self._tag_value.setPlaceholderText("value (empty = missing)")
        self._tag_value.setFixedWidth(120)
        layout.addWidget(self._tag_value)

        # ── Age filter ────────────────────────────────────────────────────────
        layout.addWidget(QLabel("  Age >"))
        self._age_spin = QSpinBox()
        self._age_spin.setRange(0, 3650)
        self._age_spin.setValue(0)
        self._age_spin.setSuffix(" days")
        self._age_spin.setFixedWidth(90)
        layout.addWidget(self._age_spin)

        # ── Cost filter ───────────────────────────────────────────────────────
        layout.addWidget(QLabel("  Cost >"))
        self._cost_spin = QSpinBox()
        self._cost_spin.setRange(0, 100_000)
        self._cost_spin.setValue(0)
        self._cost_spin.setPrefix("$")
        self._cost_spin.setSuffix("/mo")
        self._cost_spin.setFixedWidth(100)
        layout.addWidget(self._cost_spin)

        # ── Apply / Clear ─────────────────────────────────────────────────────
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setFixedWidth(60)
        self._apply_btn.clicked.connect(self._emit_filters)
        layout.addWidget(self._apply_btn)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setFixedWidth(60)
        self._clear_btn.clicked.connect(self._clear_filters)
        layout.addWidget(self._clear_btn)

        layout.addStretch()

        # Auto-apply on Enter in text fields
        self._tag_key.returnPressed.connect(self._emit_filters)
        self._tag_value.returnPressed.connect(self._emit_filters)

    # ── signals ───────────────────────────────────────────────────────────────

    def _emit_filters(self) -> None:
        self.filters_changed.emit(self.current_filters())

    def _clear_filters(self) -> None:
        self._tag_key.clear()
        self._tag_value.clear()
        self._age_spin.setValue(0)
        self._cost_spin.setValue(0)
        self._emit_filters()

    # ── public API ────────────────────────────────────────────────────────────

    def current_filters(self) -> dict:
        return {
            "search":       self._search_input.text().strip().lower(),
            "tag_key":      self._tag_key.text().strip(),
            "tag_value":    self._tag_value.text().strip(),
            "min_age_days": self._age_spin.value(),
            "min_cost":     float(self._cost_spin.value()),
        }
