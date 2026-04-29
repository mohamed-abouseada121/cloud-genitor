"""
config/scheduler.py
───────────────────
In-app periodic scan scheduler using QTimer.
Runs a silent comprehensive scan and emits a signal when new orphans are found.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

log = logging.getLogger(__name__)


class ScanScheduler(QObject):
    """
    Fires `scan_due` signal at the configured interval.
    The main window connects this to start a background ScanWorker.
    """

    scan_due         = pyqtSignal(str, str)   # provider, region
    new_orphans_found = pyqtSignal(str, int)  # provider, count-delta

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._provider: str = ""
        self._region:   str = ""
        self._interval_ms: int = 0
        self._on_scan_complete_cb: Optional[Callable] = None

    # ── public API ────────────────────────────────────────────────────────────

    def configure(self, provider: str, region: str, interval_minutes: int) -> None:
        self._provider = provider
        self._region   = region
        self._interval_ms = interval_minutes * 60 * 1000

    def start(self) -> None:
        if self._interval_ms <= 0:
            return
        self._timer.start(self._interval_ms)
        log.info(
            f"Scheduler started: {self._provider}/{self._region} "
            f"every {self._interval_ms // 60000} min"
        )

    def stop(self) -> None:
        self._timer.stop()
        log.info("Scheduler stopped.")

    def set_on_scan_complete(self, cb: Callable) -> None:
        """
        cb(provider, region, new_count, old_count) — called after each
        scheduled scan completes (wired by main_window).
        """
        self._on_scan_complete_cb = cb

    # ── internals ─────────────────────────────────────────────────────────────

    def _on_tick(self) -> None:
        log.info(f"Scheduler tick: firing scan for {self._provider}/{self._region}")
        self.scan_due.emit(self._provider, self._region)

    # ── email notification ────────────────────────────────────────────────────

    @staticmethod
    def send_email_notification(
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        to_address: str,
        provider: str,
        count: int,
    ) -> None:
        """Send a plain-text email alert when new orphans are detected."""
        subject = f"[Cloud Janitor] {count} new orphaned resources found in {provider}"
        body = (
            f"Cloud Janitor detected {count} new orphaned / unused resources "
            f"in {provider}.\n\nOpen the application to review and clean them up."
        )
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = to_address

        try:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.sendmail(smtp_user, [to_address], msg.as_string())
            log.info(f"Email notification sent to {to_address}")
        except Exception as exc:
            log.error(f"Failed to send email: {exc}")
