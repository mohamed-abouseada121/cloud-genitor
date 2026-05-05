"""
main.py
────────
Application entry point.
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


def _get_resource_path(relative_path: str) -> str:
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def _discover_plugins() -> list[type]:
    discovered: list[type] = []
    plugins_dir = _get_resource_path("plugins")
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
                    discovered.append(obj)
        except Exception as exc:
            log.warning(f"[Plugins] Failed to load {fname}: {exc}")

    return discovered


def _check_pending(state_manager: StateManager, app: QApplication) -> None:
    pending = state_manager.get_pending()
    if not pending:
        return
    reply = QMessageBox.question(
        None,
        "Interrupted Session Detected",
        f"{len(pending)} deletion(s) were in progress when the application last closed.\n"
        "Would you like to mark them as FAILED so you can retry them?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
    )
    if reply == QMessageBox.StandardButton.Yes:
        for row in pending:
            state_manager.mark_failed(row["resource_id"], "Application interrupted")


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Cloud Resource Janitor")
    app.setApplicationVersion("1.0.0")
    app.setStyle("Fusion")

    base_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
    settings_path = os.path.join(base_dir, "settings.json")
    db_path = os.path.join(base_dir, "cleanup_state.db")

    settings = Settings(path=settings_path)
    state_manager = StateManager(db_path=db_path)

    _check_pending(state_manager, app)
    _discover_plugins()

    window = MainWindow(settings, state_manager)
    window.show()

    last_provider = settings.get("last_provider", "AWS")
    window._sidebar.select_provider(last_provider)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
