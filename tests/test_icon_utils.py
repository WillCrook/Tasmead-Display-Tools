import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

from icon_utils import (
    AppIcon,
    create_icon,
    icon_asset_path,
    icon_colour,
    refresh_icons,
    set_button_icon,
)
from theme import ThemeMode


def opaque_colours(icon):
    image = icon.pixmap(QSize(24, 24)).toImage()
    return {
        image.pixelColor(x, y).name().upper()
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() == 255
    }


class IconUtilsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_every_requested_local_svg_asset_is_present(self):
        self.assertEqual(
            set(AppIcon),
            {
                AppIcon.FOLDER_PLUS,
                AppIcon.TRASH,
                AppIcon.LIST,
                AppIcon.MONITOR,
                AppIcon.INFO_CIRCLE,
                AppIcon.X,
            },
        )
        for asset in AppIcon:
            with self.subTest(asset=asset.value):
                self.assertTrue(icon_asset_path(asset).is_file())

    def test_local_svg_assets_create_valid_qicons(self):
        for asset in AppIcon:
            with self.subTest(asset=asset.value):
                self.assertFalse(create_icon(asset, ThemeMode.LIGHT).isNull())

    def test_light_and_dark_icons_use_their_accessible_theme_colours(self):
        light = create_icon(AppIcon.INFO_CIRCLE, ThemeMode.LIGHT)
        dark = create_icon(AppIcon.INFO_CIRCLE, ThemeMode.DARK)

        self.assertNotEqual(light.cacheKey(), dark.cacheKey())
        self.assertIn(icon_colour(ThemeMode.LIGHT).name().upper(), opaque_colours(light))
        self.assertIn(icon_colour(ThemeMode.DARK).name().upper(), opaque_colours(dark))

    def test_registered_button_icon_refreshes_for_a_theme_change(self):
        root = QWidget()
        button = QPushButton("Remove", root)
        set_button_icon(button, AppIcon.TRASH, ThemeMode.LIGHT)
        light_key = button.icon().cacheKey()

        refresh_icons(root, ThemeMode.DARK)

        self.assertEqual(button.property("tasmeadIcon"), AppIcon.TRASH.value)
        self.assertFalse(button.icon().isNull())
        self.assertNotEqual(light_key, button.icon().cacheKey())
        self.assertIn(
            icon_colour(ThemeMode.DARK).name().upper(),
            opaque_colours(button.icon()),
        )


if __name__ == "__main__":
    unittest.main()
