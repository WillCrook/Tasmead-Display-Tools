"""Tasmead Display Tools application entry point."""

import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from app_window import App
from resource_paths import find_icon_path
from theme import ThemeController


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("Tasmead")
    app.setApplicationName("Tasmead Display Tools")
    app_icon = find_icon_path()
    if app_icon:
        app.setWindowIcon(QIcon(app_icon))

    theme_controller = ThemeController(app, parent=app)
    window = App(theme_controller)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
