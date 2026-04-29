"""
ui/dialogs/confirm_delete.py
─────────────────────────────
Safety confirmation dialog before executing deletions.
Shows resource breakdown, estimated savings, and requires "DELETE" confirmation for live runs.
"""

from __future__ import annotations

from collections import Counter

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QTextEdit, QVBoxLayout,
)

from models.resource import CloudResource


class ConfirmDeleteDialog(QDialog):
    """
    Modal dialog that must be accepted before deletion proceeds.

    In dry-run mode  : Accept button is always enabled.
    In live mode     : The user must type "DELETE" in the input box to unlock Accept.
    """

    def __init__(self, resources: list[CloudResource], dry_run: bool,
                 parent=None) -> None:
        super().__init__(parent)
        self._resources = resources
        self._dry_run   = dry_run
        self.setWindowTitle("Confirm Deletion")
        self.setMinimumWidth(500)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ── DRY RUN badge ─────────────────────────────────────────────────────
        if self._dry_run:
            badge = QLabel(" 🟡  DRY RUN MODE — no resources will actually be deleted ")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet(
                "QLabel { background: #3a3000; color: #e5c07b; "
                "border: 1px solid #e5c07b; border-radius: 4px; padding: 6px; }"
            )
            layout.addWidget(badge)
        else:
            badge = QLabel(" 🔴  LIVE MODE — this will permanently delete cloud resources ")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet(
                "QLabel { background: #3a0000; color: #e06c75; "
                "border: 1px solid #e06c75; border-radius: 4px; padding: 6px; }"
            )
            layout.addWidget(badge)

        # ── Summary ───────────────────────────────────────────────────────────
        layout.addWidget(QLabel(f"<b>Resources selected for deletion: {len(self._resources)}</b>"))

        # Breakdown by type
        type_counts = Counter(r.resource_type.value for r in self._resources)
        provider_counts = Counter(r.provider.value for r in self._resources)
        total_cost = sum(r.estimated_cost for r in self._resources)

        breakdown = QTextEdit()
        breakdown.setReadOnly(True)
        breakdown.setMaximumHeight(180)
        lines = ["<b>By type:</b>"]
        for rtype, count in sorted(type_counts.items()):
            lines.append(f"  • {rtype}: {count}")
        lines.append("<br><b>By provider:</b>")
        for prov, count in sorted(provider_counts.items()):
            lines.append(f"  • {prov}: {count}")
        if total_cost > 0:
            lines.append(f"<br><b>Estimated monthly savings: ${total_cost:,.2f}</b>")
        breakdown.setHtml("<br>".join(lines))
        layout.addWidget(breakdown)

        # ── Confirmation input (live mode only) ───────────────────────────────
        self._confirm_input: QLineEdit | None = None
        if not self._dry_run:
            row = QHBoxLayout()
            row.addWidget(QLabel('Type <b style="color:#e06c75">DELETE</b> to confirm:'))
            self._confirm_input = QLineEdit()
            self._confirm_input.setPlaceholderText("DELETE")
            self._confirm_input.textChanged.connect(self._on_text_changed)
            row.addWidget(self._confirm_input)
            layout.addLayout(row)

        # ── Buttons ───────────────────────────────────────────────────────────
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("Delete" if not self._dry_run else "Simulate")

        if not self._dry_run:
            ok_btn.setEnabled(False)   # locked until "DELETE" is typed

        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def _on_text_changed(self, text: str) -> None:
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setEnabled(text.strip() == "DELETE")
