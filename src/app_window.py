from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QMainWindow, QMessageBox, QPushButton,
    QRadioButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from pages import DebrisPage, TransposePage
from resource_paths import find_icon_path


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
    def __init__(self):
        super().__init__()
        self._close_pending = False
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
        self.setCentralWidget(central)

        self.root_layout = QVBoxLayout(central)

        self.build_mode_selector()

        self.container = QFrame()
        self.container_layout = QVBoxLayout(self.container)
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
        bar = QHBoxLayout()

        self.mode_group = QButtonGroup(self)

        self.rb_transpose = QRadioButton("Transpose to Airfield")
        self.rb_debris = QRadioButton("Debris Trajectory")

        self.rb_transpose.setChecked(True)

        self.mode_group.addButton(self.rb_transpose)
        self.mode_group.addButton(self.rb_debris)

        self.rb_transpose.toggled.connect(self.switch_mode)

        bar.addWidget(self.rb_transpose)
        bar.addWidget(self.rb_debris)

        about_btn = QPushButton("About")
        about_btn.clicked.connect(self.show_about_dialog)
        bar.addWidget(about_btn)

        bar.addStretch()

        self.root_layout.addLayout(bar)

    # (build_menu removed)

    def show_about_dialog(self):
        QMessageBox.about(
            self,
            "About",
            "Tasmead Display Tools\n\n"
            "Authors:\n"
            "- Tasmead Display Tool Created by Will Crook\n"
            "GitHub:\n"
            "https://github.com/WillCrook\n\n"
            "- Debris Trajectory Calculations Created by mkarachalios-1\n"
            "GitHub:\n"
            "https://github.com/mkarachalios-1/airshow-trajectory-app/blob/main/streamlit_app.py\n\n"
            "Contact us:\n"
            "rich.pillans@tasmead.com"
        )

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
