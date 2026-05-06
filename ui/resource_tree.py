"""
ui/resource_tree.py
───────────────────
QTreeWidget subclass for displaying cloud resources with:
  - Tri-state parent checkboxes (auto-checks/unchecks children)
  - Mode A: flat list (comprehensive scan)
  - Mode B: hierarchical tree (VPC/RG → children)
  - Double-click → raw JSON preview dialog
  - Tooltip with resource summary
"""

from __future__ import annotations

import json
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLabel,
    QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from models.resource import CloudResource, DeletionStatus

# Columns
COL_NAME   = 0
COL_TYPE   = 1
COL_ID     = 2
COL_REGION = 3
COL_STATUS = 4
COL_COST   = 5
COL_LAYER  = 6

_HEADERS = ["Resource Name", "Type", "ID", "Region", "Status", "Est. Cost/mo", "Layer"]

# Deletion status colours
_STATUS_COLOURS = {
    DeletionStatus.SUCCESS:  QColor("#4caf50"),
    DeletionStatus.FAILED:   QColor("#e06c75"),
    DeletionStatus.BLOCKED:  QColor("#e5c07b"),
    DeletionStatus.SKIPPED:  QColor("#8e949a"),
    DeletionStatus.PENDING:  QColor("#61afef"),
}


class ResourceTree(QTreeWidget):
    """
    Dual-mode resource tree with checkbox propagation.
    """

    selection_changed = pyqtSignal(int)   # emits count of checked items

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._blocking_signals = False
        self._id_to_item: dict[str, QTreeWidgetItem] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        self.setColumnCount(len(_HEADERS))
        self.setHeaderLabels(_HEADERS)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)

        header = self.header()
        header.setSectionResizeMode(COL_NAME,   QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_TYPE,   QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_ID,     QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_REGION, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_COST,   QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_LAYER,  QHeaderView.ResizeMode.ResizeToContents)

        self.itemChanged.connect(self._on_item_changed)
        self.itemDoubleClicked.connect(self._on_double_click)

    # ── public API ────────────────────────────────────────────────────────────

    def populate_flat(self, resources: list[CloudResource]) -> None:
        """Mode A — flat list."""
        self.clear()
        self._id_to_item.clear()
        self.setUpdatesEnabled(False)
        self._blocking_signals = True
        try:
            for r in resources:
                item = self._make_item(r)
                self.addTopLevelItem(item)
                self._id_to_item[r.resource_id] = item
        finally:
            self._blocking_signals = False
            self.setUpdatesEnabled(True)

    def populate_tree(self, resources: list[CloudResource]) -> None:
        """Mode B — hierarchical tree grouped by parent_id."""
        self.clear()
        self._id_to_item.clear()
        self.setUpdatesEnabled(False)
        self._blocking_signals = True

        try:
            # Filter duplicates by ID just in case
            unique_resources = {}
            for r in resources:
                if r.resource_id not in unique_resources:
                    unique_resources[r.resource_id] = r
            
            res_list = list(unique_resources.values())

            # First pass: create all items
            for r in res_list:
                item = self._make_item(r)
                self._id_to_item[r.resource_id] = item

            # Second pass: attach to parents or root
            for r in res_list:
                item = self._id_to_item[r.resource_id]
                if r.parent_id and r.parent_id in self._id_to_item:
                    parent_item = self._id_to_item[r.parent_id]
                    if parent_item != item: # Prevent self-parenting
                        parent_item.addChild(item)
                    else:
                        self.addTopLevelItem(item)
                else:
                    self.addTopLevelItem(item)

            # Only expand all if total count is reasonable
            if len(res_list) < 500:
                self.expandAll()
            else:
                # Just expand top level
                for i in range(self.topLevelItemCount()):
                    self.topLevelItem(i).setExpanded(True)

        finally:
            self._blocking_signals = False
            self.setUpdatesEnabled(True)

    def select_all(self) -> None:
        self.setUpdatesEnabled(False)
        self._blocking_signals = True
        try:
            for item in self._id_to_item.values():
                item.setCheckState(COL_NAME, Qt.CheckState.Checked)
        finally:
            self._blocking_signals = False
            self.setUpdatesEnabled(True)
        self.selection_changed.emit(len(self._id_to_item))

    def select_none(self) -> None:
        self.setUpdatesEnabled(False)
        self._blocking_signals = True
        try:
            for item in self._id_to_item.values():
                item.setCheckState(COL_NAME, Qt.CheckState.Unchecked)
        finally:
            self._blocking_signals = False
            self.setUpdatesEnabled(True)
        self.selection_changed.emit(0)

    def get_checked_resources(self) -> list[CloudResource]:
        """Return CloudResource objects for all checked (leaf or parent) items."""
        result: list[CloudResource] = []
        for resource_id, item in self._id_to_item.items():
            if item.checkState(COL_NAME) in (
                Qt.CheckState.Checked, Qt.CheckState.PartiallyChecked
            ):
                resource = item.data(COL_NAME, Qt.ItemDataRole.UserRole)
                if resource is not None:
                    result.append(resource)
        return result

    def apply_filters(self, filters: dict) -> None:
        """Show/hide items based on filter criteria."""
        search    = filters.get("search", "")
        tag_key   = filters.get("tag_key", "")
        tag_value = filters.get("tag_value", "")
        min_age   = filters.get("min_age_days", 0)
        min_cost  = filters.get("min_cost", 0.0)

        def _matches(r: CloudResource) -> bool:
            if search:
                if search not in r.display_name.lower() and search not in r.resource_id.lower():
                    return False
            if min_age > 0 and 0 <= r.age_days < min_age:
                return False
            if min_cost > 0 and r.estimated_cost < min_cost:
                return False
            if tag_key:
                v = r.tags.get(tag_key)
                if tag_value:
                    if v != tag_value:
                        return False
                else:
                    # filter: tag key is missing
                    if v is not None:
                        return False
            return True

        self.setUpdatesEnabled(False)
        try:
            for resource_id, item in self._id_to_item.items():
                resource = item.data(COL_NAME, Qt.ItemDataRole.UserRole)
                if resource:
                    item.setHidden(not _matches(resource))
        finally:
            self.setUpdatesEnabled(True)

    def update_deletion_status(self, resource_id: str, status: DeletionStatus) -> None:
        item = self._id_to_item.get(resource_id)
        if item:
            colour = _STATUS_COLOURS.get(status, QColor("#d4d4d4"))
            for col in range(self.columnCount()):
                item.setForeground(col, colour)
            item.setText(COL_STATUS, status.value)

    def total_estimated_savings(self) -> float:
        total = 0.0
        for resource_id, item in self._id_to_item.items():
            if item.checkState(COL_NAME) == Qt.CheckState.Checked:
                r = item.data(COL_NAME, Qt.ItemDataRole.UserRole)
                if r:
                    total += r.estimated_cost
        return total

    # ── item construction ─────────────────────────────────────────────────────

    def _make_item(self, resource: CloudResource) -> QTreeWidgetItem:
        item = QTreeWidgetItem([
            resource.display_name,
            resource.resource_type.value,
            resource.resource_id,
            resource.region,
            resource.status,
            resource.cost_label,
            str(resource.deletion_layer),
        ])
        item.setCheckState(COL_NAME, Qt.CheckState.Unchecked)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                      | Qt.ItemFlag.ItemIsAutoTristate)
        item.setData(COL_NAME, Qt.ItemDataRole.UserRole, resource)
        item.setToolTip(COL_NAME,
            f"<b>{resource.display_name}</b><br>"
            f"ID: {resource.resource_id}<br>"
            f"Region: {resource.region}<br>"
            f"Status: {resource.status}<br>"
            f"Age: {resource.age_days} days<br>"
            f"Tags: {resource.tags_summary}"
        )
        return item

    # ── checkbox propagation ──────────────────────────────────────────────────

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._blocking_signals or column != COL_NAME:
            return
        self._blocking_signals = True
        try:
            state = item.checkState(COL_NAME)
            if state != Qt.CheckState.PartiallyChecked:
                self._set_children_check(item, state)
            self._update_parent_check(item.parent())
        finally:
            self._blocking_signals = False
        checked = sum(
            1 for it in self._id_to_item.values()
            if it.checkState(COL_NAME) == Qt.CheckState.Checked
        )
        self.selection_changed.emit(checked)

    def _set_children_check(self, item: QTreeWidgetItem,
                             state: Qt.CheckState) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(COL_NAME, state)
            self._set_children_check(child, state)

    def _update_parent_check(self, parent: Optional[QTreeWidgetItem]) -> None:
        if parent is None:
            return
        checked   = sum(1 for i in range(parent.childCount())
                        if parent.child(i).checkState(COL_NAME) == Qt.CheckState.Checked)
        unchecked = sum(1 for i in range(parent.childCount())
                        if parent.child(i).checkState(COL_NAME) == Qt.CheckState.Unchecked)
        total     = parent.childCount()

        if checked == total:
            parent.setCheckState(COL_NAME, Qt.CheckState.Checked)
        elif unchecked == total:
            parent.setCheckState(COL_NAME, Qt.CheckState.Unchecked)
        else:
            parent.setCheckState(COL_NAME, Qt.CheckState.PartiallyChecked)

        self._update_parent_check(parent.parent())

    # ── double-click preview ──────────────────────────────────────────────────

    def _on_double_click(self, item: QTreeWidgetItem, column: int) -> None:
        resource: Optional[CloudResource] = item.data(COL_NAME, Qt.ItemDataRole.UserRole)
        if resource is None:
            return
        dlg = _JsonPreviewDialog(resource, self)
        dlg.exec()


class _JsonPreviewDialog(QDialog):
    def __init__(self, resource: CloudResource, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Resource Preview — {resource.display_name}")
        self.setMinimumSize(700, 500)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"<b>{resource.display_name}</b>  "
            f"[{resource.resource_type.value} / {resource.provider.value} / {resource.region}]"
        ))

        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setPlainText(json.dumps(resource.metadata, indent=2, default=str))
        self.editor.setFont(QFont("Consolas", 9))
        layout.addWidget(self.editor)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy_btn = btns.addButton("📋 Copy JSON", QDialogButtonBox.ButtonRole.ActionRole)
        copy_btn.clicked.connect(self._copy_json)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _copy_json(self) -> None:
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.editor.toPlainText())
        self.setWindowTitle("JSON Copied! ✅")
