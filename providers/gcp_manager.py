"""
providers/gcp_manager.py
────────────────────────
GCP provider: Projects, VPC Networks, Subnetworks, Compute Instances, Disks,
Firewall Rules, Cloud Storage Buckets, Cloud Load Balancers.

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
        ResourceType.CLOUD_SQL, ResourceType.PUBLIC_IP, ResourceType.KUBERNETES,
        ResourceType.KUBERNETES_NODE_GROUP, ResourceType.NAT_GATEWAY,
        # Phase 1
        ResourceType.GCS_BUCKET, ResourceType.GCP_LB,
        # Phase 2
        ResourceType.CLOUD_FUNCTION, ResourceType.GCR_REPO,
        ResourceType.GCP_MEMORYSTORE, ResourceType.PUBSUB_TOPIC, ResourceType.GCP_DNS,
        # Phase 3
        ResourceType.GCP_SECRET, ResourceType.GCP_ALERT_POLICY,
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
        self._addresses_client:   Any = None
        self._sql_client:         Any = None
        self._gke_client:         Any = None
        self._routers_client:     Any = None
        self._gcs_client:         Any = None
        self._backend_client:     Any = None
        self._functions_client:   Any = None
        self._pubsub_client:      Any = None
        self._redis_client:       Any = None
        self._dns_client:         Any = None
        self._secret_client:      Any = None
        self._monitoring_client:  Any = None

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
            self._addresses_client   = compute_v1.AddressesClient(credentials=self._credentials)

            # Cloud SQL Client
            from googleapiclient.discovery import build
            self._sql_client = build("sqladmin", "v1beta4", credentials=self._credentials)

            # GKE Client
            try:
                from google.cloud import container_v1 # type: ignore
                self._gke_client = container_v1.ClusterManagerClient(credentials=self._credentials)
            except ImportError:
                self._emit("[GCP] Warning: google-cloud-container not installed. GKE scanning disabled.")
                self._gke_client = None

            # Routers Client (for NAT)
            self._routers_client = compute_v1.RoutersClient(credentials=self._credentials)

            # Cloud Storage Client
            try:
                from google.cloud import storage as gcs  # type: ignore
                self._gcs_client = gcs.Client(
                    project=self._project_id, credentials=self._credentials
                )
            except ImportError:
                self._emit("[GCP] Warning: google-cloud-storage not installed. GCS scanning disabled.")
                self._gcs_client = None

            # Backend Services Client (for HTTP/HTTPS Load Balancers)
            self._backend_client = compute_v1.BackendServicesClient(
                credentials=self._credentials
            )

            # Phase 2 clients (best-effort)
            try:
                from google.cloud import functions_v1  # type: ignore
                self._functions_client = functions_v1.CloudFunctionsServiceClient(
                    credentials=self._credentials
                )
            except ImportError: pass
            try:
                from google.cloud import pubsub_v1  # type: ignore
                self._pubsub_client = pubsub_v1.PublisherClient(
                    credentials=self._credentials
                )
            except ImportError: pass
            try:
                from google.cloud import redis_v1  # type: ignore
                self._redis_client = redis_v1.CloudRedisClient(
                    credentials=self._credentials
                )
            except ImportError: pass
            try:
                from google.cloud import dns  # type: ignore
                self._dns_client = dns.Client(
                    project=self._project_id, credentials=self._credentials
                )
            except ImportError: pass

            # Phase 3 clients
            try:
                from google.cloud import secretmanager  # type: ignore
                self._secret_client = secretmanager.SecretManagerServiceClient(credentials=self._credentials)
            except ImportError: pass
            try:
                from google.cloud import monitoring_v3  # type: ignore
                self._monitoring_client = monitoring_v3.AlertPolicyServiceClient(credentials=self._credentials)
            except ImportError: pass

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
        if not self._networks_client:
            return []
        resources: list[CloudResource] = []
        resources.extend(self._scan_unattached_disks())
        resources.extend(self._scan_terminated_instances())
        resources.extend([r for r in self._scan_addresses() if r.status == "RESERVED"])
        self._emit(f"[GCP] Comprehensive scan done — {len(resources)} orphans found")
        return resources

    def scan_hierarchical(self, region: str) -> list[CloudResource]:
        if not self._networks_client:
            self._emit("[GCP] Error: Provider not connected or clients not initialized.")
            return []
        resources: list[CloudResource] = []
        resources.extend(self._scan_networks())
        resources.extend(self._scan_subnetworks())
        resources.extend(self._scan_all_instances())
        resources.extend(self._scan_all_disks())
        resources.extend(self._scan_firewalls())
        resources.extend(self._scan_sql_instances())
        resources.extend(self._scan_addresses())
        resources.extend(self._scan_gke_clusters())
        resources.extend(self._scan_nat_gateways())
        resources.extend(self._scan_gcs_buckets())
        resources.extend(self._scan_gcp_load_balancers())
        # Phase 2
        resources.extend(self._scan_cloud_functions())
        resources.extend(self._scan_pubsub_topics())
        resources.extend(self._scan_gcp_redis())
        resources.extend(self._scan_gcp_dns_zones())
        # Phase 3
        resources.extend(self._scan_secret_manager())
        resources.extend(self._scan_alert_policies())
        self._emit(f"[GCP] Hierarchical scan done — {len(resources)} resources")
        return resources

    # ── deletion ──────────────────────────────────────────────────────────────

    def delete_resource(self, resource: CloudResource, dry_run: bool = False) -> str:
        from google.api_core.exceptions import NotFound  # type: ignore
        prefix = "[DRY RUN] " if dry_run else ""
        self._emit(f"{prefix}[GCP] Deleting {resource.resource_type.value}: {resource.display_name}")
        if dry_run:
            return f"DRY RUN: would delete {resource.resource_type.value} {resource.resource_id}"

        handlers = {
            ResourceType.INSTANCE:             self._delete_instance,
            ResourceType.DISK:                 self._delete_disk,
            ResourceType.FIREWALL_RULE:        self._delete_firewall_rule,
            ResourceType.SUBNET:               self._delete_subnet,
            ResourceType.VPC:                  self._delete_vpc,
            ResourceType.CLOUD_SQL:            self._delete_cloud_sql,
            ResourceType.PUBLIC_IP:            self._delete_public_ip,
            ResourceType.KUBERNETES:           self._delete_gke_cluster,
            ResourceType.KUBERNETES_NODE_GROUP:self._delete_gke_node_pool,
            ResourceType.NAT_GATEWAY:          self._delete_nat_gateway,
            ResourceType.GCS_BUCKET:           self._delete_gcs_bucket_wrapper,
            ResourceType.GCP_LB:               self._delete_gcp_lb,
            ResourceType.CLOUD_FUNCTION:       self._delete_cloud_function,
            ResourceType.PUBSUB_TOPIC:         self._delete_pubsub_topic,
            ResourceType.GCP_MEMORYSTORE:      self._delete_gcp_memorystore,
            ResourceType.GCP_DNS:              self._delete_gcp_dns,
            ResourceType.GCP_SECRET:           self._delete_gcp_secret,
            ResourceType.GCP_ALERT_POLICY:     self._delete_gcp_alert_policy,
        }

        handler = handlers.get(resource.resource_type)
        if not handler:
            return f"No deletion handler for {resource.resource_type.value}"

        try:
            return handler(resource)
        except NotFound:
            # If resource is already gone, count as success
            self._emit(f"[GCP] Resource {resource.resource_id} already deleted (404).")
            return f"Already deleted: {resource.resource_id}"

    # ── deletion helpers ──────────────────────────────────────────────────────

    def _delete_instance(self, r: CloudResource) -> str:
        op = self._instances_client.delete(project=self._project_id, zone=r.metadata.get("zone", ""), instance=r.resource_id)
        self._wait_zone_op(op.name, r.metadata.get("zone", ""))
        return f"Deleted instance {r.resource_id}"

    def _delete_disk(self, r: CloudResource) -> str:
        op = self._disks_client.delete(project=self._project_id, zone=r.metadata.get("zone", ""), disk=r.resource_id)
        self._wait_zone_op(op.name, r.metadata.get("zone", ""))
        return f"Deleted disk {r.resource_id}"

    def _delete_firewall_rule(self, r: CloudResource) -> str:
        op = self._firewalls_client.delete(project=self._project_id, firewall=r.resource_id)
        self._wait_global_op(op.name)
        return f"Deleted firewall rule {r.resource_id}"

    def _delete_subnet(self, r: CloudResource) -> str:
        op = self._subnetworks_client.delete(project=self._project_id, region=r.metadata.get("region", ""), subnetwork=r.resource_id)
        self._wait_region_op(op.name, r.metadata.get("region", ""))
        return f"Deleted subnetwork {r.resource_id}"

    def _delete_vpc(self, r: CloudResource) -> str:
        # First aggressively delete associated routes to prevent "already in use" errors
        try:
            from google.cloud import compute_v1  # type: ignore
            routes_client = compute_v1.RoutesClient(credentials=self._credentials)
            network_url = f"https://www.googleapis.com/compute/v1/projects/{self._project_id}/global/networks/{r.resource_id}"
            
            req = compute_v1.ListRoutesRequest(
                project=self._project_id,
                filter=f'network="{network_url}"'
            )
            for route in routes_client.list(request=req):
                self._emit(f"[GCP] Deleting associated route: {route.name}")
                try:
                    op = routes_client.delete(project=self._project_id, route=route.name)
                    self._wait_global_op(op.name)
                except Exception as e:
                    self._emit(f"[GCP] Warning: Could not delete route {route.name}: {e}")
        except Exception as e:
            self._emit(f"[GCP] Warning: Route cleanup failed for {r.resource_id}: {e}")

        op = self._networks_client.delete(project=self._project_id, network=r.resource_id)
        self._wait_global_op(op.name)
        return f"Deleted network {r.resource_id} and its routes"

    def _delete_cloud_sql(self, r: CloudResource) -> str:
        op = self._sql_client.instances().delete(project=self._project_id, instance=r.resource_id).execute()
        self._wait_sql_op(op["name"])
        return f"Deleted SQL instance {r.resource_id}"

    def _delete_public_ip(self, r: CloudResource) -> str:
        op = self._addresses_client.delete(project=self._project_id, region=r.metadata.get("region", ""), address=r.resource_id)
        self._wait_region_op(op.name, r.metadata.get("region", ""))
        return f"Deleted static IP {r.resource_id}"

    def _delete_gke_cluster(self, r: CloudResource) -> str:
        loc = r.metadata.get("zone", "") or r.metadata.get("region", "")
        self._gke_client.delete_cluster(name=f"projects/{self._project_id}/locations/{loc}/clusters/{r.resource_id}")
        return f"Initiated GKE cluster deletion {r.resource_id}"

    def _delete_gke_node_pool(self, r: CloudResource) -> str:
        loc = r.metadata.get("zone", "") or r.metadata.get("region", "")
        cluster_name = r.metadata.get("cluster_name")
        self._gke_client.delete_node_pool(name=f"projects/{self._project_id}/locations/{loc}/clusters/{cluster_name}/nodePools/{r.resource_id}")
        return f"Initiated GKE node pool deletion {r.resource_id}"

    def _delete_nat_gateway(self, r: CloudResource) -> str:
        op = self._routers_client.delete(project=self._project_id, region=r.metadata.get("region", ""), router=r.resource_id)
        self._wait_region_op(op.name, r.metadata.get("region", ""))
        return f"Deleted Cloud NAT Router {r.resource_id}"

    def _delete_gcs_bucket_wrapper(self, r: CloudResource) -> str:
        return self._delete_gcs_bucket(r.resource_id)

    def _delete_gcp_lb(self, r: CloudResource) -> str:
        op = self._backend_client.delete(project=self._project_id, backend_service=r.resource_id)
        self._wait_global_op(op.name)
        return f"Deleted GCP Backend Service (LB) {r.resource_id}"

    def _delete_cloud_function(self, r: CloudResource) -> str:
        if self._functions_client:
            location = r.metadata.get("location", "-")
            fn_name  = f"projects/{self._project_id}/locations/{location}/functions/{r.resource_id}"
            op = self._functions_client.delete_function(name=fn_name)
            op.result()
        return f"Deleted Cloud Function {r.resource_id}"

    def _delete_pubsub_topic(self, r: CloudResource) -> str:
        if self._pubsub_client:
            topic_path = self._pubsub_client.topic_path(self._project_id, r.resource_id)
            self._pubsub_client.delete_topic(request={"topic": topic_path})
        return f"Deleted Pub/Sub topic {r.resource_id}"

    def _delete_gcp_memorystore(self, r: CloudResource) -> str:
        if self._redis_client:
            location = r.metadata.get("location", "us-central1")
            inst_name = f"projects/{self._project_id}/locations/{location}/instances/{r.resource_id}"
            op = self._redis_client.delete_instance(name=inst_name)
            op.result()
        return f"Deleted Memorystore Redis instance {r.resource_id}"

    def _delete_gcp_dns(self, r: CloudResource) -> str:
        if self._dns_client:
            zone = self._dns_client.zone(r.resource_id)
            zone.delete()
        return f"Deleted Cloud DNS zone {r.resource_id}"

    def _delete_gcp_secret(self, r: CloudResource) -> str:
        if self._secret_client:
            self._secret_client.delete_secret(request={"name": r.resource_id})
        return f"Deleted Secret {r.resource_id}"

    def _delete_gcp_alert_policy(self, r: CloudResource) -> str:
        if self._monitoring_client:
            self._monitoring_client.delete_alert_policy(request={"name": r.resource_id})
        return f"Deleted Alert Policy {r.resource_id}"

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
                
                # Extract instance names from user URLs as dependencies
                # e.g., "https://.../instances/my-vm" -> "my-vm"
                deps = []
                for user_url in users:
                    if "/instances/" in user_url:
                        deps.append(user_url.split("/")[-1])

                resources.append(self._make_resource(
                    disk.name, disk.name, ResourceType.DISK,
                    status="unattached" if not users else "attached",
                    metadata={"zone": zone, "users": users},
                    layer=2, estimated_cost=cost if not users else 0.0,
                ))
                # Add dependencies to the last added resource
                resources[-1].dependencies = deps
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

    def _scan_sql_instances(self) -> list[CloudResource]:
        resources = []
        if not self._sql_client:
            return []
        try:
            req = self._sql_client.instances().list(project=self._project_id)
            while req is not None:
                resp = req.execute()
                for inst in resp.get("items", []):
                    rid    = inst["name"]
                    db_ver = inst.get("databaseVersion", "Unknown")
                    status = inst.get("state", "")
                    region = inst.get("region", "")
                    resources.append(self._make_resource(
                        rid, rid, ResourceType.CLOUD_SQL,
                        status=f"{status} ({db_ver})",
                        metadata={"region": region, "name": rid},
                        layer=1,
                    ))
                req = self._sql_client.instances().list_next(previous_request=req, previous_response=resp)
        except Exception as exc:
            self._emit(f"[GCP] Error scanning SQL: {exc}")
        return resources

    def _scan_addresses(self) -> list[CloudResource]:
        resources = []
        try:
            for item in gcp_paginate(self._addresses_client.aggregated_list,
                                      project=self._project_id):
                scope_name, scoped_list = item
                for addr in (scoped_list.addresses or []):
                    # region: https://.../regions/us-east1
                    region = self._region_from_url(addr.region) if addr.region else ""
                    # status: RESERVED, IN_USE
                    status = addr.status
                    resources.append(self._make_resource(
                        addr.name, addr.name, ResourceType.PUBLIC_IP,
                        status=status,
                        metadata={"region": region, "address": addr.address},
                        layer=2,
                    ))
        except Exception as exc:
            self._emit(f"[GCP] Error scanning addresses: {exc}")
        return resources

    def _scan_gke_clusters(self) -> list[CloudResource]:
        resources = []
        if not self._gke_client: return []
        try:
            resp = self._gke_client.list_clusters(parent=f"projects/{self._project_id}/locations/-")
            for cluster in resp.clusters:
                resources.append(self._make_resource(
                    cluster.name, cluster.name, ResourceType.KUBERNETES,
                    parent_id=cluster.network,
                    status=cluster.status.name,
                    metadata={"zone": cluster.location, "name": cluster.name},
                    layer=2,
                ))
                
                # Scan node pools
                try:
                    pool_resp = self._gke_client.list_node_pools(parent=f"projects/{self._project_id}/locations/{cluster.location}/clusters/{cluster.name}")
                    for pool in pool_resp.node_pools:
                        resources.append(self._make_resource(
                            f"{cluster.name}-{pool.name}", pool.name, ResourceType.KUBERNETES_NODE_GROUP,
                            parent_id=cluster.name, status=pool.status.name,
                            metadata={"zone": cluster.location, "cluster_name": cluster.name, "name": pool.name},
                            layer=1,
                        ))
                except Exception as e:
                    self._emit(f"[GCP] Warning scanning Node Pools for {cluster.name}: {e}")

        except Exception as exc:
            self._emit(f"[GCP] Error scanning GKE: {exc}")
        return resources

    def _scan_nat_gateways(self) -> list[CloudResource]:
        resources = []
        if not self._routers_client: return []
        try:
            for item in gcp_paginate(self._routers_client.aggregated_list, project=self._project_id):
                scope_name, scoped_list = item
                for router in (scoped_list.routers or []):
                    # Check if router has NAT configurations
                    if router.nats:
                        region = self._region_from_url(router.region)
                        resources.append(self._make_resource(
                            router.name, router.name, ResourceType.NAT_GATEWAY,
                            parent_id=router.network.split("/")[-1],
                            status="ACTIVE",
                            metadata={"region": region, "name": router.name},
                            layer=3,
                        ))
        except Exception as exc:
            self._emit(f"[GCP] Error scanning Cloud NAT: {exc}")
        return resources

    # ── GCS Buckets ─────────────────────────────────────────────────────────

    def _scan_gcs_buckets(self) -> list[CloudResource]:
        """List all GCS buckets in the project."""
        resources = []
        if not self._gcs_client:
            return []
        try:
            for bucket in self._gcs_client.list_buckets():
                # Bucket location (e.g. "US", "europe-west1")
                location = bucket.location or "unknown"
                # Size is not directly available from list_buckets; use 0 as default
                # A full size check requires iterating objects (expensive)
                resources.append(self._make_resource(
                    bucket.name, bucket.name, ResourceType.GCS_BUCKET,
                    status=location,
                    metadata={
                        "location":       location,
                        "storage_class":  bucket.storage_class,
                        "created":        str(bucket.time_created),
                    },
                    layer=1,
                    estimated_cost=0.0,  # Requires size data to estimate
                ))
        except Exception as exc:
            self._emit(f"[GCP] Error scanning GCS buckets: {exc}")
        return resources

    def _delete_gcs_bucket(self, name: str) -> str:
        """Delete a GCS bucket — forces deletion of all objects first."""
        if not self._gcs_client:
            return f"GCS client not initialized"
        try:
            bucket = self._gcs_client.bucket(name)
            self._emit(f"[GCP] Emptying GCS bucket {name}…")
            blobs = list(self._gcs_client.list_blobs(name))
            if blobs:
                bucket.delete_blobs(blobs)
            bucket.delete(force=False)
            return f"Deleted GCS bucket {name}"
        except Exception as exc:
            raise Exception(f"Failed to delete GCS bucket {name}: {exc}")

    # ── GCP Load Balancers (Backend Services) ─────────────────────────────

    def _scan_gcp_load_balancers(self) -> list[CloudResource]:
        """Scan GCP Backend Services (HTTPS/HTTP LBs) globally."""
        resources = []
        if not self._backend_client:
            return []
        try:
            for bs in gcp_paginate(
                self._backend_client.list, project=self._project_id
            ):
                resources.append(self._make_resource(
                    bs.name, bs.name, ResourceType.GCP_LB,
                    status="ACTIVE",
                    metadata={"protocol": bs.protocol, "load_balancing_scheme": bs.load_balancing_scheme},
                    layer=1,
                    estimated_cost=18.0,  # GCP LB ~$18/month base
                ))
        except Exception as exc:
            self._emit(f"[GCP] Error scanning Backend Services (LBs): {exc}")
        return resources

    # ── Phase 2: Cloud Functions ─────────────────────────────────────────────

    def _scan_cloud_functions(self) -> list[CloudResource]:
        """Scan Cloud Functions v1 in the project (across all locations)."""
        resources = []
        if not self._functions_client:
            return []
        try:
            parent = f"projects/{self._project_id}/locations/-"
            for fn in self._functions_client.list_functions(request={"parent": parent}):
                location = fn.name.split("/")[3] if fn.name else ""
                fn_name  = fn.name.split("/")[-1]
                resources.append(self._make_resource(
                    fn_name, fn_name, ResourceType.CLOUD_FUNCTION,
                    status=fn.status.name if hasattr(fn.status, 'name') else str(fn.status),
                    metadata={"location": location, "runtime": fn.runtime},
                    layer=1, estimated_cost=0.0,  # pay-per-invocation
                ))
        except Exception as exc:
            self._emit(f"[GCP] Error scanning Cloud Functions: {exc}")
        return resources

    # ── Phase 2: Pub/Sub Topics ───────────────────────────────────────────────

    def _scan_pubsub_topics(self) -> list[CloudResource]:
        resources = []
        if not self._pubsub_client:
            return []
        try:
            project_path = f"projects/{self._project_id}"
            for topic in self._pubsub_client.list_topics(request={"project": project_path}):
                name = topic.name.split("/")[-1]
                resources.append(self._make_resource(
                    name, name, ResourceType.PUBSUB_TOPIC,
                    status="active",
                    metadata={"full_name": topic.name},
                    layer=1, estimated_cost=0.0,  # pay-per-message
                ))
        except Exception as exc:
            self._emit(f"[GCP] Error scanning Pub/Sub: {exc}")
        return resources

    # ── Phase 2: Memorystore (Redis) ─────────────────────────────────────────

    def _scan_gcp_redis(self) -> list[CloudResource]:
        resources = []
        if not self._redis_client:
            return []
        try:
            parent = f"projects/{self._project_id}/locations/-"
            for inst in self._redis_client.list_instances(request={"parent": parent}):
                location = inst.name.split("/")[3] if inst.name else ""
                name     = inst.name.split("/")[-1]
                # Memorystore Redis M1 ~$50/month
                resources.append(self._make_resource(
                    name, name, ResourceType.GCP_MEMORYSTORE,
                    status=inst.state.name if hasattr(inst.state, 'name') else str(inst.state),
                    metadata={"location": location, "tier": str(inst.tier),
                              "memory_size_gb": inst.memory_size_gb},
                    layer=1, estimated_cost=50.0,
                ))
        except Exception as exc:
            self._emit(f"[GCP] Error scanning Memorystore: {exc}")
        return resources

    # ── Phase 2: Cloud DNS Zones ───────────────────────────────────────────────

    def _scan_gcp_dns_zones(self) -> list[CloudResource]:
        resources = []
        if not self._dns_client:
            return []
        try:
            for zone in self._dns_client.list_zones():
                resources.append(self._make_resource(
                    zone.name, zone.dns_name, ResourceType.GCP_DNS,
                    status="active",
                    metadata={"dns_name": zone.dns_name},
                    layer=1, estimated_cost=0.20,  # Cloud DNS: $0.20/zone/month
                ))
        except Exception as exc:
            self._emit(f"[GCP] Error scanning Cloud DNS: {exc}")
        return resources

    # ── operation waiters ─────────────────────────────────────────────────────

    def _wait_zone_op(self, op_name: str, zone: str) -> None:
        deadline = time.monotonic() + _POLL_TIMEOUT
        while time.monotonic() < deadline:
            op = self._zone_ops_client.get(
                project=self._project_id, zone=zone, operation=op_name)
            if op.status.name == "DONE":
                if op.error:
                    msg = "; ".join(e.message for e in op.error.errors) if op.error.errors else "Unknown error"
                    raise Exception(f"Zone Operation failed: {msg}")
                return
            time.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"Zone operation {op_name} did not complete within {_POLL_TIMEOUT}s")

    def _wait_region_op(self, op_name: str, region: str) -> None:
        deadline = time.monotonic() + _POLL_TIMEOUT
        while time.monotonic() < deadline:
            op = self._region_ops_client.get(
                project=self._project_id, region=region, operation=op_name)
            if op.status.name == "DONE":
                if op.error:
                    msg = "; ".join(e.message for e in op.error.errors) if op.error.errors else "Unknown error"
                    raise Exception(f"Region Operation failed: {msg}")
                return
            time.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"Region operation {op_name} did not complete within {_POLL_TIMEOUT}s")

    def _wait_global_op(self, op_name: str) -> None:
        deadline = time.monotonic() + _POLL_TIMEOUT
        while time.monotonic() < deadline:
            op = self._global_ops_client.get(
                project=self._project_id, operation=op_name)
            if op.status.name == "DONE":
                if op.error:
                    msg = "; ".join(e.message for e in op.error.errors) if op.error.errors else "Unknown error"
                    raise Exception(f"Global Operation failed: {msg}")
                return
            time.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"Global operation {op_name} did not complete within {_POLL_TIMEOUT}s")

    def _wait_sql_op(self, op_name: str) -> None:
        deadline = time.monotonic() + _POLL_TIMEOUT
        while time.monotonic() < deadline:
            op = self._sql_client.operations().get(
                project=self._project_id, operation=op_name).execute()
            if op.get("status") == "DONE":
                if "error" in op:
                    errors = op["error"].get("errors", [])
                    msg = "; ".join(e.get("message", "Unknown error") for e in errors)
                    raise Exception(f"SQL Operation failed: {msg}")
                return
            time.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"SQL operation {op_name} did not complete within {_POLL_TIMEOUT}s")

    # ── Phase 3: Secret Manager ───────────────────────────────────────────────

    def _scan_secret_manager(self) -> list[CloudResource]:
        resources = []
        if not self._secret_client:
            return []
        try:
            parent = f"projects/{self._project_id}"
            for secret in self._secret_client.list_secrets(request={"parent": parent}):
                name = secret.name.split("/")[-1]
                resources.append(self._make_resource(
                    secret.name, name, ResourceType.GCP_SECRET,
                    status="active",
                    metadata={"labels": dict(secret.labels) if secret.labels else {}},
                    layer=1, estimated_cost=0.06,  # 6 active secrets = $0.06 / month each (approx)
                ))
        except Exception as exc:
            self._emit(f"[GCP] Error scanning Secret Manager: {exc}")
        return resources

    # ── Phase 3: Cloud Monitoring ─────────────────────────────────────────────

    def _scan_alert_policies(self) -> list[CloudResource]:
        resources = []
        if not self._monitoring_client:
            return []
        try:
            name = f"projects/{self._project_id}"
            for policy in self._monitoring_client.list_alert_policies(request={"name": name}):
                # format is projects/.../alertPolicies/...
                policy_id = policy.name.split("/")[-1]
                resources.append(self._make_resource(
                    policy.name, policy.display_name or policy_id, ResourceType.GCP_ALERT_POLICY,
                    status="enabled" if policy.enabled else "disabled",
                    metadata={"conditions": len(policy.conditions)},
                    layer=1, estimated_cost=0.0,
                ))
        except Exception as exc:
            self._emit(f"[GCP] Error scanning Alert Policies: {exc}")
        return resources

