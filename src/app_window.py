from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QMainWindow,
    QMessageBox, QPushButton, QScrollArea, QStackedWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from pages import DebrisPage, TransposePage
from google_maps_settings import GoogleMapsSettings
from icon_utils import refresh_icons
from map_preview_widget import MapPreviewWidget, WEBENGINE_AVAILABLE
from resource_paths import find_icon_path
from settings_dialog import SettingsDialog
from theme import ThemeController, apply_card_shadows


class PageScrollArea(QScrollArea):
    """Keep keyboard-focused page controls visible in a scrollable mode page."""

    def focusNextPrevChild(self, next):
        moved = super().focusNextPrevChild(next)
        focused = self.focusWidget()
        page = self.widget()
        if moved and focused is not None and page is not None and page.isAncestorOf(focused):
            self.ensureWidgetVisible(focused, 8, 8)
        return moved


class App(QMainWindow):
    MINIMUM_WINDOW_WIDTH = 900
    MINIMUM_WINDOW_HEIGHT = 600
    DEFAULT_WINDOW_WIDTH = 1100
    DEFAULT_WINDOW_HEIGHT = 900
    WINDOW_SCREEN_MARGIN = 40
    COMPACT_HEADER_BREAKPOINT = 1000

    @classmethod
    def _initial_window_size(cls, available_geometry):
        return (
            min(
                cls.DEFAULT_WINDOW_WIDTH,
                max(1, available_geometry.width() - cls.WINDOW_SCREEN_MARGIN),
            ),
            min(
                cls.DEFAULT_WINDOW_HEIGHT,
                max(1, available_geometry.height() - cls.WINDOW_SCREEN_MARGIN),
            ),
        )

    def __init__(self, theme_controller=None):
        super().__init__()
        self._close_pending = False
        application = QApplication.instance()
        if application is None:
            raise RuntimeError("App requires an active QApplication")
        self.theme_controller = (
            theme_controller
            if theme_controller is not None
            else ThemeController(application, parent=self)
        )
        self.maps_settings = GoogleMapsSettings(
            settings=self.theme_controller.settings,
            parent=self,
        )
        self._settings_dialog = None
        self._preview_owner = None
        self._window_state_before_preview = None
        self._window_geometry_before_preview = None
        # Choose an OS-appropriate icon (app.icns on macOS, app.ico on
        # Windows, app.png as a fallback). The helper resolves the path
        # from the bundle or source directory.
        icon_path = find_icon_path()
        if icon_path:
            try:
                self.setWindowIcon(QIcon(icon_path))
            except Exception:
                # don't crash if QIcon can't load the file
                pass

        self.setWindowTitle("Tasmead Display Tools")
        available_geometry = QApplication.primaryScreen().availableGeometry()
        self.resize(*self._initial_window_size(available_geometry))
        self.setMinimumSize(self.MINIMUM_WINDOW_WIDTH, self.MINIMUM_WINDOW_HEIGHT)
        self.workspace_stack = QStackedWidget()
        self.setCentralWidget(self.workspace_stack)
        central = QWidget()
        central.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.workspace_stack.addWidget(central)
        self.normal_workspace = central

        self.root_layout = QVBoxLayout(central)
        self.root_layout.setContentsMargins(0, 0, 0, 0)
        self.root_layout.setSpacing(0)

        self.build_mode_selector()

        self.container = QFrame()
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(0, 0, 0, 0)
        self.page_stack = QStackedWidget()
        self.container_layout.addWidget(self.page_stack)
        self.root_layout.addWidget(self.container)

        self.transpose_page = TransposePage()
        self.debris_page = DebrisPage()
        self.map_preview = MapPreviewWidget()
        self.workspace_stack.addWidget(self.map_preview)
        self.map_preview.close_requested.connect(self.close_map_preview)
        self.map_preview.scene_applied.connect(self._apply_map_preview)
        self.map_preview.scene_export_requested.connect(self._export_map_preview)
        self.map_preview.fullscreen_requested.connect(
            self._set_map_preview_fullscreen
        )
        self.map_preview.settings_requested.connect(
            self._open_preview_maps_settings
        )
        self.transpose_page.preview_requested.connect(
            lambda scene: self.open_map_preview(self.transpose_page, scene)
        )
        self.debris_page.preview_requested.connect(
            lambda scene: self.open_map_preview(self.debris_page, scene)
        )
        self.debris_page.simulation_busy_changed.connect(
            self._on_debris_simulation_busy_changed
        )

        self.page_scrolls = {
            self.transpose_page: self._create_page_scroll(self.transpose_page),
            self.debris_page: self._create_page_scroll(self.debris_page),
        }

        self.set_page(self.transpose_page)
        self.theme_controller.effective_mode_changed.connect(
            self._apply_theme_decorations
        )
        self._apply_theme_decorations(self.theme_controller.effective_mode)
        central.setFocus(Qt.FocusReason.OtherFocusReason)

        self.preview_escape_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Escape),
            self,
        )
        self.preview_escape_shortcut.setContext(
            Qt.ShortcutContext.ApplicationShortcut
        )
        self.preview_escape_shortcut.activated.connect(
            self._escape_map_preview
        )

    def _create_page_scroll(self, page):
        scroll = PageScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(page)
        self.page_stack.addWidget(scroll)
        return scroll

    # ---------- MODE SELECTOR ----------
    def build_mode_selector(self):
        self.header = QFrame()
        self.header.setObjectName("appHeader")
        self.header.setFixedHeight(80)
        bar = QGridLayout(self.header)
        bar.setContentsMargins(18, 13, 18, 13)
        bar.setHorizontalSpacing(16)
        bar.setColumnStretch(0, 1)
        bar.setColumnStretch(2, 1)

        # Balance the settings button's fixed width so the segmented control is
        # centred on the window, rather than merely in the remaining space.
        self.header_balance = QWidget()
        self.header_balance.setFixedSize(40, 40)
        bar.addWidget(
            self.header_balance,
            0,
            0,
            alignment=(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            ),
        )

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)

        self.mode_switch = QFrame()
        self.mode_switch.setObjectName("modeSwitch")
        self.mode_switch.setFixedSize(440, 52)
        switch_layout = QHBoxLayout(self.mode_switch)
        switch_layout.setContentsMargins(4, 4, 4, 4)
        switch_layout.setSpacing(0)

        self.mode_selection = QFrame(self.mode_switch)
        self.mode_selection.setObjectName("modeSelection")
        self.mode_selection.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
        )
        self.mode_selection.setGeometry(4, 4, 216, 44)

        # Keep these long-standing attribute names for compatibility with the
        # page-switching and simulation-lockout tests.
        self.rb_transpose = QPushButton("Transpose to Airfield")
        self.rb_debris = QPushButton("Debris Trajectory")
        for button in (self.rb_transpose, self.rb_debris):
            button.setObjectName("modeSegment")
            button.setCheckable(True)
            switch_layout.addWidget(button, 1)

        self.rb_transpose.setChecked(True)

        self.mode_group.addButton(self.rb_transpose)
        self.mode_group.addButton(self.rb_debris)

        self.rb_transpose.toggled.connect(self.switch_mode)
        self.rb_transpose.toggled.connect(self._sync_mode_selection)

        bar.addWidget(
            self.mode_switch,
            0,
            1,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )

        self.settings_button = QToolButton()
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setText("⚙︎")
        self.settings_button.setAccessibleName("Open settings")
        self.settings_button.setToolTip("Settings")
        self.settings_button.clicked.connect(self.open_settings)
        bar.addWidget(
            self.settings_button,
            0,
            2,
            alignment=(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            ),
        )

        self.root_layout.addWidget(self.header)
        self._update_header_responsiveness()

    def _sync_mode_selection(self, transpose_selected):
        segment_width = (self.mode_switch.width() - 8) // 2
        selection_x = 4 if transpose_selected else 4 + segment_width
        self.mode_selection.setGeometry(
            selection_x,
            4,
            segment_width,
            self.mode_switch.height() - 8,
        )
        self.mode_selection.lower()

    def _update_header_responsiveness(self):
        if self.width() < self.COMPACT_HEADER_BREAKPOINT:
            self.header.setFixedHeight(64)
            self.header.layout().setContentsMargins(10, 8, 10, 8)
            self.mode_switch.setFixedSize(360, 48)
        else:
            self.header.setFixedHeight(80)
            self.header.layout().setContentsMargins(18, 13, 18, 13)
            self.mode_switch.setFixedSize(440, 52)
        self._sync_mode_selection(self.rb_transpose.isChecked())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "header"):
            self._update_header_responsiveness()

    def _apply_card_shadows(self, mode):
        apply_card_shadows(self, mode)

    def _apply_theme_decorations(self, mode):
        """Refresh theme-dependent decorative resources after a mode change."""
        apply_card_shadows(self, mode)
        refresh_icons(self, mode)

    # (build_menu removed)

    def open_settings(self, section: str | None = None):
        if self._settings_dialog is None:
            self._settings_dialog = SettingsDialog(
                self.theme_controller,
                self,
                maps_settings=self.maps_settings,
            )
        if section:
            self._settings_dialog.select_tab(section)
        self._settings_dialog.exec()

    def _ensure_maps_api_key(self) -> str | None:
        key = self.maps_settings.api_key
        if key:
            return key
        prompt = QMessageBox(self)
        prompt.setIcon(QMessageBox.Icon.Information)
        prompt.setWindowTitle("Google Maps API key required")
        prompt.setText(
            "The 3D preview needs your Google Maps JavaScript API key. "
            "KML export remains available without a key."
        )
        settings_button = prompt.addButton(
            "Open Maps Settings",
            QMessageBox.ButtonRole.AcceptRole,
        )
        prompt.addButton(QMessageBox.StandardButton.Cancel)
        prompt.exec()
        if prompt.clickedButton() is not settings_button:
            return None
        self.open_settings("Google Maps")
        key = self.maps_settings.api_key
        if not key:
            return None
        return key

    def open_map_preview(self, owner, scene) -> bool:
        if not WEBENGINE_AVAILABLE:
            QMessageBox.critical(
                self,
                "3D preview unavailable",
                "PyQt6-WebEngine is not installed. Install the dependencies "
                "from requirements.txt and restart the application. KML "
                "export remains available.",
            )
            return False
        key = self._ensure_maps_api_key()
        if not key:
            return False

        previous_workspace = self.workspace_stack.currentWidget()
        previous_window_state = self.windowState()
        previous_window_geometry = self.saveGeometry()
        self.workspace_stack.setCurrentWidget(self.map_preview)
        self.map_preview.set_fullscreen_state(self.isFullScreen())
        self.map_preview.setFocus(Qt.FocusReason.OtherFocusReason)
        try:
            started = self.map_preview.set_scene(scene, key)
            if started is False:
                raise RuntimeError(
                    "The secure local preview service could not be started."
                )
        except Exception as error:
            self.map_preview.shutdown()
            if previous_workspace is not None:
                self.workspace_stack.setCurrentWidget(previous_workspace)
            self.map_preview.set_fullscreen_state(False)
            self._preview_owner = None
            self._window_state_before_preview = None
            self._window_geometry_before_preview = None
            QMessageBox.critical(
                self,
                "3D preview unavailable",
                str(error) or "The preview scene could not be prepared.",
            )
            return False
        self._preview_owner = owner
        self._window_state_before_preview = previous_window_state
        self._window_geometry_before_preview = previous_window_geometry
        return True

    def _apply_map_preview(self, scene) -> None:
        owner = self._preview_owner
        if owner is not None:
            owner.accept_preview_scene(scene)
        self.close_map_preview()

    def _open_preview_maps_settings(self) -> None:
        self.open_settings("Google Maps")
        key = self.maps_settings.api_key
        if key and self.workspace_stack.currentWidget() is self.map_preview:
            self.map_preview.set_api_key_and_retry(key)

    def _export_map_preview(self, scene) -> None:
        owner = self._preview_owner
        if owner is not None:
            owner.accept_preview_scene(scene)
        self.close_map_preview()
        if owner is not None:
            QTimer.singleShot(0, owner.export_committed_scene)

    def _set_map_preview_fullscreen(self, fullscreen: bool) -> None:
        if self.workspace_stack.currentWidget() is not self.map_preview:
            return
        if fullscreen:
            self.showFullScreen()
        else:
            prior_state = self._window_state_before_preview
            if prior_state and prior_state & Qt.WindowState.WindowMaximized:
                self.showMaximized()
            else:
                self.showNormal()
                if self._window_geometry_before_preview is not None:
                    self.restoreGeometry(self._window_geometry_before_preview)
        self.map_preview.set_fullscreen_state(self.isFullScreen())

    def _escape_map_preview(self) -> None:
        if self.workspace_stack.currentWidget() is not self.map_preview:
            return
        if self.isFullScreen() and not (
            self._window_state_before_preview
            and self._window_state_before_preview & Qt.WindowState.WindowFullScreen
        ):
            self._set_map_preview_fullscreen(False)
            return
        self.close_map_preview()

    def close_map_preview(self) -> None:
        if self.workspace_stack.currentWidget() is not self.map_preview:
            return
        prior_state = self._window_state_before_preview
        prior_geometry = self._window_geometry_before_preview
        self.workspace_stack.setCurrentWidget(self.normal_workspace)
        self._preview_owner = None
        if prior_state is not None:
            if prior_state & Qt.WindowState.WindowFullScreen:
                self.showFullScreen()
            elif prior_state & Qt.WindowState.WindowMaximized:
                self.showMaximized()
            else:
                self.showNormal()
                if prior_geometry is not None:
                    self.restoreGeometry(prior_geometry)
        self.map_preview.set_fullscreen_state(False)
        self._window_state_before_preview = None
        self._window_geometry_before_preview = None

    def set_page(self, widget):
        self.page_stack.setCurrentWidget(self.page_scrolls[widget])

    def switch_mode(self):
        if self.rb_transpose.isChecked():
            self.set_page(self.transpose_page)
        else:
            self.set_page(self.debris_page)

    def _on_debris_simulation_busy_changed(self, busy):
        for button in self.mode_group.buttons():
            button.setEnabled(not busy)
        if not busy and self._close_pending:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        if not self.debris_page.has_active_simulation():
            self.map_preview.shutdown()
            event.accept()
            return

        if self._close_pending:
            event.ignore()
            return

        choice = QMessageBox.question(
            self,
            "Simulation in progress",
            "Cancel the running debris simulation and close the application?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        self._close_pending = True
        self.debris_page.cancel_simulation(silent=True)
        event.ignore()
