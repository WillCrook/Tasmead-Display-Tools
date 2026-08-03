"""Helpers for locating bundled application resources."""

import os
import sys

from PyQt6.QtCore import QStandardPaths


def resource_path(relative_path):
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def app_data_path(relative_path):
    """Return a writable, per-user path for application-managed data."""
    base_path = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    if not base_path:
        raise OSError("No writable application data location is available")
    return os.path.join(base_path, relative_path)


def _select_icon_name():
    if sys.platform == "darwin":
        return "app.icns"
    if sys.platform.startswith("win"):
        return "app.ico"
    return "app.png"


def find_icon_path():
    candidates = [_select_icon_name(), "app.ico", "app.icns", "app.png"]
    for name in candidates:
        path = resource_path(name)
        if os.path.exists(path):
            return path
    return None
