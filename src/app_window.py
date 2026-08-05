from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QMainWindow,
    QMessageBox, QPushButton, QScrollArea, QStackedWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from pages import DebrisPage, TransposePage
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
        self._settings_dialog = None
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
        self.resize(900, 500)
        self.setMinimumSize(900, 500)
        central = QWidget()
        central.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCentralWidget(central)

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
        self.debris_page.simulation_busy_changed.connect(
            self._on_debris_simulation_busy_changed
        )

        self.page_scrolls = {
            self.transpose_page: self._create_page_scroll(self.transpose_page),
            self.debris_page: self._create_page_scroll(self.debris_page),
        }

        self.set_page(self.transpose_page)
        self.theme_controller.effective_mode_changed.connect(
            self._apply_card_shadows
        )
        self._apply_card_shadows(self.theme_controller.effective_mode)
        central.setFocus(Qt.FocusReason.OtherFocusReason)

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

    def _sync_mode_selection(self, transpose_selected):
        self.mode_selection.move(4 if transpose_selected else 220, 4)
        self.mode_selection.lower()

    def _apply_card_shadows(self, mode):
        apply_card_shadows(self, mode)

    # (build_menu removed)

    def open_settings(self):
        if self._settings_dialog is None:
            self._settings_dialog = SettingsDialog(self.theme_controller, self)
        self._settings_dialog.exec()

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
