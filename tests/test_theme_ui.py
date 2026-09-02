import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QLabel, QLineEdit

from app_window import App
from google_maps_settings import GOOGLE_MAPS_API_KEY_SETTING
from settings_dialog import SettingsDialog
from theme import (
    DARK_TOKENS,
    LIGHT_TOKENS,
    THEME_SETTING_KEY,
    ThemeController,
    ThemeMode,
    build_palette,
    build_stylesheet,
)


class MemorySettings:
    def __init__(self, initial=None):
        self.values = dict(initial or {})
        self.sync_count = 0

    def value(self, key, default=None, type=None):
        value = self.values.get(key, default)
        return type(value) if type is not None else value

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        self.sync_count += 1


def contrast_ratio(first: str, second: str) -> float:
    def luminance(colour: str) -> float:
        channels = [int(colour[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            value / 12.92
            if value <= 0.04045
            else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    brighter, darker = sorted(
        (luminance(first), luminance(second)),
        reverse=True,
    )
    return (brighter + 0.05) / (darker + 0.05)


class ThemeControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.original_palette = self.app.palette()
        self.original_stylesheet = self.app.styleSheet()

    def tearDown(self):
        self.app.setPalette(self.original_palette)
        self.app.setStyleSheet(self.original_stylesheet)

    def test_supplied_tokens_and_accessible_text_variants_are_exact(self):
        self.assertEqual(
            asdict(LIGHT_TOKENS),
            {
                "primary": "#0F766E",
                "primary_hover": "#115E59",
                "primary_pressed": "#134E4A",
                "primary_soft": "#CCFBF1",
                "background": "#F8FAFC",
                "surface": "#FFFFFF",
                "surface_alt": "#F1F5F9",
                "border": "#CBD5E1",
                "heading_text": "#0F172A",
                "body_text": "#334155",
                "muted_text": "#64748B",
                "accent_blue": "#3B82F6",
                "success": "#22C55E",
                "warning": "#F59E0B",
                "error": "#EF4444",
                "on_primary": "#FFFFFF",
                "accent_text": "#1D4ED8",
                "success_text": "#15803D",
                "warning_text": "#B45309",
                "error_text": "#B91C1C",
            },
        )
        self.assertEqual(
            asdict(DARK_TOKENS),
            {
                "primary": "#2DD4BF",
                "primary_hover": "#5EEAD4",
                "primary_pressed": "#14B8A6",
                "primary_soft": "#134E4A",
                "background": "#0B1120",
                "surface": "#111827",
                "surface_alt": "#1E293B",
                "border": "#334155",
                "heading_text": "#F8FAFC",
                "body_text": "#E2E8F0",
                "muted_text": "#94A3B8",
                "accent_blue": "#60A5FA",
                "success": "#4ADE80",
                "warning": "#FBBF24",
                "error": "#F87171",
                "on_primary": "#0B1120",
                "accent_text": "#60A5FA",
                "success_text": "#4ADE80",
                "warning_text": "#FBBF24",
                "error_text": "#F87171",
            },
        )

    def test_semantic_text_and_primary_foregrounds_meet_normal_text_contrast(self):
        for tokens in (LIGHT_TOKENS, DARK_TOKENS):
            with self.subTest(theme=tokens.background):
                self.assertGreaterEqual(
                    contrast_ratio(tokens.primary, tokens.on_primary),
                    4.5,
                )
                for semantic_text in (
                    tokens.accent_text,
                    tokens.success_text,
                    tokens.warning_text,
                    tokens.error_text,
                ):
                    self.assertGreaterEqual(
                        contrast_ratio(semantic_text, tokens.surface),
                        4.5,
                    )

    def test_palette_and_stylesheet_use_semantic_tokens(self):
        palette = build_palette(DARK_TOKENS)
        self.assertEqual(
            palette.color(QPalette.ColorRole.Window).name().upper(),
            DARK_TOKENS.background,
        )
        self.assertEqual(
            palette.color(QPalette.ColorRole.Highlight).name().upper(),
            DARK_TOKENS.primary_soft,
        )
        self.assertEqual(
            palette.color(QPalette.ColorRole.HighlightedText).name().upper(),
            DARK_TOKENS.heading_text,
        )
        stylesheet = build_stylesheet(DARK_TOKENS)
        for name in DARK_TOKENS.__dataclass_fields__:
            colour = getattr(DARK_TOKENS, name)
            self.assertIn(colour, stylesheet)
        self.assertIn(DARK_TOKENS.warning, stylesheet)
        self.assertIn(DARK_TOKENS.error, stylesheet)
        self.assertIn("QPushButton#primaryButton:pressed", stylesheet)
        self.assertIn("QListWidget::item:selected", stylesheet)
        self.assertIn(
            'QLabel#previewOffsetStatusDot[offsetState="active"]',
            stylesheet,
        )
        self.assertIn(
            'QLabel#previewOffsetStatusDot[offsetState="mismatch"]',
            stylesheet,
        )
        self.assertIn(
            'QLabel#runwayDetectionStatusDot[detectionState="high"]',
            stylesheet,
        )
        self.assertIn(
            'QLabel#runwayDetectionStatusDot[detectionState="moderate"]',
            stylesheet,
        )
        self.assertIn(
            'QLabel#runwayDetectionStatusDot[detectionState="low"]',
            stylesheet,
        )
        self.assertIn("QDialog#persistentInfoPopup", stylesheet)
        self.assertIn("QLabel#persistentInfoPopupText", stylesheet)
        self.assertIn("border-left: 3px solid #2DD4BF", stylesheet)
        self.assertNotEqual(DARK_TOKENS.primary, DARK_TOKENS.success)

    def test_invalid_setting_defaults_to_system_and_explicit_mode_persists(self):
        settings = MemorySettings({THEME_SETTING_KEY: "sepia"})
        controller = ThemeController(self.app, settings=settings)
        self.assertEqual(controller.mode, ThemeMode.SYSTEM)

        controller.set_mode(ThemeMode.DARK)
        self.assertEqual(controller.mode, ThemeMode.DARK)
        self.assertEqual(settings.values[THEME_SETTING_KEY], "dark")
        self.assertEqual(settings.sync_count, 1)
        self.assertEqual(controller.effective_mode, ThemeMode.DARK)

        restored = ThemeController(self.app, settings=settings)
        self.assertEqual(restored.mode, ThemeMode.DARK)
        self.assertEqual(restored.effective_mode, ThemeMode.DARK)

    def test_system_changes_only_affect_system_mode(self):
        controller = ThemeController(self.app, settings=MemorySettings())
        controller._on_system_colour_scheme_changed(Qt.ColorScheme.Dark)
        self.assertEqual(controller.effective_mode, ThemeMode.DARK)

        controller.set_mode(ThemeMode.LIGHT)
        controller._on_system_colour_scheme_changed(Qt.ColorScheme.Dark)
        self.assertEqual(controller.effective_mode, ThemeMode.LIGHT)


class SettingsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.original_palette = self.app.palette()
        self.original_stylesheet = self.app.styleSheet()
        self.settings = MemorySettings()
        self.controller = ThemeController(self.app, settings=self.settings)
        self.dialog = SettingsDialog(self.controller)

    def tearDown(self):
        self.dialog.close()
        self.app.setPalette(self.original_palette)
        self.app.setStyleSheet(self.original_stylesheet)

    def test_tabs_about_content_and_live_theme_selector(self):
        self.assertEqual(self.dialog.tabs.count(), 3)
        self.assertEqual(self.dialog.tabs.tabText(0), "Appearance")
        self.assertEqual(self.dialog.tabs.tabText(1), "Google Maps")
        self.assertEqual(self.dialog.tabs.tabText(2), "About")
        self.assertTrue(self.dialog.theme_buttons[ThemeMode.SYSTEM].isChecked())

        self.dialog.theme_buttons[ThemeMode.DARK].click()
        self.assertEqual(self.controller.mode, ThemeMode.DARK)
        self.assertEqual(self.settings.values[THEME_SETTING_KEY], "dark")

        about_text = " ".join(
            label.text() for label in self.dialog.tabs.widget(2).findChildren(QLabel)
        )
        self.assertIn("Tasmead Display Tools", about_text)
        self.assertIn("Will Crook", about_text)
        self.assertIn("rich.pillans@tasmead.com", about_text)

    def test_google_maps_key_is_masked_saved_and_cleared(self):
        self.assertEqual(
            self.dialog.maps_api_key_input.echoMode(),
            QLineEdit.EchoMode.Password,
        )
        self.dialog.maps_api_key_input.setText("  test-browser-key  ")
        self.dialog.save_maps_key_button.click()
        self.assertEqual(
            self.settings.values[GOOGLE_MAPS_API_KEY_SETTING],
            "test-browser-key",
        )
        self.dialog.show_maps_key_button.click()
        self.assertEqual(
            self.dialog.maps_api_key_input.echoMode(),
            QLineEdit.EchoMode.Normal,
        )
        self.dialog.clear_maps_key_button.click()
        self.assertEqual(
            self.settings.values[GOOGLE_MAPS_API_KEY_SETTING],
            "",
        )


class ModernHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.original_palette = self.app.palette()
        self.original_stylesheet = self.app.styleSheet()
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.path_patches = [
            patch(
                "pages.debris_page.app_data_path",
                side_effect=lambda relative: str(root / "app-data" / relative),
            ),
            patch(
                "pages.debris_page.resource_path",
                side_effect=lambda relative: str(root / "resources" / relative),
            ),
            patch(
                "pages.transpose_page.app_data_path",
                side_effect=lambda relative: str(root / "app-data" / relative),
            ),
            patch(
                "pages.transpose_page.resource_path",
                side_effect=lambda relative: str(root / "resources" / relative),
            ),
        ]
        for path_patch in self.path_patches:
            path_patch.start()
        self.controller = ThemeController(self.app, settings=MemorySettings())
        self.window = App(self.controller)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        for path_patch in reversed(self.path_patches):
            path_patch.stop()
        self.temp_dir.cleanup()
        self.app.setPalette(self.original_palette)
        self.app.setStyleSheet(self.original_stylesheet)

    def test_mode_switch_is_centered_and_cog_is_right_aligned(self):
        switch_center = self.window.mode_switch.mapTo(
            self.window,
            self.window.mode_switch.rect().center(),
        ).x()
        self.assertLessEqual(abs(switch_center - self.window.rect().center().x()), 1)
        self.assertEqual(self.window.rb_transpose.text(), "Transpose to Airfield")
        self.assertEqual(self.window.rb_debris.text(), "Debris Trajectory")
        self.assertEqual(self.window.rb_kml_editor.text(), "KML Editor")
        self.assertEqual(self.window.settings_button.accessibleName(), "Open settings")
        self.assertGreater(
            self.window.settings_button.mapTo(self.window, self.window.settings_button.pos()).x(),
            switch_center,
        )

    def test_switching_pages_and_busy_lockout_are_preserved(self):
        self.window.rb_debris.click()
        self.assertIs(
            self.window.page_stack.currentWidget(),
            self.window.page_scrolls[self.window.debris_page],
        )
        self.window.rb_kml_editor.click()
        self.assertIs(
            self.window.page_stack.currentWidget(),
            self.window.page_scrolls[self.window.kml_editor_page],
        )
        self.window._on_debris_simulation_busy_changed(True)
        self.assertFalse(self.window.rb_transpose.isEnabled())
        self.assertFalse(self.window.rb_debris.isEnabled())
        self.assertFalse(self.window.rb_kml_editor.isEnabled())
        self.assertTrue(self.window.settings_button.isEnabled())

    def test_three_segment_selection_indicator_tracks_the_checked_workspace(self):
        segment_width = (self.window.mode_switch.width() - 8) // 3
        for index, button in enumerate(self.window.top_level_mode_buttons):
            button.click()
            self.app.processEvents()
            self.assertEqual(self.window.mode_selection.x(), 4 + index * segment_width)
            self.assertEqual(self.window.mode_selection.width(), segment_width)

    def test_settings_cog_reuses_one_modal_dialog(self):
        with patch.object(SettingsDialog, "exec", return_value=0) as execute:
            self.window.settings_button.click()
            first_dialog = self.window._settings_dialog
            self.window.settings_button.click()
        self.assertIs(first_dialog, self.window._settings_dialog)
        self.assertEqual(execute.call_count, 2)

    def test_page_level_introductions_are_removed_but_card_titles_remain(self):
        self.assertEqual(
            self.window.transpose_page.findChildren(QLabel, "pageTitle"),
            [],
        )
        self.assertEqual(
            self.window.debris_page.findChildren(QLabel, "pageTitle"),
            [],
        )
        transpose_text = {
            label.text() for label in self.window.transpose_page.findChildren(QLabel)
        }
        debris_text = {
            label.text() for label in self.window.debris_page.findChildren(QLabel)
        }
        self.assertIn("Input files", transpose_text)
        self.assertIn("Debris model", debris_text)

    def test_brand_actions_success_states_and_coordinate_data_are_separate(self):
        stylesheet = self.app.styleSheet()
        self.assertIn("background: #0F766E", stylesheet)
        self.assertIn("border: 2px solid #22C55E", stylesheet)
        self.assertIn("color: #1D4ED8", stylesheet)
        self.assertNotEqual(LIGHT_TOKENS.primary, LIGHT_TOKENS.success)
        for label in (
            self.window.debris_page.kml_meta_pen_lat,
            self.window.debris_page.kml_meta_pen_lon,
            self.window.debris_page.kml_meta_fin_lat,
            self.window.debris_page.kml_meta_fin_lon,
        ):
            self.assertEqual(label.property("dataRole"), "coordinate")

    def test_only_top_level_cards_receive_reactive_restrained_shadows(self):
        top_level_cards = (
            self.window.transpose_page.source_card,
            self.window.transpose_page.target_card,
            self.window.debris_page.config_widget,
            self.window.debris_page.results_widget,
        )
        for card in top_level_cards:
            effect = card.graphicsEffect()
            self.assertIsInstance(effect, QGraphicsDropShadowEffect)
            self.assertEqual(effect.blurRadius(), 18.0)
            self.assertEqual(effect.offset().y(), 2.0)
            self.assertEqual(effect.color().alpha(), 26)

        nested_panel = self.window.debris_page.kml_drop_zone
        self.assertIsNone(nested_panel.graphicsEffect())

        self.controller.set_mode(ThemeMode.DARK)
        self.app.processEvents()
        for card in top_level_cards:
            self.assertEqual(card.graphicsEffect().color().alpha(), 89)

    def test_registered_action_icons_refresh_with_the_effective_theme(self):
        self.controller.set_mode(ThemeMode.LIGHT)
        self.app.processEvents()
        button = self.window.transpose_page.add_files_btn
        light_icon_key = button.icon().cacheKey()

        self.controller.set_mode(ThemeMode.DARK)
        self.app.processEvents()

        self.assertFalse(button.icon().isNull())
        self.assertNotEqual(light_icon_key, button.icon().cacheKey())

    def test_preview_replaces_the_whole_workspace_and_cancel_restores_it(self):
        owner = self.window.transpose_page
        scene = object()

        def start_visible_preview(_scene, _key):
            self.assertIs(
                self.window.workspace_stack.currentWidget(),
                self.window.map_preview,
            )
            self.assertTrue(self.window.map_preview.isVisible())
            return True

        with (
            patch.object(self.window, "_ensure_maps_api_key", return_value="key"),
            patch.object(
                self.window.map_preview,
                "set_scene",
                side_effect=start_visible_preview,
            ) as set_scene,
        ):
            opened = self.window.open_map_preview(owner, scene)

        self.assertTrue(opened)
        set_scene.assert_called_once_with(scene, "key")
        self.assertIs(
            self.window.workspace_stack.currentWidget(),
            self.window.map_preview,
        )
        self.assertIs(self.window._preview_owner, owner)

        self.window.close_map_preview()

        self.assertIs(
            self.window.workspace_stack.currentWidget(),
            self.window.normal_workspace,
        )
        self.assertIsNone(self.window._preview_owner)

    def test_preview_initialisation_failure_restores_the_previous_workspace(self):
        owner = self.window.transpose_page
        previous_workspace = self.window.workspace_stack.currentWidget()
        with (
            patch.object(self.window, "_ensure_maps_api_key", return_value="key"),
            patch.object(self.window.map_preview, "set_scene", return_value=False),
            patch.object(self.window.map_preview, "shutdown") as shutdown,
            patch("app_window.QMessageBox.critical") as critical,
        ):
            opened = self.window.open_map_preview(owner, object())

        self.assertFalse(opened)
        self.assertIs(
            self.window.workspace_stack.currentWidget(),
            previous_workspace,
        )
        self.assertIsNone(self.window._preview_owner)
        self.assertIsNone(self.window._window_state_before_preview)
        self.assertIsNone(self.window._window_geometry_before_preview)
        shutdown.assert_called_once_with()
        critical.assert_called_once()

    def test_exiting_preview_fullscreen_restores_a_maximized_window(self):
        self.window.workspace_stack.setCurrentWidget(self.window.map_preview)
        self.window._window_state_before_preview = Qt.WindowState.WindowMaximized
        with (
            patch.object(self.window, "showMaximized") as maximize,
            patch.object(self.window, "showNormal") as normal,
        ):
            self.window._set_map_preview_fullscreen(False)

        maximize.assert_called_once_with()
        normal.assert_not_called()

    def test_missing_key_settings_save_retries_the_original_preview(self):
        settings_button = object()
        prompt = object()
        with patch("app_window.QMessageBox") as message_box:
            instance = message_box.return_value
            instance.addButton.side_effect = [settings_button, object()]
            instance.clickedButton.return_value = settings_button

            def save_key(_section):
                self.window.maps_settings.set_api_key("saved-browser-key")

            with (
                patch.object(self.window, "open_settings", side_effect=save_key) as settings,
                patch.object(self.window.map_preview, "set_scene") as set_scene,
            ):
                opened = self.window.open_map_preview(
                    self.window.transpose_page,
                    prompt,
                )

        self.assertTrue(opened)
        settings.assert_called_once_with("Google Maps")
        set_scene.assert_called_once_with(prompt, "saved-browser-key")
        self.window.close_map_preview()

    def test_escape_leaves_preview_fullscreen_before_closing_preview(self):
        self.window.workspace_stack.setCurrentWidget(self.window.map_preview)
        self.window._window_state_before_preview = Qt.WindowState.WindowNoState
        with (
            patch.object(self.window, "isFullScreen", return_value=True),
            patch.object(self.window, "_set_map_preview_fullscreen") as fullscreen,
            patch.object(self.window, "close_map_preview") as close,
        ):
            self.window._escape_map_preview()
        fullscreen.assert_called_once_with(False)
        close.assert_not_called()

        with (
            patch.object(self.window, "isFullScreen", return_value=False),
            patch.object(self.window, "close_map_preview") as close,
        ):
            self.window._escape_map_preview()
        close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
