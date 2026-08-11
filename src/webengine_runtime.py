"""Early process-wide configuration for Qt WebEngine rendering."""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping


def select_scene_graph_backend(
    *,
    platform: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Choose the stable macOS Qt Quick backend without overriding the user."""

    active_platform = sys.platform if platform is None else platform
    active_environ = os.environ if environ is None else environ
    if active_platform == "darwin":
        active_environ.setdefault("QSG_RHI_BACKEND", "opengl")


def configure_webengine_runtime() -> None:
    """Configure Qt's compositor before QApplication or WebEngine is created."""

    select_scene_graph_backend()

    # Import QtCore only after the environment is ready.  Qt WebEngine Widgets
    # embeds a Qt Quick scene graph, so OpenGL contexts must be shareable before
    # QApplication constructs any platform graphics resources.
    from PyQt6.QtCore import QCoreApplication, Qt

    QCoreApplication.setAttribute(
        Qt.ApplicationAttribute.AA_ShareOpenGLContexts,
    )


__all__ = ["configure_webengine_runtime", "select_scene_graph_backend"]
