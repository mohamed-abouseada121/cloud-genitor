"""
providers/oracle_manager.py
───────────────────────────
Oracle Cloud Infrastructure provider:
Compartments, VCNs, Subnets, Instances, Block Volumes, Security Lists,
Route Tables, Gateways, Object Storage Buckets, Load Balancers.

Deletion order (inside-out):
  Layer 1: Compute Instances, Boot Volumes (after instance termination), Load Balancers
  Layer 2: Block Volumes, VNICs, Object Storage Buckets
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
        ResourceType.KUBERNETES, ResourceType.CLOUD_SQL,
        # Phase 2
        ResourceType.OCI_BUCKET, ResourceType.ORACLE_LB,
        # Phase 3
        ResourceType.ORACLE_FUNCTION, ResourceType.ORACLE_NOSQL,
        ResourceType.KUBERNETES_NODE_GROUP,
    ]

    def __init__(self, log_callback=None) -> None:
        super().__init__(log_callback)
        self._config:          Optional[dict] = None
        self._identity:        Any = None
        self._compute:         Any = None
        self._block_storage:   Any = None
        self._virtual_network: Any = None
        self._database:        Any = None
        self._container_engine: Any = None
        self._object_storage:  Any = None
        self._load_balancer:   Any = None
        self._functions_client:Any = None
        self._nosql_client:    Any = None
        self._tenancy_id:      str = ""

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self, credentials: OracleCredentials) -> bool:
        try:
            import oci  # type: ignore
            from utils.credentials import CredentialManager

            self._config = CredentialManager().get_oci_config(credentials)
            self._tenancy_id = self._config["tenancy"]

            self._identity        = oci.identity.IdentityClient(self._config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
            self._compute         = oci.core.ComputeClient(self._config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
            self._block_storage   = oci.core.BlockstorageClient(self._config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
            self._virtual_network = oci.core.VirtualNetworkClient(self._config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
            self._database        = oci.database.DatabaseClient(self._config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
            self._container_engine = oci.container_engine.ContainerEngineClient(self._config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
            self._object_storage  = oci.object_storage.ObjectStorageClient(self._config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
            self._load_balancer   = oci.load_balancer.LoadBalancerClient(self._config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)

            # Phase 3 clients
            self._functions_client = oci.functions.FunctionsManagementClient(self._config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)
            self._nosql_client     = oci.nosql.NosqlClient(self._config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)

            self._emit(f"[Oracle] Connected — tenancy={self._tenancy_id}")
            return True
        except Exception as exc:
            self._emit(f"[Oracle] Connection failed: {exc}")
            return False

    # ── regions ───────────────────────────────────────────────────────────────

    def list_regions(self) -> list[str]:
        import oci  # type: ignore
        regions = self._identity.list_regions().data
        return sorted(r.name for r in regions)

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
            resources.extend(self._scan_nsgs(cid))
            resources.extend(self._scan_oke_clusters(cid))
            resources.extend(self._scan_databases(cid))
            resources.extend(self._scan_oci_buckets(cid))
            resources.extend(self._scan_oci_load_balancers(cid))
            # Phase 3
            resources.extend(self._scan_functions(cid))
            resources.extend(self._scan_nosql_tables(cid))
        self._emit(f"[Oracle] Hierarchical scan done — {len(resources)} resources found")
        return resources

    # ── deletion ──────────────────────────────────────────────────────────────

    def delete_resource(self, resource: CloudResource, dry_run: bool = False) -> str:
        prefix = "[DRY RUN] " if dry_run else ""
        self._emit(f"{prefix}[Oracle] Deleting {resource.resource_type.value}: {resource.display_name}")
        if dry_run:
            return f"DRY RUN: would delete {resource.resource_type.value} {resource.resource_id}"

        handlers = {
            ResourceType.INSTANCE:             self._delete_instance,
            ResourceType.BLOCK_VOLUME:         self._delete_block_volume,
            ResourceType.INTERNET_GATEWAY:     self._delete_internet_gateway,
            ResourceType.NAT_GATEWAY:          self._delete_nat_gateway,
            ResourceType.SUBNET:               self._delete_subnet,
            ResourceType.ROUTE_TABLE:          self._delete_route_table,
            ResourceType.SECURITY_GROUP:       self._delete_security_group,
            ResourceType.NSG:                  self._delete_nsg,
            ResourceType.VCN:                  self._delete_vcn,
            ResourceType.COMPARTMENT:          self._delete_compartment,
            ResourceType.KUBERNETES:           self._delete_oke_cluster,
            ResourceType.KUBERNETES_NODE_GROUP:self._delete_oke_node_pool,
            ResourceType.CLOUD_SQL:            self._delete_database,
            ResourceType.OCI_BUCKET:           self._delete_oci_bucket_wrapper,
            ResourceType.ORACLE_LB:            self._delete_load_balancer,
            ResourceType.ORACLE_FUNCTION:      self._delete_function,
            ResourceType.ORACLE_NOSQL:         self._delete_nosql,
        }

        handler = handlers.get(resource.resource_type)
        if not handler:
            return f"No deletion handler for {resource.resource_type.value}"

        try:
            return handler(resource)
        except Exception as exc:
            msg = str(exc).lower()
            if "notfound" in msg or "404" in msg or "not exist" in msg:
                self._emit(f"[Oracle] Resource {resource.resource_id} already deleted.")
                return f"Already deleted: {resource.resource_id}"
            raise exc

        return f"No deletion handler for {resource.resource_type.value}"

    # ── deletion helpers ──────────────────────────────────────────────────────

    def _delete_instance(self, r: CloudResource) -> str:
        self._compute.terminate_instance(r.resource_id)
        self._wait_lifecycle(lambda: self._compute.get_instance(r.resource_id).data.lifecycle_state, "TERMINATED")
        return f"Terminated instance {r.resource_id}"

    def _delete_block_volume(self, r: CloudResource) -> str:
        self._block_storage.delete_volume(r.resource_id)
        return f"Deleted block volume {r.resource_id}"

    def _delete_internet_gateway(self, r: CloudResource) -> str:
        self._virtual_network.delete_internet_gateway(r.resource_id)
        return f"Deleted Internet Gateway {r.resource_id}"

    def _delete_nat_gateway(self, r: CloudResource) -> str:
        self._virtual_network.delete_nat_gateway(r.resource_id)
        return f"Deleted NAT Gateway {r.resource_id}"

    def _delete_subnet(self, r: CloudResource) -> str:
        self._virtual_network.delete_subnet(r.resource_id)
        return f"Deleted Subnet {r.resource_id}"

    def _delete_route_table(self, r: CloudResource) -> str:
        self._virtual_network.delete_route_table(r.resource_id)
        return f"Deleted Route Table {r.resource_id}"

    def _delete_security_group(self, r: CloudResource) -> str:
        self._virtual_network.delete_security_list(r.resource_id)
        return f"Deleted Security List {r.resource_id}"

    def _delete_nsg(self, r: CloudResource) -> str:
        self._virtual_network.delete_network_security_group(r.resource_id)
        return f"Deleted NSG {r.resource_id}"

    def _delete_vcn(self, r: CloudResource) -> str:
        self._virtual_network.delete_vcn(r.resource_id)
        return f"Deleted VCN {r.resource_id}"

    def _delete_compartment(self, r: CloudResource) -> str:
        self._identity.delete_compartment(r.resource_id)
        return f"Deleted Compartment {r.resource_id}"

    def _delete_oke_cluster(self, r: CloudResource) -> str:
        self._container_engine.delete_cluster(r.resource_id)
        return f"Initiated OKE cluster deletion {r.resource_id}"

    def _delete_oke_node_pool(self, r: CloudResource) -> str:
        self._container_engine.delete_node_pool(r.resource_id)
        return f"Initiated OKE Node Pool deletion {r.resource_id}"

    def _delete_database(self, r: CloudResource) -> str:
        if "autonomous" in r.resource_id.lower():
            self._database.delete_autonomous_database(r.resource_id)
        else:
            self._database.terminate_db_system(r.resource_id)
        return f"Initiated Database termination {r.resource_id}"

    def _delete_oci_bucket_wrapper(self, r: CloudResource) -> str:
        return self._delete_oci_bucket(r.resource_id, r.metadata.get("namespace", ""))

    def _delete_load_balancer(self, r: CloudResource) -> str:
        self._load_balancer.delete_load_balancer(r.resource_id)
        return f"Initiated OCI Load Balancer deletion {r.resource_id}"

    def _delete_function(self, r: CloudResource) -> str:
        self._functions_client.delete_function(r.resource_id)
        return f"Deleted OCI Function {r.resource_id}"

    def _delete_nosql(self, r: CloudResource) -> str:
        self._nosql_client.delete_table(table_name_or_id=r.resource_id, compartment_id=r.parent_id)
        return f"Deleted OCI NoSQL Table {r.resource_id}"

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
            ResourceType.COMPARTMENT, layer=7,
        )]
        for comp in resp.data:
            if comp.lifecycle_state == "ACTIVE":
                resources.append(self._make_resource(
                    comp.id, comp.name,
                    ResourceType.COMPARTMENT,
                    parent_id=comp.compartment_id,
                    status=comp.lifecycle_state,
                    layer=7,
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
                    status=vcn.lifecycle_state, layer=6,
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
                    status=sn.lifecycle_state, layer=3,
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
                status=igw.lifecycle_state, layer=5,
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
                status=nat.lifecycle_state, layer=5,
            ))
        return resources

    def _scan_route_tables(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        for rt in oci_paginate(self._virtual_network, "list_route_tables",
                                compartment_id=compartment_id):
            # Skip default route table as it cannot be deleted independently
            if "Default Route Table" in (rt.display_name or ""):
                continue
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
            # Skip default security list as it cannot be deleted independently
            if "Default Security List" in (sl.display_name or ""):
                continue
            resources.append(self._make_resource(
                sl.id, sl.display_name,
                ResourceType.SECURITY_GROUP,
                parent_id=sl.vcn_id,
                status=sl.lifecycle_state, layer=4,
            ))
        return resources

    def _scan_nsgs(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        for nsg in oci_paginate(self._virtual_network, "list_network_security_groups",
                                 compartment_id=compartment_id):
            resources.append(self._make_resource(
                nsg.id, nsg.display_name,
                ResourceType.NSG,
                parent_id=nsg.vcn_id,
                status=nsg.lifecycle_state, layer=4,
            ))
        return resources

    def _scan_oke_clusters(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        if not self._container_engine: return []
        try:
            resp = self._container_engine.list_clusters(compartment_id=compartment_id)
            for cluster in resp.data:
                if cluster.lifecycle_state not in ("DELETED", "DELETING"):
                    resources.append(self._make_resource(
                        cluster.id, cluster.name, ResourceType.KUBERNETES,
                        parent_id=cluster.vcn_id, status=cluster.lifecycle_state, layer=2,
                    ))
                    
                    # Scan Node Pools
                    try:
                        pool_resp = self._container_engine.list_node_pools(compartment_id=compartment_id, cluster_id=cluster.id)
                        for pool in pool_resp.data:
                            if pool.lifecycle_state not in ("DELETED", "DELETING"):
                                resources.append(self._make_resource(
                                    pool.id, pool.name, ResourceType.KUBERNETES_NODE_GROUP,
                                    parent_id=cluster.id, status=pool.lifecycle_state, layer=1,
                                ))
                    except Exception as e:
                        self._emit(f"[Oracle] Warning scanning OKE Node Pools for {cluster.name}: {e}")
        except Exception as exc:
            self._emit(f"[Oracle] Error scanning OKE: {exc}")
        return resources

    def _scan_databases(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        if not self._database: return []
        try:
            # 1. Autonomous Databases
            adb = self._database.list_autonomous_databases(compartment_id=compartment_id).data
            for db in adb:
                if db.lifecycle_state not in ("TERMINATED", "TERMINATING"):
                    resources.append(self._make_resource(
                        db.id, db.display_name, ResourceType.CLOUD_SQL,
                        parent_id=compartment_id, status=db.lifecycle_state, layer=1,
                    ))
            # 2. DB Systems
            db_systems = self._database.list_db_systems(compartment_id=compartment_id).data
            for ds in db_systems:
                if ds.lifecycle_state not in ("TERMINATED", "TERMINATING"):
                    resources.append(self._make_resource(
                        ds.id, ds.display_name, ResourceType.CLOUD_SQL,
                        parent_id=compartment_id, status=ds.lifecycle_state, layer=1,
                    ))
        except Exception as exc:
            self._emit(f"[Oracle] Error scanning Databases: {exc}")
        return resources

    # ── Phase 2: OCI Object Storage Buckets ──────────────────────────────────

    def _scan_oci_buckets(self, compartment_id: str) -> list[CloudResource]:
        """List OCI Object Storage buckets in a compartment."""
        resources = []
        if not self._object_storage:
            return []
        try:
            namespace = self._object_storage.get_namespace().data
            for bucket in oci_paginate(
                self._object_storage, "list_buckets",
                namespace_name=namespace,
                compartment_id=compartment_id,
            ):
                resources.append(self._make_resource(
                    bucket.name, bucket.name, ResourceType.OCI_BUCKET,
                    parent_id=compartment_id,
                    status="available",
                    metadata={"namespace": namespace, "compartment_id": compartment_id},
                    layer=2,
                    estimated_cost=0.0,  # OCI Object Storage: ~$0.0255/GB/month
                ))
        except Exception as exc:
            self._emit(f"[Oracle] Error scanning OCI Buckets: {exc}")
        return resources

    def _delete_oci_bucket(self, bucket_name: str, namespace: str) -> str:
        """Delete an OCI Object Storage bucket - empties objects first."""
        if not self._object_storage:
            return "Object storage client not initialised"
        if not namespace:
            namespace = self._object_storage.get_namespace().data
        # Delete all objects first
        try:
            next_start = None
            while True:
                resp = self._object_storage.list_objects(
                    namespace_name=namespace, bucket_name=bucket_name,
                    start=next_start, limit=1000
                )
                for obj in resp.data.objects:
                    self._object_storage.delete_object(
                        namespace_name=namespace,
                        bucket_name=bucket_name,
                        object_name=obj.name,
                    )
                next_start = resp.data.next_start_with
                if not next_start:
                    break
        except Exception as exc:
            self._emit(f"[Oracle] Warning emptying bucket {bucket_name}: {exc}")
        self._object_storage.delete_bucket(
            namespace_name=namespace, bucket_name=bucket_name
        )
        return f"Deleted OCI bucket {bucket_name}"

    # ── Phase 2: OCI Load Balancers ───────────────────────────────────────────

    def _scan_oci_load_balancers(self, compartment_id: str) -> list[CloudResource]:
        """Scan OCI Load Balancers (flexible shape)."""
        resources = []
        if not self._load_balancer:
            return []
        try:
            for lb in oci_paginate(
                self._load_balancer, "list_load_balancers",
                compartment_id=compartment_id,
            ):
                if lb.lifecycle_state in ("DELETED", "DELETING"):
                    continue
                resources.append(self._make_resource(
                    lb.id, lb.display_name, ResourceType.ORACLE_LB,
                    parent_id=compartment_id,
                    status=lb.lifecycle_state,
                    metadata={"shape": lb.shape_name, "compartment_id": compartment_id},
                    layer=1,
                    estimated_cost=18.0,  # OCI LB flexible ~$18/month
                ))
        except Exception as exc:
            self._emit(f"[Oracle] Error scanning Load Balancers: {exc}")
        return resources

    # ── waiter ────────────────────────────────────────────────────────────────

    def _wait_lifecycle(self, state_fn, target: str, timeout: int = _POLL_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if state_fn() == target:
                return
            time.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"Resource did not reach {target} within {timeout}s")

    # ── Phase 3: Oracle Functions ─────────────────────────────────────────────

    def _scan_functions(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        if not self._functions_client: return []
        try:
            # First list applications in compartment
            for app in oci_paginate(self._functions_client, "list_applications", compartment_id=compartment_id):
                if app.lifecycle_state not in ("DELETED", "DELETING"):
                    # Then list functions for each application
                    for fn in oci_paginate(self._functions_client, "list_functions", application_id=app.id):
                        if fn.lifecycle_state not in ("DELETED", "DELETING"):
                            resources.append(self._make_resource(
                                fn.id, fn.display_name, ResourceType.ORACLE_FUNCTION,
                                parent_id=app.id, status=fn.lifecycle_state,
                                layer=1, estimated_cost=0.0,
                            ))
        except Exception as exc:
            self._emit(f"[Oracle] Error scanning Functions: {exc}")
        return resources

    # ── Phase 3: Oracle NoSQL Tables ──────────────────────────────────────────

    def _scan_nosql_tables(self, compartment_id: str) -> list[CloudResource]:
        resources = []
        if not self._nosql_client: return []
        try:
            for table in oci_paginate(self._nosql_client, "list_tables", compartment_id=compartment_id):
                if table.lifecycle_state not in ("DELETED", "DELETING"):
                    resources.append(self._make_resource(
                        table.id, table.name, ResourceType.ORACLE_NOSQL,
                        parent_id=compartment_id, status=table.lifecycle_state,
                        layer=1, estimated_cost=0.0,
                    ))
        except Exception as exc:
            self._emit(f"[Oracle] Error scanning NoSQL Tables: {exc}")
        return resources

