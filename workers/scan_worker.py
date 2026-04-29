"""
workers/scan_worker.py
──────────────────────
Background worker that runs provider scans (comprehensive or hierarchical).
"""

from __future__ import annotations

from typing import Any

from providers.base_provider import CloudProvider
from workers.base_worker import BaseWorker


class ScanWorker(BaseWorker):
    """
    Runs a cloud provider scan on a background thread.

    Parameters
    ----------
    provider : CloudProvider instance (already connected)
    region   : region string, or "All Regions" to iterate all available regions
    mode     : "comprehensive" | "hierarchical"
    """

    def __init__(self, provider: CloudProvider, region: str, mode: str,
                 parent: Any = None) -> None:
        super().__init__(parent)
        self._provider = provider
        self._region   = region
        self._mode     = mode

    def run(self) -> None:
        try:
            self._log(f"[Scan] Starting {self._mode} scan for {self._provider.provider_name} / {self._region}")

            regions = self._resolve_regions()
            all_resources = []
            total = len(regions)

            for idx, region in enumerate(regions):
                if self._check_cancelled():
                    self._log("[Scan] Cancelled by user.")
                    break

                self._log(f"[Scan] Scanning region: {region}")
                try:
                    if self._mode == "comprehensive":
                        resources = self._provider.scan_comprehensive(region)
                    else:
                        resources = self._provider.scan_hierarchical(region)

                    for r in resources:
                        self.resource_found.emit(r)
                    all_resources.extend(resources)

                    self._log(f"[Scan] {region}: {len(resources)} resources found")
                except Exception as exc:
                    self._log(f"[Scan] Error scanning {region}: {exc}")

                pct = int((idx + 1) / total * 100)
                self.progress.emit(pct)

            self._log(f"[Scan] Done — total {len(all_resources)} resources across {total} region(s)")
            self.finished.emit(all_resources)

        except Exception as exc:
            self.error.emit(str(exc))
            self._log(f"[Scan] Fatal error: {exc}")

    def _resolve_regions(self) -> list[str]:
        if self._region.lower() == "all regions":
            try:
                return self._provider.list_regions()
            except Exception as exc:
                self._log(f"[Scan] Could not list regions: {exc}")
                return [self._region]
        return [self._region]
