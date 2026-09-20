"""Early process-wide configuration for Qt WebEngine rendering."""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping


def presentation_watchdog_enabled(
    *,
    platform: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> bool:
    """Retain compatibility by default; the opt-in Metal profile uses native paints."""

    active_platform = sys.platform if platform is None else platform
    active_environ = os.environ if environ is None else environ
    override = active_environ.get("TASMEAD_MAP_PRESENTATION_WATCHDOG")
    if override is not None:
        return override != "0"
    return not (
        active_platform == "darwin"
        and active_environ.get("TASMEAD_MAP_RENDERING_PROFILE") == "metal"
        and active_environ.get("QSG_RHI_BACKEND", "metal") == "metal"
    )


def select_scene_graph_backend(
    *,
    platform: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Select compatibility or the opt-in macOS candidate, preserving overrides."""

    active_platform = sys.platform if platform is None else platform
    active_environ = os.environ if environ is None else environ
    if active_platform == "darwin":
        candidate = active_environ.get("TASMEAD_MAP_RENDERING_PROFILE") == "metal"
        active_environ.setdefault("QSG_RHI_BACKEND", "metal" if candidate else "opengl")


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


__all__ = [
    "configure_webengine_runtime",
    "presentation_watchdog_enabled",
    "select_scene_graph_backend",
]
