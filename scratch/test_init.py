
import sys
from PyQt6.QtWidgets import QApplication
from ui.main_window import MainWindow
from config.settings import Settings
from db.state_manager import StateManager
import os

app = QApplication(sys.argv)
settings = Settings("settings.json")
state_manager = StateManager("cleanup_state.db")

try:
    window = MainWindow(settings, state_manager)
    print("SUCCESS: MainWindow created")
except Exception as e:
    import traceback
    traceback.print_exc()
    sys.exit(1)
