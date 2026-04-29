"""
providers/aws_manager.py
────────────────────────
AWS cloud provider: VPCs, EC2, EBS, EIP, RDS.

Deletion order (inside-out):
  Layer 1: RDS instances, EC2 instances
  Layer 2: EBS volumes, EIPs, ENIs
  Layer 3: NAT Gateways, Internet Gateways
  Layer 4: Subnets, Route Tables, Security Groups
  Layer 5: VPCs
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from models.resource import (
    CloudResource, DeletionStatus, ProviderName, ResourceType,
)
from providers.base_provider import CloudProvider
from utils.credentials import AWSCredentials
from utils.pagination import aws_paginate

log = logging.getLogger(__name__)


class AWSManager(CloudProvider):

    provider_name = "AWS"
    icon_path     = "assets/icons/aws.png"
    supported_resource_types = [
        ResourceType.VPC, ResourceType.SUBNET, ResourceType.INSTANCE,
        ResourceType.EBS_VOLUME, ResourceType.EIP, ResourceType.RDS,
        ResourceType.SECURITY_GROUP, ResourceType.INTERNET_GATEWAY,
        ResourceType.NAT_GATEWAY, ResourceType.ROUTE_TABLE, ResourceType.NIC,
    ]

    def __init__(self, log_callback=None) -> None:
        super().__init__(log_callback)
        self._session: Any = None
        self._region: str  = "us-east-1"

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self, credentials: AWSCredentials) -> bool:
        try:
            from utils.credentials import CredentialManager
            mgr = CredentialManager()
            self._session = mgr.get_aws_session(credentials)
            self._region  = credentials.region
            # Quick connectivity test
            self._ec2().describe_account_attributes()
            self._emit(f"[AWS] Connected — profile={credentials.profile_name} region={self._region}")
            return True
        except Exception as exc:
            self._emit(f"[AWS] Connection failed: {exc}")
            return False

    # ── regions ───────────────────────────────────────────────────────────────

    def list_regions(self) -> list[str]:
        ec2 = self._ec2()
        resp = ec2.describe_regions(Filters=[{"Name": "opt-in-status",
                                              "Values": ["opt-in-not-required", "opted-in"]}])
        return sorted(r["RegionName"] for r in resp["Regions"])

    # ── scanning ──────────────────────────────────────────────────────────────

    def scan_comprehensive(self, region: str) -> list[CloudResource]:
        """Return only unattached / orphaned resources."""
        self._region = region
        resources: list[CloudResource] = []
        resources.extend(self._scan_unattached_ebs())
        resources.extend(self._scan_free_eips())
        resources.extend(self._scan_instances(states=["stopped"]))
        resources.extend(self._scan_rds())
        self._emit(f"[AWS] Comprehensive scan done — {len(resources)} orphans found in {region}")
        return resources

    def scan_hierarchical(self, region: str) -> list[CloudResource]:
        """Return all resources ordered for tree display."""
        self._region = region
        resources: list[CloudResource] = []
        resources.extend(self._scan_vpcs())
        resources.extend(self._scan_subnets())
        resources.extend(self._scan_instances())
        resources.extend(self._scan_ebs_volumes())
        resources.extend(self._scan_eips())
        resources.extend(self._scan_rds())
        resources.extend(self._scan_internet_gateways())
        resources.extend(self._scan_nat_gateways())
        resources.extend(self._scan_route_tables())
        resources.extend(self._scan_security_groups())
        self._emit(f"[AWS] Hierarchical scan done — {len(resources)} resources in {region}")
        return resources

    # ── deletion ──────────────────────────────────────────────────────────────

    def delete_resource(self, resource: CloudResource, dry_run: bool = False) -> str:
        prefix = "[DRY RUN] " if dry_run else ""
        self._emit(f"{prefix}[AWS] Deleting {resource.resource_type.value}: {resource.display_name}")

        if dry_run:
            return f"DRY RUN: would delete {resource.resource_type.value} {resource.resource_id}"

        handlers = {
            ResourceType.INSTANCE:         self._delete_instance,
            ResourceType.EBS_VOLUME:       self._delete_ebs,
            ResourceType.EIP:              self._delete_eip,
            ResourceType.RDS:              self._delete_rds,
            ResourceType.INTERNET_GATEWAY: self._delete_igw,
            ResourceType.NAT_GATEWAY:      self._delete_nat_gw,
            ResourceType.SUBNET:           self._delete_subnet,
            ResourceType.ROUTE_TABLE:      self._delete_route_table,
            ResourceType.SECURITY_GROUP:   self._delete_security_group,
            ResourceType.VPC:              self._delete_vpc,
        }
        handler = handlers.get(resource.resource_type)
        if handler:
            return handler(resource)
        return f"No deletion handler for {resource.resource_type.value}"

    # ── private scan helpers ──────────────────────────────────────────────────

    def _ec2(self, region: Optional[str] = None):
        return self._session.client("ec2", region_name=region or self._region)

    def _rds_client(self):
        return self._session.client("rds", region_name=self._region)

    def _make_resource(self, rid, name, rtype, vpc_id=None,
                       status="", metadata=None, layer=1,
                       estimated_cost=0.0, age_days=-1, tags=None) -> CloudResource:
        return CloudResource(
            resource_id=rid,
            name=name,
            resource_type=rtype,
            provider=ProviderName.AWS,
            region=self._region,
            parent_id=vpc_id,
            deletion_layer=layer,
            status=status,
            estimated_cost=estimated_cost,
            metadata=metadata or {},
            tags=tags or {},
            age_days=age_days,
        )

    @staticmethod
    def _age(launch_str: Optional[str]) -> int:
        if not launch_str:
            return -1
        try:
            dt = datetime.fromisoformat(str(launch_str).replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - dt).days
        except Exception:
            return -1

    @staticmethod
    def _tags_dict(tags_list: list) -> dict:
        return {t["Key"]: t["Value"] for t in (tags_list or [])}

    @staticmethod
    def _name_from_tags(tags_list: list, fallback: str = "") -> str:
        tags = {t["Key"]: t["Value"] for t in (tags_list or [])}
        return tags.get("Name", fallback)

    # ── VPCs ─────────────────────────────────────────────────────────────────

    def _scan_vpcs(self) -> list[CloudResource]:
        ec2 = self._ec2()
        resources = []
        for vpc in aws_paginate(ec2, "describe_vpcs", "Vpcs"):
            rid  = vpc["VpcId"]
            name = self._name_from_tags(vpc.get("Tags", []), rid)
            resources.append(self._make_resource(
                rid, name, ResourceType.VPC,
                status=vpc.get("State", ""),
                metadata=vpc, layer=5,
                tags=self._tags_dict(vpc.get("Tags", [])),
            ))
        return resources

    # ── Subnets ───────────────────────────────────────────────────────────────

    def _scan_subnets(self) -> list[CloudResource]:
        ec2 = self._ec2()
        resources = []
        for s in aws_paginate(ec2, "describe_subnets", "Subnets"):
            rid   = s["SubnetId"]
            name  = self._name_from_tags(s.get("Tags", []), rid)
            resources.append(self._make_resource(
                rid, name, ResourceType.SUBNET,
                vpc_id=s.get("VpcId"),
                status=s.get("State", ""),
                metadata=s, layer=4,
                tags=self._tags_dict(s.get("Tags", [])),
            ))
        return resources

    # ── EC2 instances ─────────────────────────────────────────────────────────

    def _scan_instances(self, states: Optional[list] = None) -> list[CloudResource]:
        ec2 = self._ec2()
        filters = []
        if states:
            filters.append({"Name": "instance-state-name", "Values": states})
        resources = []
        for reservation in aws_paginate(ec2, "describe_instances", "Reservations",
                                        Filters=filters):
            for inst in reservation.get("Instances", []):
                rid   = inst["InstanceId"]
                name  = self._name_from_tags(inst.get("Tags", []), rid)
                state = inst.get("State", {}).get("Name", "")
                resources.append(self._make_resource(
                    rid, name, ResourceType.INSTANCE,
                    vpc_id=inst.get("VpcId"),
                    status=state,
                    metadata=inst, layer=1,
                    age_days=self._age(inst.get("LaunchTime")),
                    tags=self._tags_dict(inst.get("Tags", [])),
                ))
        return resources

    # ── EBS Volumes ───────────────────────────────────────────────────────────

    def _scan_ebs_volumes(self) -> list[CloudResource]:
        ec2 = self._ec2()
        resources = []
        for vol in aws_paginate(ec2, "describe_volumes", "Volumes"):
            rid  = vol["VolumeId"]
            name = self._name_from_tags(vol.get("Tags", []), rid)
            size_gb = vol.get("Size", 0)
            # ~$0.10/GB/month for gp2
            cost = round(size_gb * 0.10, 4)
            resources.append(self._make_resource(
                rid, name, ResourceType.EBS_VOLUME,
                status=vol.get("State", ""),
                metadata=vol, layer=2,
                estimated_cost=cost,
                age_days=self._age(vol.get("CreateTime")),
                tags=self._tags_dict(vol.get("Tags", [])),
            ))
        return resources

    def _scan_unattached_ebs(self) -> list[CloudResource]:
        ec2 = self._ec2()
        resources = []
        for vol in aws_paginate(ec2, "describe_volumes", "Volumes",
                                Filters=[{"Name": "status", "Values": ["available"]}]):
            rid  = vol["VolumeId"]
            name = self._name_from_tags(vol.get("Tags", []), rid)
            size_gb = vol.get("Size", 0)
            cost    = round(size_gb * 0.10, 4)
            resources.append(self._make_resource(
                rid, name, ResourceType.EBS_VOLUME,
                status="unattached",
                metadata=vol, layer=2,
                estimated_cost=cost,
                age_days=self._age(vol.get("CreateTime")),
                tags=self._tags_dict(vol.get("Tags", [])),
            ))
        return resources

    # ── EIPs ──────────────────────────────────────────────────────────────────

    def _scan_eips(self) -> list[CloudResource]:
        ec2 = self._ec2()
        resp = ec2.describe_addresses()
        resources = []
        for addr in resp.get("Addresses", []):
            rid  = addr.get("AllocationId", addr.get("PublicIp", ""))
            name = self._name_from_tags(addr.get("Tags", []), addr.get("PublicIp", rid))
            resources.append(self._make_resource(
                rid, name, ResourceType.EIP,
                status="associated" if addr.get("AssociationId") else "free",
                metadata=addr, layer=2,
                estimated_cost=0.005 * 730,   # ~$3.65/mo for idle EIP
                tags=self._tags_dict(addr.get("Tags", [])),
            ))
        return resources

    def _scan_free_eips(self) -> list[CloudResource]:
        return [r for r in self._scan_eips() if r.status == "free"]

    # ── RDS ───────────────────────────────────────────────────────────────────

    def _scan_rds(self) -> list[CloudResource]:
        rds = self._rds_client()
        resources = []
        paginator = rds.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            for db in page["DBInstances"]:
                rid  = db["DBInstanceIdentifier"]
                resources.append(self._make_resource(
                    rid, rid, ResourceType.RDS,
                    status=db.get("DBInstanceStatus", ""),
                    metadata=db, layer=1,
                    age_days=self._age(db.get("InstanceCreateTime")),
                ))
        return resources

    # ── Internet Gateways ─────────────────────────────────────────────────────

    def _scan_internet_gateways(self) -> list[CloudResource]:
        ec2 = self._ec2()
        resources = []
        for igw in aws_paginate(ec2, "describe_internet_gateways", "InternetGateways"):
            rid  = igw["InternetGatewayId"]
            name = self._name_from_tags(igw.get("Tags", []), rid)
            attachments = igw.get("Attachments", [])
            vpc_id = attachments[0]["VpcId"] if attachments else None
            resources.append(self._make_resource(
                rid, name, ResourceType.INTERNET_GATEWAY,
                vpc_id=vpc_id,
                status="attached" if attachments else "detached",
                metadata=igw, layer=3,
                tags=self._tags_dict(igw.get("Tags", [])),
            ))
        return resources

    # ── NAT Gateways ──────────────────────────────────────────────────────────

    def _scan_nat_gateways(self) -> list[CloudResource]:
        ec2 = self._ec2()
        resources = []
        for nat in aws_paginate(ec2, "describe_nat_gateways", "NatGateways",
                                Filter=[{"Name": "state",
                                         "Values": ["available", "pending"]}]):
            rid  = nat["NatGatewayId"]
            name = self._name_from_tags(nat.get("Tags", []), rid)
            resources.append(self._make_resource(
                rid, name, ResourceType.NAT_GATEWAY,
                vpc_id=nat.get("VpcId"),
                status=nat.get("State", ""),
                metadata=nat, layer=3,
                estimated_cost=32.40,   # ~$0.045/h * 720 h
                tags=self._tags_dict(nat.get("Tags", [])),
            ))
        return resources

    # ── Route Tables ─────────────────────────────────────────────────────────

    def _scan_route_tables(self) -> list[CloudResource]:
        ec2 = self._ec2()
        resources = []
        for rt in aws_paginate(ec2, "describe_route_tables", "RouteTables"):
            rid   = rt["RouteTableId"]
            name  = self._name_from_tags(rt.get("Tags", []), rid)
            assoc = rt.get("Associations", [])
            is_main = any(a.get("Main") for a in assoc)
            resources.append(self._make_resource(
                rid, name, ResourceType.ROUTE_TABLE,
                vpc_id=rt.get("VpcId"),
                status="main" if is_main else "custom",
                metadata=rt, layer=4,
                tags=self._tags_dict(rt.get("Tags", [])),
            ))
        return resources

    # ── Security Groups ───────────────────────────────────────────────────────

    def _scan_security_groups(self) -> list[CloudResource]:
        ec2 = self._ec2()
        resources = []
        for sg in aws_paginate(ec2, "describe_security_groups", "SecurityGroups"):
            rid  = sg["GroupId"]
            name = sg.get("GroupName", rid)
            resources.append(self._make_resource(
                rid, name, ResourceType.SECURITY_GROUP,
                vpc_id=sg.get("VpcId"),
                status="default" if sg.get("GroupName") == "default" else "custom",
                metadata=sg, layer=4,
                tags=self._tags_dict(sg.get("Tags", [])),
            ))
        return resources

    # ── deletion helpers ──────────────────────────────────────────────────────

    def _delete_instance(self, resource: CloudResource) -> str:
        ec2 = self._ec2()
        ec2.terminate_instances(InstanceIds=[resource.resource_id])
        waiter = ec2.get_waiter("instance_terminated")
        waiter.wait(InstanceIds=[resource.resource_id])
        return f"Terminated EC2 {resource.resource_id}"

    def _delete_ebs(self, resource: CloudResource) -> str:
        self._ec2().delete_volume(VolumeId=resource.resource_id)
        return f"Deleted EBS volume {resource.resource_id}"

    def _delete_eip(self, resource: CloudResource) -> str:
        ec2 = self._ec2()
        # Disassociate first if associated
        assoc_id = resource.metadata.get("AssociationId")
        if assoc_id:
            ec2.disassociate_address(AssociationId=assoc_id)
        ec2.release_address(AllocationId=resource.resource_id)
        return f"Released EIP {resource.resource_id}"

    def _delete_rds(self, resource: CloudResource) -> str:
        self._rds_client().delete_db_instance(
            DBInstanceIdentifier=resource.resource_id,
            SkipFinalSnapshot=True,
        )
        return f"Deleted RDS instance {resource.resource_id}"

    def _delete_igw(self, resource: CloudResource) -> str:
        ec2  = self._ec2()
        igw  = resource.resource_id
        vpc_id = resource.parent_id or resource.metadata.get("Attachments", [{}])[0].get("VpcId")
        if vpc_id:
            ec2.detach_internet_gateway(InternetGatewayId=igw, VpcId=vpc_id)
        ec2.delete_internet_gateway(InternetGatewayId=igw)
        return f"Deleted Internet Gateway {igw}"

    def _delete_nat_gw(self, resource: CloudResource) -> str:
        ec2 = self._ec2()
        ec2.delete_nat_gateway(NatGatewayId=resource.resource_id)
        # Poll until deleted (can take minutes)
        for _ in range(60):
            resp  = ec2.describe_nat_gateways(NatGatewayIds=[resource.resource_id])
            state = resp["NatGateways"][0]["State"]
            if state == "deleted":
                break
            time.sleep(10)
        return f"Deleted NAT Gateway {resource.resource_id}"

    def _delete_subnet(self, resource: CloudResource) -> str:
        self._ec2().delete_subnet(SubnetId=resource.resource_id)
        return f"Deleted Subnet {resource.resource_id}"

    def _delete_route_table(self, resource: CloudResource) -> str:
        if resource.status == "main":
            return f"Skipped main route table {resource.resource_id}"
        self._ec2().delete_route_table(RouteTableId=resource.resource_id)
        return f"Deleted Route Table {resource.resource_id}"

    def _delete_security_group(self, resource: CloudResource) -> str:
        if resource.status == "default":
            return f"Skipped default SG {resource.resource_id}"
        ec2 = self._ec2()
        # Strip all inbound/outbound rules first (handles circular refs)
        sg_data = ec2.describe_security_groups(GroupIds=[resource.resource_id])
        sg      = sg_data["SecurityGroups"][0]
        if sg.get("IpPermissions"):
            ec2.revoke_security_group_ingress(
                GroupId=resource.resource_id, IpPermissions=sg["IpPermissions"]
            )
        if sg.get("IpPermissionsEgress"):
            ec2.revoke_security_group_egress(
                GroupId=resource.resource_id, IpPermissions=sg["IpPermissionsEgress"]
            )
        ec2.delete_security_group(GroupId=resource.resource_id)
        return f"Deleted Security Group {resource.resource_id}"

    def _delete_vpc(self, resource: CloudResource) -> str:
        self._ec2().delete_vpc(VpcId=resource.resource_id)
        return f"Deleted VPC {resource.resource_id}"
