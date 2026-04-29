"""
ui/main_window.py
──────────────────
Main application window.
Layout: sidebar | [toolbar + resource tree + filter bar] | log console (bottom)
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QSplitter, QStatusBar, QToolBar, QVBoxLayout, QWidget,
)

from config.scheduler import ScanScheduler
from config.settings import Settings
from db.state_manager import StateManager
from models.resource import CloudResource, ProviderName
from providers.account_manager import AccountManager
from providers.aws_manager import AWSManager
from providers.azure_manager import AzureManager
from providers.alibaba_manager import AlibabaManager
from providers.gcp_manager import GCPManager
from providers.oracle_manager import OracleManager
from ui.dialogs.confirm_delete import ConfirmDeleteDialog
from ui.dialogs.credentials_dialog import CredentialsDialog
from ui.filter_bar import FilterBar
from ui.log_console import LogConsole, QtLogHandler
from ui.resource_tree import ResourceTree
from ui.sidebar import Sidebar
from utils.credentials import (
    AWSCredentials, AzureCredentials, AlibabaCredentials,
    GCPCredentials, OracleCredentials,
)
from utils.report_exporter import export_csv, export_pdf
from workers.delete_worker import DeleteWorker
from workers.scan_worker import ScanWorker
from workers.snapshot_worker import SnapshotWorker

log = logging.getLogger(__name__)

_PROVIDER_CLASSES = {
    "AWS":     AWSManager,
    "Azure":   AzureManager,
    "Alibaba": AlibabaManager,
    "GCP":     GCPManager,
    "Oracle":  OracleManager,
}


class MainWindow(QMainWindow):

    def __init__(self, settings: Settings, state_manager: StateManager) -> None:
        super().__init__()
        self._settings       = settings
        self._state_manager  = state_manager
        self._account_manager = AccountManager.from_settings_dict(
            settings.get("accounts", {})
        )
        self._active_provider_name: str         = ""
        self._active_provider: Optional[object] = None
        self._scan_worker:   Optional[ScanWorker]   = None
        self._delete_worker: Optional[DeleteWorker] = None
        self._all_resources: list[CloudResource]    = []

        self._build_ui()
        self._setup_shortcuts()
        self._setup_logging()
        self._setup_scheduler()
        self._restore_geometry()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setWindowTitle("Cloud Resource Janitor")
        self.resize(1400, 850)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Toolbar ───────────────────────────────────────────────────────────
        self._toolbar = self._build_toolbar()
        self.addToolBar(self._toolbar)

        # ── Body splitter (sidebar | content) ────────────────────────────────
        body_splitter = QSplitter(Qt.Orientation.Horizontal)

        self._sidebar = Sidebar()
        self._sidebar.setFixedWidth(200)
        self._sidebar.provider_selected.connect(self._on_provider_selected)
        body_splitter.addWidget(self._sidebar)

        # Right panel: filter + tree + log
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 4, 4, 4)
        right_layout.setSpacing(4)

        self._filter_bar = FilterBar()
        self._filter_bar.filters_changed.connect(self._on_filters_changed)
        right_layout.addWidget(self._filter_bar)

        # Savings banner
        self._savings_label = QLabel("")
        self._savings_label.setStyleSheet("color: #4caf50; font-weight: bold; padding: 2px 4px;")
        right_layout.addWidget(self._savings_label)

        self._resource_tree = ResourceTree()
        self._resource_tree.selection_changed.connect(self._on_tree_selection_changed)
        right_layout.addWidget(self._resource_tree, stretch=3)

        # Log console
        log_box = QGroupBox("Console Log")
        log_box.setMaximumHeight(220)
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(2, 2, 2, 2)
        self._log_console = LogConsole()
        log_layout.addWidget(self._log_console)
        right_layout.addWidget(log_box)

        body_splitter.addWidget(right)
        body_splitter.setStretchFactor(1, 1)
        main_layout.addWidget(body_splitter)

        # ── Status bar ────────────────────────────────────────────────────────
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._progress = QProgressBar()
        self._progress.setFixedWidth(200)
        self._progress.setVisible(False)
        self._status_bar.addPermanentWidget(self._progress)
        self._status_label = QLabel("Ready")
        self._status_bar.addWidget(self._status_label)

    def _build_toolbar(self) -> QToolBar:
        bar = QToolBar("Main Toolbar")
        bar.setMovable(False)
        bar.setStyleSheet("QToolBar { spacing: 8px; padding: 4px; }")

        # Provider label (dynamic)
        self._provider_label = QLabel("Provider: —")
        bar.addWidget(self._provider_label)
        bar.addSeparator()

        # Region selector
        bar.addWidget(QLabel("Region:"))
        self._region_combo = QComboBox()
        self._region_combo.setMinimumWidth(180)
        self._region_combo.addItem("All Regions")
        bar.addWidget(self._region_combo)
        bar.addSeparator()

        # Scan mode
        bar.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["Comprehensive (Orphans)", "Hierarchical (Full Tree)"])
        bar.addWidget(self._mode_combo)
        bar.addSeparator()

        # Dry run toggle
        self._dry_run_cb = QCheckBox("Dry Run")
        self._dry_run_cb.setChecked(self._settings.get("dry_run", True))
        self._dry_run_cb.setToolTip("Simulate deletions without executing real API calls")
        bar.addWidget(self._dry_run_cb)
        bar.addSeparator()

        # Buttons
        self._scan_btn = QPushButton("🔍 Scan")
        self._scan_btn.setShortcut("Ctrl+S")
        self._scan_btn.clicked.connect(self._on_scan)
        bar.addWidget(self._scan_btn)

        self._delete_btn = QPushButton("🗑 Delete Selected")
        self._delete_btn.setShortcut("Ctrl+D")
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._on_delete)
        bar.addWidget(self._delete_btn)

        self._export_btn = QPushButton("📄 Export")
        self._export_btn.clicked.connect(self._on_export)
        bar.addWidget(self._export_btn)

        self._retry_btn = QPushButton("🔄 Retry Failed")
        self._retry_btn.clicked.connect(self._on_retry_failed)
        bar.addWidget(self._retry_btn)

        self._creds_btn = QPushButton("🔑 Credentials")
        self._creds_btn.setToolTip("Enter / edit cloud provider credentials")
        self._creds_btn.clicked.connect(self._on_credentials)
        bar.addWidget(self._creds_btn)

        bar.addSeparator()

        # Theme toggle
        self._theme_btn = QPushButton("🌙")
        self._theme_btn.setToolTip("Toggle Dark/Light theme")
        self._theme_btn.setFixedWidth(32)
        self._theme_btn.clicked.connect(self._toggle_theme)
        bar.addWidget(self._theme_btn)

        return bar

    # ── shortcuts ─────────────────────────────────────────────────────────────

    # ── credentials dialog ──────────────────────────────────────────────────

    def _on_credentials(self) -> None:
        dlg = CredentialsDialog(self._settings, parent=self)
        if dlg.exec():
            # Re-connect current provider with new creds
            if self._active_provider_name:
                self._connect_provider(self._active_provider_name, "default")
                self._refresh_regions()

    # ── shortcuts ─────────────────────────────────────────────────────────────

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(self._log_console.clear)
        QShortcut(QKeySequence("F5"),     self).activated.connect(self._refresh_regions)
        QShortcut(QKeySequence("Ctrl+E"), self).activated.connect(self._on_export)
        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(
            lambda: self._resource_tree.clearSelection()
        )

    # ── logging ───────────────────────────────────────────────────────────────

    def _setup_logging(self) -> None:
        handler = QtLogHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                               datefmt="%H:%M:%S"))
        self._log_console.connect_handler(handler)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.DEBUG)

    # ── scheduler ─────────────────────────────────────────────────────────────

    def _setup_scheduler(self) -> None:
        sched_cfg = self._settings.schedule
        if not sched_cfg.get("enabled"):
            return
        self._scheduler = ScanScheduler(self)
        self._scheduler.configure(
            provider=self._settings.get("last_provider", "AWS"),
            region=self._settings.get("last_region", "All Regions"),
            interval_minutes=sched_cfg.get("interval_minutes", 1440),
        )
        self._scheduler.scan_due.connect(self._on_scheduled_scan)
        self._scheduler.start()

    # ── event handlers ────────────────────────────────────────────────────────

    def _on_provider_selected(self, provider_name: str, account_name: str) -> None:
        self._active_provider_name = provider_name
        self._provider_label.setText(f"Provider: {provider_name}")
        self._log_console.log_message.emit(f"[UI] Switched to {provider_name} / {account_name}")
        self._settings.set("last_provider", provider_name)
        self._settings.save()
        self._connect_provider(provider_name, account_name)
        self._refresh_regions()

    def _connect_provider(self, provider_name: str, account_name: str) -> None:
        cls = _PROVIDER_CLASSES.get(provider_name)
        if cls is None:
            return
        self._active_provider = cls(
            log_callback=lambda msg: self._log_console.log_message.emit(msg)
        )
        creds = self._build_credentials(provider_name, account_name)
        if creds is not None:
            self._active_provider.connect(creds)

    def _build_credentials(self, provider_name: str, account_name: str):
        """Build a credentials object from settings. Returns None if not configured."""
        import os
        accounts = self._settings.get_accounts(provider_name)
        acc = next((a for a in accounts if a.get("name") == account_name), None)
        if not acc and accounts:
            acc = accounts[0]

        if provider_name == "AWS":
            if acc and acc.get("access_key_id"):
                os.environ["AWS_ACCESS_KEY_ID"]     = acc["access_key_id"]
                os.environ["AWS_SECRET_ACCESS_KEY"] = acc.get("access_key_secret", "")
            if acc and acc.get("region"):
                os.environ["AWS_DEFAULT_REGION"] = acc["region"]
            return AWSCredentials(
                profile_name=acc.get("profile_name", "default") if acc else "default",
                region=acc.get("region", "us-east-1") if acc else "us-east-1",
                role_arn=acc.get("role_arn") if acc else None,
            )
        if provider_name == "Azure":
            if acc:
                if acc.get("tenant_id"):
                    os.environ["AZURE_TENANT_ID"]     = acc["tenant_id"]
                    os.environ["AZURE_CLIENT_ID"]     = acc.get("client_id", "")
                    os.environ["AZURE_CLIENT_SECRET"] = acc.get("client_secret", "")
            return AzureCredentials(
                subscription_id=acc.get("subscription_id", "") if acc else ""
            )
        if provider_name == "Alibaba":
            return AlibabaCredentials(
                access_key_id=acc.get("access_key_id", "") if acc else "",
                access_key_secret=acc.get("access_key_secret", "") if acc else "",
                region_id="me-central-1",
            )
        if provider_name == "GCP":
            return GCPCredentials(
                project_id=acc.get("project_id", "") if acc else "",
                key_file=acc.get("key_file", "") if acc else "",
            )
        if provider_name == "Oracle":
            return OracleCredentials(
                config_file=acc.get("config_file", "~/.oci/config") if acc else "~/.oci/config",
                profile_name=acc.get("profile_name", "DEFAULT") if acc else "DEFAULT",
            )
        return None

    def _refresh_regions(self) -> None:
        if self._active_provider is None:
            return
        try:
            regions = self._active_provider.list_regions()
            current = self._region_combo.currentText()
            self._region_combo.clear()
            self._region_combo.addItem("All Regions")
            self._region_combo.addItems(regions)
            idx = self._region_combo.findText(current)
            if idx >= 0:
                self._region_combo.setCurrentIndex(idx)
        except Exception as exc:
            log.warning(f"Could not list regions: {exc}")

    def _on_scan(self) -> None:
        if self._active_provider is None:
            QMessageBox.warning(self, "No Provider", "Please select a cloud provider first.")
            return

        self._all_resources.clear()
        self._resource_tree.clear()
        self._savings_label.clear()

        region = self._region_combo.currentText()
        mode   = "comprehensive" if self._mode_combo.currentIndex() == 0 else "hierarchical"

        self._scan_worker = ScanWorker(self._active_provider, region, mode)
        self._scan_worker.log_message.connect(self._log_console.log_message)
        self._scan_worker.resource_found.connect(self._on_resource_found)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.progress.connect(self._progress.setValue)
        self._scan_worker.error.connect(lambda e: self._log_console.log_message.emit(f"ERROR: {e}"))

        self._progress.setVisible(True)
        self._scan_btn.setEnabled(False)
        self._status_label.setText("Scanning…")
        self._scan_worker.start()

        # Record scan in state DB
        self._state_manager.record_scan(
            self._active_provider_name, region, mode, 0
        )

    def _on_resource_found(self, resource: CloudResource) -> None:
        self._all_resources.append(resource)

    def _on_scan_finished(self, resources: list) -> None:
        mode = "comprehensive" if self._mode_combo.currentIndex() == 0 else "hierarchical"
        if mode == "comprehensive":
            self._resource_tree.populate_flat(resources)
        else:
            self._resource_tree.populate_tree(resources)

        self._progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._status_label.setText(f"Scan complete — {len(resources)} resources found")
        self._delete_btn.setEnabled(len(resources) > 0)

    def _on_tree_selection_changed(self, count: int) -> None:
        self._delete_btn.setEnabled(count > 0)
        savings = self._resource_tree.total_estimated_savings()
        if savings > 0:
            self._savings_label.setText(
                f"💰 Deleting selected resources saves ~${savings:,.2f}/month"
            )
        else:
            self._savings_label.clear()

    def _on_delete(self) -> None:
        selected = self._resource_tree.get_checked_resources()
        if not selected:
            QMessageBox.information(self, "Nothing Selected",
                                    "Check at least one resource before deleting.")
            return

        dry_run = self._dry_run_cb.isChecked()
        dlg = ConfirmDeleteDialog(selected, dry_run, self)
        if dlg.exec() != ConfirmDeleteDialog.DialogCode.Accepted:
            return

        # Optionally snapshot first
        # (For simplicity we skip snapshot UX here; see SnapshotWorker for hook)

        ordered = self._active_provider.get_deletion_order(selected)
        self._delete_worker = DeleteWorker(
            self._active_provider, ordered, dry_run, self._state_manager
        )
        self._delete_worker.log_message.connect(self._log_console.log_message)
        self._delete_worker.resource_deleted.connect(self._on_resource_deleted)
        self._delete_worker.finished.connect(self._on_delete_finished)
        self._delete_worker.progress.connect(self._progress.setValue)

        self._progress.setVisible(True)
        self._delete_btn.setEnabled(False)
        self._status_label.setText("Deleting…")
        self._delete_worker.start()

    def _on_resource_deleted(self, resource: CloudResource, msg: str) -> None:
        if resource.deletion_status:
            self._resource_tree.update_deletion_status(
                resource.resource_id, resource.deletion_status
            )

    def _on_delete_finished(self, resources: list) -> None:
        self._progress.setVisible(False)
        self._delete_btn.setEnabled(True)
        self._status_label.setText("Deletion complete.")

    def _on_filters_changed(self, filters: dict) -> None:
        self._resource_tree.apply_filters(filters)

    def _on_export(self) -> None:
        if not self._all_resources:
            QMessageBox.information(self, "No Data", "Run a scan first.")
            return
        csv_path = export_csv(self._all_resources)
        pdf_path = export_pdf(self._all_resources,
                              title=f"{self._active_provider_name} Cleanup Report")
        self._log_console.log_message.emit(f"[Export] CSV: {csv_path}")
        self._log_console.log_message.emit(f"[Export] PDF: {pdf_path}")
        QMessageBox.information(self, "Export Complete",
                                f"CSV: {csv_path}\nPDF: {pdf_path}")

    def _on_retry_failed(self) -> None:
        failed = self._state_manager.get_failed(self._active_provider_name)
        if not failed:
            QMessageBox.information(self, "No Failed Deletions",
                                    "No failed deletion records found in the database.")
            return
        self._log_console.log_message.emit(
            f"[Retry] Found {len(failed)} failed deletion(s) — re-queuing…"
        )
        # Reconstruct minimal CloudResource objects from log entries
        from models.resource import ResourceType
        resources = []
        for row in failed:
            try:
                rt = ResourceType(row["resource_type"])
            except ValueError:
                rt = ResourceType.UNKNOWN
            resources.append(CloudResource(
                resource_id=row["resource_id"],
                name=row["resource_name"] or row["resource_id"],
                resource_type=rt,
                provider=ProviderName(row["provider"]),
                region=row["region"],
            ))
        if resources and self._active_provider:
            self._delete_worker = DeleteWorker(
                self._active_provider, resources,
                dry_run=self._dry_run_cb.isChecked(),
                state_manager=self._state_manager,
            )
            self._delete_worker.log_message.connect(self._log_console.log_message)
            self._delete_worker.resource_deleted.connect(self._on_resource_deleted)
            self._delete_worker.finished.connect(self._on_delete_finished)
            self._delete_worker.progress.connect(self._progress.setValue)
            self._progress.setVisible(True)
            self._delete_worker.start()

    def _on_scheduled_scan(self, provider: str, region: str) -> None:
        self._log_console.log_message.emit(
            f"[Scheduler] Auto-scan triggered for {provider}/{region}"
        )
        old_count = self._state_manager.get_last_scan_count(provider, region, "comprehensive")
        # TODO: compare new count vs old_count and set badge / send email

    # ── theme ─────────────────────────────────────────────────────────────────

    def _toggle_theme(self) -> None:
        current = self._settings.get("theme", "dark")
        new_theme = "light" if current == "dark" else "dark"
        self._settings.set("theme", new_theme)
        self._settings.save()
        self._apply_theme(new_theme)
        self._theme_btn.setText("☀" if new_theme == "light" else "🌙")

    def _apply_theme(self, theme: str) -> None:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QPalette, QColor
        app = QApplication.instance()
        if theme == "dark":
            app.setStyle("Fusion")
            palette = QPalette()
            palette.setColor(QPalette.ColorRole.Window,       QColor(37, 37, 38))
            palette.setColor(QPalette.ColorRole.WindowText,   QColor(212, 212, 212))
            palette.setColor(QPalette.ColorRole.Base,         QColor(30, 30, 30))
            palette.setColor(QPalette.ColorRole.AlternateBase,QColor(45, 45, 48))
            palette.setColor(QPalette.ColorRole.Text,         QColor(212, 212, 212))
            palette.setColor(QPalette.ColorRole.Button,       QColor(50, 50, 50))
            palette.setColor(QPalette.ColorRole.ButtonText,   QColor(212, 212, 212))
            palette.setColor(QPalette.ColorRole.Highlight,    QColor(9, 71, 113))
            palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
            app.setPalette(palette)
        else:
            app.setStyle("Fusion")
            app.setPalette(app.style().standardPalette())

    # ── geometry ──────────────────────────────────────────────────────────────

    def _restore_geometry(self) -> None:
        geo = self._settings.get("window_geometry", {})
        if geo:
            try:
                self.resize(geo.get("w", 1400), geo.get("h", 850))
                self.move(geo.get("x", 100), geo.get("y", 100))
            except Exception:
                pass

    def closeEvent(self, event) -> None:
        # Save geometry
        self._settings.set("window_geometry", {
            "x": self.x(), "y": self.y(),
            "w": self.width(), "h": self.height(),
        })
        self._settings.save()

        # Cancel any running workers
        for worker in (self._scan_worker, self._delete_worker):
            if worker and worker.isRunning():
                worker.cancel()
                worker.wait(3000)

        self._state_manager.close()
        event.accept()
