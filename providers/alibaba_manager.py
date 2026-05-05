"""
providers/alibaba_manager.py
────────────────────────────
Alibaba Cloud provider: VPCs, ECS Instances, Cloud Disks, EIPs,
OSS Buckets, Server Load Balancers (SLB).

Special handling for me-central-1 (UAE/Qatar):
  - Reduced request rate (5 req/s vs 10 req/s for other regions)
  - Extended timeout and retry for GRPC issues specific to that region
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

from models.resource import CloudResource, ProviderName, ResourceType
from providers.base_provider import CloudProvider
from utils.credentials import AlibabaCredentials
from utils.pagination import alibaba_paginate

log = logging.getLogger(__name__)

# Regions known to require conservative rate-limiting
_SENSITIVE_REGIONS = {"me-central-1", "me-east-1"}
_DEFAULT_RPS       = 10.0
_SENSITIVE_RPS     = 5.0
_MAX_RETRY         = 5
_BACKOFF_BASE      = 2.0    # seconds
_BACKOFF_MAX       = 30.0   # seconds


class _RateLimiter:
    """Token-bucket rate limiter (thread-safe)."""

    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps
        self._lock     = Lock()
        self._last     = 0.0

    def acquire(self) -> None:
        with self._lock:
            now   = time.monotonic()
            wait  = self._interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


class AlibabaManager(CloudProvider):

    provider_name = "Alibaba"
    icon_path     = "assets/icons/alibaba.png"
    supported_resource_types = [
        ResourceType.VPC, ResourceType.SUBNET, ResourceType.INSTANCE,
        ResourceType.DISK, ResourceType.EIP, ResourceType.SECURITY_GROUP,
        ResourceType.NAT_GATEWAY, ResourceType.KUBERNETES, ResourceType.CLOUD_SQL,
        ResourceType.KUBERNETES_NODE_GROUP,
        # Object Storage
        ResourceType.OSS_BUCKET,
        # Load Balancers
        ResourceType.ALIBABA_SLB,
        # Phase 3
        ResourceType.ALIBABA_FUNCTION, ResourceType.ALIBABA_CR,
        ResourceType.ALIBABA_MNS, ResourceType.ALIBABA_DNS,
    ]

    # Known regions (dynamically updated after connect)
    KNOWN_REGIONS: list[str] = [
        "cn-hangzhou", "cn-shanghai", "cn-beijing", "cn-shenzhen",
        "cn-hongkong", "ap-southeast-1", "ap-southeast-2", "ap-southeast-3",
        "ap-southeast-5", "ap-northeast-1", "ap-south-1",
        "us-east-1", "us-west-1",
        "eu-west-1", "eu-central-1",
        "me-east-1", "me-central-1",
    ]

    def __init__(self, log_callback=None) -> None:
        super().__init__(log_callback)
        self._region: str      = "me-central-1"
        self._ecs_client: Any  = None
        self._vpc_client: Any  = None
        self._rds_client: Any  = None
        self._cs_client: Any   = None
        self._slb_client: Any  = None
        self._oss_client: Any  = None
        self._limiter: Optional[_RateLimiter] = None
        self._fc_client:  Any = None
        self._cr_client:  Any = None
        self._mns_client: Any = None
        self._dns_client: Any = None

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self, credentials: AlibabaCredentials) -> bool:
        try:
            from alibabacloud_ecs20140526.client import Client as EcsClient    # type: ignore
            from alibabacloud_vpc20160428.client import Client as VpcClient    # type: ignore
            from utils.credentials import CredentialManager

            self._region = credentials.region_id
            cfg          = CredentialManager().get_alibaba_config(credentials)

            self._ecs_client = EcsClient(cfg)
            self._vpc_client = VpcClient(cfg)

            from alibabacloud_rds20140815.client import Client as RdsClient  # type: ignore
            from alibabacloud_cs20151215.client import Client as CsClient    # type: ignore
            self._rds_client = RdsClient(cfg)
            self._cs_client  = CsClient(cfg)

            # SLB Client
            try:
                from alibabacloud_slb20140515.client import Client as SlbClient  # type: ignore
                self._slb_client = SlbClient(cfg)
            except ImportError:
                self._emit("[Alibaba] SLB SDK not installed — skipping SLB scan")

            # OSS Client
            try:
                import oss2  # type: ignore
                auth = oss2.Auth(
                    credentials.access_key_id,
                    credentials.access_key_secret,
                )
                endpoint = f"https://oss-{self._region}.aliyuncs.com"
                self._oss_client = oss2.Service(auth, endpoint)
            except ImportError:
                self._emit("[Alibaba] oss2 SDK not installed — skipping OSS scan")

            # Phase 3 clients
            try:
                from alibabacloud_fc_open20210406.client import Client as FcClient  # type: ignore
                self._fc_client = FcClient(cfg)
            except ImportError: pass
            try:
                from alibabacloud_cr20181201.client import Client as CrClient  # type: ignore
                self._cr_client = CrClient(cfg)
            except ImportError: pass
            try:
                from alibabacloud_mns_open20220119.client import Client as MnsClient  # type: ignore
                self._mns_client = MnsClient(cfg)
            except ImportError: pass
            try:
                from alibabacloud_alidns20150109.client import Client as DnsClient  # type: ignore
                self._dns_client = DnsClient(cfg)
            except ImportError: pass

            rps = _SENSITIVE_RPS if self._region in _SENSITIVE_REGIONS else _DEFAULT_RPS
            self._limiter = _RateLimiter(rps)

            self._emit(f"[Alibaba] Connected — region={self._region}, rate_limit={rps} req/s")
            return True
        except Exception as exc:
            self._emit(f"[Alibaba] Connection failed: {exc}")
            return False

    # ── rate-limited API call with retry ──────────────────────────────────────

    def _call(self, fn, *args, **kwargs) -> Any:
        """Execute fn with rate-limiting and exponential backoff for throttle/timeout errors."""
        if self._limiter:
            self._limiter.acquire()

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRY):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                msg = str(exc).lower()
                if any(k in msg for k in ("throttling", "timeout", "grpc", "requestlimitexceeded")):
                    wait = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                    self._emit(f"[Alibaba] Rate/timeout error, retry {attempt+1}/{_MAX_RETRY} in {wait}s")
                    time.sleep(wait)
                    last_exc = exc
                else:
                    raise
        raise last_exc  # type: ignore[misc]

    # ── regions ───────────────────────────────────────────────────────────────

    def list_regions(self) -> list[str]:
        try:
            from alibabacloud_ecs20140526 import models as ecs_models  # type: ignore
            resp = self._call(self._ecs_client.describe_regions,
                              ecs_models.DescribeRegionsRequest())
            regions = [r.region_id for r in resp.body.regions.region]
            self.KNOWN_REGIONS = sorted(regions)
            return self.KNOWN_REGIONS
        except Exception:
            return sorted(self.KNOWN_REGIONS)

    # ── scanning ──────────────────────────────────────────────────────────────

    def scan_comprehensive(self, region: str) -> list[CloudResource]:
        self._region = region
        resources: list[CloudResource] = []
        resources.extend(self._scan_free_disks())
        resources.extend(self._scan_free_eips())
        self._emit(f"[Alibaba] Comprehensive scan done — {len(resources)} orphans in {region}")
        return resources

    def scan_hierarchical(self, region: str) -> list[CloudResource]:
        self._region = region
        resources: list[CloudResource] = []
        resources.extend(self._scan_vpcs())
        resources.extend(self._scan_vswitches())
        resources.extend(self._scan_instances())
        resources.extend(self._scan_disks())
        resources.extend(self._scan_eips())
        resources.extend(self._scan_security_groups())
        resources.extend(self._scan_nat_gateways())
        resources.extend(self._scan_rds_instances())
        resources.extend(self._scan_ack_clusters())
        resources.extend(self._scan_oss_buckets())
        resources.extend(self._scan_slb_load_balancers())
        # Phase 3
        resources.extend(self._scan_fc_functions())
        resources.extend(self._scan_acr_repos())
        resources.extend(self._scan_mns_topics())
        resources.extend(self._scan_dns_zones())
        self._emit(f"[Alibaba] Hierarchical scan done — {len(resources)} resources in {region}")
        return resources

    # ── deletion ──────────────────────────────────────────────────────────────

    def delete_resource(self, resource: CloudResource, dry_run: bool = False) -> str:
        prefix = "[DRY RUN] " if dry_run else ""
        self._emit(f"{prefix}[Alibaba] Deleting {resource.resource_type.value}: {resource.display_name}")
        if dry_run:
            return f"DRY RUN: would delete {resource.resource_type.value} {resource.resource_id}"

        handlers = {
            ResourceType.INSTANCE:             self._delete_ecs_instance,
            ResourceType.DISK:                 self._delete_disk,
            ResourceType.EIP:                  self._delete_eip,
            ResourceType.SUBNET:               self._delete_vswitch,
            ResourceType.SECURITY_GROUP:       self._delete_security_group,
            ResourceType.VPC:                  self._delete_vpc,
            ResourceType.NAT_GATEWAY:          self._delete_nat_gateway,
            ResourceType.CLOUD_SQL:            self._delete_rds_instance,
            ResourceType.KUBERNETES:           self._delete_ack_cluster,
            ResourceType.KUBERNETES_NODE_GROUP:self._delete_ack_node_pool,
            ResourceType.OSS_BUCKET:           self._delete_oss_bucket_wrapper,
            ResourceType.ALIBABA_SLB:          self._delete_slb,
            ResourceType.ALIBABA_FUNCTION:     self._delete_fc_function,
            ResourceType.ALIBABA_CR:           self._delete_acr_repo,
            ResourceType.ALIBABA_MNS:          self._delete_mns_topic,
            ResourceType.ALIBABA_DNS:          self._delete_alidns_domain,
        }

        handler = handlers.get(resource.resource_type)
        if not handler:
            return f"No deletion handler for {resource.resource_type.value}"

        try:
            return handler(resource)
        except Exception as exc:
            msg = str(exc).lower()
            if "notfound" in msg or "forbidden.resourcenotfound" in msg or "not exist" in msg:
                self._emit(f"[Alibaba] Resource {resource.resource_id} already deleted.")
                return f"Already deleted: {resource.resource_id}"
            raise exc

    # ── deletion helpers ──────────────────────────────────────────────────────

    def _delete_ecs_instance(self, r: CloudResource) -> str:
        from alibabacloud_ecs20140526 import models as ecs_models  # type: ignore
        self._call(self._ecs_client.stop_instances, ecs_models.StopInstancesRequest(instance_id=[r.resource_id]))
        self._wait_instance(r.resource_id, "Stopped")
        self._call(self._ecs_client.delete_instance, ecs_models.DeleteInstanceRequest(instance_id=r.resource_id, force=True))
        return f"Deleted ECS instance {r.resource_id}"

    def _delete_disk(self, r: CloudResource) -> str:
        from alibabacloud_ecs20140526 import models as ecs_models  # type: ignore
        self._call(self._ecs_client.delete_disk, ecs_models.DeleteDiskRequest(disk_id=r.resource_id))
        return f"Deleted disk {r.resource_id}"

    def _delete_eip(self, r: CloudResource) -> str:
        from alibabacloud_vpc20160428 import models as vpc_models  # type: ignore
        self._call(self._vpc_client.release_eip_address, vpc_models.ReleaseEipAddressRequest(allocation_id=r.resource_id))
        return f"Released EIP {r.resource_id}"

    def _delete_vswitch(self, r: CloudResource) -> str:
        from alibabacloud_vpc20160428 import models as vpc_models  # type: ignore
        self._call(self._vpc_client.delete_vswitch, vpc_models.DeleteVSwitchRequest(v_switch_id=r.resource_id))
        return f"Deleted VSwitch {r.resource_id}"

    def _delete_security_group(self, r: CloudResource) -> str:
        from alibabacloud_ecs20140526 import models as ecs_models  # type: ignore
        self._call(self._ecs_client.delete_security_group, ecs_models.DeleteSecurityGroupRequest(security_group_id=r.resource_id))
        return f"Deleted Security Group {r.resource_id}"

    def _delete_vpc(self, r: CloudResource) -> str:
        from alibabacloud_vpc20160428 import models as vpc_models  # type: ignore
        self._call(self._vpc_client.delete_vpc, vpc_models.DeleteVpcRequest(vpc_id=r.resource_id))
        return f"Deleted VPC {r.resource_id}"

    def _delete_nat_gateway(self, r: CloudResource) -> str:
        from alibabacloud_vpc20160428 import models as vpc_models  # type: ignore
        self._call(self._vpc_client.delete_nat_gateway, vpc_models.DeleteNatGatewayRequest(nat_gateway_id=r.resource_id))
        return f"Deleted NAT Gateway {r.resource_id}"

    def _delete_rds_instance(self, r: CloudResource) -> str:
        from alibabacloud_rds20140815 import models as rds_models  # type: ignore
        self._call(self._rds_client.delete_dbinstance, rds_models.DeleteDBInstanceRequest(dbinstance_id=r.resource_id))
        return f"Deleted RDS instance {r.resource_id}"

    def _delete_ack_cluster(self, r: CloudResource) -> str:
        self._call(self._cs_client.delete_cluster, r.resource_id)
        return f"Initiated ACK Cluster deletion {r.resource_id}"

    def _delete_ack_node_pool(self, r: CloudResource) -> str:
        cluster_id = r.metadata.get("cluster_id")
        self._call(self._cs_client.delete_cluster_nodepool, cluster_id, r.resource_id)
        return f"Initiated ACK Node Pool deletion {r.resource_id}"

    def _delete_oss_bucket_wrapper(self, r: CloudResource) -> str:
        return self._delete_oss_bucket(r.resource_id)

    def _delete_slb(self, r: CloudResource) -> str:
        from alibabacloud_slb20140515 import models as slb_models  # type: ignore
        self._call(self._slb_client.delete_load_balancer, slb_models.DeleteLoadBalancerRequest(load_balancer_id=r.resource_id))
        return f"Deleted SLB {r.resource_id}"

    def _delete_fc_function(self, r: CloudResource) -> str:
        self._call(self._fc_client.delete_function, r.parent_id, r.resource_id)
        return f"Deleted Function Compute {r.resource_id}"

    def _delete_acr_repo(self, r: CloudResource) -> str:
        from alibabacloud_cr20181201 import models as cr_models  # type: ignore
        ns = r.metadata.get("namespace", "")
        self._call(self._cr_client.delete_repo, cr_models.DeleteRepoRequest(instance_id=r.parent_id, repo_namespace_name=ns, repo_name=r.resource_id))
        return f"Deleted ACR Repo {r.resource_id}"

    def _delete_mns_topic(self, r: CloudResource) -> str:
        from alibabacloud_mns_open20220119 import models as mns_models  # type: ignore
        self._call(self._mns_client.delete_topic, mns_models.DeleteTopicRequest(topic_name=r.resource_id))
        return f"Deleted MNS Topic {r.resource_id}"

    def _delete_alidns_domain(self, r: CloudResource) -> str:
        from alibabacloud_alidns20150109 import models as dns_models  # type: ignore
        self._call(self._dns_client.delete_domain, dns_models.DeleteDomainRequest(domain_name=r.resource_id))
        return f"Deleted Alibaba DNS {r.resource_id}"

    # ── private scan helpers ──────────────────────────────────────────────────

    def _make_resource(self, rid, name, rtype, parent_id=None,
                       status="", metadata=None, layer=1,
                       estimated_cost=0.0, tags=None, age_days=-1) -> CloudResource:
        return CloudResource(
            resource_id=rid, name=name, resource_type=rtype,
            provider=ProviderName.ALIBABA, region=self._region,
            parent_id=parent_id, deletion_layer=layer,
            status=status, estimated_cost=estimated_cost,
            metadata=metadata or {}, tags=tags or {}, age_days=age_days,
        )

    def _scan_vpcs(self) -> list[CloudResource]:
        from alibabacloud_vpc20160428 import models as vpc_models  # type: ignore

        def builder(page, size):
            return vpc_models.DescribeVpcsRequest(region_id=self._region,
                                                   page_number=page, page_size=size)
        def extractor(resp): return resp.body.vpcs.vpc
        def total(resp):    return resp.body.total_count

        resources = []
        for vpc in alibaba_paginate(
            lambda r: self._call(self._vpc_client.describe_vpcs, r),
            builder, extractor, total,
        ):
            resources.append(self._make_resource(
                vpc.vpc_id, vpc.vpc_name or vpc.vpc_id,
                ResourceType.VPC, status=vpc.status,
                metadata={"cidr": vpc.cidr_block}, layer=5,
            ))
        return resources

    def _scan_vswitches(self) -> list[CloudResource]:
        from alibabacloud_vpc20160428 import models as vpc_models  # type: ignore

        def builder(page, size):
            return vpc_models.DescribeVSwitchesRequest(region_id=self._region,
                                                        page_number=page, page_size=size)
        def extractor(resp): return resp.body.v_switches.v_switch
        def total(resp):    return resp.body.total_count

        resources = []
        for sw in alibaba_paginate(
            lambda r: self._call(self._vpc_client.describe_vswitches, r),
            builder, extractor, total,
        ):
            resources.append(self._make_resource(
                sw.v_switch_id, sw.v_switch_name or sw.v_switch_id,
                ResourceType.SUBNET, parent_id=sw.vpc_id,
                status=sw.status, layer=4,
            ))
        return resources

    def _scan_instances(self) -> list[CloudResource]:
        from alibabacloud_ecs20140526 import models as ecs_models  # type: ignore

        def builder(page, size):
            return ecs_models.DescribeInstancesRequest(region_id=self._region,
                                                        page_number=page, page_size=size)
        def extractor(resp): return resp.body.instances.instance
        def total(resp):    return resp.body.total_count

        resources = []
        for inst in alibaba_paginate(
            lambda r: self._call(self._ecs_client.describe_instances, r),
            builder, extractor, total,
        ):
            resources.append(self._make_resource(
                inst.instance_id,
                inst.instance_name or inst.instance_id,
                ResourceType.INSTANCE,
                parent_id=inst.vpc_attributes.vpc_id if inst.vpc_attributes else None,
                status=inst.status, layer=1,
            ))
        return resources

    def _scan_disks(self) -> list[CloudResource]:
        from alibabacloud_ecs20140526 import models as ecs_models  # type: ignore

        def builder(page, size):
            return ecs_models.DescribeDisksRequest(region_id=self._region,
                                                    page_number=page, page_size=size)
        def extractor(resp): return resp.body.disks.disk
        def total(resp):    return resp.body.total_count

        resources = []
        for disk in alibaba_paginate(
            lambda r: self._call(self._ecs_client.describe_disks, r),
            builder, extractor, total,
        ):
            cost = round((disk.size or 0) * 0.04, 4)
            resources.append(self._make_resource(
                disk.disk_id, disk.disk_name or disk.disk_id,
                ResourceType.DISK, status=disk.status,
                layer=2, estimated_cost=cost,
            ))
        return resources

    def _scan_free_disks(self) -> list[CloudResource]:
        from alibabacloud_ecs20140526 import models as ecs_models  # type: ignore

        def builder(page, size):
            return ecs_models.DescribeDisksRequest(
                region_id=self._region, status="Available",
                page_number=page, page_size=size,
            )
        def extractor(resp): return resp.body.disks.disk
        def total(resp):    return resp.body.total_count

        resources = []
        for disk in alibaba_paginate(
            lambda r: self._call(self._ecs_client.describe_disks, r),
            builder, extractor, total,
        ):
            cost = round((disk.size or 0) * 0.04, 4)
            resources.append(self._make_resource(
                disk.disk_id, disk.disk_name or disk.disk_id,
                ResourceType.DISK, status="unattached",
                layer=2, estimated_cost=cost,
            ))
        return resources

    def _scan_eips(self) -> list[CloudResource]:
        from alibabacloud_vpc20160428 import models as vpc_models  # type: ignore

        def builder(page, size):
            return vpc_models.DescribeEipAddressesRequest(
                region_id=self._region, page_number=page, page_size=size,
            )
        def extractor(resp): return resp.body.eip_addresses.eip_address
        def total(resp):    return resp.body.total_count

        resources = []
        for eip in alibaba_paginate(
            lambda r: self._call(self._vpc_client.describe_eip_addresses, r),
            builder, extractor, total,
        ):
            resources.append(self._make_resource(
                eip.allocation_id, eip.ip_address,
                ResourceType.EIP,
                status=eip.status, layer=2,
                estimated_cost=3.65 if eip.status == "Available" else 0.0,
            ))
        return resources

    def _scan_free_eips(self) -> list[CloudResource]:
        return [r for r in self._scan_eips() if r.status == "Available"]

    def _scan_security_groups(self) -> list[CloudResource]:
        from alibabacloud_ecs20140526 import models as ecs_models  # type: ignore

        def builder(page, size):
            return ecs_models.DescribeSecurityGroupsRequest(
                region_id=self._region, page_number=page, page_size=size,
            )
        def extractor(resp): return resp.body.security_groups.security_group
        def total(resp):    return resp.body.total_count

        resources = []
        for sg in alibaba_paginate(
            lambda r: self._call(self._ecs_client.describe_security_groups, r),
            builder, extractor, total,
        ):
            resources.append(self._make_resource(
                sg.security_group_id,
                sg.security_group_name or sg.security_group_id,
                ResourceType.SECURITY_GROUP,
                parent_id=sg.vpc_id, layer=4,
            ))
        return resources

    def _scan_nat_gateways(self) -> list[CloudResource]:
        from alibabacloud_vpc20160428 import models as vpc_models  # type: ignore
        def builder(page, size):
            return vpc_models.DescribeNatGatewaysRequest(region_id=self._region, page_number=page, page_size=size)
        def extractor(resp): return resp.body.nat_gateways.nat_gateway
        def total(resp):    return resp.body.total_count

        resources = []
        for nat in alibaba_paginate(lambda r: self._call(self._vpc_client.describe_nat_gateways, r), builder, extractor, total):
            resources.append(self._make_resource(
                nat.nat_gateway_id, nat.name or nat.nat_gateway_id, ResourceType.NAT_GATEWAY,
                parent_id=nat.vpc_id, status=nat.status, layer=3,
            ))
        return resources

    def _scan_rds_instances(self) -> list[CloudResource]:
        from alibabacloud_rds20140815 import models as rds_models  # type: ignore
        def builder(page, size):
            return rds_models.DescribeDBInstancesRequest(region_id=self._region, page_number=page, page_size=size)
        def extractor(resp): return resp.body.items.dbinstance
        def total(resp):    return resp.body.total_count

        resources = []
        try:
            for db in alibaba_paginate(lambda r: self._call(self._rds_client.describe_dbinstances, r), builder, extractor, total):
                resources.append(self._make_resource(
                    db.dbinstance_id, db.dbinstance_description or db.dbinstance_id, ResourceType.CLOUD_SQL,
                    parent_id=db.vpc_id, status=db.dbinstance_status, layer=1,
                ))
        except Exception as exc:
            self._emit(f"[Alibaba] Error scanning RDS: {exc}")
        return resources

    def _scan_ack_clusters(self) -> list[CloudResource]:
        resources = []
        try:
            resp = self._call(self._cs_client.describe_clusters_v1)
            for cluster in resp.body:
                resources.append(self._make_resource(
                    cluster.cluster_id, cluster.name, ResourceType.KUBERNETES,
                    parent_id=cluster.vpc_id, status=cluster.state, layer=2,
                ))
                
                # Scan Node Pools
                try:
                    pool_resp = self._call(self._cs_client.describe_cluster_node_pools, cluster.cluster_id)
                    for pool in pool_resp.body.nodepools:
                        resources.append(self._make_resource(
                            pool.nodepool_info.nodepool_id, pool.nodepool_info.name, ResourceType.KUBERNETES_NODE_GROUP,
                            parent_id=cluster.cluster_id, status=pool.status.state,
                            metadata={"cluster_id": cluster.cluster_id}, layer=1,
                        ))
                except Exception as e:
                    self._emit(f"[Alibaba] Warning scanning ACK Node Pools for {cluster.name}: {e}")

        except Exception as exc:
            self._emit(f"[Alibaba] Error scanning ACK: {exc}")
        return resources

    # ── OSS Buckets ───────────────────────────────────────────────────────

    def _scan_oss_buckets(self) -> list[CloudResource]:
        """List all OSS buckets in the current region."""
        resources = []
        if not self._oss_client:
            return []
        try:
            import oss2  # type: ignore
            for bucket_info in oss2.BucketIterator(self._oss_client):
                resources.append(self._make_resource(
                    bucket_info.name, bucket_info.name, ResourceType.OSS_BUCKET,
                    status=self._region,
                    metadata={
                        "location":       self._region,
                        "creation_date":  bucket_info.creation_date,
                    },
                    layer=1,
                    estimated_cost=0.0,  # Size requires listing objects
                ))
        except Exception as exc:
            self._emit(f"[Alibaba] Error scanning OSS buckets: {exc}")
        return resources

    def _delete_oss_bucket(self, bucket_name: str) -> str:
        """Delete an OSS bucket — empties all objects first."""
        try:
            import oss2  # type: ignore
            # Re-create a per-bucket client using the bucket name
            auth     = self._oss_client._auth  # reuse existing auth
            endpoint = f"https://oss-{self._region}.aliyuncs.com"
            bucket   = oss2.Bucket(auth, endpoint, bucket_name)

            self._emit(f"[Alibaba] Emptying OSS bucket {bucket_name}…")
            # Delete all objects in batches
            for obj in oss2.ObjectIterator(bucket):
                bucket.delete_object(obj.key)

            bucket.delete()
            return f"Deleted OSS bucket {bucket_name}"
        except Exception as exc:
            raise Exception(f"Failed to delete OSS bucket {bucket_name}: {exc}")

    # ── SLB (Server Load Balancers) ──────────────────────────────────────

    def _scan_slb_load_balancers(self) -> list[CloudResource]:
        """Scan Alibaba Server Load Balancers."""
        resources = []
        if not self._slb_client:
            return []
        try:
            from alibabacloud_slb20140515 import models as slb_models  # type: ignore

            def builder(page, size):
                return slb_models.DescribeLoadBalancersRequest(
                    region_id=self._region, page_number=page, page_size=size
                )
            def extractor(resp): return resp.body.load_balancers.load_balancer
            def total(resp):    return resp.body.total_count

            from utils.pagination import alibaba_paginate
            for lb in alibaba_paginate(
                lambda r: self._call(self._slb_client.describe_load_balancers, r),
                builder, extractor, total,
            ):
                resources.append(self._make_resource(
                    lb.load_balancer_id,
                    lb.load_balancer_name or lb.load_balancer_id,
                    ResourceType.ALIBABA_SLB,
                    parent_id=lb.vpc_id,
                    status=lb.load_balancer_status,
                    layer=1,
                    estimated_cost=14.0,  # Alibaba SLB ~$14/month
                ))
        except Exception as exc:
            self._emit(f"[Alibaba] Error scanning SLB: {exc}")
        return resources

    # ── wait helper ───────────────────────────────────────────────────────────

    def _wait_instance(self, instance_id: str, target_status: str, timeout: int = 120) -> None:
        from alibabacloud_ecs20140526 import models as ecs_models  # type: ignore
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self._call(
                self._ecs_client.describe_instances,
                ecs_models.DescribeInstancesRequest(
                    region_id=self._region, instance_ids=f'["{instance_id}"]',
                ),
            )
            instances = resp.body.instances.instance
            if instances and instances[0].status == target_status:
                return
            time.sleep(5)
        raise TimeoutError(f"Instance {instance_id} did not reach {target_status} within {timeout}s")

    # ── Phase 3: Function Compute ─────────────────────────────────────────────

    def _scan_fc_functions(self) -> list[CloudResource]:
        resources = []
        if not self._fc_client: return []
        try:
            from alibabacloud_fc_open20210406 import models as fc_models  # type: ignore
            # Iterate through services first
            services_resp = self._call(self._fc_client.list_services, fc_models.ListServicesRequest())
            for svc in services_resp.body.services:
                svc_name = svc.service_name
                # List functions in service
                fns_resp = self._call(self._fc_client.list_functions, svc_name, fc_models.ListFunctionsRequest())
                for fn in fns_resp.body.functions:
                    resources.append(self._make_resource(
                        fn.function_name, fn.function_name, ResourceType.ALIBABA_FUNCTION,
                        parent_id=svc_name, status="active",
                        metadata={"service": svc_name, "runtime": fn.runtime},
                        layer=1, estimated_cost=0.0,
                    ))
        except Exception as exc:
            self._emit(f"[Alibaba] Error scanning Function Compute: {exc}")
        return resources

    # ── Phase 3: ACR (Alibaba Container Registry) ────────────────────────────

    def _scan_acr_repos(self) -> list[CloudResource]:
        resources = []
        if not self._cr_client: return []
        try:
            from alibabacloud_cr20181201 import models as cr_models  # type: ignore
            
            def builder(page, size):
                return cr_models.ListRepoRequest(page_no=page, page_size=size)
            def extractor(resp): return resp.body.repos
            def total(resp):    return resp.body.total_count

            from utils.pagination import alibaba_paginate
            for repo in alibaba_paginate(
                lambda r: self._call(self._cr_client.list_repo, r),
                builder, extractor, total,
            ):
                resources.append(self._make_resource(
                    repo.repo_name, repo.repo_name, ResourceType.ALIBABA_CR,
                    parent_id=repo.instance_id, status=repo.repo_status,
                    metadata={"namespace": repo.repo_namespace_name, "type": repo.repo_type},
                    layer=1, estimated_cost=0.0,
                ))
        except Exception as exc:
            self._emit(f"[Alibaba] Error scanning ACR: {exc}")
        return resources

    # ── Phase 3: MNS Topics ───────────────────────────────────────────────────

    def _scan_mns_topics(self) -> list[CloudResource]:
        resources = []
        if not self._mns_client: return []
        try:
            from alibabacloud_mns_open20220119 import models as mns_models  # type: ignore
            resp = self._call(self._mns_client.list_topic, mns_models.ListTopicRequest())
            for topic in getattr(resp.body.data, 'page_data', []):
                resources.append(self._make_resource(
                    topic.topic_name, topic.topic_name, ResourceType.ALIBABA_MNS,
                    status="active",
                    metadata={}, layer=1, estimated_cost=0.0,
                ))
        except Exception as exc:
            self._emit(f"[Alibaba] Error scanning MNS: {exc}")
        return resources

    # ── Phase 3: DNS ──────────────────────────────────────────────────────────

    def _scan_dns_zones(self) -> list[CloudResource]:
        resources = []
        if not self._dns_client: return []
        try:
            from alibabacloud_alidns20150109 import models as dns_models  # type: ignore
            
            def builder(page, size):
                return dns_models.DescribeDomainsRequest(page_number=page, page_size=size)
            def extractor(resp): return resp.body.domains.domain
            def total(resp):    return resp.body.total_count

            from utils.pagination import alibaba_paginate
            for domain in alibaba_paginate(
                lambda r: self._call(self._dns_client.describe_domains, r),
                builder, extractor, total,
            ):
                resources.append(self._make_resource(
                    domain.domain_name, domain.domain_name, ResourceType.ALIBABA_DNS,
                    status="active",
                    metadata={"domain_id": domain.domain_id}, layer=1,
                    estimated_cost=0.0,
                ))
        except Exception as exc:
            self._emit(f"[Alibaba] Error scanning Alibaba DNS: {exc}")
        return resources

