"""
workers/snapshot_worker.py
──────────────────────────
Pre-deletion snapshot / backup worker.
Creates provider snapshots for Disks/Volumes and exports JSON config for network resources.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from PyQt6.QtCore import pyqtSignal

from models.resource import CloudResource, ProviderName, ResourceType
from workers.base_worker import BaseWorker

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "..", "reports")


class SnapshotWorker(BaseWorker):
    """
    Takes safety snapshots / config exports BEFORE deletion.

    Parameters
    ----------
    resources : list of CloudResource to snapshot
    provider  : connected CloudProvider (used to resolve clients)
    """

    snapshot_done = pyqtSignal(object, str)  # (resource, snapshot_id_or_path)

    def __init__(self, resources: list[CloudResource],
                 provider: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self._resources = resources
        self._provider  = provider

    def run(self) -> None:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        total   = len(self._resources)
        results: dict[str, str] = {}   # resource_id → snapshot_id/path

        for idx, resource in enumerate(self._resources):
            if self._check_cancelled():
                self._log("[Snapshot] Cancelled.")
                break

            try:
                snap_ref = self._snapshot_resource(resource)
                results[resource.resource_id] = snap_ref
                self._log(f"[Snapshot] ✓ {resource.display_name} → {snap_ref}")
                self.snapshot_done.emit(resource, snap_ref)
            except Exception as exc:
                self._log(f"[Snapshot] ✗ {resource.display_name}: {exc}")
                self.snapshot_done.emit(resource, f"ERROR: {exc}")

            self.progress.emit(int((idx + 1) / total * 100))

        self._log(f"[Snapshot] Done — {len(results)}/{total} snapshots/exports completed.")
        self.finished.emit(results)

    def _snapshot_resource(self, resource: CloudResource) -> str:
        """
        Dispatch to provider-specific snapshot logic.
        Returns a snapshot ID or local file path.
        """
        rt = resource.resource_type

        # ── Disk / Volume snapshots ───────────────────────────────────────────
        if resource.provider == ProviderName.AWS and rt == ResourceType.EBS_VOLUME:
            return self._aws_snapshot_ebs(resource)

        if resource.provider == ProviderName.AZURE and rt == ResourceType.DISK:
            return self._azure_snapshot_disk(resource)

        if resource.provider == ProviderName.ALIBABA and rt == ResourceType.DISK:
            return self._alibaba_snapshot_disk(resource)

        if resource.provider == ProviderName.GCP and rt == ResourceType.DISK:
            return self._gcp_snapshot_disk(resource)

        if resource.provider == ProviderName.ORACLE and rt == ResourceType.BLOCK_VOLUME:
            return self._oracle_snapshot_volume(resource)

        # ── JSON config export for all other resource types ───────────────────
        return self._export_json(resource)

    # ── AWS EBS snapshot ──────────────────────────────────────────────────────

    def _aws_snapshot_ebs(self, resource: CloudResource) -> str:
        import boto3  # type: ignore
        session = boto3.session.Session()
        ec2 = session.client("ec2", region_name=resource.region)
        resp = ec2.create_snapshot(
            VolumeId=resource.resource_id,
            Description=f"cloud-janitor pre-delete {datetime.utcnow().isoformat()}",
        )
        return resp["SnapshotId"]

    # ── Azure Disk snapshot ───────────────────────────────────────────────────

    def _azure_snapshot_disk(self, resource: CloudResource) -> str:
        from azure.identity import DefaultAzureCredential  # type: ignore
        from azure.mgmt.compute import ComputeManagementClient  # type: ignore

        rg  = resource.metadata.get("resource_group", "")
        sub = resource.metadata.get("subscription_id", "")
        cred = DefaultAzureCredential()
        client = ComputeManagementClient(cred, sub)

        snap_name = f"janitor-snap-{resource.resource_id[:20]}"
        poller = client.snapshots.begin_create_or_update(
            rg, snap_name,
            {
                "location": resource.metadata.get("location", ""),
                "creation_data": {
                    "create_option": "Copy",
                    "source_resource_id": resource.metadata.get("disk_id", resource.resource_id),
                },
            },
        )
        snap = poller.result()
        return snap.name

    # ── Alibaba disk snapshot ─────────────────────────────────────────────────

    def _alibaba_snapshot_disk(self, resource: CloudResource) -> str:
        from alibabacloud_ecs20140526.client import Client as EcsClient  # type: ignore
        from alibabacloud_ecs20140526 import models as ecs_models        # type: ignore

        # Re-use provider's existing client if available via metadata
        resp = self._provider._ecs_client.create_snapshot(
            ecs_models.CreateSnapshotRequest(
                disk_id=resource.resource_id,
                snapshot_name=f"janitor-snap-{resource.resource_id[:20]}",
            )
        )
        return resp.body.snapshot_id

    # ── GCP disk snapshot ─────────────────────────────────────────────────────

    def _gcp_snapshot_disk(self, resource: CloudResource) -> str:
        from google.cloud import compute_v1  # type: ignore

        client  = compute_v1.DisksClient(credentials=self._provider._credentials)
        snap_rq = compute_v1.Snapshot(
            name=f"janitor-snap-{resource.resource_id[:40].replace('_', '-')}",
            description=f"cloud-janitor pre-delete {datetime.utcnow().isoformat()}",
        )
        op = client.create_snapshot(
            project=self._provider._project_id,
            zone=resource.metadata.get("zone", ""),
            disk=resource.resource_id,
            snapshot_resource=snap_rq,
        )
        return op.name  # operation name used as ref

    # ── Oracle volume backup ──────────────────────────────────────────────────

    def _oracle_snapshot_volume(self, resource: CloudResource) -> str:
        import oci  # type: ignore

        client = oci.core.BlockstorageClient(self._provider._config)
        resp   = client.create_volume_backup(
            oci.models.CreateVolumeBackupDetails(
                volume_id=resource.resource_id,
                display_name=f"janitor-snap-{resource.resource_id[:30]}",
                type="INCREMENTAL",
            )
        )
        return resp.data.id

    # ── Generic JSON export ───────────────────────────────────────────────────

    def _export_json(self, resource: CloudResource) -> str:
        ts        = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe_name = resource.resource_id.replace("/", "-").replace(":", "-")
        filename  = f"config_{resource.provider.value}_{safe_name}_{ts}.json"
        path      = os.path.join(REPORTS_DIR, filename)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(resource.to_dict(), fh, indent=2, ensure_ascii=False)
        return path
