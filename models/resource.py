"""
models/resource.py
──────────────────
Core data structures shared across all cloud providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── Enumerations ─────────────────────────────────────────────────────────────

class ProviderName(str, Enum):
    AWS     = "AWS"
    AZURE   = "Azure"
    ALIBABA = "Alibaba"
    GCP     = "GCP"
    ORACLE  = "Oracle"


class ResourceType(str, Enum):
    # Networking
    VPC              = "VPC"
    VNET             = "VNET"
    VCN              = "VCN"
    SUBNET           = "Subnet"
    ROUTE_TABLE      = "Route Table"
    SECURITY_GROUP   = "Security Group"
    NSG              = "NSG"
    INTERNET_GATEWAY = "Internet Gateway"
    NAT_GATEWAY      = "NAT Gateway"
    NIC              = "NIC"
    FIREWALL_RULE    = "Firewall Rule"
    # Compute
    INSTANCE         = "Instance"
    # Storage
    DISK             = "Disk"
    EBS_VOLUME       = "EBS Volume"
    BLOCK_VOLUME     = "Block Volume"
    # IP
    EIP              = "EIP"
    PUBLIC_IP        = "Public IP"
    # Database
    RDS              = "RDS"
    # Containers / High-level
    LOAD_BALANCER    = "Load Balancer"
    # Organisational
    RESOURCE_GROUP   = "Resource Group"
    COMPARTMENT      = "Compartment"
    PROJECT          = "Project"
    # Generic fallback
    UNKNOWN          = "Unknown"


class DeletionStatus(str, Enum):
    PENDING   = "PENDING"
    SUCCESS   = "SUCCESS"
    FAILED    = "FAILED"
    BLOCKED   = "BLOCKED"   # blocked because a dependency failed
    SKIPPED   = "SKIPPED"   # dry-run or user-cancelled
    SNAPSHOT  = "SNAPSHOT"  # pre-deletion snapshot in progress


# ── Main data class ───────────────────────────────────────────────────────────

@dataclass
class CloudResource:
    """
    Universal representation of a cloud resource across all five providers.

    Attributes
    ----------
    resource_id       : Provider-assigned unique identifier.
    name              : Human-readable display name.
    resource_type     : Enum describing the kind of resource.
    provider          : Which cloud provider owns this resource.
    region            : Provider region / location string.
    parent_id         : resource_id of the logical parent (VPC, RG, etc.).
    deletion_layer    : 1 = innermost (delete first), 5 = outermost (delete last).
    status            : Current lifecycle status reported by the provider.
    estimated_cost    : Estimated monthly cost in USD (0.0 if unknown).
    dependencies      : List of resource_ids that must be deleted before this one.
    metadata          : Raw API response dict — stored as-is for the preview dialog.
    tags              : Flat dict of resource tags.
    age_days          : Age in days since creation (–1 if unknown).
    """

    resource_id:    str
    name:           str
    resource_type:  ResourceType
    provider:       ProviderName
    region:         str

    parent_id:      Optional[str]    = None
    deletion_layer: int              = 1
    status:         str              = ""
    estimated_cost: float            = 0.0
    dependencies:   list[str]        = field(default_factory=list)
    metadata:       dict             = field(default_factory=dict)
    tags:           dict             = field(default_factory=dict)
    age_days:       int              = -1

    # Assigned at runtime by the StateManager / DeleteWorker
    deletion_status: Optional[DeletionStatus] = None
    error_message:   Optional[str]            = None

    # ── helpers ──────────────────────────────────────────────────────────────

    @property
    def display_name(self) -> str:
        return self.name or self.resource_id

    @property
    def cost_label(self) -> str:
        if self.estimated_cost <= 0:
            return "–"
        return f"${self.estimated_cost:,.2f}/mo"

    @property
    def tags_summary(self) -> str:
        if not self.tags:
            return "(no tags)"
        return ", ".join(f"{k}={v}" for k, v in list(self.tags.items())[:5])

    def to_dict(self) -> dict:
        return {
            "resource_id":    self.resource_id,
            "name":           self.name,
            "resource_type":  self.resource_type.value,
            "provider":       self.provider.value,
            "region":         self.region,
            "parent_id":      self.parent_id,
            "deletion_layer": self.deletion_layer,
            "status":         self.status,
            "estimated_cost": self.estimated_cost,
            "dependencies":   self.dependencies,
            "tags":           self.tags,
            "age_days":       self.age_days,
        }
