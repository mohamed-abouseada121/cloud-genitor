"""
plugins/example_provider_template.py
──────────────────────────────────────
Template for adding a third-party cloud provider as a drop-in plugin.

Instructions
────────────
1. Copy this file to plugins/my_provider.py
2. Rename MyProviderManager to something descriptive (e.g., DigitalOceanManager)
3. Fill in all abstract methods
4. The plugin is auto-discovered on application startup — no other changes needed.

The application loader scans this directory for any .py file containing a class
that subclasses CloudProvider and registers it automatically.
"""

from __future__ import annotations

from models.resource import CloudResource, ProviderName, ResourceType
from providers.base_provider import CloudProvider


class MyProviderManager(CloudProvider):
    """
    Example third-party provider plugin.
    Replace 'MyProvider' with your provider's name throughout.
    """

    # ── Self-description (REQUIRED) ───────────────────────────────────────────

    provider_name             = "MyProvider"   # display name in the sidebar
    icon_path                 = "plugins/icons/myprovider.png"
    supported_resource_types  = [
        ResourceType.INSTANCE,
        ResourceType.DISK,
        # add ResourceType values that this provider supports
    ]

    def __init__(self, log_callback=None) -> None:
        super().__init__(log_callback)
        # initialise your SDK client reference here (set in connect())
        self._client = None

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, credentials: object) -> bool:
        """
        Initialise the SDK client with the given credentials.
        credentials is whatever object your provider needs (dataclass, dict, etc.)
        Return True on success, False on failure.
        """
        try:
            # Example:
            # import my_sdk
            # self._client = my_sdk.Client(api_key=credentials.api_key)
            self._emit("[MyProvider] Connected.")
            return True
        except Exception as exc:
            self._emit(f"[MyProvider] Connection failed: {exc}")
            return False

    # ── Regions ───────────────────────────────────────────────────────────────

    def list_regions(self) -> list[str]:
        """Return a list of region/datacenter identifiers."""
        # Example: return self._client.list_regions()
        return ["nyc1", "ams3", "sgp1"]

    # ── Scanning ──────────────────────────────────────────────────────────────

    def scan_comprehensive(self, region: str) -> list[CloudResource]:
        """Return only orphaned / unused resources in this region."""
        resources: list[CloudResource] = []
        # TODO: call your API, filter orphaned items, append CloudResource objects
        return resources

    def scan_hierarchical(self, region: str) -> list[CloudResource]:
        """Return all resources with parent_id for tree display."""
        resources: list[CloudResource] = []
        # TODO: populate parent/child relationships via parent_id
        return resources

    # ── Deletion ──────────────────────────────────────────────────────────────

    def delete_resource(self, resource: CloudResource, dry_run: bool = False) -> str:
        """
        Delete a single resource.
        Must raise an exception on failure so the DeleteWorker can handle it.
        """
        if dry_run:
            return f"DRY RUN: would delete {resource.resource_type.value} {resource.resource_id}"

        # TODO: self._client.delete(resource.resource_id)
        return f"Deleted {resource.resource_type.value} {resource.resource_id}"
