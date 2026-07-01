"""Tasmead Display Tools application entry point."""

import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from app_window import App
from resource_paths import find_icon_path


def main():
    app = QApplication(sys.argv)
    app_icon = find_icon_path()
    if app_icon:
        app.setWindowIcon(QIcon(app_icon))

    window = App()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
