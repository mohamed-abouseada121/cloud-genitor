"""
providers/oracle_manager.py
───────────────────────────
Oracle Cloud Infrastructure provider:
Compartments, VCNs, Subnets, Instances, Block Volumes, Security Lists, Route Tables, Gateways.

Deletion order (inside-out):
  Layer 1: Compute Instances, Boot Volumes (after instance termination)
  Layer 2: Block Volumes, VNICs
  Layer 3: Internet / NAT / Service Gateways
  Layer 4: Subnets, Security Lists, Route Tables, NSGs
  Layer 5: VCN, Compartment (rarely deleted)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from models.resource import CloudResource, ProviderName, ResourceType
from providers.base_provider import CloudProvider
from utils.credentials import OracleCredentials
from utils.pagination import oci_paginate

log = logging.getLogger(__name__)

_POLL_INTERVAL = 10
_POLL_TIMEOUT  = 600


class OracleManager(CloudProvider):

    provider_name = "Oracle"
    icon_path     = "assets/icons/oracle.png"
    supported_resource_types = [
        ResourceType.COMPARTMENT, ResourceType.VCN, ResourceType.SUBNET,
        ResourceType.INSTANCE, ResourceType.BLOCK_VOLUME,
        ResourceType.INTERNET_GATEWAY, ResourceType.NAT_GATEWAY,
        ResourceType.ROUTE_TABLE, ResourceType.SECURITY_GROUP,
    ]

    def __init__(self, log_callback=None) -> None:
        super().__init__(log_callback)
        self._config:          Optional[dict] = None
        self._identity:        Any = None
        self._compute:         Any = None
        self._block_storage:   Any = None
        self._virtual_network: Any = None
        self._tenancy_id:      str = ""

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self, credentials: OracleCredentials) -> bool:
        try:
            import oci  # type: ignore
            from utils.credentials import CredentialManager

            self._config = CredentialManager().get_oci_config(credentials)
            self._tenancy_id = self._config["tenancy"]

            self._identity        = oci.identity.IdentityClient(self._config)
            self._compute         = oci.core.ComputeClient(self._config)
            self._block_storage   = oci.core.BlockstorageClient(self._config)
            self._virtual_network = oci.core.VirtualNetworkClient(self._config)

            self._emit(f"[Oracle] Connected — tenancy={self._tenancy_id}")
            return True
        except Exception as exc:
            self._emit(f"[Oracle] Connection failed: {exc}")
            return False

    # ── regions ───────────────────────────────────────────────────────────────

    def list_regions(self) -> list[str]:
        import oci  # type: ignore
        regions = oci.pagination.list_call_get_all_results(
            self._identity.list_region_subscriptions, self._tenancy_id
        ).data
        return sorted(r.region_name for r in regions)

    # ── scanning ──────────────────────────────────────────────────────────────

    def scan_comprehensive(self, region: str) -> list[CloudResource]:
        resources: list[CloudResource] = []
        compartments = self._list_all_compartments()
        for comp_id in [c.resource_id for c in compartments]:
            resources.extend(self._scan_free_volumes(comp_id))
        self._emit(f"[Oracle] Comprehensive scan done — {len(resources)} orphans found")
        return resources

    def scan_hierarchical(self, region: str) -> list[CloudResource]:
        resources: list[CloudResource] = []
        compartments = self._list_all_compartments()
        resources.extend(compartments)
        for comp in compartments:
            cid = comp.resource_id
            resources.extend(self._scan_vcns(cid))
            resources.extend(self._scan_subnets(cid))
            resources.extend(self._scan_instances(cid))
            resources.extend(self._scan_volumes(cid))
            resources.extend(self._scan_internet_gateways(cid))
            resources.extend(self._scan_nat_gateways(cid))
            resources.extend(self._scan_route_tables(cid))
            resources.extend(self._scan_security_lists(cid))
        self._emit(f"[Oracle] Hierarchical scan done — {len(resources)} resources found")
        return resources

    # ── deletion ──────────────────────────────────────────────────────────────

    def delete_resource(self, resource: CloudResource, dry_run: bool = False) -> str:
        prefix = "[DRY RUN] " if dry_run else ""
        self._emit(f"{prefix}[Oracle] Deleting {resource.resource_type.value}: {resource.display_name}")
        if dry_run:
            return f"DRY RUN: would delete {resource.resource_type.value} {resource.resource_id}"

        rt  = resource.resource_type
        rid = resource.resource_id

        if rt == ResourceType.INSTANCE:
            self._compute.terminate_instance(rid)
            self._wait_lifecycle(
                lambda: self._compute.get_instance(rid).data.lifecycle_state,
                "TERMINATED",
            )
            return f"Terminated instance {rid}"

        if rt == ResourceType.BLOCK_VOLUME:
            self._block_storage.delete_volume(rid)
            return f"Deleted block volume {rid}"

        if rt == ResourceType.INTERNET_GATEWAY:
            self._virtual_network.delete_internet_gateway(rid)
            return f"Deleted Internet Gateway {rid}"

        if rt == ResourceType.NAT_GATEWAY:
            self._virtual_network.delete_nat_gateway(rid)
            return f"Deleted NAT Gateway {rid}"

        if rt == ResourceType.SUBNET:
            self._virtual_network.delete_subnet(rid)
            return f"Deleted Subnet {rid}"

        if rt == ResourceType.ROUTE_TABLE:
            self._virtual_network.delete_route_table(rid)
            return f"Deleted Route Table {rid}"

        if rt == ResourceType.SECURITY_GROUP:   # Security List
            self._virtual_network.delete_security_list(rid)
            return f"Deleted Security List {rid}"

        if rt == ResourceType.VCN:
            self._virtual_network.delete_vcn(rid)
            return f"Deleted VCN {rid}"

        if rt == ResourceType.COMPARTMENT:
            self._identity.delete_compartment(rid)
            return f"Deleted Compartment {rid}"

        return f"No deletion handler for {rt.value}"

    # ── scan helpers ──────────────────────────────────────────────────────────

    def _make_resource(self, rid, name, rtype, parent_id=None,
                       status="", metadata=None, layer=1,
                       estimated_cost=0.0) -> CloudResource:
        return CloudResource(
            resource_id=rid, name=name, resource_type=rtype,
            provider=ProviderName.ORACLE, region=self._config.get("region", "") if self._config else "",
            parent_id=parent_id, deletion_layer=layer,
            status=status, estimated_cost=estimated_cost,
            metadata=metadata or {},
        )

    def _list_all_compartments(self) -> list[CloudResource]:
        import oci  # type: ignore
        resp = oci.pagination.list_call_get_all_results(
            self._identity.list_compartments,
            self._tenancy_id,
            compartment_id_in_subtree=True,
        )
        resources = [self._make_resource(
            self._tenancy_id, "Root Tenancy",
            ResourceType.COMPARTMENT, layer=5,
        )]
        for comp in resp.data:
            if comp.lifecycle_state == "ACTIVE":
                resources.append(self._make_resource(
                    comp.id, comp.name,
                    ResourceType.COMPARTMENT,
                    parent_id=comp.compartment_id,
                    status=comp.lifecycle_state,
                    layer=5,
                ))
        return resources

    def _scan_vcns(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        for vcn in oci_paginate(self._virtual_network, "list_vcns",
                                compartment_id=compartment_id):
            if vcn.lifecycle_state not in ("TERMINATED", "TERMINATING"):
                resources.append(self._make_resource(
                    vcn.id, vcn.display_name,
                    ResourceType.VCN, parent_id=compartment_id,
                    status=vcn.lifecycle_state, layer=5,
                ))
        return resources

    def _scan_subnets(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        for sn in oci_paginate(self._virtual_network, "list_subnets",
                               compartment_id=compartment_id):
            if sn.lifecycle_state not in ("TERMINATED", "TERMINATING"):
                resources.append(self._make_resource(
                    sn.id, sn.display_name,
                    ResourceType.SUBNET, parent_id=sn.vcn_id,
                    status=sn.lifecycle_state, layer=4,
                ))
        return resources

    def _scan_instances(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        for inst in oci_paginate(self._compute, "list_instances",
                                 compartment_id=compartment_id):
            if inst.lifecycle_state not in ("TERMINATED", "TERMINATING"):
                resources.append(self._make_resource(
                    inst.id, inst.display_name,
                    ResourceType.INSTANCE, parent_id=compartment_id,
                    status=inst.lifecycle_state, layer=1,
                    metadata={"shape": inst.shape},
                ))
        return resources

    def _scan_volumes(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        for vol in oci_paginate(self._block_storage, "list_volumes",
                                compartment_id=compartment_id):
            if vol.lifecycle_state not in ("TERMINATED", "TERMINATING"):
                size_gb = vol.size_in_gbs or 0
                cost    = round(size_gb * 0.0255, 4)  # ~$0.0255/GB/month
                resources.append(self._make_resource(
                    vol.id, vol.display_name,
                    ResourceType.BLOCK_VOLUME, parent_id=compartment_id,
                    status=vol.lifecycle_state, layer=2,
                    estimated_cost=cost if vol.lifecycle_state == "AVAILABLE" else 0.0,
                ))
        return resources

    def _scan_free_volumes(self, compartment_id: str) -> list[CloudResource]:
        return [r for r in self._scan_volumes(compartment_id)
                if r.status == "AVAILABLE"]

    def _scan_internet_gateways(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        for igw in oci_paginate(self._virtual_network, "list_internet_gateways",
                                compartment_id=compartment_id):
            resources.append(self._make_resource(
                igw.id, igw.display_name,
                ResourceType.INTERNET_GATEWAY,
                parent_id=igw.vcn_id,
                status=igw.lifecycle_state, layer=3,
            ))
        return resources

    def _scan_nat_gateways(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        for nat in oci_paginate(self._virtual_network, "list_nat_gateways",
                                compartment_id=compartment_id):
            resources.append(self._make_resource(
                nat.id, nat.display_name,
                ResourceType.NAT_GATEWAY,
                parent_id=nat.vcn_id,
                status=nat.lifecycle_state, layer=3,
            ))
        return resources

    def _scan_route_tables(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        for rt in oci_paginate(self._virtual_network, "list_route_tables",
                               compartment_id=compartment_id):
            resources.append(self._make_resource(
                rt.id, rt.display_name,
                ResourceType.ROUTE_TABLE,
                parent_id=rt.vcn_id,
                status=rt.lifecycle_state, layer=4,
            ))
        return resources

    def _scan_security_lists(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        for sl in oci_paginate(self._virtual_network, "list_security_lists",
                               compartment_id=compartment_id):
            resources.append(self._make_resource(
                sl.id, sl.display_name,
                ResourceType.SECURITY_GROUP,
                parent_id=sl.vcn_id,
                status=sl.lifecycle_state, layer=4,
            ))
        return resources

    # ── waiter ───────────────────────────────────────────────────────────────

    def _wait_lifecycle(self, state_fn, target: str, timeout: int = _POLL_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if state_fn() == target:
                return
            time.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"Resource did not reach {target} within {timeout}s")
