"""
ui/main_window.py
──────────────────
Main application window.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import Qt, QSize, QThread, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
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
from ui.dialogs.schedule_dialog import ScheduleDialog
from ui.filter_bar import FilterBar
from ui.log_console import LogConsole, QtLogHandler
from ui.resource_tree import ResourceTree
from ui.sidebar import Sidebar
from ui.multi_region_selector import MultiRegionSelector
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

_STATIC_REGIONS = {
    "AWS": [
        "us-east-1", "us-east-2", "us-west-1", "us-west-2",
        "ca-central-1", "ca-west-1",
        "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1", "eu-central-2",
        "eu-north-1", "eu-south-1", "eu-south-2",
        "ap-southeast-1", "ap-southeast-2", "ap-southeast-3", "ap-southeast-4",
        "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
        "ap-south-1", "ap-south-2", "ap-east-1",
        "sa-east-1",
        "me-south-1", "me-central-1",
        "af-south-1",
        "il-central-1",
    ],
    "Azure": [
        "eastus", "eastus2", "westus", "westus2", "westus3",
        "centralus", "northcentralus", "southcentralus", "westcentralus",
        "canadacentral", "canadaeast",
        "brazilsouth", "brazilsoutheast",
        "northeurope", "westeurope",
        "uksouth", "ukwest",
        "francecentral", "francesouth",
        "germanywestcentral", "germanynorth",
        "swedencentral",
        "switzerlandnorth", "switzerlandwest",
        "norwayeast", "norwaywest",
        "italynorth", "polandcentral", "spaincentral",
        "eastasia", "southeastasia",
        "japaneast", "japanwest",
        "koreacentral", "koreasouth",
        "australiaeast", "australiasoutheast", "australiacentral", "australiacentral2",
        "centralindia", "southindia", "westindia",
        "southafricanorth", "southafricawest",
        "uaenorth", "uaecentral",
        "israelcentral",
        "qatarcentral",
    ],
    "GCP": [
        "us-central1", "us-east1", "us-east4", "us-east5",
        "us-west1", "us-west2", "us-west3", "us-west4",
        "us-south1",
        "northamerica-northeast1", "northamerica-northeast2",
        "southamerica-east1", "southamerica-west1",
        "europe-west1", "europe-west2", "europe-west3", "europe-west4",
        "europe-west6", "europe-west8", "europe-west9", "europe-west10", "europe-west12",
        "europe-north1", "europe-central2", "europe-southwest1",
        "asia-east1", "asia-east2",
        "asia-northeast1", "asia-northeast2", "asia-northeast3",
        "asia-southeast1", "asia-southeast2",
        "asia-south1", "asia-south2",
        "australia-southeast1", "australia-southeast2",
        "me-west1", "me-central1", "me-central2",
        "africa-south1",
    ],
    "Alibaba": [
        "cn-hangzhou", "cn-shanghai", "cn-beijing", "cn-shenzhen",
        "cn-zhangjiakou", "cn-huhehaote", "cn-wulanchabu",
        "cn-nanjing", "cn-fuzhou", "cn-wuhan-lr", "cn-heyuan", "cn-guangzhou",
        "cn-chengdu", "cn-hongkong",
        "ap-northeast-1", "ap-northeast-2",
        "ap-southeast-1", "ap-southeast-2", "ap-southeast-3", "ap-southeast-5", "ap-southeast-6", "ap-southeast-7",
        "ap-south-1",
        "us-east-1", "us-west-1",
        "eu-west-1", "eu-central-1",
        "me-east-1", "me-central-1",
    ],
    "Oracle": [
        "us-ashburn-1", "us-phoenix-1", "us-sanjose-1", "us-chicago-1",
        "ca-toronto-1", "ca-montreal-1",
        "sa-saopaulo-1", "sa-vinhedo-1", "sa-bogota-1", "sa-santiago-1",
        "eu-frankfurt-1", "eu-amsterdam-1", "eu-zurich-1", "eu-milan-1",
        "eu-stockholm-1", "eu-marseille-1", "eu-jovanovac-1", "eu-paris-1",
        "eu-london-1", "eu-dcc-rating-1", "eu-dcc-rating-2",
        "uk-london-1", "uk-cardiff-1",
        "ap-tokyo-1", "ap-osaka-1",
        "ap-sydney-1", "ap-melbourne-1",
        "ap-singapore-1", "ap-singapore-2",
        "ap-mumbai-1", "ap-hyderabad-1",
        "ap-seoul-1", "ap-chuncheon-1",
        "me-jeddah-1", "me-dubai-1", "me-riyadh-1", "me-abudhabi-1",
        "af-johannesburg-1",
        "il-jerusalem-1",
        "mx-queretaro-1", "mx-monterrey-1",
    ],
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
        self._active_account_name: str          = ""
        self._active_provider: Optional[object] = None
        self._scan_workers:  list[ScanWorker] = []
        self._delete_workers: list[DeleteWorker] = []
        self._is_deleting:   bool                   = False
        self._all_resources: list[CloudResource]    = []

        self._build_ui()
        self._setup_shortcuts()
        self._setup_logging()
    def _setup_scheduler(self) -> None:
        from PyQt6.QtCore import QTimer
        self._schedule_timer = QTimer(self)
        self._schedule_timer.timeout.connect(self._check_schedules)
        self._schedule_timer.start(60000) # Check every minute

    def _apply_theme(self, theme: str) -> None:
        self._restore_geometry()

    def _build_ui(self) -> None:
        self.setWindowTitle("Cloud Resource Janitor")
        self.resize(1400, 850)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self._toolbar = self._build_toolbar()
        self.addToolBar(self._toolbar)

        body_splitter = QSplitter(Qt.Orientation.Horizontal)

        self._sidebar = Sidebar()
        self._sidebar.setFixedWidth(220)
        self._sidebar.provider_selected.connect(self._on_provider_selected)
        body_splitter.addWidget(self._sidebar)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(8)

        self._resource_tree = ResourceTree()
        self._resource_tree.selection_changed.connect(self._on_tree_selection_changed)

        self._filter_bar = FilterBar()
        self._filter_bar.filters_changed.connect(self._on_filters_changed)
        right_layout.addWidget(self._filter_bar)

        select_bar = QHBoxLayout()
        self._select_all_btn = QPushButton("Check All")
        self._select_all_btn.clicked.connect(self._resource_tree.select_all)
        
        self._select_none_btn = QPushButton("Uncheck All")
        self._select_none_btn.clicked.connect(self._resource_tree.select_none)

        self._savings_label = QLabel("")
        self._savings_label.setObjectName("savingsLabel")
        
        select_bar.addWidget(self._select_all_btn)
        select_bar.addWidget(self._select_none_btn)
        select_bar.addWidget(self._savings_label)
        select_bar.addStretch()
        right_layout.addLayout(select_bar)
        right_layout.addWidget(self._resource_tree, stretch=3)

        log_box = QGroupBox("Console Log")
        log_box.setMaximumHeight(220)
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(5, 10, 5, 5)
        self._log_console = LogConsole()
        log_layout.addWidget(self._log_console)
        right_layout.addWidget(log_box)

        body_splitter.addWidget(right)
        body_splitter.setStretchFactor(1, 1)
        main_layout.addWidget(body_splitter)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._progress = QProgressBar()
        self._progress.setFixedWidth(250)
        self._progress.setVisible(False)
        self._status_bar.addPermanentWidget(self._progress)
        self._status_label = QLabel("Ready")
        self._status_bar.addWidget(self._status_label)

    def _build_toolbar(self) -> QToolBar:
        bar = QToolBar("Main Toolbar")
        bar.setMovable(False)
        bar.setIconSize(QSize(20, 20))

        self._provider_label = QLabel("Provider: —")
        self._provider_label.setStyleSheet("font-weight: bold; padding: 0 10px;")
        bar.addWidget(self._provider_label)
        bar.addSeparator()

        bar.addWidget(QLabel(" Region: "))
        self._region_selector = MultiRegionSelector()
        bar.addWidget(self._region_selector)
        bar.addSeparator()

        self._dry_run_cb = QCheckBox("Dry Run")
        self._dry_run_cb.setChecked(self._settings.get("dry_run", True))
        bar.addWidget(self._dry_run_cb)

        self._retry_delete_cb = QCheckBox("Retry")
        self._retry_delete_cb.setChecked(self._settings.get("retry_delete", False))
        bar.addWidget(self._retry_delete_cb)
        bar.addSeparator()

        self._scan_btn = QPushButton("🔍  Scan")
        self._scan_btn.clicked.connect(self._on_scan)
        bar.addWidget(self._scan_btn)

        self._scan_all_btn = QPushButton("🚀  Scan All")
        self._scan_all_btn.clicked.connect(self._on_scan_all)
        bar.addWidget(self._scan_all_btn)

        self._delete_btn = QPushButton("🗑  Delete Selected")
        self._delete_btn.setEnabled(False)
        self._delete_btn.setObjectName("deleteBtn")
        self._delete_btn.clicked.connect(self._on_delete)
        bar.addWidget(self._delete_btn)

        self._stop_btn = QPushButton("🛑  Stop Deletion")
        self._stop_btn.setVisible(False)
        self._stop_btn.setObjectName("stopBtn")
        self._stop_btn.clicked.connect(self._on_stop)
        bar.addWidget(self._stop_btn)

        self._export_btn = QPushButton("📊  Export")
        self._export_btn.clicked.connect(self._on_export)
        bar.addWidget(self._export_btn)

        self._creds_btn = QPushButton("⚙  Credentials")
        self._creds_btn.clicked.connect(self._on_credentials)
        bar.addWidget(self._creds_btn)

        self._schedule_btn = QPushButton("📅  Schedule")
        self._schedule_btn.clicked.connect(self._on_schedule)
        bar.addWidget(self._schedule_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        self._theme_btn = QPushButton("🌙")
        self._theme_btn.setFixedWidth(40)
        self._theme_btn.clicked.connect(self._toggle_theme)
        bar.addWidget(self._theme_btn)

        return bar

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(self._log_console.clear)
        QShortcut(QKeySequence("F5"),     self).activated.connect(self._refresh_regions)

    def _setup_logging(self) -> None:
        handler = QtLogHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        self._log_console.connect_handler(handler)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

    def _setup_scheduler(self) -> None:
        sched_cfg = self._settings.schedule
        if not sched_cfg.get("enabled"): return
        self._scheduler = ScanScheduler(self)
        self._scheduler.configure(
            provider=self._settings.get("last_provider", "AWS"),
            region=self._settings.get("last_region", "All Regions"),
            interval_minutes=sched_cfg.get("interval_minutes", 1440),
        )
        self._scheduler.scan_due.connect(self._on_scheduled_scan)
        self._scheduler.start()

    def _on_provider_selected(self, provider_name: str, account_name: str) -> None:
        self._active_provider_name = provider_name
        self._active_account_name = account_name
        self._provider_label.setText(f"Provider: {provider_name}")
        self._settings.set("last_provider", provider_name)
        self._settings.save()
        self._connect_provider(provider_name, account_name)
        self._refresh_regions()

    def _connect_provider(self, provider_name: str, account_name: str) -> None:
        cls = _PROVIDER_CLASSES.get(provider_name)
        if cls is None: return
        self._active_provider = cls(log_callback=lambda msg: self._log_console.log_message.emit(msg))
        creds = self._build_credentials(provider_name, account_name)
        if creds: self._active_provider.connect(creds)

    def _build_credentials(self, provider_name: str, account_name: str):
        import os
        accounts = self._settings.get_accounts(provider_name)
        acc = next((a for a in accounts if a.get("name") == account_name), None) or (accounts[0] if accounts else None)
        if not acc: return None

        if provider_name == "AWS":
            return AWSCredentials(
                region=acc.get("region", "us-east-1"),
                access_key_id=acc.get("access_key_id"),
                secret_access_key=acc.get("access_key_secret")
            )
        if provider_name == "Azure":
            return AzureCredentials(
                subscription_id=acc.get("subscription_id", ""),
                tenant_id=acc.get("tenant_id"),
                client_id=acc.get("client_id"),
                client_secret=acc.get("client_secret")
            )
        if provider_name == "Alibaba":
            return AlibabaCredentials(
                access_key_id=acc.get("access_key_id", ""),
                access_key_secret=acc.get("access_key_secret", ""),
                region_id=acc.get("region_id", "me-central-1")
            )
        if provider_name == "GCP":
            return GCPCredentials(
                project_id=acc.get("project_id", ""),
                key_file=acc.get("key_file", "")
            )
        if provider_name == "Oracle":
            return OracleCredentials(
                config_file=acc.get("config_file", "~/.oci/config"),
                profile_name=acc.get("profile_name", "DEFAULT")
            )
        return None

    def _refresh_regions(self) -> None:
        if not self._active_provider: return
        
        provider_name = self._active_provider_name
        static_list = _STATIC_REGIONS.get(provider_name, ["Default"])
        
        # Show static regions immediately
        self._region_selector.set_regions(static_list)
        
        # If we have no credentials, keep static list and stop
        creds = self._build_credentials(provider_name, self._active_account_name)
        if not creds:
            return

        # Async live-sync in background (won't block UI)
        class RegionWorker(QThread):
            done = pyqtSignal(list)
            def __init__(self, p): super().__init__(); self.p = p
            def run(self):
                try:
                    res = self.p.list_regions()
                    self.done.emit(res if res else [])
                except:
                    self.done.emit([])
        
        if hasattr(self, "_rw") and self._rw.isRunning():
            self._rw.terminate()
            
        self._rw = RegionWorker(self._active_provider)
        self._rw.done.connect(self._on_regions_loaded)
        self._rw.start()

    def _on_regions_loaded(self, regions: list) -> None:
        if regions:
            combined = sorted(list(set(regions + _STATIC_REGIONS.get(self._active_provider_name, []))))
            self._region_selector.set_regions(combined)
        else:
            self._region_selector.set_regions(_STATIC_REGIONS.get(self._active_provider_name, []))

    def _on_scan(self) -> None:
        if not self._active_provider: return
        if self._scan_workers:
            for w in self._scan_workers:
                w.cancel()
            self._scan_workers.clear()
            return
        
        self._all_resources.clear()
        self._resource_tree.clear()
        regions = self._region_selector.get_selected()
        if not regions:
            self._log_console.log_message.emit("⚠ Please select at least one region.")
            return

        self._start_scan_for_provider(self._active_provider_name, regions)

    def _on_scan_all(self) -> None:
        if self._scan_workers:
            for w in self._scan_workers:
                w.cancel()
            self._scan_workers.clear()
            return

        self._all_resources.clear()
        self._resource_tree.clear()
        
        active_providers = []
        for p_name in _PROVIDER_CLASSES.keys():
            creds = self._build_credentials(p_name, self._active_account_name or "Default")
            if creds:
                active_providers.append(p_name)
        
        if not active_providers:
            self._log_console.log_message.emit("⚠ No valid credentials found for any provider.")
            return

        self._log_console.log_message.emit(f"🚀 Starting parallel scan for: {', '.join(active_providers)}")
        for p_name in active_providers:
            regions = _STATIC_REGIONS.get(p_name, ["Default"])
            self._start_scan_for_provider(p_name, regions)

    def _start_scan_for_provider(self, provider_name: str, regions: list[str]) -> None:
        def _factory():
            p = _PROVIDER_CLASSES[provider_name](log_callback=lambda m: self._log_console.log_message.emit(m))
            creds = self._build_credentials(provider_name, self._active_account_name or "Default")
            if not creds:
                raise ValueError(f"No credentials found for {provider_name}")
            if not p.connect(creds, test_connection=False):
                raise ConnectionError(f"Failed to connect to {provider_name}")
            return p

        worker = ScanWorker(_factory, provider_name, regions, "hierarchical", state_manager=self._state_manager)
        worker.log_message.connect(self._log_console.log_message)
        worker.resource_found.connect(self._on_resource_found)
        worker.finished.connect(lambda res, w=worker: self._on_worker_finished(w))
        worker.progress.connect(self._progress.setValue)
        
        self._scan_workers.append(worker)
        self._progress.setVisible(True)
        self._scan_btn.setText("🛑 Stop Scan")
        self._scan_all_btn.setText("🛑 Stop All")
        worker.start()

    def _on_resource_found(self, r: CloudResource) -> None:
        self._all_resources.append(r)

    def _on_worker_finished(self, worker: ScanWorker) -> None:
        if worker in self._scan_workers:
            self._scan_workers.remove(worker)
        
        if not self._scan_workers:
            self._progress.setVisible(False)
            self._scan_btn.setText("🔍  Scan")
            self._scan_all_btn.setText("🚀  Scan All")
            self._status_label.setText(f"Scan complete — {len(self._all_resources)} resources found.")
            self._resource_tree.populate_tree(self._all_resources)
            self._update_ui_state()

    def _on_tree_selection_changed(self, count: int) -> None:
        self._update_ui_state()
        savings = self._resource_tree.total_estimated_savings()
        self._savings_label.setText(f"💰 Savings: ~${savings:,.2f}/mo" if savings > 0 else "")

    def _on_delete(self) -> None:
        selected = self._resource_tree.get_checked_resources()
        if not selected: return
        if not ConfirmDeleteDialog(selected, self._dry_run_cb.isChecked(), self).exec(): return

        # Group by provider
        by_provider: dict[str, list[CloudResource]] = {}
        for r in selected:
            p_name = r.provider.value
            if p_name not in by_provider: by_provider[p_name] = []
            by_provider[p_name].append(r)

        self._is_deleting = True
        self._update_ui_state()
        self._progress.setVisible(True)

        for p_name, resources in by_provider.items():
            def _factory(name=p_name):
                p = _PROVIDER_CLASSES[name](log_callback=lambda m: self._log_console.log_message.emit(m))
                creds = self._build_credentials(name, self._active_account_name or "Default")
                if not creds: raise ValueError(f"No credentials for {name}")
                if not p.connect(creds, test_connection=False): raise ConnectionError(f"Failed to connect to {name}")
                return p

            worker = DeleteWorker(_factory, resources, dry_run=self._dry_run_cb.isChecked(), state_manager=self._state_manager)
            worker.log_message.connect(self._log_console.log_message)
            worker.resource_deleted.connect(self._on_resource_deleted)
            worker.finished.connect(lambda r, w=worker: self._on_delete_worker_finished(w))
            worker.progress.connect(self._progress.setValue)
            
            self._delete_workers.append(worker)
            worker.start()

    def _on_delete_worker_finished(self, worker: DeleteWorker) -> None:
        if worker in self._delete_workers:
            self._delete_workers.remove(worker)
        
        if not self._delete_workers:
            self._is_deleting = False
            self._progress.setVisible(False)
            self._update_ui_state()

    def _on_stop(self) -> None:
        if self._delete_workers:
            for w in self._delete_workers:
                w.cancel()
            self._delete_workers.clear()
        self._is_deleting = False
        self._update_ui_state()

    def _update_ui_state(self) -> None:
        scanning = bool(self._scan_workers)
        self._scan_btn.setEnabled(not self._is_deleting)
        self._scan_all_btn.setEnabled(not self._is_deleting)
        self._delete_btn.setEnabled(not scanning and not self._is_deleting and len(self._resource_tree.get_checked_resources()) > 0)
        self._stop_btn.setVisible(self._is_deleting)
        self._sidebar.setEnabled(not scanning and not self._is_deleting)

    def _on_resource_deleted(self, r, msg) -> None:
        if r.deletion_status: self._resource_tree.update_deletion_status(r.resource_id, r.deletion_status)

    def _on_filters_changed(self, f) -> None:
        if self._resource_tree: self._resource_tree.apply_filters(f)

    def _on_export(self) -> None:
        if self._all_resources: export_csv(self._all_resources); export_pdf(self._all_resources)

    def _on_retry_failed(self) -> None: pass

    def _on_scheduled_scan(self, p, r) -> None: pass

    def _on_credentials(self) -> None:
        if CredentialsDialog(self._settings, self).exec():
            if self._active_provider_name: self._connect_provider(self._active_provider_name, "default"); self._refresh_regions()

    def _on_schedule(self) -> None:
        if not self._active_provider:
            self._log_console.log_message.emit("⚠ Please select a provider first.")
            return
        regions = self._region_selector.get_selected()
        if not regions:
            self._log_console.log_message.emit("⚠ Please select regions to schedule.")
            return
            
        dlg = ScheduleDialog(self._active_provider_name, regions, self)
        if dlg.exec():
            data = dlg.get_data()
            self._state_manager.add_schedule(
                self._active_provider_name, regions, "hierarchical", data["interval"]
            )
            self._log_console.log_message.emit(f"📅 Scheduled scan every {data['interval']}h for {len(regions)} region(s).")

    def _check_schedules(self) -> None:
        if self._scan_worker and self._scan_worker.isRunning():
            return
            
        schedules = self._state_manager.get_active_schedules()
        now = datetime.utcnow()
        
        for s in schedules:
            next_run = datetime.fromisoformat(s["next_run"])
            if now >= next_run:
                import json
                self._log_console.log_message.emit(f"🕒 [Auto] Starting scheduled scan for {s['provider']}...")
                self._state_manager.update_schedule_run(s["id"])
                
                # Trigger scan logic (simplified for background)
                regions = json.loads(s["regions"])
                def _factory():
                    p = _PROVIDER_CLASSES[s["provider"]](log_callback=lambda m: self._log_console.log_message.emit(m))
                    creds = self._build_credentials(s["provider"], "default")
                    if not p.connect(creds):
                         raise ConnectionError(f"Failed to connect to {s['provider']}")
                    return p

                self._scan_worker = ScanWorker(_factory, s["provider"], regions, s["mode"], state_manager=self._state_manager)
                self._scan_worker.log_message.connect(self._log_console.log_message)
                self._scan_worker.resource_found.connect(self._on_resource_found)
                self._scan_worker.finished.connect(self._on_scan_finished)
                self._scan_worker.start()
                break # Only run one at a time

    def _toggle_theme(self) -> None:
        new_theme = "light" if self._settings.get("theme") == "dark" else "dark"
        self._settings.set("theme", new_theme); self._settings.save()
        self._apply_theme(new_theme)

    def _apply_theme(self, theme: str) -> None:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if not app: return
        if theme == "dark":
            app.setStyle("Fusion")
            app.setStyleSheet("""
                QMainWindow { background-color: #121212; }
                QWidget { font-family: 'Inter', 'Segoe UI', sans-serif; font-size: 14px; color: #e1e1e1; }
                QToolBar { background: #1e1e1e; border-bottom: 1px solid #333; spacing: 12px; padding: 10px; }
                QPushButton { background: #2c2c2c; border: 1px solid #3d3d3d; border-radius: 6px; padding: 8px 18px; font-weight: 500; }
                QPushButton:hover { background: #383838; border-color: #3498db; }
                QPushButton#deleteBtn { background: #4a1c1c; border-color: #632a2a; }
                QPushButton#deleteBtn:hover { background: #632a2a; border-color: #e74c3c; }
                QPushButton#stopBtn { background: #c0392b; color: white; border: none; }
                QListWidget { background: #1e1e1e; border: none; border-right: 1px solid #333; outline: none; }
                QListWidget::item { padding: 15px; border-bottom: 1px solid #2d2d2d; }
                QListWidget::item:selected { background: #0d47a1; color: white; border-left: 4px solid #3498db; }
                QTreeWidget { background: #181818; border: 1px solid #333; alternate-background-color: #1e1e1e; }
                QHeaderView::section { background: #252525; color: #aaa; padding: 10px; border: 1px solid #181818; font-weight: bold; }
                QProgressBar { border: 1px solid #333; border-radius: 10px; text-align: center; height: 12px; background: #252525; }
                QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3498db, stop:1 #2980b9); border-radius: 8px; }
                QGroupBox { font-weight: bold; border: 1px solid #333; margin-top: 15px; border-radius: 8px; padding-top: 15px; background: #1a1a1a; }
                QComboBox, QLineEdit, QTextEdit, QPlainTextEdit { background: #252525; border: 1px solid #3d3d3d; border-radius: 6px; padding: 8px; color: #e1e1e1; selection-background-color: #3498db; }
                QComboBox::drop-down { border: none; }
                QComboBox QAbstractItemView { background-color: #252525; border: 1px solid #333; selection-background-color: #3498db; }
                #sidebarTitle { background: #252525; color: #3498db; }
                #savingsLabel { color: #2ecc71; font-weight: bold; font-size: 15px; padding-left: 10px; }
                QStatusBar { background: #1e1e1e; color: #888; border-top: 1px solid #333; }
            """)
        else:
            app.setStyle("Fusion"); app.setStyleSheet("")

    def _restore_geometry(self) -> None:
        g = self._settings.get("window_geometry", {})
        if g: self.resize(g.get("w", 1400), g.get("h", 850)); self.move(g.get("x", 100), g.get("y", 100))

    def closeEvent(self, event) -> None:
        self._settings.set("window_geometry", {"x": self.x(), "y": self.y(), "w": self.width(), "h": self.height()}); self._settings.save()
        self._state_manager.close(); event.accept()
