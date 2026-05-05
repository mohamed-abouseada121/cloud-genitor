"""
workers/delete_worker.py
────────────────────────
Background worker that deletes selected cloud resources in safe dependency order.
Integrates with StateManager for crash recovery and retry support.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import pyqtSignal

from db.state_manager import StateManager
from models.resource import CloudResource, DeletionStatus
from providers.base_provider import CloudProvider
from workers.base_worker import BaseWorker


class DeleteWorker(BaseWorker):
    """
    Deletes resources in safe topological order (layer 1 → 5).

    Parameters
    ----------
    provider      : connected CloudProvider instance
    resources     : list of CloudResource objects to delete (pre-sorted by caller
                    OR passed unsorted — worker will sort via get_deletion_order)
    dry_run       : if True, simulate deletion without actual API calls
    state_manager : optional StateManager for audit logging
    """

    # Extra signal: per-resource delete result
    resource_deleted = pyqtSignal(object, str)  # (CloudResource, result_msg)

    def __init__(
        self,
        provider: CloudProvider,
        resources: list[CloudResource],
        dry_run: bool = False,
        state_manager: Any = None,
        retry: bool = False,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._provider      = provider
        self._resources     = resources
        self._dry_run       = dry_run
        self._retry         = retry
        self._state_manager: StateManager | None = state_manager

    def run(self) -> None:
        try:
            # Sort resources into safe deletion order
            ordered = self._provider.get_deletion_order(self._resources)
            total   = len(ordered)

            self._log(
                f"[Delete] Starting deletion of {total} resources "
                f"({'DRY RUN' if self._dry_run else 'LIVE'}) "
                f"via {self._provider.provider_name}"
            )

            current_layer = None
            failed_layers: set[int] = set()
            succeeded = 0
            failed    = 0

            for idx, resource in enumerate(ordered):
                if self._check_cancelled():
                    self._log("[Delete] Cancelled by user.")
                    break

                # Layer barrier: if a previous layer had failures, block dependents
                if resource.deletion_layer in failed_layers:
                    msg = (f"BLOCKED: {resource.display_name} "
                           f"(layer {resource.deletion_layer} blocked due to earlier failure)")
                    self._log(f"[Delete] {msg}")
                    resource.deletion_status = DeletionStatus.BLOCKED
                    if self._state_manager:
                        self._state_manager.log_attempt(resource)
                        self._state_manager.mark_blocked(resource.resource_id)
                    self.resource_deleted.emit(resource, msg)
                    continue

                # Announce layer transitions
                if resource.deletion_layer != current_layer:
                    if current_layer is not None:
                        # Wait for cloud to fully process previous layer deletions
                        import time
                        self._log("[Delete] Waiting 5s for cloud to finalize previous deletions…")
                        time.sleep(5)
                    current_layer = resource.deletion_layer
                    self._log(f"[Delete] ── Layer {current_layer} ─────────────────────")

                # Log attempt
                if self._state_manager and not self._dry_run:
                    self._state_manager.log_attempt(resource)

                max_attempts = 3 if self._retry else 1
                for attempt in range(max_attempts):
                    try:
                        result = self._provider.delete_resource(resource, dry_run=self._dry_run)
                        resource.deletion_status = DeletionStatus.SKIPPED if self._dry_run else DeletionStatus.SUCCESS
                        self._log(f"[Delete] ✓ {result}")

                        if self._state_manager and not self._dry_run:
                            self._state_manager.mark_success(resource.resource_id)

                        self.resource_deleted.emit(resource, result)
                        succeeded += 1
                        break  # Success, exit retry loop

                    except Exception as exc:
                        error_msg = str(exc)
                        if attempt < max_attempts - 1:
                            self._log(f"[Delete] ⚠ Deletion failed for {resource.display_name}: {error_msg} — retrying ({attempt+1}/{max_attempts}) in 3s...")
                            import time
                            time.sleep(3)
                        else:
                            resource.deletion_status = DeletionStatus.FAILED
                            resource.error_message   = error_msg
                            self._log(f"[Delete] ✗ Failed: {resource.display_name} — {error_msg}")

                            if self._state_manager and not self._dry_run:
                                self._state_manager.mark_failed(resource.resource_id, error_msg)

                            # Block all higher layers from proceeding
                            for layer in range(resource.deletion_layer + 1, 6):
                                failed_layers.add(layer)

                            self.resource_deleted.emit(resource, f"FAILED: {error_msg}")
                            failed += 1

                pct = int((idx + 1) / total * 100)
                self.progress.emit(pct)

            summary = (
                f"[Delete] Complete — {succeeded} succeeded, {failed} failed"
                f"{', BLOCKED layers: ' + str(sorted(failed_layers)) if failed_layers else ''}"
                f"{' [DRY RUN]' if self._dry_run else ''}"
            )
            self._log(summary)
            self.finished.emit(ordered)

        except Exception as exc:
            self.error.emit(str(exc))
            self._log(f"[Delete] Fatal error: {exc}")
