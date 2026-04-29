"""
ui/dialogs/credentials_dialog.py
──────────────────────────────────
Dialog for entering / editing cloud-provider credentials.
Each provider has its own tab with the relevant fields.
Values are saved to settings.json via the Settings object.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTabWidget, QVBoxLayout, QWidget,
)

from config.settings import Settings


_FIELD_STYLE = """
QLineEdit {
    background: #1e1e1e;
    color: #d4d4d4;
    border: 1px solid #555;
    border-radius: 3px;
    padding: 4px 6px;
    font-size: 12px;
}
QLineEdit:focus { border-color: #007acc; }
"""

_LABEL_STYLE = "color: #9cdcfe; font-size: 11px;"
_HINT_STYLE  = "color: #6a9955; font-size: 10px; font-style: italic;"


def _make_field(placeholder: str = "", password: bool = False,
                width: int = 340) -> QLineEdit:
    f = QLineEdit()
    f.setPlaceholderText(placeholder)
    f.setFixedWidth(width)
    f.setStyleSheet(_FIELD_STYLE)
    if password:
        f.setEchoMode(QLineEdit.EchoMode.Password)
    return f


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(_HINT_STYLE)
    lbl.setWordWrap(True)
    return lbl


# ─────────────── AWS Tab ──────────────────────────────────────────────────────

class _AWSTab(QWidget):
    def __init__(self, acc: dict) -> None:
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)

        form.addRow(_hint(
            "Use a named AWS profile (from ~/.aws/credentials) OR enter "
            "Access Key ID + Secret directly. Leave key fields empty to rely on the profile."
        ))

        self.profile  = _make_field("default")
        self.key_id   = _make_field("AKIA…")
        self.secret   = _make_field("••••••••", password=True)
        self.region   = _make_field("us-east-1")
        self.role_arn = _make_field("arn:aws:iam::123456:role/MyRole  (optional)")

        form.addRow(QLabel("Profile name:"),    self.profile)
        form.addRow(QLabel("Access Key ID:"),   self.key_id)
        form.addRow(QLabel("Secret Key:"),      self.secret)
        form.addRow(QLabel("Default Region:"),  self.region)
        form.addRow(QLabel("Assume Role ARN:"), self.role_arn)

        # pre-fill
        self.profile.setText(acc.get("profile_name", "default"))
        self.key_id.setText(acc.get("access_key_id", ""))
        self.secret.setText(acc.get("access_key_secret", ""))
        self.region.setText(acc.get("region", "us-east-1"))
        self.role_arn.setText(acc.get("role_arn", ""))

    def to_dict(self) -> dict:
        return {
            "name":              "default",
            "profile_name":      self.profile.text().strip(),
            "access_key_id":     self.key_id.text().strip(),
            "access_key_secret": self.secret.text().strip(),
            "region":            self.region.text().strip() or "us-east-1",
            "role_arn":          self.role_arn.text().strip(),
        }


# ─────────────── Azure Tab ───────────────────────────────────────────────────

class _AzureTab(QWidget):
    def __init__(self, acc: dict) -> None:
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)

        form.addRow(_hint(
            "Enter your Azure Service Principal credentials. "
            "Get them via: az ad sp create-for-rbac --sdk-auth"
        ))

        self.sub_id     = _make_field("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        self.tenant_id  = _make_field("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        self.client_id  = _make_field("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        self.client_sec = _make_field("••••••••", password=True)

        form.addRow(QLabel("Subscription ID:"), self.sub_id)
        form.addRow(QLabel("Tenant ID:"),       self.tenant_id)
        form.addRow(QLabel("Client ID:"),       self.client_id)
        form.addRow(QLabel("Client Secret:"),   self.client_sec)

        self.sub_id.setText(acc.get("subscription_id", ""))
        self.tenant_id.setText(acc.get("tenant_id", ""))
        self.client_id.setText(acc.get("client_id", ""))
        self.client_sec.setText(acc.get("client_secret", ""))

    def to_dict(self) -> dict:
        return {
            "name":            "default",
            "subscription_id": self.sub_id.text().strip(),
            "tenant_id":       self.tenant_id.text().strip(),
            "client_id":       self.client_id.text().strip(),
            "client_secret":   self.client_sec.text().strip(),
        }


# ─────────────── Alibaba Tab ─────────────────────────────────────────────────

class _AlibabaTab(QWidget):
    def __init__(self, acc: dict) -> None:
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)

        form.addRow(_hint(
            "RAM user or STS credentials from the Alibaba Cloud console."
        ))

        self.key_id  = _make_field("LTAI…")
        self.secret  = _make_field("••••••••", password=True)
        self.region  = _make_field("me-central-1")

        form.addRow(QLabel("Access Key ID:"),     self.key_id)
        form.addRow(QLabel("Access Key Secret:"), self.secret)
        form.addRow(QLabel("Region ID:"),         self.region)

        self.key_id.setText(acc.get("access_key_id", ""))
        self.secret.setText(acc.get("access_key_secret", ""))
        self.region.setText(acc.get("region_id", "me-central-1"))

    def to_dict(self) -> dict:
        return {
            "name":              "default",
            "access_key_id":     self.key_id.text().strip(),
            "access_key_secret": self.secret.text().strip(),
            "region_id":         self.region.text().strip() or "me-central-1",
        }


# ─────────────── GCP Tab ─────────────────────────────────────────────────────

class _GCPTab(QWidget):
    def __init__(self, acc: dict) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(_hint(
            "Enter your GCP Project ID and (optionally) the path to a "
            "service-account JSON key file.  If the key file is empty, "
            "Application Default Credentials (gcloud auth) are used."
        ))

        form = QFormLayout()
        form.setSpacing(10)
        self.project = _make_field("my-gcp-project-id")
        form.addRow(QLabel("Project ID:"), self.project)

        # Key file row with browse button
        key_row = QHBoxLayout()
        self.key_file = _make_field("/path/to/service-account.json", width=280)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(70)
        browse_btn.clicked.connect(self._browse)
        key_row.addWidget(self.key_file)
        key_row.addWidget(browse_btn)
        form.addRow(QLabel("Key File (JSON):"), key_row)
        layout.addLayout(form)
        layout.addStretch()

        self.project.setText(acc.get("project_id", ""))
        self.key_file.setText(acc.get("key_file", ""))

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Service Account JSON", "", "JSON Files (*.json)"
        )
        if path:
            self.key_file.setText(path)

    def to_dict(self) -> dict:
        return {
            "name":       "default",
            "project_id": self.project.text().strip(),
            "key_file":   self.key_file.text().strip(),
        }


# ─────────────── Oracle Tab ──────────────────────────────────────────────────

class _OracleTab(QWidget):
    def __init__(self, acc: dict) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(_hint(
            "OCI credentials are stored in ~/.oci/config by default. "
            "Browse to choose a different config file, then pick the profile name."
        ))

        form = QFormLayout()
        form.setSpacing(10)

        # Config file row
        cfg_row = QHBoxLayout()
        self.config_file = _make_field("~/.oci/config", width=260)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(70)
        browse_btn.clicked.connect(self._browse)
        cfg_row.addWidget(self.config_file)
        cfg_row.addWidget(browse_btn)
        form.addRow(QLabel("Config File:"), cfg_row)

        self.profile = _make_field("DEFAULT")
        form.addRow(QLabel("Profile Name:"), self.profile)
        layout.addLayout(form)
        layout.addStretch()

        self.config_file.setText(acc.get("config_file", "~/.oci/config"))
        self.profile.setText(acc.get("profile_name", "DEFAULT"))

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select OCI Config File", "", "Config Files (*)"
        )
        if path:
            self.config_file.setText(path)

    def to_dict(self) -> dict:
        return {
            "name":         "default",
            "config_file":  self.config_file.text().strip() or "~/.oci/config",
            "profile_name": self.profile.text().strip() or "DEFAULT",
        }


# ─────────────── Main Dialog ─────────────────────────────────────────────────

class CredentialsDialog(QDialog):
    """
    Opens a tabbed dialog for entering credentials for all 5 providers.
    Saving writes the values back to settings and persists settings.json.
    """

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Cloud Provider Credentials")
        self.setMinimumWidth(520)
        self.setStyleSheet("""
            QDialog   { background: #1e1e1e; color: #d4d4d4; }
            QTabWidget::pane { border: 1px solid #444; }
            QTabBar::tab {
                background: #2d2d2d; color: #aaa;
                padding: 6px 18px; border: 1px solid #444;
            }
            QTabBar::tab:selected { background: #007acc; color: #fff; }
            QLabel { color: #d4d4d4; }
            QPushButton {
                background: #0e639c; color: #fff;
                border: none; border-radius: 3px; padding: 6px 14px;
            }
            QPushButton:hover { background: #1177bb; }
            QPushButton:pressed { background: #094771; }
        """)
        self._build_ui()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _first_account(self, provider: str) -> dict:
        accounts = self._settings.get_accounts(provider)
        return accounts[0] if accounts else {}

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        # header
        header = QLabel("🔑  Cloud Credentials")
        header.setStyleSheet("font-size: 14px; font-weight: bold; color: #9cdcfe; padding: 4px 0;")
        root.addWidget(header)

        # tabs
        self._tabs = QTabWidget()
        self._aws     = _AWSTab(self._first_account("AWS"))
        self._azure   = _AzureTab(self._first_account("Azure"))
        self._alibaba = _AlibabaTab(self._first_account("Alibaba"))
        self._gcp     = _GCPTab(self._first_account("GCP"))
        self._oracle  = _OracleTab(self._first_account("Oracle"))

        self._tabs.addTab(self._aws,     "☁  AWS")
        self._tabs.addTab(self._azure,   "🔷  Azure")
        self._tabs.addTab(self._alibaba, "🟠  Alibaba")
        self._tabs.addTab(self._gcp,     "🟡  GCP")
        self._tabs.addTab(self._oracle,  "🔴  Oracle")
        root.addWidget(self._tabs)

        # buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── save ─────────────────────────────────────────────────────────────────

    def _save(self) -> None:
        mapping = {
            "AWS":     self._aws.to_dict(),
            "Azure":   self._azure.to_dict(),
            "Alibaba": self._alibaba.to_dict(),
            "GCP":     self._gcp.to_dict(),
            "Oracle":  self._oracle.to_dict(),
        }
        for provider, data in mapping.items():
            accounts = self._settings.get_accounts(provider)
            # Replace or insert the "default" account entry
            updated = [a for a in accounts if a.get("name") != "default"]
            updated.insert(0, data)
            self._settings._data["accounts"][provider] = updated

        # Azure: set env vars so DefaultAzureCredential picks them up
        import os
        az = mapping["Azure"]
        if az.get("tenant_id"):
            os.environ["AZURE_TENANT_ID"]     = az["tenant_id"]
            os.environ["AZURE_CLIENT_ID"]     = az["client_id"]
            os.environ["AZURE_CLIENT_SECRET"] = az["client_secret"]

        # AWS: set env vars if key was entered directly
        aws = mapping["AWS"]
        if aws.get("access_key_id"):
            os.environ["AWS_ACCESS_KEY_ID"]     = aws["access_key_id"]
            os.environ["AWS_SECRET_ACCESS_KEY"] = aws["access_key_secret"]
            if aws.get("region"):
                os.environ["AWS_DEFAULT_REGION"] = aws["region"]

        # Alibaba: set env vars
        ali = mapping["Alibaba"]
        if ali.get("access_key_id"):
            os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"]     = ali["access_key_id"]
            os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] = ali["access_key_secret"]

        self._settings.save()
        self.accept()
