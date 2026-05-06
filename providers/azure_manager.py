"""
providers/azure_manager.py
──────────────────────────
Azure provider: Resource Groups, VNETs, VMs, Managed Disks, Public IPs, NICs,
Blob Storage, Application Gateways, Azure Load Balancers.

Deletion order (inside-out):
  Layer 1: VMs, Load Balancers (App Gateway + Azure LB)
  Layer 2: NICs, Managed Disks, Public IPs, Blob Storage Accounts
  Layer 4: NSGs, Subnets
  Layer 5: VNETs, Resource Groups
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional, Dict

from models.resource import CloudResource, ProviderName, ResourceType
from providers.base_provider import CloudProvider
from utils.credentials import AzureCredentials, CredentialManager
from utils.pagination import azure_paginate

# Azure SDK Imports
try:
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.resource import ResourceManagementClient
    from azure.mgmt.sql import SqlManagementClient
    from azure.mgmt.storage import StorageManagementClient
    from azure.mgmt.containerservice import ContainerServiceClient
    from azure.mgmt.cosmosdb import CosmosDBManagementClient
    from azure.mgmt.redis import RedisManagementClient
    from azure.mgmt.servicebus import ServiceBusManagementClient
    from azure.mgmt.dns import DnsManagementClient
    from azure.mgmt.cdn import CdnManagementClient
    from azure.mgmt.web import WebSiteManagementClient
    from azure.mgmt.containerregistry import ContainerRegistryManagementClient
    from azure.mgmt.containerinstance import ContainerInstanceManagementClient
    from azure.mgmt.keyvault import KeyVaultManagementClient
    from azure.mgmt.monitor import MonitorManagementClient
except ImportError:
    pass

log = logging.getLogger(__name__)


class AzureManager(CloudProvider):

    provider_name = "Azure"
    icon_path     = "assets/icons/azure.png"
    supported_resource_types = [
        ResourceType.RESOURCE_GROUP, ResourceType.VNET, ResourceType.SUBNET,
        ResourceType.INSTANCE, ResourceType.DISK, ResourceType.PUBLIC_IP,
        ResourceType.CLOUD_SQL,
        ResourceType.KUBERNETES, ResourceType.KUBERNETES_NODE_GROUP, ResourceType.NAT_GATEWAY,
        # Phase 1
        ResourceType.BLOB_STORAGE,
        ResourceType.APP_GATEWAY, ResourceType.AZURE_LB,
        # Phase 2
        ResourceType.AZURE_FUNCTION, ResourceType.ACR_REPO,
        ResourceType.SERVICE_BUS, ResourceType.AZURE_DNS, ResourceType.AZURE_CDN,
        # Phase 3
        ResourceType.CONTAINER_INSTANCE, ResourceType.KEY_VAULT, ResourceType.AZURE_MONITOR_ALERT,
    ]

    _scan_lock = threading.Lock()
    _cached_resources: list[CloudResource] | None = None
    _cache_time = 0.0

    def __init__(self, log_callback=None) -> None:
        super().__init__(log_callback)
        self._credential: Any    = None
        self._subscription_id: str = ""
        self._compute_client: Any  = None
        self._network_client: Any  = None
        self._resource_client: Any = None
        self._sql_client: Any      = None
        self._aks_client: Any      = None
        self._storage_client: Any  = None
        self._cosmos_client: Any   = None
        self._redis_client: Any    = None
        self._servicebus_client: Any = None
        self._dns_client: Any      = None
        self._cdn_client: Any      = None
        self._web_client: Any      = None
        self._acr_client: Any      = None
        self._container_client: Any= None
        self._keyvault_client: Any = None
        self._monitor_client: Any  = None
        
        # Silence noisy Azure identity logs
        import logging
        logging.getLogger("azure.identity").setLevel(logging.ERROR)
        logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.ERROR)

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self, credentials: AzureCredentials, test_connection: bool = True) -> bool:
        try:
            from utils.credentials import CredentialManager
            from azure.mgmt.compute import ComputeManagementClient                  # type: ignore
            from azure.mgmt.network import NetworkManagementClient                  # type: ignore
            from azure.mgmt.resource import ResourceManagementClient                # type: ignore
            from azure.mgmt.sql import SqlManagementClient                          # type: ignore
            from azure.mgmt.storage import StorageManagementClient                  # type: ignore

            self._subscription_id = credentials.subscription_id
            self._credential      = CredentialManager().get_azure_credential(credentials)
            self._compute_client  = ComputeManagementClient(self._credential, self._subscription_id)
            self._network_client  = NetworkManagementClient(self._credential, self._subscription_id)
            self._resource_client = ResourceManagementClient(self._credential, self._subscription_id)
            self._sql_client      = SqlManagementClient(self._credential, self._subscription_id)
            self._storage_client  = StorageManagementClient(self._credential, self._subscription_id)

            try:
                from azure.mgmt.containerservice import ContainerServiceClient          # type: ignore
                self._aks_client      = ContainerServiceClient(self._credential, self._subscription_id)
            except ImportError as e:
                self._emit(f"[Azure] Warning: {e}")
                self._aks_client = None


            # Phase 2 clients (best-effort)
            try:
                from azure.mgmt.cosmosdb import CosmosDBManagementClient           # type: ignore
                self._cosmos_client = CosmosDBManagementClient(self._credential, self._subscription_id)
            except ImportError: pass
            try:
                from azure.mgmt.redis import RedisManagementClient                 # type: ignore
                self._redis_client = RedisManagementClient(self._credential, self._subscription_id)
            except ImportError: pass
            try:
                from azure.mgmt.servicebus import ServiceBusManagementClient       # type: ignore
                self._servicebus_client = ServiceBusManagementClient(self._credential, self._subscription_id)
            except ImportError: pass
            try:
                from azure.mgmt.dns import DnsManagementClient                    # type: ignore
                self._dns_client = DnsManagementClient(self._credential, self._subscription_id)
            except ImportError: pass
            try:
                from azure.mgmt.cdn import CdnManagementClient                    # type: ignore
                self._cdn_client = CdnManagementClient(self._credential, self._subscription_id)
            except ImportError: pass
            try:
                from azure.mgmt.web import WebSiteManagementClient                 # type: ignore
                self._web_client = WebSiteManagementClient(self._credential, self._subscription_id)
            except ImportError: pass
            try:
                from azure.mgmt.containerregistry import ContainerRegistryManagementClient  # type: ignore
                self._acr_client = ContainerRegistryManagementClient(self._credential, self._subscription_id)
            except ImportError: pass
            
            # Phase 3 clients
            try:
                from azure.mgmt.containerinstance import ContainerInstanceManagementClient  # type: ignore
                self._container_client = ContainerInstanceManagementClient(self._credential, self._subscription_id)
            except ImportError: pass
            try:
                from azure.mgmt.keyvault import KeyVaultManagementClient  # type: ignore
                self._keyvault_client = KeyVaultManagementClient(self._credential, self._subscription_id)
            except ImportError: pass
            try:
                from azure.mgmt.monitor import MonitorManagementClient  # type: ignore
                self._monitor_client = MonitorManagementClient(self._credential, self._subscription_id)
            except ImportError: pass

            # Connectivity check
            import logging
            logging.getLogger("azure.identity").setLevel(logging.ERROR)
            logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.ERROR)
            
            list(self._resource_client.resource_groups.list())
            self._emit(f"[Azure] Connected — subscription={self._subscription_id}")
            return True
        except Exception as exc:
            # Clean up the error message to avoid 100-line traces in the UI
            short_msg = str(exc).split("\n")[0]
            self._emit(f"[Azure] Connection failed: {short_msg} (Check credentials)")
            return False

    # ── regions (Azure calls them locations) ──────────────────────────────────

    def list_regions(self) -> list[str]:
        from azure.mgmt.resource import SubscriptionClient  # type: ignore
        sub_client = SubscriptionClient(self._credential)
        locs = sub_client.subscriptions.list_locations(self._subscription_id)
        return sorted(loc.name for loc in locs)

    # ── scanning ──────────────────────────────────────────────────────────────

    def scan_comprehensive(self, region: str) -> list[CloudResource]:
        return self.scan_hierarchical(region)

    def scan_hierarchical(self, region: str) -> list[CloudResource]:
        if not self._resource_client:
            return []
        import time
        with AzureManager._scan_lock:
            # Cache the entire subscription scan for 60 seconds
            if AzureManager._cached_resources is None or (time.time() - AzureManager._cache_time > 60.0):
                self._emit("[Azure] Fetching global subscription resources to cache...")
                resources: list[CloudResource] = []
                rgs = self._scan_resource_groups()
                resources.extend(rgs)
                
                from concurrent.futures import ThreadPoolExecutor, as_completed
                
                def _fetch_rg(rg):
                    local_res = []
                    local_res.extend(self._scan_vnets(rg.resource_id))
                    local_res.extend(self._scan_subnets_for_rg(rg.resource_id))
                    local_res.extend(self._scan_vms(rg.resource_id))
                    local_res.extend(self._scan_nics(rg.resource_id))
                    local_res.extend(self._scan_disks(rg.resource_id))
                    local_res.extend(self._scan_public_ips(rg.resource_id))
                    local_res.extend(self._scan_nsgs(rg.resource_id))
                    local_res.extend(self._scan_sql_servers(rg.resource_id))
                    local_res.extend(self._scan_aks_clusters(rg.resource_id))
                    local_res.extend(self._scan_nat_gateways(rg.resource_id))
                    local_res.extend(self._scan_blob_storage(rg.resource_id))
                    local_res.extend(self._scan_app_gateways(rg.resource_id))
                    local_res.extend(self._scan_azure_load_balancers(rg.resource_id))
                    # Phase 2
                    local_res.extend(self._scan_azure_functions(rg.resource_id))
                    local_res.extend(self._scan_acr_repos(rg.resource_id))
                    local_res.extend(self._scan_cosmos_accounts(rg.resource_id))
                    local_res.extend(self._scan_azure_redis(rg.resource_id))
                    local_res.extend(self._scan_service_bus(rg.resource_id))
                    local_res.extend(self._scan_azure_dns(rg.resource_id))
                    # Phase 3
                    local_res.extend(self._scan_container_instances(rg.resource_id))
                    local_res.extend(self._scan_key_vaults(rg.resource_id))
                    local_res.extend(self._scan_azure_monitor_alerts(rg.resource_id))
                    return local_res

                with ThreadPoolExecutor(max_workers=10) as executor:
                    futures = [executor.submit(_fetch_rg, rg) for rg in rgs]
                    for future in as_completed(futures):
                        try:
                            resources.extend(future.result())
                        except Exception as e:
                            self._emit(f"[Azure] Error scanning resource group: {e}")
                            
                AzureManager._cached_resources = resources
                AzureManager._cache_time = time.time()
                self._emit(f"[Azure] Cached {len(resources)} global resources.")

        # Filter the cached resources by the requested region and ensure uniqueness
        unique_map = {}
        if AzureManager._cached_resources:
            for r in AzureManager._cached_resources:
                if r.metadata.get("location") == region:
                    if r.resource_id not in unique_map:
                        unique_map[r.resource_id] = r
        
        filtered = list(unique_map.values())
        self._emit(f"[Azure] Returned {len(filtered)} resources for region '{region}' from cache.")
        return filtered


    # ── deletion ──────────────────────────────────────────────────────────────

    def delete_resource(self, resource: CloudResource, dry_run: bool = False) -> str:
        prefix = "[DRY RUN] " if dry_run else ""
        self._emit(f"{prefix}[Azure] Deleting {resource.resource_type.value}: {resource.display_name}")
        if dry_run:
            return f"DRY RUN: would delete {resource.resource_type.value} {resource.resource_id}"

        handlers = {
            ResourceType.INSTANCE:       self._delete_vm,
            ResourceType.DISK:           self._delete_disk,
            ResourceType.PUBLIC_IP:      self._delete_public_ip,
            ResourceType.NIC:            self._delete_nic,
            ResourceType.NSG:            self._delete_nsg,
            ResourceType.SUBNET:         self._delete_subnet,
            ResourceType.VNET:           self._delete_vnet,
            ResourceType.RESOURCE_GROUP: self._delete_resource_group,
            ResourceType.CLOUD_SQL:      self._delete_sql_server,
            ResourceType.KUBERNETES:     self._delete_aks,
            ResourceType.KUBERNETES_NODE_GROUP: self._delete_aks_agent_pool,
            ResourceType.NAT_GATEWAY:    self._delete_nat_gw,
            ResourceType.BLOB_STORAGE:   self._delete_storage_account,
            ResourceType.APP_GATEWAY:    self._delete_app_gateway,
            ResourceType.AZURE_LB:       self._delete_azure_lb,
            ResourceType.AZURE_FUNCTION: self._delete_azure_function,
            ResourceType.ACR_REPO:       self._delete_acr,
            ResourceType.COSMOS_DB:      self._delete_cosmos,
            ResourceType.AZURE_REDIS:    self._delete_redis,
            ResourceType.SERVICE_BUS:    self._delete_service_bus,
            ResourceType.AZURE_DNS:      self._delete_azure_dns,
            ResourceType.CONTAINER_INSTANCE: self._delete_container_instance,
            ResourceType.KEY_VAULT:      self._delete_key_vault,
            ResourceType.AZURE_MONITOR_ALERT: self._delete_azure_monitor_alert,
        }
        handler = handlers.get(resource.resource_type)
        if handler:
            try:
                return handler(resource)
            except Exception as exc:
                if "ResourceNotFound" in str(exc) or "404" in str(exc):
                    self._emit(f"[Azure] Resource {resource.display_name} already deleted.")
                    return f"Already deleted: {resource.display_name}"
                raise exc

        return f"No deletion handler for {resource.resource_type.value}"

    # ── scan helpers ──────────────────────────────────────────────────────────

    def _make_resource(self, rid, name, rtype, rg_id=None,
                       status="", metadata=None, layer=1,
                       estimated_cost=0.0, age_days=0, tags=None) -> CloudResource:
        meta = metadata or {}
        meta["resource_group"] = rg_id or ""
        return CloudResource(
            resource_id=rid, name=name, resource_type=rtype,
            provider=ProviderName.AZURE, region=meta.get("location", ""),
            parent_id=rg_id, deletion_layer=layer,
            status=status, estimated_cost=estimated_cost,
            age_days=age_days,
            metadata=meta, tags=tags or {},
        )

    def _scan_resource_groups(self) -> list[CloudResource]:
        resources = []
        for rg in azure_paginate(self._resource_client.resource_groups.list()):
            resources.append(self._make_resource(
                rg.name, rg.name, ResourceType.RESOURCE_GROUP,
                rg_id=rg.name,   # so resource_id == parent_id used by children
                status=rg.properties.provisioning_state if rg.properties else "",
                metadata={"location": rg.location, "resource_group": rg.name},
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
                rg_id=rg, status=vm.provisioning_state or "Running",
                metadata={"resource_group": rg, "location": vm.location, "vm_size": vm.hardware_profile.vm_size if vm.hardware_profile else ""},
                layer=1,
                estimated_cost=45.0, # Average B2s/D2s VM cost
                age_days=self._get_age(vm),
                tags=vm.tags or {},
            ))
        return resources

    def _scan_disks(self, rg: str) -> list[CloudResource]:
        resources = []
        for disk in azure_paginate(self._compute_client.disks.list_by_resource_group(rg)):
            attached = disk.managed_by is not None
            size_gb  = disk.disk_size_gb or 0
            cost     = round(size_gb * 0.040, 4)   # ~$0.04/GB/month (Premium SSD approx)
            
            deps = []
            if disk.managed_by:
                # ManagedBy is the VM resource ID. We need the VM name (rid).
                # Azure IDs: /subscriptions/.../resourceGroups/.../providers/Microsoft.Compute/virtualMachines/NAME
                deps.append(disk.managed_by.split("/")[-1])

            resources.append(self._make_resource(
                disk.name, disk.name, ResourceType.DISK,
                rg_id=rg,
                status="attached" if attached else "unattached",
                metadata={"resource_group": rg, "managed_by": disk.managed_by, "location": disk.location},
                layer=2, 
                estimated_cost=cost if not attached else 0.0,
                age_days=self._get_age(disk),
                tags=disk.tags or {},
            ))
            resources[-1].dependencies = deps
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
                metadata={"resource_group": rg, "address": ip.ip_address, "location": ip.location},
                layer=2, 
                estimated_cost=3.65 if free else 0.0,
                age_days=self._get_age(ip),
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
            deps = []
            if nic.virtual_machine:
                deps.append(nic.virtual_machine.id.split("/")[-1])

            resources.append(self._make_resource(
                nic.name, nic.name, ResourceType.NIC,
                rg_id=rg, status="",
                metadata={"resource_group": rg},
                layer=2, tags=nic.tags or {},
            ))
            resources[-1].dependencies = deps
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

    def _scan_sql_servers(self, rg: str) -> list[CloudResource]:
        resources = []
        if not self._sql_client: return []
        try:
            for server in azure_paginate(self._sql_client.servers.list_by_resource_group(rg)):
                resources.append(self._make_resource(
                    server.name, server.name, ResourceType.CLOUD_SQL,
                    rg_id=rg, status=server.state or "Ready",
                    metadata={"resource_group": rg, "version": server.version},
                    layer=1, tags=server.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning SQL: {exc}")
        return resources

    def _scan_aks_clusters(self, rg: str) -> list[CloudResource]:
        resources = []
        if not self._aks_client: return []
        try:
            for cluster in azure_paginate(self._aks_client.managed_clusters.list_by_resource_group(rg)):
                resources.append(self._make_resource(
                    cluster.name, cluster.name, ResourceType.KUBERNETES,
                    rg_id=rg, status=cluster.provisioning_state,
                    metadata={"resource_group": rg, "location": cluster.location},
                    layer=2, estimated_cost=73.0,
                    age_days=self._get_age(cluster),
                    tags=cluster.tags or {},
                ))
                
                # Scan Agent Pools (Node Groups)
                try:
                    for pool in azure_paginate(self._aks_client.agent_pools.list(rg, cluster.name)):
                        resources.append(self._make_resource(
                            f"{cluster.name}-{pool.name}", pool.name, ResourceType.KUBERNETES_NODE_GROUP,
                            parent_id=cluster.name, status=pool.provisioning_state,
                            metadata={"resource_group": rg, "cluster_name": cluster.name},
                            layer=1,
                        ))
                except Exception as e:
                    self._emit(f"[Azure] Warning scanning AKS Agent Pools for {cluster.name}: {e}")

        except Exception as exc:
            self._emit(f"[Azure] Error scanning AKS: {exc}")
        return resources

    def _scan_nat_gateways(self, rg: str) -> list[CloudResource]:
        resources = []
        if not self._network_client: return []
        try:
            for nat in azure_paginate(self._network_client.nat_gateways.list(rg)):
                resources.append(self._make_resource(
                    nat.name, nat.name, ResourceType.NAT_GATEWAY,
                    rg_id=rg, status=nat.provisioning_state,
                    metadata={"resource_group": rg, "location": nat.location},
                    layer=3, tags=nat.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning NAT: {exc}")
        return resources

    # ── Blob Storage (Storage Accounts) ───────────────────────────────────

    def _scan_blob_storage(self, rg: str) -> list[CloudResource]:
        """Scan Azure Storage Accounts (Blob/Table/Queue/File)."""
        resources = []
        if not self._storage_client:
            return []
        try:
            for acct in azure_paginate(
                self._storage_client.storage_accounts.list_by_resource_group(rg)
            ):
                # Estimate cost: Hot tier Blob ~$0.018/GB; storage accounts have minimum ~$5/month
                cost = 5.0  # base cost; size requires extra blob metrics API call
                resources.append(self._make_resource(
                    acct.name, acct.name, ResourceType.BLOB_STORAGE,
                    rg_id=rg,
                    status=acct.provisioning_state or "Succeeded",
                    metadata={
                        "resource_group": rg,
                        "location":        acct.location,
                        "kind":            acct.kind,
                        "sku":             acct.sku.name if acct.sku else "",
                    },
                    layer=2,
                    estimated_cost=cost,
                    age_days=self._get_age(acct),
                    tags=acct.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning Blob Storage: {exc}")
        return resources

    # ── Application Gateways ────────────────────────────────────────────

    def _scan_app_gateways(self, rg: str) -> list[CloudResource]:
        """Scan Application Gateways (WAF/v2)."""
        resources = []
        if not self._network_client:
            return []
        try:
            for agw in azure_paginate(
                self._network_client.application_gateways.list(rg)
            ):
                resources.append(self._make_resource(
                    agw.name, agw.name, ResourceType.APP_GATEWAY,
                    rg_id=rg,
                    status=agw.provisioning_state or "",
                    metadata={"resource_group": rg, "location": agw.location},
                    layer=1,
                    estimated_cost=125.0,  # App Gateway v2 ~$125/month base
                    tags=agw.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning Application Gateways: {exc}")
        return resources

    # ── Azure Load Balancers ─────────────────────────────────────────────

    def _scan_azure_load_balancers(self, rg: str) -> list[CloudResource]:
        """Scan Azure Load Balancers (Standard / Basic)."""
        resources = []
        if not self._network_client:
            return []
        try:
            for lb in azure_paginate(
                self._network_client.load_balancers.list(rg)
            ):
                sku_name = lb.sku.name if lb.sku else "Basic"
                # Standard LB: ~$18/month base; Basic: free but limited
                cost = 18.0 if sku_name == "Standard" else 0.0
                resources.append(self._make_resource(
                    lb.name, lb.name, ResourceType.AZURE_LB,
                    rg_id=rg,
                    status=lb.provisioning_state or "",
                    metadata={
                        "resource_group": rg,
                        "location":        lb.location,
                        "sku":             sku_name,
                    },
                    layer=1,
                    estimated_cost=cost,
                    tags=lb.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning Azure Load Balancers: {exc}")
        return resources

    # ── delete helpers ────────────────────────────────────────────────────────

    def _delete_vm(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._compute_client.virtual_machines.begin_delete(rg, r.display_name)
        poller.result()
        return f"Deleted VM {r.display_name}"

    def _delete_disk(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._compute_client.disks.begin_delete(rg, r.display_name)
        poller.result()
        return f"Deleted Disk {r.display_name}"

    def _delete_public_ip(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._network_client.public_ip_addresses.begin_delete(rg, r.display_name)
        poller.result()
        return f"Deleted Public IP {r.display_name}"

    def _delete_nic(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._network_client.network_interfaces.begin_delete(rg, r.display_name)
        poller.result()
        return f"Deleted NIC {r.display_name}"

    def _delete_nsg(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._network_client.network_security_groups.begin_delete(rg, r.display_name)
        poller.result()
        return f"Deleted NSG {r.display_name}"

    def _delete_subnet(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        vnet = r.metadata.get("vnet", "")
        poller = self._network_client.subnets.begin_delete(rg, vnet, r.display_name)
        poller.result()
        return f"Deleted Subnet {r.display_name}"

    def _delete_vnet(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._network_client.virtual_networks.begin_delete(rg, r.display_name)
        poller.result()
        return f"Deleted VNET {r.display_name}"

    def _delete_resource_group(self, r: CloudResource) -> str:
        poller = self._resource_client.resource_groups.begin_delete(r.display_name)
        poller.result()
        return f"Deleted Resource Group {r.display_name}"

    def _delete_sql_server(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._sql_client.servers.begin_delete(rg, r.display_name)
        poller.result()
        return f"Initiated SQL Server deletion {r.display_name}"

    def _delete_aks(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._aks_client.managed_clusters.begin_delete(rg, r.display_name)
        poller.result()
        return f"Initiated AKS cluster deletion {r.display_name}"

    def _delete_aks_agent_pool(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        cluster_name = r.metadata.get("cluster_name", "")
        poller = self._aks_client.agent_pools.begin_delete(rg, cluster_name, r.display_name)
        poller.result()
        return f"Initiated AKS Agent Pool deletion {r.display_name}"

    def _delete_nat_gw(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._network_client.nat_gateways.begin_delete(rg, r.display_name)
        poller.result()
        return f"Deleted NAT Gateway {r.display_name}"

    def _delete_storage_account(self, r: CloudResource) -> str:
        """Delete an Azure Storage Account (Blob/Table/Queue/File)."""
        rg = r.metadata.get("resource_group", "")
        self._storage_client.storage_accounts.delete(rg, r.display_name)
        return f"Deleted Storage Account {r.display_name}"

    def _delete_app_gateway(self, r: CloudResource) -> str:
        """Delete an Application Gateway."""
        rg = r.metadata.get("resource_group", "")
        poller = self._network_client.application_gateways.begin_delete(rg, r.display_name)
        poller.result()
        return f"Deleted Application Gateway {r.display_name}"

    def _delete_azure_lb(self, r: CloudResource) -> str:
        """Delete an Azure Load Balancer."""
        rg = r.metadata.get("resource_group", "")
        poller = self._network_client.load_balancers.begin_delete(rg, r.display_name)
        poller.result()
        return f"Deleted Azure Load Balancer {r.display_name}"

    # ── Phase 2: Azure Functions ────────────────────────────────────────────

    def _scan_azure_functions(self, rg: str) -> list[CloudResource]:
        resources = []
        if not self._web_client: return []
        try:
            for app in azure_paginate(self._web_client.web_apps.list_by_resource_group(rg)):
                if app.kind and "functionapp" in app.kind.lower():
                    resources.append(self._make_resource(
                        app.name, app.name, ResourceType.AZURE_FUNCTION,
                        rg_id=rg, status=app.state or "",
                        metadata={"resource_group": rg, "location": app.location, "kind": app.kind},
                        layer=1, estimated_cost=0.0,  # Azure Functions: pay-per-execution
                        tags=app.tags or {},
                    ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning Functions: {exc}")
        return resources

    def _delete_azure_function(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._web_client.web_apps.begin_delete(rg, r.display_name)
        if hasattr(poller, 'result'): poller.result()
        return f"Deleted Azure Function App {r.display_name}"

    # ── Phase 2: ACR Repositories ────────────────────────────────────────────

    def _scan_acr_repos(self, rg: str) -> list[CloudResource]:
        resources = []
        if not self._acr_client: return []
        try:
            for reg in azure_paginate(self._acr_client.registries.list_by_resource_group(rg)):
                resources.append(self._make_resource(
                    reg.name, reg.name, ResourceType.ACR_REPO,
                    rg_id=rg, status=reg.provisioning_state or "",
                    metadata={"resource_group": rg, "location": reg.location,
                              "sku": reg.sku.name if reg.sku else ""},
                    layer=1, estimated_cost=5.0,  # ACR Basic ~$5/month
                    tags=reg.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning ACR: {exc}")
        return resources

    def _delete_acr(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._acr_client.registries.begin_delete(rg, r.display_name)
        poller.result()
        return f"Deleted ACR registry {r.display_name}"

    # ── Phase 2: CosmosDB Accounts ──────────────────────────────────────────

    def _scan_cosmos_accounts(self, rg: str) -> list[CloudResource]:
        resources = []
        if not self._cosmos_client: return []
        try:
            for acct in azure_paginate(
                self._cosmos_client.database_accounts.list_by_resource_group(rg)
            ):
                resources.append(self._make_resource(
                    acct.name, acct.name, ResourceType.COSMOS_DB,
                    rg_id=rg, status=acct.provisioning_state or "",
                    metadata={"resource_group": rg, "location": acct.location,
                              "kind": acct.kind},
                    layer=1, estimated_cost=24.0,  # CosmosDB ~$24/month minimum (serverless)
                    tags=acct.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning CosmosDB: {exc}")
        return resources

    def _delete_cosmos(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._cosmos_client.database_accounts.begin_delete(rg, r.display_name)
        poller.result()
        return f"Deleted CosmosDB account {r.display_name}"

    # ── Phase 2: Azure Redis Cache ────────────────────────────────────────────

    def _scan_azure_redis(self, rg: str) -> list[CloudResource]:
        resources = []
        if not self._redis_client: return []
        try:
            for cache in azure_paginate(self._redis_client.redis.list_by_resource_group(rg)):
                resources.append(self._make_resource(
                    cache.name, cache.name, ResourceType.AZURE_REDIS,
                    rg_id=rg, status=cache.provisioning_state or "",
                    metadata={"resource_group": rg, "location": cache.location,
                              "sku": cache.sku.name if cache.sku else ""},
                    layer=1, estimated_cost=55.0,  # Redis Cache C1 ~$55/month
                    tags=cache.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning Redis Cache: {exc}")
        return resources

    def _delete_redis(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        poller = self._redis_client.redis.begin_delete(rg, r.display_name)
        poller.result()
        return f"Deleted Redis Cache {r.display_name}"

    # ── Phase 2: Azure Service Bus ───────────────────────────────────────────

    def _scan_service_bus(self, rg: str) -> list[CloudResource]:
        resources = []
        if not self._servicebus_client: return []
        try:
            for ns in azure_paginate(
                self._servicebus_client.namespaces.list_by_resource_group(rg)
            ):
                resources.append(self._make_resource(
                    ns.name, ns.name, ResourceType.SERVICE_BUS,
                    rg_id=rg, status=ns.provisioning_state or "",
                    metadata={"resource_group": rg, "location": ns.location,
                              "sku": ns.sku.name if ns.sku else ""},
                    layer=1, estimated_cost=10.0,  # Service Bus Basic ~$10/month
                    tags=ns.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning Service Bus: {exc}")
        return resources

    def _delete_service_bus(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        self._servicebus_client.namespaces.begin_delete(rg, r.display_name).result()
        return f"Deleted Service Bus namespace {r.display_name}"

    # ── Phase 2: Azure DNS Zones ──────────────────────────────────────────────

    def _scan_azure_dns(self, rg: str) -> list[CloudResource]:
        resources = []
        if not self._dns_client: return []
        try:
            for zone in azure_paginate(self._dns_client.zones.list_by_resource_group(rg)):
                resources.append(self._make_resource(
                    zone.name, zone.name, ResourceType.AZURE_DNS,
                    rg_id=rg, status="active",
                    metadata={"resource_group": rg, "record_sets": zone.number_of_record_sets},
                    layer=1, estimated_cost=0.50,  # Azure DNS: $0.50/zone/month
                    tags=zone.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning DNS: {exc}")
        return resources

    def _delete_azure_dns(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        self._dns_client.zones.begin_delete(rg, r.display_name).result()
        return f"Deleted Azure DNS zone {r.display_name}"

    # ── Phase 3: Container Instances ──────────────────────────────────────────

    def _scan_container_instances(self, rg: str) -> list[CloudResource]:
        resources = []
        if not self._container_client: return []
        try:
            for ci in azure_paginate(self._container_client.container_groups.list_by_resource_group(rg)):
                resources.append(self._make_resource(
                    ci.name, ci.name, ResourceType.CONTAINER_INSTANCE,
                    rg_id=rg, status=ci.provisioning_state or "",
                    metadata={"resource_group": rg, "location": ci.location},
                    layer=1, estimated_cost=0.0,
                    tags=ci.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning Container Instances: {exc}")
        return resources

    def _delete_container_instance(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        self._container_client.container_groups.begin_delete(rg, r.display_name).result()
        return f"Deleted Container Instance {r.display_name}"

    # ── Phase 3: Key Vault ────────────────────────────────────────────────────

    def _scan_key_vaults(self, rg: str) -> list[CloudResource]:
        resources = []
        if not self._keyvault_client: return []
        try:
            for kv in azure_paginate(self._keyvault_client.vaults.list_by_resource_group(rg)):
                resources.append(self._make_resource(
                    kv.name, kv.name, ResourceType.KEY_VAULT,
                    rg_id=rg, status="",
                    metadata={"resource_group": rg, "location": kv.location},
                    layer=1, estimated_cost=0.0,
                    tags=kv.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning Key Vaults: {exc}")
        return resources

    def _delete_key_vault(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        self._keyvault_client.vaults.delete(rg, r.display_name)
        return f"Deleted Key Vault {r.display_name}"

    # ── Phase 3: Azure Monitor Alerts ─────────────────────────────────────────

    def _scan_azure_monitor_alerts(self, rg: str) -> list[CloudResource]:
        resources = []
        if not self._monitor_client: return []
        try:
            # We scan metric alerts as an example
            for alert in azure_paginate(self._monitor_client.metric_alerts.list_by_resource_group(rg)):
                resources.append(self._make_resource(
                    alert.name, alert.name, ResourceType.AZURE_MONITOR_ALERT,
                    rg_id=rg, status="Enabled" if alert.enabled else "Disabled",
                    metadata={"resource_group": rg, "location": alert.location},
                    layer=1, estimated_cost=0.10,
                    tags=alert.tags or {},
                ))
        except Exception as exc:
            self._emit(f"[Azure] Error scanning Azure Monitor Alerts: {exc}")
        return resources

    def _delete_azure_monitor_alert(self, r: CloudResource) -> str:
        rg = r.metadata.get("resource_group", "")
        self._monitor_client.metric_alerts.delete(rg, r.display_name)
        return f"Deleted Azure Monitor Alert {r.display_name}"
