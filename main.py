"""
main.py
────────
Application entry point.

Usage:
    python main.py

Startup sequence:
1. Create QApplication with dark Fusion palette
2. Load Settings (settings.json)
3. Load/create SQLite StateManager
4. Check for pending deletions from previous sessions
5. Auto-discover plugin providers from plugins/
6. Launch MainWindow
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import sys

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QMessageBox

from config.settings import Settings
from db.state_manager import StateManager
from providers.base_provider import CloudProvider
from ui.main_window import MainWindow

log = logging.getLogger(__name__)


# ── Dark palette ──────────────────────────────────────────────────────────────

def _build_dark_palette() -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window,          QColor(37, 37, 38))
    p.setColor(QPalette.ColorRole.WindowText,      QColor(212, 212, 212))
    p.setColor(QPalette.ColorRole.Base,            QColor(30, 30, 30))
    p.setColor(QPalette.ColorRole.AlternateBase,   QColor(45, 45, 48))
    p.setColor(QPalette.ColorRole.ToolTipBase,     QColor(37, 37, 38))
    p.setColor(QPalette.ColorRole.ToolTipText,     QColor(212, 212, 212))
    p.setColor(QPalette.ColorRole.Text,            QColor(212, 212, 212))
    p.setColor(QPalette.ColorRole.Button,          QColor(50, 50, 50))
    p.setColor(QPalette.ColorRole.ButtonText,      QColor(212, 212, 212))
    p.setColor(QPalette.ColorRole.BrightText,      QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.Link,            QColor(86, 156, 214))
    p.setColor(QPalette.ColorRole.Highlight,       QColor(9, 71, 113))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    return p


# ── Plugin discovery ──────────────────────────────────────────────────────────

def _discover_plugins() -> list[type]:
    """
    Scan the plugins/ directory for any class that subclasses CloudProvider.
    Returns a list of discovered provider classes (excluding the template).
    """
    discovered: list[type] = []
    plugins_dir = os.path.join(os.path.dirname(__file__), "plugins")
    if not os.path.isdir(plugins_dir):
        return discovered

    for fname in os.listdir(plugins_dir):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        if fname == "example_provider_template.py":
            continue
        module_name = f"plugins.{fname[:-3]}"
        try:
            mod = importlib.import_module(module_name)
            for _, obj in inspect.getmembers(mod, inspect.isclass):
                if (issubclass(obj, CloudProvider)
                        and obj is not CloudProvider
                        and obj not in discovered):
                    log.info(f"[Plugins] Discovered: {obj.provider_name} from {fname}")
                    discovered.append(obj)
        except Exception as exc:
            log.warning(f"[Plugins] Failed to load {fname}: {exc}")

    return discovered


# ── Pending / crashed session check ──────────────────────────────────────────

def _check_pending(state_manager: StateManager, app: QApplication) -> None:
    pending = state_manager.get_pending()
    if not pending:
        return
    reply = QMessageBox.question(
        None,
        "Interrupted Session Detected",
        f"{len(pending)} deletion(s) were in progress when the application last closed.\n"
        "These have been left as PENDING in the audit log.\n\n"
        "Would you like to mark them as FAILED so you can retry them?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
    )
    if reply == QMessageBox.StandardButton.Yes:
        for row in pending:
            state_manager.mark_failed(row["resource_id"], "Application interrupted")
        log.info(f"[Startup] Marked {len(pending)} pending records as FAILED for retry.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Cloud Resource Janitor")
    app.setApplicationVersion("1.0.0")
    app.setStyle("Fusion")

    settings = Settings()
    theme    = settings.get("theme", "dark")
    if theme == "dark":
        app.setPalette(_build_dark_palette())

    state_manager = StateManager()

    # Check for interrupted sessions
    _check_pending(state_manager, app)

    # Discover plugins
    plugins = _discover_plugins()
    if plugins:
        log.info(f"[Plugins] {len(plugins)} third-party provider(s) loaded.")

    window = MainWindow(settings, state_manager)
    window.show()

    # Auto-select last-used provider
    last_provider = settings.get("last_provider", "AWS")
    window._sidebar.select_provider(last_provider)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
