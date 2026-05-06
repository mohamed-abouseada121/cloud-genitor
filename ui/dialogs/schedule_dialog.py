"""
ui/dialogs/schedule_dialog.py
──────────────────────────────
Dialog to configure automated periodic scans.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QVBoxLayout, QHBoxLayout,
    QLabel, QSpinBox, QComboBox, QListWidget, QPushButton
)

class ScheduleDialog(QDialog):
    def __init__(self, provider: str, regions: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Schedule Automated Scan")
        self.setMinimumWidth(400)
        
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel(f"<b>Provider:</b> {provider}"))
        
        layout.addWidget(QLabel("<b>Regions to scan:</b>"))
        self.region_list = QListWidget()
        for r in regions:
            self.region_list.addItem(r)
        layout.addWidget(self.region_list)
        
        h_row = QHBoxLayout()
        h_row.addWidget(QLabel("Scan every:"))
        self.interval = QSpinBox()
        self.interval.setRange(1, 168) # Up to 1 week
        self.interval.setValue(24)
        h_row.addWidget(self.interval)
        h_row.addWidget(QLabel("hours"))
        layout.addLayout(h_row)
        
        layout.addWidget(QLabel("<small>Note: Application must be running for scheduled scans to trigger.</small>"))
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_data(self) -> dict:
        return {
            "interval": self.interval.value()
        }
