from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QMainWindow, QMessageBox, QPushButton,
    QRadioButton, QVBoxLayout, QWidget,
)

from pages import DebrisPage, TransposePage
from resource_paths import find_icon_path

class App(QMainWindow):
    def __init__(self):
        super().__init__()
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
        central = QWidget()
        self.setCentralWidget(central)

        self.root_layout = QVBoxLayout(central)

        self.build_mode_selector()

        self.container = QFrame()
        self.container_layout = QVBoxLayout(self.container)
        self.root_layout.addWidget(self.container)

        self.transpose_page = TransposePage()
        self.debris_page = DebrisPage()

        self.set_page(self.transpose_page)

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
        while self.container_layout.count():
            item = self.container_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
        self.container_layout.addWidget(widget)

    def switch_mode(self):
        if self.rb_transpose.isChecked():
            self.set_page(self.transpose_page)
        else:
            self.set_page(self.debris_page)
