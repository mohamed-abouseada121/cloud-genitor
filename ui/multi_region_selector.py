"""
ui/multi_region_selector.py
────────────────────────────
A custom widget that looks like a button but opens a searchable 
checklist dialog for selecting multiple cloud regions.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QListWidget, QListWidgetItem,
    QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout, QLabel,
    QWidget
)

class MultiRegionSelector(QPushButton):
    changed = pyqtSignal(list)  # emits list of selected region strings

    def __init__(self, parent=None) -> None:
        super().__init__("Select Regions (All)", parent)
        self._selected_regions: list[str] = []
        self._all_regions: list[str] = []
        self.clicked.connect(self._open_dialog)
        self.setMinimumWidth(200)

    def set_regions(self, regions: list[str]) -> None:
        self._all_regions = sorted(regions)
        # By default, "All" means empty selection implies all, or we select all.
        # Let's say default is all selected.
        self._selected_regions = list(self._all_regions)
        self._update_button_text()

    def get_selected(self) -> list[str]:
        return self._selected_regions

    def _update_button_text(self) -> None:
        if not self._selected_regions:
            self.setText("Select Regions (None)")
        elif len(self._selected_regions) == len(self._all_regions):
            self.setText("Select Regions (All)")
        elif len(self._selected_regions) == 1:
            self.setText(f"Region: {self._selected_regions[0]}")
        else:
            self.setText(f"Regions: {len(self._selected_regions)} selected")

    def _open_dialog(self) -> None:
        dlg = _RegionListDialog(self._all_regions, self._selected_regions, self)
        if dlg.exec():
            self._selected_regions = dlg.selected_regions()
            self._update_button_text()
            self.changed.emit(self._selected_regions)


class _RegionListDialog(QDialog):
    def __init__(self, all_regions: list[str], selected: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Regions")
        self.setMinimumSize(400, 500)
        
        layout = QVBoxLayout(self)
        
        # Search bar
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search regions...")
        self.search.textChanged.connect(self._filter_list)
        layout.addWidget(self.search)
        
        # Select All / None
        btns_row = QHBoxLayout()
        all_btn = QPushButton("Select All")
        none_btn = QPushButton("Select None")
        all_btn.clicked.connect(self._select_all)
        none_btn.clicked.connect(self._select_none)
        btns_row.addWidget(all_btn)
        btns_row.addWidget(none_btn)
        layout.addLayout(btns_row)
        
        # List
        self.list_widget = QListWidget()
        for r in all_regions:
            item = QListWidgetItem(r)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            state = Qt.CheckState.Checked if r in selected else Qt.CheckState.Unchecked
            item.setCheckState(state)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)
        
        # Dialog buttons
        db = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        db.accepted.connect(self.accept)
        db.rejected.connect(self.reject)
        layout.addWidget(db)

    def _filter_list(self, text: str) -> None:
        text = text.lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setHidden(text not in item.text().lower())

    def _select_all(self) -> None:
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.CheckState.Checked)

    def _select_none(self) -> None:
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.CheckState.Unchecked)

    def selected_regions(self) -> list[str]:
        res = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                res.append(item.text())
        return res
