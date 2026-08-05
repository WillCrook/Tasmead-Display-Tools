"""Local, theme-aware SVG icons for application action controls."""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
import sys

from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPalette, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QAbstractButton, QApplication, QWidget

from theme import ThemeMode, tokens_for


class AppIcon(str, Enum):
    """Names of the local Tabler SVG assets used by action controls."""

    FOLDER_PLUS = "folder-plus"
    TRASH = "trash"
    LIST = "list"
    MONITOR = "monitor"
    INFO_CIRCLE = "info-circle"
    X = "x"


_ICON_PROPERTY = "tasmeadIcon"
_RENDER_SIZES = (16, 20, 24, 32, 48)


def icon_asset_path(icon: AppIcon | str) -> Path:
    """Return the on-disk local SVG path in source and frozen builds."""
    name = AppIcon(icon).value
    bundle_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundle_root) if bundle_root else Path(__file__).resolve().parents[1]
    return root / "assets" / "icons" / f"{name}.svg"


@lru_cache(maxsize=None)
def _svg_source(icon: AppIcon) -> str:
    path = icon_asset_path(icon)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise FileNotFoundError(f"Local icon asset is unavailable: {path}") from error


def icon_colour(mode: ThemeMode | str) -> QColor:
    """Return the accessible action-icon foreground for an effective theme."""
    return QColor(tokens_for(ThemeMode(mode)).body_text)


def _render_icon(svg: str) -> QIcon:
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise ValueError("Local SVG icon could not be rendered")

    icon = QIcon()
    for size in _RENDER_SIZES:
        image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(QPixmap.fromImage(image))
    return icon


def create_icon(icon: AppIcon | str, mode: ThemeMode | str) -> QIcon:
    """Create a QIcon by recolouring a vendored Tabler SVG in memory."""
    asset = AppIcon(icon)
    colour = icon_colour(mode).name(QColor.NameFormat.HexRgb)
    svg = _svg_source(asset).replace("currentColor", colour)
    return _render_icon(svg)


def _mode_from_application_palette() -> ThemeMode:
    application = QApplication.instance()
    if application is None:
        return ThemeMode.LIGHT
    colour = application.palette().color(QPalette.ColorRole.Window)
    luminance = (
        0.2126 * colour.redF()
        + 0.7152 * colour.greenF()
        + 0.0722 * colour.blueF()
    )
    return ThemeMode.DARK if luminance < 0.5 else ThemeMode.LIGHT


def set_button_icon(
    button: QAbstractButton,
    icon: AppIcon | str,
    mode: ThemeMode | str | None = None,
) -> None:
    """Assign and register a local icon so it can be refreshed on theme changes."""
    asset = AppIcon(icon)
    button.setProperty(_ICON_PROPERTY, asset.value)
    button.setIcon(create_icon(asset, mode or _mode_from_application_palette()))


def refresh_icons(root: QWidget, mode: ThemeMode | str) -> None:
    """Rebuild all registered descendant action icons for an effective theme."""
    buttons: list[QAbstractButton] = []
    if isinstance(root, QAbstractButton):
        buttons.append(root)
    buttons.extend(root.findChildren(QAbstractButton))
    for button in buttons:
        asset_name = button.property(_ICON_PROPERTY)
        if asset_name:
            button.setIcon(create_icon(AppIcon(str(asset_name)), mode))
