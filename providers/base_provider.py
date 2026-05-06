"""
providers/base_provider.py
──────────────────────────
Abstract interface every cloud provider manager must implement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from models.resource import CloudResource


LogCallback = Callable[[str], None]   # simple str → None log emitter


class CloudProvider(ABC):
    """
    Base class for all cloud provider managers.

    Each concrete subclass must implement all abstract methods.
    The `log` callback is injected at construction time and is called from
    background threads — it must be connected to a Qt signal for thread safety.

    Self-description attributes (used by the plugin system):
        provider_name          : display name, e.g. "AWS"
        icon_path              : relative path to a 48×48 icon PNG
        supported_resource_types : list of ResourceType values this provider handles
    """

    provider_name:           str  = "Unknown"
    icon_path:               str  = ""
    supported_resource_types: list = []

    def __init__(self, log_callback: Optional[LogCallback] = None) -> None:
        self._log = log_callback or (lambda msg: None)

    # ── connection ────────────────────────────────────────────────────────────

    @abstractmethod
    def connect(self, credentials: object, test_connection: bool = True) -> bool:
        """
        Initialise SDK clients with the given credentials object.
        Returns True if the connection succeeds, False otherwise.
        """

    # ── regions ───────────────────────────────────────────────────────────────

    @abstractmethod
    def list_regions(self) -> list[str]:
        """Return all available region names for this provider."""

    # ── scanning ──────────────────────────────────────────────────────────────

    @abstractmethod
    def scan_comprehensive(self, region: str) -> list[CloudResource]:
        """
        Mode A — quick orphan scan.
        Returns only unattached / unused resources (unattached disks, free IPs, …).
        """

    @abstractmethod
    def scan_hierarchical(self, region: str) -> list[CloudResource]:
        """
        Mode B — full hierarchical scan.
        Returns all resources with parent_id set to build the tree.
        """

    # ── deletion ──────────────────────────────────────────────────────────────

    @abstractmethod
    def delete_resource(self, resource: CloudResource, dry_run: bool = False) -> str:
        """
        Delete a single resource.
        Returns a human-readable result string.
        Raises an exception on hard failure.
        """

    # ── helpers ───────────────────────────────────────────────────────────────

    def get_deletion_order(self, resources: list[CloudResource]) -> list[CloudResource]:
        """
        Default implementation delegates to the dependency graph utility.
        Providers may override to inject provider-specific pre-steps
        (e.g., stripping AWS SG rules before deletion).
        """
        from utils.dependency_graph import get_safe_deletion_order
        return get_safe_deletion_order(resources)

    def _emit(self, msg: str) -> None:
        self._log(msg)

    def _age(self, creation_time: object) -> int:
        """Calculate age in days from a datetime object or ISO string."""
        if not creation_time:
            return 0
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            
            # 1. Handle datetime objects
            if isinstance(creation_time, datetime):
                dt = creation_time
            # 2. Handle numeric timestamps (seconds)
            elif isinstance(creation_time, (int, float)):
                dt = datetime.fromtimestamp(creation_time, tz=timezone.utc)
            # 3. Handle ISO strings and variations
            else:
                s = str(creation_time).strip()
                if not s: return 0
                
                # Standard ISO fix
                s = s.replace("Z", "+00:00")
                try:
                    dt = datetime.fromisoformat(s)
                except ValueError:
                    # Try common cloud formats: YYYY-MM-DDTHH:MM:SSZ
                    try:
                        from dateutil import parser # type: ignore
                        dt = parser.parse(s)
                    except Exception:
                        return 0

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
                
            diff = now - dt
            return max(0, diff.days)
        except Exception:
            return 0

    def _get_age(self, obj: object) -> int:
        """Robustly extract age from common cloud provider timestamp attributes."""
        if not obj: return 0
        # Common attribute names across AWS, Azure, GCP, Alibaba, Oracle
        attrs = [
            "creation_time", "create_time", "CreationTime", "CreateTime",
            "time_created", "created_time", "TimeCreated", "CreatedTime",
            "creation_timestamp", "CreationTimestamp", "gmt_create",
            "LaunchTime", "CreationDate", "creationDate"
        ]
        
        # Handle dictionary-like objects (AWS/GCP)
        if isinstance(obj, dict):
            for attr in attrs:
                if attr in obj:
                    return self._age(obj[attr])
            return 0
            
        # Handle SDK objects (Alibaba/Azure/Oracle)
        for attr in attrs:
            val = getattr(obj, attr, None)
            if val:
                return self._age(val)
                
        # Try properties attribute (Azure)
        props = getattr(obj, "properties", None)
        if props:
            for attr in attrs:
                val = getattr(props, attr, None)
                if val: return self._age(val)

        return 0
