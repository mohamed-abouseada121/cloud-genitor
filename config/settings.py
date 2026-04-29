"""
config/settings.py
──────────────────
Load / save persistent application settings to a local JSON file.
"""

from __future__ import annotations

import json
import os
from typing import Any

_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "..", "settings.json")

_DEFAULTS: dict[str, Any] = {
    "last_provider":   "AWS",
    "last_region":     "",
    "last_scan_mode":  "comprehensive",
    "dry_run":         True,
    "theme":           "dark",
    "window_geometry": {},
    # provider → list of account dicts
    "accounts": {
        "AWS":     [],
        "Azure":   [],
        "Alibaba": [],
        "GCP":     [],
        "Oracle":  [],
    },
    # scheduled scan settings
    "schedule": {
        "enabled":          False,
        "interval_minutes": 1440,   # 24 h
        "notify_email":     "",
        "smtp_host":        "",
        "smtp_port":        587,
        "smtp_user":        "",
        "smtp_password":    "",
    },
}


class Settings:
    def __init__(self, path: str = _SETTINGS_FILE) -> None:
        self._path = os.path.abspath(path)
        self._data: dict[str, Any] = {}
        self.load()

    # ── persistence ──────────────────────────────────────────────────────────

    def load(self) -> None:
        if os.path.isfile(self._path):
            with open(self._path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            # Merge stored over defaults (shallow)
            self._data = {**_DEFAULTS, **stored}
        else:
            self._data = dict(_DEFAULTS)

    def save(self) -> None:
        with open(self._path, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, ensure_ascii=False)

    # ── accessors ────────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    # ── account helpers ───────────────────────────────────────────────────────

    def get_accounts(self, provider: str) -> list[dict]:
        return self._data["accounts"].get(provider, [])

    def add_account(self, provider: str, account: dict) -> None:
        if provider not in self._data["accounts"]:
            self._data["accounts"][provider] = []
        self._data["accounts"][provider].append(account)
        self.save()

    def remove_account(self, provider: str, account_name: str) -> None:
        accounts = self._data["accounts"].get(provider, [])
        self._data["accounts"][provider] = [
            a for a in accounts if a.get("name") != account_name
        ]
        self.save()

    # ── schedule helpers ──────────────────────────────────────────────────────

    @property
    def schedule(self) -> dict:
        return self._data["schedule"]

    def update_schedule(self, **kwargs: Any) -> None:
        self._data["schedule"].update(kwargs)
        self.save()
