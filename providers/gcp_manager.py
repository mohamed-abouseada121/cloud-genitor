"""
providers/gcp_manager.py
────────────────────────
GCP provider: Projects, VPC Networks, Subnetworks, Compute Instances, Disks, Firewall Rules.

All GCP operations are async — every delete returns an Operation that must be polled.
Uses aggregated_list() to discover resources across all zones without iterating zones.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from models.resource import CloudResource, ProviderName, ResourceType
from providers.base_provider import CloudProvider
from utils.credentials import GCPCredentials
from utils.pagination import gcp_paginate

log = logging.getLogger(__name__)

_POLL_INTERVAL = 5   # seconds between operation status checks
_POLL_TIMEOUT  = 300 # max seconds to wait for an operation


class GCPManager(CloudProvider):

    provider_name = "GCP"
    icon_path     = "assets/icons/gcp.png"
    supported_resource_types = [
        ResourceType.VPC, ResourceType.SUBNET, ResourceType.INSTANCE,
        ResourceType.DISK, ResourceType.FIREWALL_RULE, ResourceType.PROJECT,
    ]

    def __init__(self, log_callback=None) -> None:
        super().__init__(log_callback)
        self._project_id: str  = ""
        self._credentials: Any = None
        self._instances_client:   Any = None
        self._disks_client:       Any = None
        self._networks_client:    Any = None
        self._subnetworks_client: Any = None
        self._firewalls_client:   Any = None
        self._zone_ops_client:    Any = None
        self._region_ops_client:  Any = None
        self._global_ops_client:  Any = None

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self, credentials: GCPCredentials) -> bool:
        try:
            from google.cloud import compute_v1  # type: ignore
            from utils.credentials import CredentialManager

            self._project_id  = credentials.project_id
            self._credentials = CredentialManager().get_gcp_credentials(credentials)

            self._instances_client   = compute_v1.InstancesClient(credentials=self._credentials)
            self._disks_client       = compute_v1.DisksClient(credentials=self._credentials)
            self._networks_client    = compute_v1.NetworksClient(credentials=self._credentials)
            self._subnetworks_client = compute_v1.SubnetworksClient(credentials=self._credentials)
            self._firewalls_client   = compute_v1.FirewallsClient(credentials=self._credentials)
            self._zone_ops_client    = compute_v1.ZoneOperationsClient(credentials=self._credentials)
            self._region_ops_client  = compute_v1.RegionOperationsClient(credentials=self._credentials)
            self._global_ops_client  = compute_v1.GlobalOperationsClient(credentials=self._credentials)

            self._emit(f"[GCP] Connected — project={self._project_id}")
            return True
        except Exception as exc:
            self._emit(f"[GCP] Connection failed: {exc}")
            return False

    # ── regions ───────────────────────────────────────────────────────────────

    def list_regions(self) -> list[str]:
        from google.cloud import compute_v1  # type: ignore
        client = compute_v1.RegionsClient(credentials=self._credentials)
        regions = []
        for region in gcp_paginate(client.list, project=self._project_id):
            regions.append(region.name)
        return sorted(regions)

    # ── scanning ──────────────────────────────────────────────────────────────

    def scan_comprehensive(self, region: str) -> list[CloudResource]:
        resources: list[CloudResource] = []
        resources.extend(self._scan_unattached_disks())
        resources.extend(self._scan_terminated_instances())
        self._emit(f"[GCP] Comprehensive scan done — {len(resources)} orphans found")
        return resources

    def scan_hierarchical(self, region: str) -> list[CloudResource]:
        resources: list[CloudResource] = []
        resources.extend(self._scan_networks())
        resources.extend(self._scan_subnetworks())
        resources.extend(self._scan_all_instances())
        resources.extend(self._scan_all_disks())
        resources.extend(self._scan_firewalls())
        self._emit(f"[GCP] Hierarchical scan done — {len(resources)} resources")
        return resources

    # ── deletion ──────────────────────────────────────────────────────────────

    def delete_resource(self, resource: CloudResource, dry_run: bool = False) -> str:
        prefix = "[DRY RUN] " if dry_run else ""
        self._emit(f"{prefix}[GCP] Deleting {resource.resource_type.value}: {resource.display_name}")
        if dry_run:
            return f"DRY RUN: would delete {resource.resource_type.value} {resource.resource_id}"

        rt   = resource.resource_type
        name = resource.resource_id
        zone = resource.metadata.get("zone", "")
        rg   = resource.metadata.get("region", "")

        if rt == ResourceType.INSTANCE:
            op = self._instances_client.delete(project=self._project_id, zone=zone, instance=name)
            self._wait_zone_op(op.name, zone)
            return f"Deleted instance {name}"

        if rt == ResourceType.DISK:
            op = self._disks_client.delete(project=self._project_id, zone=zone, disk=name)
            self._wait_zone_op(op.name, zone)
            return f"Deleted disk {name}"

        if rt == ResourceType.FIREWALL_RULE:
            op = self._firewalls_client.delete(project=self._project_id, firewall=name)
            self._wait_global_op(op.name)
            return f"Deleted firewall rule {name}"

        if rt == ResourceType.SUBNET:
            op = self._subnetworks_client.delete(
                project=self._project_id, region=rg, subnetwork=name)
            self._wait_region_op(op.name, rg)
            return f"Deleted subnetwork {name}"

        if rt == ResourceType.VPC:
            op = self._networks_client.delete(project=self._project_id, network=name)
            self._wait_global_op(op.name)
            return f"Deleted network {name}"

        return f"No deletion handler for {rt.value}"

    # ── scan helpers ──────────────────────────────────────────────────────────

    def _make_resource(self, rid, name, rtype, parent_id=None,
                       status="", metadata=None, layer=1,
                       estimated_cost=0.0) -> CloudResource:
        return CloudResource(
            resource_id=rid, name=name, resource_type=rtype,
            provider=ProviderName.GCP, region=self._project_id,
            parent_id=parent_id, deletion_layer=layer,
            status=status, estimated_cost=estimated_cost,
            metadata=metadata or {},
        )

    @staticmethod
    def _zone_from_url(url: str) -> str:
        parts = url.split("/")
        for i, part in enumerate(parts):
            if part == "zones" and i + 1 < len(parts):
                return parts[i + 1]
        return ""

    @staticmethod
    def _region_from_url(url: str) -> str:
        parts = url.split("/")
        for i, part in enumerate(parts):
            if part == "regions" and i + 1 < len(parts):
                return parts[i + 1]
        return ""

    def _scan_networks(self) -> list[CloudResource]:
        resources = []
        for net in gcp_paginate(self._networks_client.list, project=self._project_id):
            resources.append(self._make_resource(
                net.name, net.name, ResourceType.VPC,
                status="", metadata={}, layer=5,
            ))
        return resources

    def _scan_subnetworks(self) -> list[CloudResource]:
        resources = []
        for item in gcp_paginate(self._subnetworks_client.aggregated_list,
                                  project=self._project_id):
            # aggregated_list yields (scope_name, scoped_list) tuples
            scope_name, scoped_list = item
            for sn in (scoped_list.subnetworks or []):
                region = self._region_from_url(sn.region)
                net    = sn.network.split("/")[-1] if sn.network else ""
                resources.append(self._make_resource(
                    sn.name, sn.name, ResourceType.SUBNET,
                    parent_id=net, status="",
                    metadata={"region": region, "network": net},
                    layer=4,
                ))
        return resources

    def _scan_all_instances(self) -> list[CloudResource]:
        resources = []
        for item in gcp_paginate(self._instances_client.aggregated_list,
                                  project=self._project_id):
            scope_name, scoped_list = item
            for inst in (scoped_list.instances or []):
                zone   = self._zone_from_url(inst.zone)
                status = inst.status   # RUNNING, TERMINATED, STOPPED, …
                resources.append(self._make_resource(
                    inst.name, inst.name, ResourceType.INSTANCE,
                    status=status,
                    metadata={"zone": zone},
                    layer=1,
                ))
        return resources

    def _scan_terminated_instances(self) -> list[CloudResource]:
        return [r for r in self._scan_all_instances()
                if r.status in ("TERMINATED", "STOPPED")]

    def _scan_all_disks(self) -> list[CloudResource]:
        resources = []
        for item in gcp_paginate(self._disks_client.aggregated_list,
                                  project=self._project_id):
            scope_name, scoped_list = item
            for disk in (scoped_list.disks or []):
                zone    = self._zone_from_url(disk.zone)
                users   = list(disk.users or [])
                size_gb = disk.size_gb or 0
                cost    = round(size_gb * 0.040, 4)
                resources.append(self._make_resource(
                    disk.name, disk.name, ResourceType.DISK,
                    status="unattached" if not users else "attached",
                    metadata={"zone": zone, "users": users},
                    layer=2, estimated_cost=cost if not users else 0.0,
                ))
        return resources

    def _scan_unattached_disks(self) -> list[CloudResource]:
        return [r for r in self._scan_all_disks() if r.status == "unattached"]

    def _scan_firewalls(self) -> list[CloudResource]:
        resources = []
        for fw in gcp_paginate(self._firewalls_client.list, project=self._project_id):
            net = fw.network.split("/")[-1] if fw.network else ""
            resources.append(self._make_resource(
                fw.name, fw.name, ResourceType.FIREWALL_RULE,
                parent_id=net,
                status="ALLOW" if fw.allowed else "DENY",
                metadata={"network": net},
                layer=4,
            ))
        return resources

    # ── operation waiters ─────────────────────────────────────────────────────

    def _wait_zone_op(self, op_name: str, zone: str) -> None:
        deadline = time.monotonic() + _POLL_TIMEOUT
        while time.monotonic() < deadline:
            op = self._zone_ops_client.get(
                project=self._project_id, zone=zone, operation=op_name)
            if op.status.name == "DONE":
                return
            time.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"Zone operation {op_name} did not complete within {_POLL_TIMEOUT}s")

    def _wait_region_op(self, op_name: str, region: str) -> None:
        deadline = time.monotonic() + _POLL_TIMEOUT
        while time.monotonic() < deadline:
            op = self._region_ops_client.get(
                project=self._project_id, region=region, operation=op_name)
            if op.status.name == "DONE":
                return
            time.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"Region operation {op_name} did not complete within {_POLL_TIMEOUT}s")

    def _wait_global_op(self, op_name: str) -> None:
        deadline = time.monotonic() + _POLL_TIMEOUT
        while time.monotonic() < deadline:
            op = self._global_ops_client.get(
                project=self._project_id, operation=op_name)
            if op.status.name == "DONE":
                return
            time.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"Global operation {op_name} did not complete within {_POLL_TIMEOUT}s")
