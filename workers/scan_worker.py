"""
workers/scan_worker.py
──────────────────────
Background worker that runs provider scans (comprehensive or hierarchical).
"""

from __future__ import annotations

from typing import Any, Callable

from concurrent.futures import ThreadPoolExecutor, as_completed

from providers.base_provider import CloudProvider
from workers.base_worker import BaseWorker
from models.resource import CloudResource
import json


class ScanWorker(BaseWorker):
    """
    Runs a cloud provider scan on a background thread.

    Parameters
    ----------
    provider_factory : Callable that returns a connected CloudProvider instance
    provider_name    : Name of the provider (for logging)
    regions          : List of regions to scan
    mode             : "comprehensive" | "hierarchical"
    """

    def __init__(self, provider_factory: Callable[[], CloudProvider],
                 provider_name: str, regions: list[str], mode: str,
                 state_manager: Any = None, parent: Any = None) -> None:
        super().__init__(parent)
        self._provider_factory = provider_factory
        self._provider_name    = provider_name
        self._regions          = regions
        self._mode             = mode
        self._state_manager    = state_manager

    def run(self) -> None:
        try:
            self._log(f"[Scan] Starting {self._mode} scan for {self._provider_name} across {len(self._regions)} region(s)")

            all_resources = []
            total = len(self._regions)
            completed = 0

            # Single region: run synchronously to avoid overhead
            if total == 1:
                self._scan_region(self._regions[0], all_resources)
                completed = 1
                self.progress.emit(100)
            else:
                # Multi-region: parallelize with ThreadPoolExecutor (max 20 for high speed)
                with ThreadPoolExecutor(max_workers=20) as executor:
                    futures = {
                        executor.submit(self._scan_region, region, None): region
                        for region in self._regions
                    }

                    for future in as_completed(futures):
                        if self._check_cancelled():
                            self._log("[Scan] Cancelled by user. Shutting down threads...")
                            executor.shutdown(wait=False, cancel_futures=True)
                            break

                        try:
                            resources = future.result()
                            if resources:
                                all_resources.extend(resources)
                        except Exception as exc:
                            region = futures[future]
                            self._log(f"[Scan] Unhandled error scanning {region}: {exc}")

                        completed += 1
                        pct = int((completed) / total * 100)
                        self.progress.emit(pct)

            self._log(f"[Scan] Done — total {len(all_resources)} resources across {completed} region(s)")
            self.finished.emit(all_resources)

        except Exception as exc:
            self.error.emit(str(exc))
            self._log(f"[Scan] Fatal error: {exc}")

    def _scan_region(self, region: str, out_list: list = None) -> list:
        if self._check_cancelled():
            return []

        self._log(f"[Scan] Scanning region: {region}")
        resources = []
        try:
            provider = self._provider_factory()
            # Fast-path: Skip redundant connectivity checks inside parallel regions
            creds = getattr(provider, "_last_credentials", None) 
            # Note: connect() is already called by _factory in MainWindow,
            # but we can ensure it's fast if called again.
            # Actually, MainWindow's factory calls connect(creds) WITHOUT test_connection=False.
            # I should update MainWindow as well.
            # For AWS, only scan global services once (in the first region of the list)
            scan_global = False
            if self._provider_name == "AWS" and self._regions and region == self._regions[0]:
                scan_global = True

            if self._mode == "comprehensive":
                resources = provider.scan_comprehensive(region)
            else:
                # Use scan_global if provider is AWS
                if self._provider_name == "AWS":
                    resources = provider.scan_hierarchical(region, scan_global=scan_global)
                else:
                    resources = provider.scan_hierarchical(region)

            for r in resources:
                self.resource_found.emit(r)
                
            if out_list is not None:
                out_list.extend(resources)

            self._log(f"[Scan] {region}: {len(resources)} resources found")
            return resources
        except Exception as exc:
            self._log(f"[Scan] Error scanning {region}: {exc}")
            return []
