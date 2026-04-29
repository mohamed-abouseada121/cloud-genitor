"""
providers/azure_manager.py
──────────────────────────
Azure provider: Resource Groups, VNETs, VMs, Managed Disks, Public IPs, NICs.

Deletion order (inside-out):
  Layer 1: VMs
  Layer 2: NICs, Managed Disks, Public IPs
  Layer 4: NSGs, Subnets
  Layer 5: VNETs, Resource Groups
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from models.resource import CloudResource, ProviderName, ResourceType
from providers.base_provider import CloudProvider
from utils.credentials import AzureCredentials
from utils.pagination import azure_paginate

log = logging.getLogger(__name__)


class AzureManager(CloudProvider):

    provider_name = "Azure"
    icon_path     = "assets/icons/azure.png"
    supported_resource_types = [
        ResourceType.RESOURCE_GROUP, ResourceType.VNET, ResourceType.SUBNET,
        ResourceType.INSTANCE, ResourceType.DISK, ResourceType.PUBLIC_IP,
        ResourceType.NIC, ResourceType.NSG,
    ]

    def __init__(self, log_callback=None) -> None:
        super().__init__(log_callback)
        self._credential: Any    = None
        self._subscription_id: str = ""
        self._compute_client: Any  = None
        self._network_client: Any  = None
        self._resource_client: Any = None

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self, credentials: AzureCredentials) -> bool:
        try:
            from azure.identity import DefaultAzureCredential                       # type: ignore
            from azure.mgmt.compute import ComputeManagementClient                  # type: ignore
            from azure.mgmt.network import NetworkManagementClient                  # type: ignore
            from azure.mgmt.resource import ResourceManagementClient                # type: ignore

            self._subscription_id = credentials.subscription_id
            self._credential      = DefaultAzureCredential()
            self._compute_client  = ComputeManagementClient(self._credential, self._subscription_id)
            self._network_client  = NetworkManagementClient(self._credential, self._subscription_id)
            self._resource_client = ResourceManagementClient(self._credential, self._subscription_id)

            # Connectivity check
            list(self._resource_client.resource_groups.list())
            self._emit(f"[Azure] Connected — subscription={self._subscription_id}")
            return True
        except Exception as exc:
            self._emit(f"[Azure] Connection failed: {exc}")
            return False

    # ── regions (Azure calls them locations) ──────────────────────────────────

    def list_regions(self) -> list[str]:
        from azure.mgmt.resource import SubscriptionClient  # type: ignore
        sub_client = SubscriptionClient(self._credential)
        locs = sub_client.subscriptions.list_locations(self._subscription_id)
        return sorted(loc.name for loc in locs)

    # ── scanning ──────────────────────────────────────────────────────────────

    def scan_comprehensive(self, region: str) -> list[CloudResource]:
        resources: list[CloudResource] = []
        resources.extend(self._scan_unattached_disks())
        resources.extend(self._scan_free_public_ips())
        self._emit(f"[Azure] Comprehensive scan done — {len(resources)} orphans found")
        return resources

    def scan_hierarchical(self, region: str) -> list[CloudResource]:
        resources: list[CloudResource] = []
        rgs = self._scan_resource_groups()
        resources.extend(rgs)
        for rg in rgs:
            resources.extend(self._scan_vnets(rg.resource_id))
            resources.extend(self._scan_subnets_for_rg(rg.resource_id))
            resources.extend(self._scan_vms(rg.resource_id))
            resources.extend(self._scan_nics(rg.resource_id))
            resources.extend(self._scan_disks(rg.resource_id))
            resources.extend(self._scan_public_ips(rg.resource_id))
            resources.extend(self._scan_nsgs(rg.resource_id))
        self._emit(f"[Azure] Hierarchical scan done — {len(resources)} resources found")
        return resources

    # ── deletion ──────────────────────────────────────────────────────────────

    def delete_resource(self, resource: CloudResource, dry_run: bool = False) -> str:
        prefix = "[DRY RUN] " if dry_run else ""
        self._emit(f"{prefix}[Azure] Deleting {resource.resource_type.value}: {resource.display_name}")
        if dry_run:
            return f"DRY RUN: would delete {resource.resource_type.value} {resource.resource_id}"

        rg = resource.metadata.get("resource_group", "")
        handlers = {
            ResourceType.INSTANCE:      lambda: self._delete_vm(rg, resource.display_name),
            ResourceType.DISK:          lambda: self._delete_disk(rg, resource.display_name),
            ResourceType.PUBLIC_IP:     lambda: self._delete_public_ip(rg, resource.display_name),
            ResourceType.NIC:           lambda: self._delete_nic(rg, resource.display_name),
            ResourceType.NSG:           lambda: self._delete_nsg(rg, resource.display_name),
            ResourceType.SUBNET:        lambda: self._delete_subnet(rg, resource),
            ResourceType.VNET:          lambda: self._delete_vnet(rg, resource.display_name),
            ResourceType.RESOURCE_GROUP: lambda: self._delete_resource_group(resource.display_name),
        }
        handler = handlers.get(resource.resource_type)
        if handler:
            return handler()
        return f"No deletion handler for {resource.resource_type.value}"

    # ── scan helpers ──────────────────────────────────────────────────────────

    def _make_resource(self, rid, name, rtype, rg_id=None,
                       status="", metadata=None, layer=1,
                       estimated_cost=0.0, tags=None) -> CloudResource:
        meta = metadata or {}
        meta["resource_group"] = rg_id or ""
        return CloudResource(
            resource_id=rid, name=name, resource_type=rtype,
            provider=ProviderName.AZURE, region="",
            parent_id=rg_id, deletion_layer=layer,
            status=status, estimated_cost=estimated_cost,
            metadata=meta, tags=tags or {},
        )

    def _scan_resource_groups(self) -> list[CloudResource]:
        resources = []
        for rg in azure_paginate(self._resource_client.resource_groups.list()):
            resources.append(self._make_resource(
                rg.name, rg.name, ResourceType.RESOURCE_GROUP,
                status=rg.properties.provisioning_state if rg.properties else "",
                metadata={"location": rg.location},
                layer=5, tags=rg.tags or {},
            ))
        return resources

    def _scan_vnets(self, rg: str) -> list[CloudResource]:
        resources = []
        for vnet in azure_paginate(self._network_client.virtual_networks.list(rg)):
            resources.append(self._make_resource(
                vnet.name, vnet.name, ResourceType.VNET,
                rg_id=rg, status="",
                metadata={"resource_group": rg, "location": vnet.location},
                layer=5, tags=vnet.tags or {},
            ))
        return resources

    def _scan_subnets_for_rg(self, rg: str) -> list[CloudResource]:
        resources = []
        for vnet in azure_paginate(self._network_client.virtual_networks.list(rg)):
            for subnet in (vnet.subnets or []):
                resources.append(self._make_resource(
                    subnet.name, subnet.name, ResourceType.SUBNET,
                    rg_id=rg, status="",
                    metadata={"resource_group": rg, "vnet": vnet.name},
                    layer=4,
                ))
        return resources

    def _scan_vms(self, rg: str) -> list[CloudResource]:
        resources = []
        for vm in azure_paginate(self._compute_client.virtual_machines.list(rg)):
            resources.append(self._make_resource(
                vm.name, vm.name, ResourceType.INSTANCE,
                rg_id=rg, status="",
                metadata={"resource_group": rg, "location": vm.location},
                layer=1, tags=vm.tags or {},
            ))
        return resources

    def _scan_disks(self, rg: str) -> list[CloudResource]:
        resources = []
        for disk in azure_paginate(self._compute_client.disks.list_by_resource_group(rg)):
            attached = disk.managed_by is not None
            size_gb  = disk.disk_size_gb or 0
            cost     = round(size_gb * 0.040, 4)   # ~$0.04/GB/month (Premium SSD approx)
            resources.append(self._make_resource(
                disk.name, disk.name, ResourceType.DISK,
                rg_id=rg,
                status="attached" if attached else "unattached",
                metadata={"resource_group": rg, "managed_by": disk.managed_by},
                layer=2, estimated_cost=cost if not attached else 0.0,
                tags=disk.tags or {},
            ))
        return resources

    def _scan_unattached_disks(self) -> list[CloudResource]:
        resources = []
        for disk in azure_paginate(self._compute_client.disks.list()):
            if disk.managed_by is None:
                size_gb = disk.disk_size_gb or 0
                cost    = round(size_gb * 0.040, 4)
                rg      = disk.id.split("/")[4] if disk.id else ""
                resources.append(self._make_resource(
                    disk.name, disk.name, ResourceType.DISK,
                    rg_id=rg, status="unattached",
                    metadata={"resource_group": rg},
                    layer=2, estimated_cost=cost,
                    tags=disk.tags or {},
                ))
        return resources

    def _scan_public_ips(self, rg: str) -> list[CloudResource]:
        resources = []
        for ip in azure_paginate(self._network_client.public_ip_addresses.list(rg)):
            free = ip.ip_configuration is None
            resources.append(self._make_resource(
                ip.name, ip.name, ResourceType.PUBLIC_IP,
                rg_id=rg, status="free" if free else "associated",
                metadata={"resource_group": rg, "address": ip.ip_address},
                layer=2, estimated_cost=3.65 if free else 0.0,
                tags=ip.tags or {},
            ))
        return resources

    def _scan_free_public_ips(self) -> list[CloudResource]:
        resources = []
        for ip in azure_paginate(self._network_client.public_ip_addresses.list_all()):
            if ip.ip_configuration is None:
                rg = ip.id.split("/")[4] if ip.id else ""
                resources.append(self._make_resource(
                    ip.name, ip.name, ResourceType.PUBLIC_IP,
                    rg_id=rg, status="free",
                    metadata={"resource_group": rg},
                    layer=2, estimated_cost=3.65,
                    tags=ip.tags or {},
                ))
        return resources

    def _scan_nics(self, rg: str) -> list[CloudResource]:
        resources = []
        for nic in azure_paginate(self._network_client.network_interfaces.list(rg)):
            resources.append(self._make_resource(
                nic.name, nic.name, ResourceType.NIC,
                rg_id=rg, status="",
                metadata={"resource_group": rg},
                layer=2, tags=nic.tags or {},
            ))
        return resources

    def _scan_nsgs(self, rg: str) -> list[CloudResource]:
        resources = []
        for nsg in azure_paginate(self._network_client.network_security_groups.list(rg)):
            resources.append(self._make_resource(
                nsg.name, nsg.name, ResourceType.NSG,
                rg_id=rg, status="",
                metadata={"resource_group": rg},
                layer=4, tags=nsg.tags or {},
            ))
        return resources

    # ── delete helpers ────────────────────────────────────────────────────────

    def _delete_vm(self, rg: str, name: str) -> str:
        poller = self._compute_client.virtual_machines.begin_delete(rg, name)
        poller.result()
        return f"Deleted VM {name}"

    def _delete_disk(self, rg: str, name: str) -> str:
        poller = self._compute_client.disks.begin_delete(rg, name)
        poller.result()
        return f"Deleted Disk {name}"

    def _delete_public_ip(self, rg: str, name: str) -> str:
        poller = self._network_client.public_ip_addresses.begin_delete(rg, name)
        poller.result()
        return f"Deleted Public IP {name}"

    def _delete_nic(self, rg: str, name: str) -> str:
        poller = self._network_client.network_interfaces.begin_delete(rg, name)
        poller.result()
        return f"Deleted NIC {name}"

    def _delete_nsg(self, rg: str, name: str) -> str:
        poller = self._network_client.network_security_groups.begin_delete(rg, name)
        poller.result()
        return f"Deleted NSG {name}"

    def _delete_subnet(self, rg: str, resource: CloudResource) -> str:
        vnet = resource.metadata.get("vnet", "")
        poller = self._network_client.subnets.begin_delete(rg, vnet, resource.display_name)
        poller.result()
        return f"Deleted Subnet {resource.display_name}"

    def _delete_vnet(self, rg: str, name: str) -> str:
        poller = self._network_client.virtual_networks.begin_delete(rg, name)
        poller.result()
        return f"Deleted VNET {name}"

    def _delete_resource_group(self, name: str) -> str:
        poller = self._resource_client.resource_groups.begin_delete(name)
        poller.result()
        return f"Deleted Resource Group {name}"
