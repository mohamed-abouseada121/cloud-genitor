"""
providers/aws_manager.py
────────────────────────
AWS cloud provider: VPCs, EC2, EBS, EIP, RDS, S3, Load Balancers.

Deletion order (inside-out):
  Layer 1: RDS instances, EC2 instances, Load Balancers, S3 Buckets
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
from utils.credentials import AWSCredentials, CredentialManager
from utils.pagination import aws_paginate
from botocore.config import Config

log = logging.getLogger(__name__)


class AWSManager(CloudProvider):

    provider_name = "AWS"
    icon_path     = "assets/icons/aws.png"
    supported_resource_types = [
        ResourceType.VPC, ResourceType.SUBNET, ResourceType.INSTANCE,
        ResourceType.EBS_VOLUME, ResourceType.EIP, ResourceType.RDS,
        ResourceType.SECURITY_GROUP, ResourceType.INTERNET_GATEWAY,
        ResourceType.NAT_GATEWAY, ResourceType.ROUTE_TABLE, ResourceType.NIC,
        ResourceType.KUBERNETES, ResourceType.KUBERNETES_NODE_GROUP, ResourceType.KUBERNETES_ADDON,
        # Phase 1
        ResourceType.S3_BUCKET,
        ResourceType.LB_ALB, ResourceType.LB_NLB, ResourceType.LB_CLASSIC,
        # Phase 2
        ResourceType.LAMBDA_FUNCTION, ResourceType.ECR_REPO,
        ResourceType.DYNAMODB_TABLE, ResourceType.ELASTICACHE,
        ResourceType.SQS_QUEUE, ResourceType.SNS_TOPIC,
        ResourceType.ROUTE53_ZONE, ResourceType.CLOUDFRONT,
        # Phase 3
        ResourceType.FARGATE_TASK, ResourceType.AWS_SECRET, ResourceType.CLOUDWATCH_ALARM,
    ]

    def __init__(self, log_callback=None) -> None:
        super().__init__(log_callback)
        self._session: Any = None
        self._region: str  = "us-east-1"

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self, credentials: AWSCredentials, test_connection: bool = True) -> bool:
        try:
            self._session = CredentialManager().get_aws_session(credentials)
            self._region  = credentials.region
            self._config = Config(
                connect_timeout=5,
                read_timeout=10,
                retries={'max_attempts': 1}
            )
            
            if test_connection:
                # Quick connectivity test
                test_config = Config(connect_timeout=10, read_timeout=10, retries={'max_attempts': 0})
                self._session.client("ec2", region_name=self._region, config=test_config).describe_account_attributes()
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
        if not self._session:
            return []
        self._region = region
        resources: list[CloudResource] = []
        resources.extend(self._scan_unattached_ebs())
        resources.extend(self._scan_free_eips())
        resources.extend(self._scan_instances(states=["stopped"]))
        resources.extend(self._scan_rds())
        self._emit(f"[AWS] Comprehensive scan done — {len(resources)} orphans found in {region}")
        return resources

    def scan_hierarchical(self, region: str, scan_global: bool = False) -> list[CloudResource]:
        """Return all resources ordered for tree display."""
        if not self._session:
            return []
        self._region = region
        # Internal parallelism: scan multiple AWS services at once
        from concurrent.futures import ThreadPoolExecutor
        tasks = [
            self._scan_vpcs, self._scan_subnets, self._scan_instances,
            self._scan_ebs_volumes, self._scan_eips, self._scan_rds,
            self._scan_internet_gateways, self._scan_nat_gateways,
            self._scan_eks, self._scan_route_tables, self._scan_security_groups,
            self._scan_load_balancers
        ]
        
        # Only scan global services if requested
        if scan_global:
            tasks.extend([
                self._scan_s3_buckets, self._scan_lambdas, self._scan_ecr_repos,
                self._scan_dynamodb_tables, self._scan_elasticache_clusters,
                self._scan_sqs_queues, self._scan_sns_topics,
                self._scan_route53_zones, self._scan_cloudfront_distributions,
                self._scan_ecs_clusters, self._scan_secrets,
                self._scan_cloudwatch_alarms
            ])

        resources: list[CloudResource] = []
        from concurrent.futures import as_completed
        skipped = False
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_task = {executor.submit(t): t.__name__ for t in tasks}
            for f in as_completed(future_to_task):
                try:
                    res = f.result()
                    if res: resources.extend(res)
                except Exception as e:
                    err_str = str(e)
                    # Handle common auth/region-not-enabled errors gracefully
                    if any(x in err_str for x in ["UnrecognizedClientException", "InvalidAccessKeyId", "AuthFailure", "InvalidClientTokenId"]):
                         if not skipped:
                             self._emit(f"[AWS] Region {region} access denied (opt-in required?) - skipping.")
                             skipped = True
                    else:
                         self._emit(f"[AWS] Internal scan error in {region}: {e}")
        
        self._emit(f"[AWS] Hierarchical scan done — {len(resources)} resources in {region}")
        return resources

    # ── deletion ──────────────────────────────────────────────────────────────

    def delete_resource(self, resource: CloudResource, dry_run: bool = False) -> str:
        prefix = "[DRY RUN] " if dry_run else ""
        self._emit(f"{prefix}[AWS] Deleting {resource.resource_type.value}: {resource.display_name} in {resource.region}")

        if dry_run:
            return f"DRY RUN: would delete {resource.resource_type.value} {resource.resource_id}"

        # Ensure we are in the correct region for this resource
        old_region = self._region
        if resource.region and resource.region != old_region:
            self._region = resource.region

        try:
            handlers = {
                ResourceType.INSTANCE:         self._delete_instance,
                ResourceType.EBS_VOLUME:       self._delete_ebs,
                ResourceType.EIP:              self._delete_eip,
                ResourceType.RDS:              self._delete_rds,
                ResourceType.INTERNET_GATEWAY: self._delete_igw,
                ResourceType.NAT_GATEWAY:      self._delete_nat_gw,
                ResourceType.KUBERNETES:       self._delete_eks,
                ResourceType.KUBERNETES_NODE_GROUP: self._delete_eks_nodegroup,
                ResourceType.KUBERNETES_ADDON: self._delete_eks_addon,
                ResourceType.SUBNET:           self._delete_subnet,
                ResourceType.ROUTE_TABLE:      self._delete_route_table,
                ResourceType.SECURITY_GROUP:   self._delete_security_group,
                ResourceType.VPC:              self._delete_vpc,
                ResourceType.S3_BUCKET:        self._delete_s3_bucket,
                ResourceType.LB_ALB:           self._delete_elbv2,
                ResourceType.LB_NLB:           self._delete_elbv2,
                ResourceType.LB_CLASSIC:       self._delete_elb_classic,
                # Phase 2
                ResourceType.LAMBDA_FUNCTION:  self._delete_lambda,
                ResourceType.ECR_REPO:         self._delete_ecr_repo,
                ResourceType.DYNAMODB_TABLE:   self._delete_dynamodb_table,
                ResourceType.ELASTICACHE:      self._delete_elasticache,
                ResourceType.SQS_QUEUE:        self._delete_sqs_queue,
                ResourceType.SNS_TOPIC:        self._delete_sns_topic,
                ResourceType.ROUTE53_ZONE:     self._delete_route53_zone,
                ResourceType.CLOUDFRONT:       self._delete_cloudfront,
                # Phase 3
                ResourceType.FARGATE_TASK:     self._delete_ecs_cluster,
                ResourceType.AWS_SECRET:       self._delete_secret,
                ResourceType.CLOUDWATCH_ALARM: self._delete_cloudwatch_alarm,
            }
            handler = handlers.get(resource.resource_type)
            if handler:
                return handler(resource)
            return f"No deletion handler for {resource.resource_type.value}"
        except Exception as exc:
            # Catch 404/NotFound errors from boto3
            exc_str = str(exc).lower()
            if "notfound" in exc_str or "nosuch" in exc_str or "does not exist" in exc_str:
                self._emit(f"[AWS] Resource {resource.resource_id} already deleted.")
                return f"Already deleted: {resource.resource_id}"
            raise exc
        finally:
            self._region = old_region

    # ── private scan helpers ──────────────────────────────────────────────────

    def _ec2(self, region: Optional[str] = None):
        return self._session.client("ec2", region_name=region or self._region, config=self._config)

    def _rds_client(self):
        return self._session.client("rds", region_name=self._region, config=self._config)

    def _eks_client(self):
        return self._session.client("eks", region_name=self._region, config=self._config)

    def _s3_client(self):
        # S3 is global, use us-east-1 to avoid regional opt-in issues
        return self._session.client("s3", region_name="us-east-1", config=self._config)

    def _elb_client(self, region: Optional[str] = None):
        return self._session.client("elb", region_name=region or self._region, config=self._config)

    def _elbv2_client(self, region: Optional[str] = None):
        return self._session.client("elbv2", region_name=region or self._region, config=self._config)

    def _elb_classic_client(self):
        return self._elb_client()

    def _lambda_client(self):
        return self._session.client("lambda", region_name=self._region, config=self._config)

    def _ecr_client(self):
        return self._session.client("ecr", region_name=self._region, config=self._config)

    def _dynamodb_client(self):
        return self._session.client("dynamodb", region_name=self._region, config=self._config)

    def _elasticache_client(self):
        return self._session.client("elasticache", region_name=self._region, config=self._config)

    def _sqs_client(self):
        return self._session.client("sqs", region_name=self._region, config=self._config)

    def _sns_client(self):
        return self._session.client("sns", region_name=self._region, config=self._config)

    def _route53_client(self):
        # Route53 is global
        return self._session.client("route53", region_name="us-east-1", config=self._config)

    def _cloudfront_client(self):
        # CloudFront is global
        return self._session.client("cloudfront", region_name="us-east-1", config=self._config)

    def _ecs_client(self):
        return self._session.client("ecs", region_name=self._region, config=self._config)

    def _secretsmanager_client(self):
        return self._session.client("secretsmanager", region_name=self._region, config=self._config)

    def _cloudwatch_client(self):
        return self._session.client("cloudwatch", region_name=self._region, config=self._config)

    def _make_resource(self, rid, name, rtype, vpc_id=None,
                       status="", metadata=None, layer=1,
                       estimated_cost=0.0, age_days=0, tags=None) -> CloudResource:
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
                # Estimate cost: ~$0.05/hour for a general purpose instance ≈ $36/month
                cost = 36.0 if state == "running" else 0.0
                resources.append(self._make_resource(
                    rid, name, ResourceType.INSTANCE,
                    vpc_id=inst.get("VpcId"),
                    status=state,
                    metadata=inst, layer=1,
                    age_days=self._get_age(inst),
                    estimated_cost=cost,
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
            
            # Extract InstanceIds from Attachments as dependencies
            deps = []
            for att in vol.get("Attachments", []):
                if att.get("InstanceId"):
                    deps.append(att["InstanceId"])

            resources.append(self._make_resource(
                rid, name, ResourceType.EBS_VOLUME,
                status=vol.get("State", ""),
                metadata=vol, layer=2,
                estimated_cost=cost,
                age_days=self._get_age(vol),
                tags=self._tags_dict(vol.get("Tags", [])),
            ))
            # Add dependencies
            resources[-1].dependencies = deps
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
                # RDS instance cost estimate (db.t3.medium class ≈ $0.10/hour ≈ $72/month)
                cost = 72.0 if db.get("DBInstanceStatus") == "available" else 0.0
                resources.append(self._make_resource(
                    rid, rid, ResourceType.RDS,
                    status=db.get("DBInstanceStatus", ""),
                    metadata=db, layer=1,
                    age_days=self._get_age(db),
                    estimated_cost=cost,
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

    # ── EKS ───────────────────────────────────────────────────────────────────

    def _scan_eks(self) -> list[CloudResource]:
        from botocore.config import Config
        resources = []
        try:
            # EKS can be slow to respond, give it more time than other services
            eks_config = Config(connect_timeout=15, read_timeout=20, retries={'max_attempts': 1})
            eks = self._session.client("eks", region_name=self._region, config=eks_config)
            
            resp = eks.list_clusters()
            for name in resp.get("clusters", []):
                cluster = eks.describe_cluster(name=name)["cluster"]
                rid = cluster["name"]
                resources.append(self._make_resource(
                    rid, name, ResourceType.KUBERNETES,
                    vpc_id=cluster.get("resourcesVpcConfig", {}).get("vpcId"),
                    status=cluster.get("status", ""),
                    metadata=cluster, layer=2,  # Layer 2, so Node Groups (Layer 1) are deleted first
                    age_days=self._age(cluster.get("createdAt")),
                    tags=cluster.get("tags", {}),
                ))
                
                # Scan Node Groups
                try:
                    for ng_page in eks.get_paginator("list_nodegroups").paginate(clusterName=name):
                        for ng_name in ng_page.get("nodegroups", []):
                            ng = eks.describe_nodegroup(clusterName=name, nodegroupName=ng_name)["nodegroup"]
                            resources.append(self._make_resource(
                                ng["nodegroupArn"], ng_name, ResourceType.KUBERNETES_NODE_GROUP,
                                parent_id=rid, status=ng.get("status", ""),
                                metadata={"clusterName": name}, layer=1,
                            ))
                except Exception as e:
                    self._emit(f"[AWS] Warning scanning EKS Node Groups for {name}: {e}")

                # Scan Addons
                try:
                    for addon_page in eks.get_paginator("list_addons").paginate(clusterName=name):
                        for addon_name in addon_page.get("addons", []):
                            addon = eks.describe_addon(clusterName=name, addonName=addon_name)["addon"]
                            resources.append(self._make_resource(
                                addon["addonArn"], addon_name, ResourceType.KUBERNETES_ADDON,
                                parent_id=rid, status=addon.get("status", ""),
                                metadata={"clusterName": name}, layer=1,
                            ))
                except Exception as e:
                    self._emit(f"[AWS] Warning scanning EKS Addons for {name}: {e}")

        except Exception as exc:
            raise exc
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

    # ── S3 Buckets ────────────────────────────────────────────────────────────

    def _scan_s3_buckets(self) -> list[CloudResource]:
        """List all S3 buckets in the account (S3 is global; we show all)."""
        s3 = self._s3_client()
        resources = []
        try:
            resp = s3.list_buckets()
            for bucket in resp.get("Buckets", []):
                name = bucket["Name"]
                created = bucket.get("CreationDate")
                age = self._age(str(created)) if created else -1

                # Get bucket size & object count via CloudWatch (best-effort)
                size_gb, cost = self._get_s3_bucket_size_cost(s3, name)

                # Get bucket location to filter by region if needed
                try:
                    loc_resp = s3.get_bucket_location(Bucket=name)
                    location = loc_resp.get("LocationConstraint") or "us-east-1"
                except Exception:
                    location = "unknown"

                # Get tags (best-effort)
                tags = {}
                try:
                    tag_resp = s3.get_bucket_tagging(Bucket=name)
                    tags = {t["Key"]: t["Value"] for t in tag_resp.get("TagSet", [])}
                except Exception:
                    pass  # NoSuchTagSet is normal

                resources.append(self._make_resource(
                    name, name, ResourceType.S3_BUCKET,
                    status=location,
                    metadata={"location": location, "size_gb": size_gb},
                    layer=1,
                    estimated_cost=cost,
                    age_days=age,
                    tags=tags,
                ))
        except Exception as exc:
            raise exc
        return resources

    def _get_s3_bucket_size_cost(self, s3_client, bucket_name: str) -> tuple[float, float]:
        """Get approximate bucket size via CloudWatch metrics. Returns (size_gb, cost_usd)."""
        try:
            cw = self._session.client("cloudwatch", region_name=self._region)
            from datetime import datetime, timezone, timedelta
            end   = datetime.now(timezone.utc)
            start = end - timedelta(days=2)
            resp  = cw.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName="BucketSizeBytes",
                Dimensions=[
                    {"Name": "BucketName",  "Value": bucket_name},
                    {"Name": "StorageType", "Value": "StandardStorage"},
                ],
                StartTime=start,
                EndTime=end,
                Period=86400,
                Statistics=["Average"],
            )
            datapoints = resp.get("Datapoints", [])
            if datapoints:
                size_bytes = max(dp["Average"] for dp in datapoints)
                size_gb    = round(size_bytes / (1024 ** 3), 4)
                cost       = round(size_gb * 0.023, 4)  # S3 Standard ~$0.023/GB
                return size_gb, cost
        except Exception:
            pass
        return 0.0, 0.0

    # ── Load Balancers (ALB / NLB) ─────────────────────────────────────────────

    def _scan_load_balancers(self) -> list[CloudResource]:
        """Scan ALB, NLB (ELBv2) and Classic ELBs."""
        resources = []
        resources.extend(self._scan_elbv2())
        resources.extend(self._scan_elb_classic())
        return resources

    def _scan_elbv2(self) -> list[CloudResource]:
        """Scan Application Load Balancers (ALB) and Network Load Balancers (NLB)."""
        elb = self._elb_client()
        resources = []
        try:
            paginator = elb.get_paginator("describe_load_balancers")
            for page in paginator.paginate():
                for lb in page.get("LoadBalancers", []):
                    lb_type = lb.get("Type", "").lower()   # "application" | "network" | "gateway"
                    rid     = lb["LoadBalancerArn"]
                    name    = lb["LoadBalancerName"]
                    vpc_id  = lb.get("VpcId")

                    if lb_type == "application":
                        rtype = ResourceType.LB_ALB
                        # ALB base cost ~$16.2/month + LCU charges (base only here)
                        cost  = 16.20
                    elif lb_type == "network":
                        rtype = ResourceType.LB_NLB
                        cost  = 16.20
                    else:
                        rtype = ResourceType.LOAD_BALANCER
                        cost  = 16.20

                    # Get tags
                    tags = {}
                    try:
                        tag_resp = elb.describe_tags(ResourceArns=[rid])
                        for td in tag_resp.get("TagDescriptions", []):
                            tags = {t["Key"]: t["Value"] for t in td.get("Tags", [])}
                    except Exception:
                        pass

                    resources.append(self._make_resource(
                        rid, name, rtype,
                        vpc_id=vpc_id,
                        status=lb.get("State", {}).get("Code", ""),
                        metadata=lb, layer=1,
                        estimated_cost=cost,
                        tags=tags,
                    ))
        except Exception as exc:
            raise exc
        return resources

    def _scan_elb_classic(self) -> list[CloudResource]:
        """Scan Classic (v1) Load Balancers."""
        elb = self._elb_classic_client()
        resources = []
        try:
            paginator = elb.get_paginator("describe_load_balancers")
            for page in paginator.paginate():
                for lb in page.get("LoadBalancerDescriptions", []):
                    name   = lb["LoadBalancerName"]
                    vpc_id = lb.get("VpcId")
                    resources.append(self._make_resource(
                        name, name, ResourceType.LB_CLASSIC,
                        vpc_id=vpc_id,
                        status="active",
                        metadata=lb, layer=1,
                        estimated_cost=18.0,  # Classic LB ~$18/month
                    ))
        except Exception as exc:
            raise exc
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

    def _delete_eks(self, resource: CloudResource) -> str:
        self._eks_client().delete_cluster(name=resource.resource_id)
        return f"Initiated deletion of EKS cluster {resource.resource_id}"

    def _delete_eks_nodegroup(self, resource: CloudResource) -> str:
        cluster_name = resource.metadata.get("clusterName")
        self._eks_client().delete_nodegroup(clusterName=cluster_name, nodegroupName=resource.display_name)
        return f"Initiated deletion of EKS Node Group {resource.display_name}"

    def _delete_eks_addon(self, resource: CloudResource) -> str:
        cluster_name = resource.metadata.get("clusterName")
        self._eks_client().delete_addon(clusterName=cluster_name, addonName=resource.display_name)
        return f"Deleted EKS Addon {resource.display_name}"

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

    def _delete_s3_bucket(self, resource: CloudResource) -> str:
        """Delete an S3 bucket — empties all objects/versions first."""
        s3   = self._s3_client()
        name = resource.resource_id

        # Step 1: Delete all object versions (handles versioned buckets)
        try:
            s3_resource = self._session.resource("s3", region_name=self._region)
            bucket = s3_resource.Bucket(name)
            self._emit(f"[AWS] Emptying S3 bucket {name} (deleting all objects/versions)…")
            bucket.object_versions.delete()
        except Exception as exc:
            # If versioning was never enabled, fall back to regular object delete
            self._emit(f"[AWS] Version delete fallback for {name}: {exc}")
            try:
                paginator = s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=name):
                    objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                    if objects:
                        s3.delete_objects(Bucket=name, Delete={"Objects": objects})
            except Exception as inner:
                self._emit(f"[AWS] Could not empty bucket {name}: {inner}")

        # Step 2: Delete the bucket itself
        s3.delete_bucket(Bucket=name)
        return f"Deleted S3 bucket {name}"

    def _delete_elbv2(self, resource: CloudResource) -> str:
        """Delete ALB or NLB by ARN."""
        self._elb_client().delete_load_balancer(
            LoadBalancerArn=resource.resource_id
        )
        return f"Deleted Load Balancer {resource.display_name}"

    def _delete_elb_classic(self, resource: CloudResource) -> str:
        """Delete a Classic ELB by name."""
        self._elb_classic_client().delete_load_balancer(
            LoadBalancerName=resource.resource_id
        )
        return f"Deleted Classic ELB {resource.resource_id}"

    # ── Phase 2: Lambda Functions ──────────────────────────────────────────────

    def _scan_lambdas(self) -> list[CloudResource]:
        lmb = self._lambda_client()
        resources = []
        try:
            paginator = lmb.get_paginator("list_functions")
            for page in paginator.paginate():
                for fn in page.get("Functions", []):
                    name    = fn["FunctionName"]
                    runtime = fn.get("Runtime", "")
                    size_mb = fn.get("CodeSize", 0) / (1024 * 1024)
                    resources.append(self._make_resource(
                        fn["FunctionArn"], name, ResourceType.LAMBDA_FUNCTION,
                        status=runtime,
                        metadata=fn, layer=1,
                        estimated_cost=0.0,  # Lambda cost is per-invocation, hard to estimate
                        age_days=self._age(fn.get("LastModified")),
                    ))
        except Exception as exc:
            raise exc
        return resources

    def _delete_lambda(self, resource: CloudResource) -> str:
        self._lambda_client().delete_function(FunctionName=resource.resource_id)
        return f"Deleted Lambda function {resource.display_name}"

    # ── Phase 2: ECR Repositories ──────────────────────────────────────────────

    def _scan_ecr_repos(self) -> list[CloudResource]:
        ecr = self._ecr_client()
        resources = []
        try:
            paginator = ecr.get_paginator("describe_repositories")
            for page in paginator.paginate():
                for repo in page.get("repositories", []):
                    name = repo["repositoryName"]
                    resources.append(self._make_resource(
                        repo["repositoryArn"], name, ResourceType.ECR_REPO,
                        status="active",
                        metadata=repo, layer=1,
                        age_days=self._get_age(repo),
                    ))
        except Exception as exc:
            raise exc
        return resources

    def _delete_ecr_repo(self, resource: CloudResource) -> str:
        """Delete ECR repo and all images inside it."""
        self._ecr_client().delete_repository(
            repositoryName=resource.display_name,
            force=True,  # deletes all images
        )
        return f"Deleted ECR repository {resource.display_name}"

    # ── Phase 2: DynamoDB Tables ───────────────────────────────────────────────

    def _scan_dynamodb_tables(self) -> list[CloudResource]:
        ddb = self._dynamodb_client()
        resources = []
        try:
            paginator = ddb.get_paginator("list_tables")
            for page in paginator.paginate():
                for table_name in page.get("TableNames", []):
                    try:
                        desc = ddb.describe_table(TableName=table_name)["Table"]
                        size_bytes = desc.get("TableSizeBytes", 0)
                        size_gb    = size_bytes / (1024 ** 3)
                        # DynamoDB on-demand ~$1.25/million writes + storage $0.25/GB
                        cost = round(size_gb * 0.25, 4)
                        resources.append(self._make_resource(
                            desc["TableArn"], table_name, ResourceType.DYNAMODB_TABLE,
                            status=desc.get("TableStatus", ""),
                            metadata=desc, layer=1,
                            estimated_cost=cost,
                            age_days=self._get_age(desc),
                        ))
                    except Exception:
                        pass
        except Exception as exc:
            raise exc
        return resources

    def _delete_dynamodb_table(self, resource: CloudResource) -> str:
        self._dynamodb_client().delete_table(TableName=resource.display_name)
        return f"Deleted DynamoDB table {resource.display_name}"

    # ── Phase 2: ElastiCache Clusters ──────────────────────────────────────────

    def _scan_elasticache_clusters(self) -> list[CloudResource]:
        ec = self._elasticache_client()
        resources = []
        try:
            paginator = ec.get_paginator("describe_cache_clusters")
            for page in paginator.paginate(ShowCacheNodeInfo=True):
                for cluster in page.get("CacheClusters", []):
                    cid    = cluster["CacheClusterId"]
                    engine = cluster.get("Engine", "")   # redis / memcached
                    # ElastiCache: ~$0.017–$0.40/hr depending on node type; use $25/mo as rough base
                    cost = 25.0
                    resources.append(self._make_resource(
                        cid, cid, ResourceType.ELASTICACHE,
                        status=cluster.get("CacheClusterStatus", ""),
                        metadata=cluster, layer=1,
                        estimated_cost=cost,
                        age_days=self._get_age(cluster),
                    ))
        except Exception as exc:
            raise exc
        return resources

    def _delete_elasticache(self, resource: CloudResource) -> str:
        self._elasticache_client().delete_cache_cluster(
            CacheClusterId=resource.resource_id
        )
        return f"Deleted ElastiCache cluster {resource.resource_id}"

    # ── Phase 2: SQS Queues ────────────────────────────────────────────────────

    def _scan_sqs_queues(self) -> list[CloudResource]:
        sqs = self._sqs_client()
        resources = []
        try:
            paginator = sqs.get_paginator("list_queues")
            for page in paginator.paginate():
                for url in page.get("QueueUrls", []):
                    name = url.split("/")[-1]
                    resources.append(self._make_resource(
                        url, name, ResourceType.SQS_QUEUE,
                        status="active",
                        metadata={"url": url}, layer=1,
                        estimated_cost=0.0,  # SQS is pay-per-use
                    ))
        except Exception as exc:
            raise exc
        return resources

    def _delete_sqs_queue(self, resource: CloudResource) -> str:
        self._sqs_client().delete_queue(QueueUrl=resource.resource_id)
        return f"Deleted SQS queue {resource.display_name}"

    # ── Phase 2: SNS Topics ────────────────────────────────────────────────────

    def _scan_sns_topics(self) -> list[CloudResource]:
        sns = self._sns_client()
        resources = []
        try:
            paginator = sns.get_paginator("list_topics")
            for page in paginator.paginate():
                for topic in page.get("Topics", []):
                    arn  = topic["TopicArn"]
                    name = arn.split(":")[-1]
                    resources.append(self._make_resource(
                        arn, name, ResourceType.SNS_TOPIC,
                        status="active",
                        metadata={"arn": arn}, layer=1,
                        estimated_cost=0.0,  # SNS is pay-per-use
                    ))
        except Exception as exc:
            raise exc
        return resources

    def _delete_sns_topic(self, resource: CloudResource) -> str:
        self._sns_client().delete_topic(TopicArn=resource.resource_id)
        return f"Deleted SNS topic {resource.display_name}"

    # ── Phase 2: Route53 Hosted Zones ──────────────────────────────────────────

    def _scan_route53_zones(self) -> list[CloudResource]:
        r53 = self._route53_client()
        resources = []
        try:
            paginator = r53.get_paginator("list_hosted_zones")
            for page in paginator.paginate():
                for zone in page.get("HostedZones", []):
                    zid  = zone["Id"].split("/")[-1]
                    name = zone["Name"]
                    # Route53: $0.50/zone/month
                    resources.append(self._make_resource(
                        zid, name, ResourceType.ROUTE53_ZONE,
                        status="private" if zone.get("Config", {}).get("PrivateZone") else "public",
                        metadata=zone, layer=1,
                        estimated_cost=0.50,
                    ))
        except Exception as exc:
            raise exc
        return resources

    def _delete_route53_zone(self, resource: CloudResource) -> str:
        """Delete Route53 zone — must remove all records first (except SOA/NS)."""
        r53 = self._route53_client()
        zone_id = resource.resource_id
        # List and delete all non-SOA/NS records
        try:
            paginator = r53.get_paginator("list_resource_record_sets")
            changes = []
            for page in paginator.paginate(HostedZoneId=zone_id):
                for rr in page.get("ResourceRecordSets", []):
                    if rr["Type"] not in ("SOA", "NS"):
                        changes.append({"Action": "DELETE", "ResourceRecordSet": rr})
            if changes:
                r53.change_resource_record_sets(
                    HostedZoneId=zone_id,
                    ChangeBatch={"Changes": changes},
                )
        except Exception as e:
            self._emit(f"[AWS] Warning clearing Route53 records: {e}")
        r53.delete_hosted_zone(Id=zone_id)
        return f"Deleted Route53 hosted zone {resource.display_name}"

    # ── Phase 2: CloudFront Distributions ─────────────────────────────────────

    def _scan_cloudfront_distributions(self) -> list[CloudResource]:
        cf = self._cloudfront_client()
        resources = []
        try:
            paginator = cf.get_paginator("list_distributions")
            for page in paginator.paginate():
                dist_list = page.get("DistributionList", {})
                for dist in dist_list.get("Items", []):
                    did    = dist["Id"]
                    domain = dist.get("DomainName", did)
                    # CloudFront: ~$1/month minimum + data transfer
                    resources.append(self._make_resource(
                        did, domain, ResourceType.CLOUDFRONT,
                        status=dist.get("Status", ""),
                        metadata=dist, layer=1,
                        estimated_cost=1.0,
                    ))
        except Exception as exc:
            raise exc
        return resources

    def _delete_cloudfront(self, resource: CloudResource) -> str:
        """Disable then delete a CloudFront distribution."""
        cf  = self._cloudfront_client()
        did = resource.resource_id
        # Get current config + ETag
        resp   = cf.get_distribution(Id=did)
        etag   = resp["ETag"]
        config = resp["Distribution"]["DistributionConfig"]
        if config.get("Enabled", True):
            config["Enabled"] = False
            upd = cf.update_distribution(Id=did, DistributionConfig=config, IfMatch=etag)
            etag = upd["ETag"]
            # Wait for disabled state
            waiter = cf.get_waiter("distribution_deployed")
            waiter.wait(Id=did)
            resp = cf.get_distribution(Id=did)
            etag = resp["ETag"]
        cf.delete_distribution(Id=did, IfMatch=etag)
        return f"Deleted CloudFront distribution {did}"

    # ── Phase 3: ECS Clusters / Fargate Tasks ─────────────────────────────────

    def _scan_ecs_clusters(self) -> list[CloudResource]:
        ecs = self._ecs_client()
        resources = []
        try:
            paginator = ecs.get_paginator("list_clusters")
            for page in paginator.paginate():
                for cluster_arn in page.get("clusterArns", []):
                    desc = ecs.describe_clusters(clusters=[cluster_arn]).get("clusters", [])
                    if desc:
                        cluster = desc[0]
                        name = cluster["clusterName"]
                        status = cluster.get("status", "")
                        resources.append(self._make_resource(
                            cluster_arn, name, ResourceType.FARGATE_TASK,
                            status=status, metadata=cluster, layer=1,
                            estimated_cost=0.0,
                        ))
        except Exception as exc:
            raise exc
        return resources

    def _delete_ecs_cluster(self, resource: CloudResource) -> str:
        """Delete ECS cluster (requires emptying services/tasks first in reality, best-effort here)."""
        ecs = self._ecs_client()
        cluster_arn = resource.resource_id
        
        # Best effort: stop all tasks and delete services
        try:
            # 1. Stop all tasks
            for task_page in ecs.get_paginator("list_tasks").paginate(cluster=cluster_arn):
                for task_arn in task_page.get("taskArns", []):
                    ecs.stop_task(cluster=cluster_arn, task=task_arn, reason="Cloud-Genitor Cleanup")
            
            # 2. Delete all services
            for svc_page in ecs.get_paginator("list_services").paginate(cluster=cluster_arn):
                for svc_arn in svc_page.get("serviceArns", []):
                    # Services must be scaled to 0 before deletion
                    ecs.update_service(cluster=cluster_arn, service=svc_arn, desiredCount=0)
                    ecs.delete_service(cluster=cluster_arn, service=svc_arn, force=True)
        except Exception as e:
            self._emit(f"[AWS] Warning cleaning up ECS services/tasks: {e}")

        # Delete cluster
        ecs.delete_cluster(cluster=cluster_arn)
        return f"Deleted ECS cluster {resource.display_name}"

    # ── Phase 3: Secrets Manager ──────────────────────────────────────────────

    def _scan_secrets(self) -> list[CloudResource]:
        sm = self._secretsmanager_client()
        resources = []
        try:
            paginator = sm.get_paginator("list_secrets")
            for page in paginator.paginate():
                for secret in page.get("SecretList", []):
                    arn = secret["ARN"]
                    name = secret["Name"]
                    # AWS Secrets Manager: $0.40 per secret per month
                    resources.append(self._make_resource(
                        arn, name, ResourceType.AWS_SECRET,
                        status="active", metadata=secret, layer=1,
                        estimated_cost=0.40,
                    ))
        except Exception as exc:
            raise exc
        return resources

    def _delete_secret(self, resource: CloudResource) -> str:
        """Delete secret without recovery window."""
        self._secretsmanager_client().delete_secret(
            SecretId=resource.resource_id,
            ForceDeleteWithoutRecovery=True
        )
        return f"Deleted AWS Secret {resource.display_name}"

    # ── Phase 3: CloudWatch Alarms ────────────────────────────────────────────

    def _scan_cloudwatch_alarms(self) -> list[CloudResource]:
        cw = self._cloudwatch_client()
        resources = []
        try:
            paginator = cw.get_paginator("describe_alarms")
            for page in paginator.paginate():
                for alarm in page.get("MetricAlarms", []):
                    arn = alarm["AlarmArn"]
                    name = alarm["AlarmName"]
                    # CloudWatch Alarm: $0.10 per alarm per month (standard)
                    resources.append(self._make_resource(
                        arn, name, ResourceType.CLOUDWATCH_ALARM,
                        status=alarm.get("StateValue", ""), metadata=alarm, layer=1,
                        estimated_cost=0.10,
                    ))
        except Exception as exc:
            raise exc
        return resources

    def _delete_cloudwatch_alarm(self, resource: CloudResource) -> str:
        self._cloudwatch_client().delete_alarms(
            AlarmNames=[resource.display_name]
        )
        return f"Deleted CloudWatch Alarm {resource.display_name}"

