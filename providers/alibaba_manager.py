"""
providers/alibaba_manager.py
────────────────────────────
Alibaba Cloud provider: VPCs, ECS Instances, Cloud Disks, EIPs.

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
        ResourceType.NAT_GATEWAY,
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
        self._limiter: Optional[_RateLimiter] = None

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
        self._emit(f"[Alibaba] Hierarchical scan done — {len(resources)} resources in {region}")
        return resources

    # ── deletion ──────────────────────────────────────────────────────────────

    def delete_resource(self, resource: CloudResource, dry_run: bool = False) -> str:
        prefix = "[DRY RUN] " if dry_run else ""
        self._emit(f"{prefix}[Alibaba] Deleting {resource.resource_type.value}: {resource.display_name}")
        if dry_run:
            return f"DRY RUN: would delete {resource.resource_type.value} {resource.resource_id}"

        from alibabacloud_ecs20140526 import models as ecs_models  # type: ignore
        from alibabacloud_vpc20160428 import models as vpc_models  # type: ignore

        rid = resource.resource_id
        rt  = resource.resource_type

        if rt == ResourceType.INSTANCE:
            # Must be in Stopped state first
            self._call(self._ecs_client.stop_instances,
                       ecs_models.StopInstancesRequest(instance_id=[rid]))
            self._wait_instance(rid, "Stopped")
            self._call(self._ecs_client.delete_instance,
                       ecs_models.DeleteInstanceRequest(instance_id=rid, force=True))
            return f"Deleted ECS instance {rid}"

        if rt == ResourceType.DISK:
            self._call(self._ecs_client.delete_disk,
                       ecs_models.DeleteDiskRequest(disk_id=rid))
            return f"Deleted disk {rid}"

        if rt == ResourceType.EIP:
            self._call(self._vpc_client.release_eip_address,
                       vpc_models.ReleaseEipAddressRequest(allocation_id=rid))
            return f"Released EIP {rid}"

        if rt == ResourceType.SUBNET:   # VSwitch
            self._call(self._vpc_client.delete_v_switch,
                       vpc_models.DeleteVSwitchRequest(v_switch_id=rid))
            return f"Deleted VSwitch {rid}"

        if rt == ResourceType.SECURITY_GROUP:
            self._call(self._ecs_client.delete_security_group,
                       ecs_models.DeleteSecurityGroupRequest(security_group_id=rid))
            return f"Deleted Security Group {rid}"

        if rt == ResourceType.VPC:
            self._call(self._vpc_client.delete_vpc,
                       vpc_models.DeleteVpcRequest(vpc_id=rid))
            return f"Deleted VPC {rid}"

        return f"No deletion handler for {rt.value}"

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
            lambda r: self._call(self._vpc_client.describe_v_switches, r),
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
