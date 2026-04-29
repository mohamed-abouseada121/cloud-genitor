"""
providers/account_manager.py
─────────────────────────────
Multi-account registry: one provider can have many registered accounts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from models.resource import ProviderName


@dataclass
class AccountEntry:
    """
    Lightweight descriptor for a registered cloud account.
    Secrets are NEVER stored here — only profile/key references.
    """
    name:      str                          # user-defined label, e.g. "prod-aws"
    provider:  ProviderName
    params:    Dict[str, Any] = field(default_factory=dict)
    # runtime-resolved credential object (not persisted)
    _resolved: Optional[Any] = field(default=None, repr=False, compare=False)


class AccountManager:
    """
    Registry of all cloud accounts across all providers.
    Populated from Settings.get_accounts() at startup.
    """

    def __init__(self) -> None:
        self._accounts: Dict[ProviderName, List[AccountEntry]] = {
            p: [] for p in ProviderName
        }

    # ── registration ─────────────────────────────────────────────────────────

    def register(self, entry: AccountEntry) -> None:
        self._accounts[entry.provider].append(entry)

    def unregister(self, provider: ProviderName, name: str) -> None:
        self._accounts[provider] = [
            a for a in self._accounts[provider] if a.name != name
        ]

    # ── lookup ────────────────────────────────────────────────────────────────

    def get_accounts(self, provider: ProviderName) -> List[AccountEntry]:
        return list(self._accounts[provider])

    def get_account(self, provider: ProviderName, name: str) -> Optional[AccountEntry]:
        return next(
            (a for a in self._accounts[provider] if a.name == name), None
        )

    def all_accounts(self) -> List[AccountEntry]:
        result: List[AccountEntry] = []
        for accounts in self._accounts.values():
            result.extend(accounts)
        return result

    # ── serialisation (for Settings) ─────────────────────────────────────────

    def to_settings_dict(self) -> Dict[str, List[Dict]]:
        out: Dict[str, List[Dict]] = {}
        for provider, accounts in self._accounts.items():
            out[provider.value] = [
                {"name": a.name, **a.params} for a in accounts
            ]
        return out

    @classmethod
    def from_settings_dict(cls, data: Dict[str, List[Dict]]) -> "AccountManager":
        mgr = cls()
        provider_map = {p.value: p for p in ProviderName}
        for provider_str, accounts in data.items():
            provider = provider_map.get(provider_str)
            if not provider:
                continue
            for acc_dict in accounts:
                name = acc_dict.pop("name", provider_str)
                mgr.register(AccountEntry(name=name, provider=provider, params=acc_dict))
        return mgr
