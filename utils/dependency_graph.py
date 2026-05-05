"""
utils/dependency_graph.py
─────────────────────────
Topological sort (Kahn's algorithm) for safe inside-out deletion ordering.

Deletion layers
───────────────
1  Compute / DB  (EC2, RDS, VMs, ECS instances, GCE instances, OCI instances)
2  Attached resources  (NICs, Disks, EIPs, Block Volumes)
3  Network gateways    (NAT GW, Internet GW, VPN GW, Service GW)
4  Subnet-level        (Subnets, Route Tables, Security Groups / NSGs)
5  Outermost container (VPC, VNET, VCN, Resource Group, Compartment)
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Iterable

from models.resource import CloudResource, ResourceType


# ── Layer mapping ─────────────────────────────────────────────────────────────

_LAYER: dict[ResourceType, int] = {
    # Layer 1 – compute / database
    ResourceType.INSTANCE:         1,
    ResourceType.RDS:              1,
    ResourceType.CLOUD_SQL:        1,
    ResourceType.LOAD_BALANCER:    1,
    # Layer 2 – attached resources
    ResourceType.NIC:              2,
    ResourceType.DISK:             2,
    ResourceType.EBS_VOLUME:       2,
    ResourceType.BLOCK_VOLUME:     2,
    ResourceType.EIP:              2,
    ResourceType.PUBLIC_IP:        2,
    # Layer 3 – gateways
    ResourceType.NAT_GATEWAY:      3,
    ResourceType.INTERNET_GATEWAY: 3,
    # Layer 4 – subnet level
    ResourceType.SUBNET:           4,
    ResourceType.ROUTE_TABLE:      4,
    ResourceType.SECURITY_GROUP:   4,
    ResourceType.NSG:              4,
    ResourceType.FIREWALL_RULE:    4,
    # Layer 5 – outermost containers
    ResourceType.VPC:              5,
    ResourceType.VNET:             5,
    ResourceType.VCN:              5,
    ResourceType.RESOURCE_GROUP:   5,
    ResourceType.COMPARTMENT:      5,
    ResourceType.PROJECT:          5,
}


def assign_deletion_layer(resource: CloudResource) -> int:
    return _LAYER.get(resource.resource_type, 1)


def get_safe_deletion_order(resources: Iterable[CloudResource]) -> list[CloudResource]:
    """
    Return a safe, ordered list for deletion.

    Algorithm
    ---------
    1. Assign each resource its deletion layer (1-5).
    2. Build an explicit dependency graph from CloudResource.dependencies.
    3. Run Kahn's topological sort within each layer group.
    4. Concatenate groups from layer 1 → 5.

    Circular dependencies (e.g., AWS SG cross-refs) are detected and surfaced
    as a warning; the affected resources are appended at their layer's end so
    rule-stripping logic can handle them.
    """
    res_list = list(resources)

    # Index by id
    by_id: dict[str, CloudResource] = {r.resource_id: r for r in res_list}

    # Assign layers (Skipped to preserve provider-specific overrides)
    # for r in res_list:
    #     r.deletion_layer = assign_deletion_layer(r)

    # Group by layer
    layers: dict[int, list[CloudResource]] = defaultdict(list)
    for r in res_list:
        layers[r.deletion_layer].append(r)

    ordered: list[CloudResource] = []

    for layer_num in sorted(layers.keys()):
        group = layers[layer_num]
        ordered.extend(_topo_sort(group, by_id))

    return ordered


def _topo_sort(
    group: list[CloudResource],
    by_id: dict[str, CloudResource],
) -> list[CloudResource]:
    """Kahn's algorithm within a single layer group."""
    group_ids = {r.resource_id for r in group}

    # Build in-degree map and adjacency for this group only
    in_degree: dict[str, int] = {r.resource_id: 0 for r in group}
    adj: dict[str, list[str]] = defaultdict(list)

    for r in group:
        for dep_id in r.dependencies:
            if dep_id in group_ids:
                # r depends on dep_id  →  dep_id must come first
                in_degree[r.resource_id] = in_degree.get(r.resource_id, 0) + 1
                adj[dep_id].append(r.resource_id)

    queue: deque[str] = deque(
        rid for rid, deg in in_degree.items() if deg == 0
    )
    result: list[CloudResource] = []

    while queue:
        rid = queue.popleft()
        result.append(by_id[rid])
        for neighbour in adj[rid]:
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    # Handle circular dependency residue
    remaining = [
        by_id[rid] for rid, deg in in_degree.items() if deg > 0
    ]
    if remaining:
        # Warn only – still append so rules can be stripped first
        import warnings
        names = [r.display_name for r in remaining]
        warnings.warn(
            f"Circular dependency detected among: {names}. "
            "Appending at end of layer; ensure rule-stripping is applied first."
        )
        result.extend(remaining)

    return result
