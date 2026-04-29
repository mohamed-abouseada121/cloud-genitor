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
    def connect(self, credentials: object) -> bool:
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
