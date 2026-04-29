"""
ui/sidebar.py
─────────────
Sidebar QListWidget for switching between cloud providers.
Each entry shows a provider icon and name.
Supports multi-account sub-items (expandable per provider).
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

_PROVIDERS = [
    ("AWS",     "assets/icons/aws.png"),
    ("Azure",   "assets/icons/azure.png"),
    ("Alibaba", "assets/icons/alibaba.png"),
    ("GCP",     "assets/icons/gcp.png"),
    ("Oracle",  "assets/icons/oracle.png"),
]


class Sidebar(QWidget):
    """
    Emits `provider_selected(provider_name, account_name)` when the user clicks
    a provider or a registered account entry.
    """

    provider_selected = pyqtSignal(str, str)  # provider_name, account_name
    badge_counts: dict[str, int] = {}         # provider → new-orphan count

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("☁  Cloud Janitor")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = QFont()
        font.setBold(True)
        font.setPointSize(11)
        title.setFont(font)
        title.setStyleSheet(
            "QLabel { background: #2d2d2d; color: #d4d4d4; "
            "padding: 12px 0; border-bottom: 1px solid #444; }"
        )
        layout.addWidget(title)

        self._list = QListWidget()
        self._list.setStyleSheet("""
            QListWidget {
                background: #252526;
                color: #cccccc;
                border: none;
                font-size: 13px;
            }
            QListWidget::item {
                padding: 12px 16px;
                border-bottom: 1px solid #333;
            }
            QListWidget::item:selected {
                background: #094771;
                color: #ffffff;
            }
            QListWidget::item:hover {
                background: #2a2d2e;
            }
        """)

        for provider_name, icon_path in _PROVIDERS:
            self._add_provider_item(provider_name, icon_path)

        self._list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list)
        layout.addStretch()

    def _add_provider_item(self, name: str, icon_path: str) -> None:
        from PyQt6.QtGui import QIcon
        import os
        item = QListWidgetItem(name)
        if os.path.isfile(icon_path):
            item.setIcon(QIcon(icon_path))
        item.setData(Qt.ItemDataRole.UserRole, {"provider": name, "account": "default"})
        self._list.addItem(item)

    # ── public API ────────────────────────────────────────────────────────────

    def add_account_item(self, provider_name: str, account_name: str) -> None:
        """Add a sub-item under a provider (indented) for a registered account."""
        item = QListWidgetItem(f"    └ {account_name}")
        item.setData(Qt.ItemDataRole.UserRole,
                     {"provider": provider_name, "account": account_name})
        # Insert after the provider's last item
        insert_row = self._list.count()
        for i in range(self._list.count()):
            it = self._list.item(i)
            d  = it.data(Qt.ItemDataRole.UserRole)
            if d and d.get("provider") == provider_name:
                insert_row = i + 1
        self._list.insertItem(insert_row, item)

    def set_badge(self, provider_name: str, count: int) -> None:
        """Show an orphan count badge next to a provider entry."""
        for i in range(self._list.count()):
            item = self._list.item(i)
            d    = item.data(Qt.ItemDataRole.UserRole)
            if d and d.get("provider") == provider_name and d.get("account") == "default":
                base = provider_name
                item.setText(f"{base}  🔴 {count}" if count > 0 else base)
                break

    def select_provider(self, provider_name: str) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            d    = item.data(Qt.ItemDataRole.UserRole)
            if d and d.get("provider") == provider_name and d.get("account") == "default":
                self._list.setCurrentRow(i)
                break

    # ── slot ──────────────────────────────────────────────────────────────────

    def _on_selection_changed(self, current, previous) -> None:
        if current is None:
            return
        d = current.data(Qt.ItemDataRole.UserRole)
        if d:
            self.provider_selected.emit(d["provider"], d.get("account", "default"))
