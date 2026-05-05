"""
ui/sidebar.py
─────────────
Sidebar QListWidget for switching between cloud providers.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QIcon
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
    provider_selected = pyqtSignal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("☁  Cloud Janitor")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setObjectName("sidebarTitle")
        layout.addWidget(title)

        self._list = QListWidget()
        # Styling is now handled by the main window QSS
        for provider_name, icon_path in _PROVIDERS:
            item = QListWidgetItem(provider_name)
            import os
            if os.path.isfile(icon_path):
                item.setIcon(QIcon(icon_path))
            item.setData(Qt.ItemDataRole.UserRole, {"provider": provider_name, "account": "default"})
            self._list.addItem(item)

        self._list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list)
        layout.addStretch()

    def select_provider(self, provider_name: str) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            d = item.data(Qt.ItemDataRole.UserRole)
            if d and d.get("provider") == provider_name:
                self._list.setCurrentRow(i)
                break

    def _on_selection_changed(self, current, previous) -> None:
        if current:
            d = current.data(Qt.ItemDataRole.UserRole)
            self.provider_selected.emit(d["provider"], d.get("account", "default"))
