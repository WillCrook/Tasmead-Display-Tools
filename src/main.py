import sys
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QRadioButton,
    QButtonGroup, QFileDialog,
    QLineEdit, QCheckBox, QFrame,
    QListWidget, QInputDialog, QMessageBox,
    QComboBox, QSplitter,
)
from PyQt6.QtCore import Qt
import json

from Debris_Trajectory_Calculator import DebrisTrajectoryCalculator

class TransposePage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        label = QLabel("Coordinate Transposition Tool\n(Placeholder)")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("font-size: 18px;")

        layout.addStretch()
        layout.addWidget(label)
        layout.addStretch()


class DebrisPage(QWidget):
    def __init__(self):
        super().__init__()

        layout = QHBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: black;
                width: 6px;
            }
        """)
        layout.addWidget(splitter)

        presets_widget = QWidget()
        config_widget = QWidget()
        file_widget = QWidget()

        presets = QVBoxLayout(presets_widget)
        config = QVBoxLayout(config_widget)
        file_panel = QVBoxLayout(file_widget)

        splitter.addWidget(presets_widget)
        splitter.addWidget(config_widget)
        splitter.addWidget(file_widget)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 2)

        self.presets_path = "data/presets.json"
        self.presets = {}

        self.build_presets_panel(presets)
        self.build_config(config)
        self.build_file_panel(file_panel)

        self.load_presets_from_disk()

    def build_presets_panel(self, layout):
        title = QLabel("Presets")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(title)

        self.preset_list = QListWidget()
        self.preset_list.itemClicked.connect(self.load_selected_preset)
        layout.addWidget(self.preset_list)

        save_btn = QPushButton("Save preset")
        load_btn = QPushButton("Delete preset")

        save_btn.clicked.connect(self.save_preset)
        load_btn.clicked.connect(self.delete_preset)

        layout.addWidget(save_btn)
        layout.addWidget(load_btn)

        layout.addStretch()

    def load_presets_from_disk(self):
        try:
            with open(self.presets_path, "r") as f:
                self.presets = json.load(f)
        except FileNotFoundError:
            self.presets = {}

        self.refresh_preset_list()

    def refresh_preset_list(self):
        self.preset_list.clear()
        for name in self.presets:
            self.preset_list.addItem(name)

    def save_preset(self):
        name, ok = QInputDialog.getText(self, "Save Preset", "Preset name:")
        if not ok or not name:
            return

        self.presets[name] = {
            k: float(v.text()) for k, v in self.inputs.items()
        }
        # Save surface selection as string
        self.presets[name]["surface"] = self.surface_combo.currentText()

        with open(self.presets_path, "w") as f:
            json.dump(self.presets, f, indent=2)

        self.refresh_preset_list()

    def load_selected_preset(self, item):
        data = self.presets.get(item.text())
        if not data:
            return

        for k, v in data.items():
            if k in self.inputs:
                self.inputs[k].setText(str(v))
            elif k == "surface":
                index = self.surface_combo.findText(v)
                if index >= 0:
                    self.surface_combo.setCurrentIndex(index)

    def delete_preset(self):
        item = self.preset_list.currentItem()
        if not item:
            return

        name = item.text()
        del self.presets[name]

        with open(self.presets_path, "w") as f:
            json.dump(self.presets, f, indent=2)

        self.refresh_preset_list()

    def build_config(self, layout):
        defaults = {
            "Mass (kg)": "",
            "Frontal area A (m²)": "",
            "Drag Coefficient Cd": "",
            "Air Density ρ (kg/m³)": "",
            "Gravity g (m/s²)": "",
            "KTAS (knots true airspeed)": "",
            "Time step (s)": "",
            "Impact / slide physics": ""
        }

        title = QLabel("Config")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(title)

        self.inputs = {}

        for key, value in defaults.items():
            lbl = QLabel(key)
            edit = QLineEdit()
            edit.setPlaceholderText(key)
            layout.addWidget(lbl)
            layout.addWidget(edit)
            self.inputs[key] = edit

        self.include_ground_drag = QCheckBox("Include ground drag")
        self.include_ground_drag.setChecked(True)
        layout.addWidget(self.include_ground_drag)

        # Add surface type dropdown
        surface_label = QLabel("Surface Type")
        layout.addWidget(surface_label)
        self.surface_combo = QComboBox()
        self.surface_combo.addItems(["concrete", "asphalt", "grass"])
        layout.addWidget(self.surface_combo)

        layout.addStretch()

    def build_file_panel(self, layout):
        title = QLabel("Flight Input & Simulation")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(title)

        self.mode_group = QButtonGroup(self)
        self.rb_kml = QRadioButton("From KML")
        self.rb_coords = QRadioButton("From Coordinates")
        self.rb_bearing = QRadioButton("From Bearing")

        self.rb_kml.setChecked(True)

        self.mode_group.addButton(self.rb_kml)
        self.mode_group.addButton(self.rb_coords)
        self.mode_group.addButton(self.rb_bearing)

        hbox_modes = QHBoxLayout()
        hbox_modes.addWidget(self.rb_kml)
        hbox_modes.addWidget(self.rb_coords)
        hbox_modes.addWidget(self.rb_bearing)
        hbox_modes.addStretch()
        layout.addLayout(hbox_modes)

        self.flight_mode = "kml"

        self.rb_kml.toggled.connect(lambda checked: self.set_flight_mode("kml") if checked else None)
        self.rb_coords.toggled.connect(lambda checked: self.set_flight_mode("coords") if checked else None)
        self.rb_bearing.toggled.connect(lambda checked: self.set_flight_mode("bearing") if checked else None)

        self.mode_stack = QWidget()
        self.mode_stack_layout = QVBoxLayout(self.mode_stack)
        layout.addWidget(self.mode_stack)

        # Altitude inputs
        alt_layout = QHBoxLayout()
        alt_m_label = QLabel("Altitude (m)")
        self.alt_m = QLineEdit()
        alt_ft_label = QLabel("Altitude (ft)")
        self.alt_ft = QLineEdit()
        alt_layout.addWidget(alt_m_label)
        alt_layout.addWidget(self.alt_m)
        alt_layout.addWidget(alt_ft_label)
        alt_layout.addWidget(self.alt_ft)
        layout.addLayout(alt_layout)

        self._alt_updating = False

        def alt_m_changed(text):
            if self._alt_updating:
                return
            self._alt_updating = True
            try:
                m = float(text)
                ft = m * 3.28084
                self.alt_ft.setText(f"{ft:.2f}")
            except ValueError:
                self.alt_ft.clear()
            self._alt_updating = False

        def alt_ft_changed(text):
            if self._alt_updating:
                return
            self._alt_updating = True
            try:
                ft = float(text)
                m = ft / 3.28084
                self.alt_m.setText(f"{m:.2f}")
            except ValueError:
                self.alt_m.clear()
            self._alt_updating = False

        self.alt_m.textChanged.connect(alt_m_changed)
        self.alt_ft.textChanged.connect(alt_ft_changed)

        # Terrain inputs
        terrain_layout = QHBoxLayout()
        terrain_m_label = QLabel("Terrain (m)")
        self.terrain_m = QLineEdit()
        terrain_ft_label = QLabel("Terrain (ft)")
        self.terrain_ft = QLineEdit()
        terrain_layout.addWidget(terrain_m_label)
        terrain_layout.addWidget(self.terrain_m)
        terrain_layout.addWidget(terrain_ft_label)
        terrain_layout.addWidget(self.terrain_ft)
        layout.addLayout(terrain_layout)

        self._terrain_updating = False

        def terrain_m_changed(text):
            if self._terrain_updating:
                return
            self._terrain_updating = True
            try:
                m = float(text)
                ft = m * 3.28084
                self.terrain_ft.setText(f"{ft:.2f}")
            except ValueError:
                self.terrain_ft.clear()
            self._terrain_updating = False

        def terrain_ft_changed(text):
            if self._terrain_updating:
                return
            self._terrain_updating = True
            try:
                ft = float(text)
                m = ft / 3.28084
                self.terrain_m.setText(f"{m:.2f}")
            except ValueError:
                self.terrain_m.clear()
            self._terrain_updating = False

        self.terrain_m.textChanged.connect(terrain_m_changed)
        self.terrain_ft.textChanged.connect(terrain_ft_changed)

        # KML drop area (only for KML mode)
        self.kml_container = QWidget()
        kml_layout = QVBoxLayout(self.kml_container)
        self.file_label = QLabel("Drop KML file here\nor click to browse")
        self.file_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.file_label.setFrameShape(QFrame.Shape.Box)
        self.file_label.setMinimumHeight(200)
        self.file_label.setAcceptDrops(True)

        self.file_label.mousePressEvent = self.browse_file
        self.file_label.dragEnterEvent = self.drag_enter
        self.file_label.dropEvent = self.drop_event

        kml_layout.addWidget(self.file_label)

        # Coordinates mode inputs
        self.coords_container = QWidget()
        coords_layout = QVBoxLayout(self.coords_container)
        lat1_label = QLabel("Lat 1")
        self.lat1_input = QLineEdit()
        lon1_label = QLabel("Lon 1")
        self.lon1_input = QLineEdit()
        lat2_label = QLabel("Lat 2")
        self.lat2_input = QLineEdit()
        lon2_label = QLabel("Lon 2")
        self.lon2_input = QLineEdit()

        coords_layout.addWidget(lat1_label)
        coords_layout.addWidget(self.lat1_input)
        coords_layout.addWidget(lon1_label)
        coords_layout.addWidget(self.lon1_input)
        coords_layout.addWidget(lat2_label)
        coords_layout.addWidget(self.lat2_input)
        coords_layout.addWidget(lon2_label)
        coords_layout.addWidget(self.lon2_input)

        # Bearing mode inputs
        self.bearing_container = QWidget()
        bearing_layout = QVBoxLayout(self.bearing_container)
        lat_label = QLabel("Lat")
        self.bearing_lat_input = QLineEdit()
        lon_label = QLabel("Lon")
        self.bearing_lon_input = QLineEdit()
        azimuth_label = QLabel("Azimuth (degrees)")
        self.azimuth_input = QLineEdit()

        bearing_layout.addWidget(lat_label)
        bearing_layout.addWidget(self.bearing_lat_input)
        bearing_layout.addWidget(lon_label)
        bearing_layout.addWidget(self.bearing_lon_input)
        bearing_layout.addWidget(azimuth_label)
        bearing_layout.addWidget(self.azimuth_input)

        # Store all flight inputs
        self.flight_inputs = {
            "kml": {},
            "coords": {
                "lat1": self.lat1_input,
                "lon1": self.lon1_input,
                "lat2": self.lat2_input,
                "lon2": self.lon2_input,
            },
            "bearing": {
                "lat": self.bearing_lat_input,
                "lon": self.bearing_lon_input,
                "azimuth": self.azimuth_input,
            }
        }

        layout.addWidget(self.kml_container)

        self.run_btn = QPushButton("Run Simulation")
        self.run_btn.clicked.connect(self.run_simulation)
        layout.addWidget(self.run_btn)

        layout.addStretch()

        self.update_flight_mode_ui()

    def set_flight_mode(self, mode):
        self.flight_mode = mode
        self.update_flight_mode_ui()

    def update_flight_mode_ui(self):
        # Clear mode stack
        for i in reversed(range(self.mode_stack_layout.count())):
            w = self.mode_stack_layout.itemAt(i).widget()
            if w:
                w.setParent(None)

        if self.flight_mode == "kml":
            self.kml_container.show()
        else:
            self.kml_container.hide()

        if self.flight_mode == "kml":
            # no additional widgets in mode_stack for kml
            pass
        elif self.flight_mode == "coords":
            self.mode_stack_layout.addWidget(self.coords_container)
        elif self.flight_mode == "bearing":
            self.mode_stack_layout.addWidget(self.bearing_container)

    def browse_file(self, _):
        file, _ = QFileDialog.getOpenFileName(
            self, "Open KML", "", "KML Files (*.kml)"
        )
        if file:
            self.kml_input_path = file
            self.file_label.setText(file)

    def drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def drop_event(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.kml_input_path = urls[0].toLocalFile()
            self.file_label.setText(self.kml_input_path)

    def run_simulation(self):
        # Validate altitude input
        try:
            altitude_m = float(self.alt_m.text())
        except (ValueError, AttributeError):
            QMessageBox.warning(self, "Invalid input", "Please enter a valid altitude in metres.")
            return

        # Validate terrain input
        try:
            terrain_m = float(self.terrain_m.text())
        except (ValueError, AttributeError):
            QMessageBox.warning(self, "Invalid input", "Please enter a valid terrain height in metres.")
            return

        # Validate config inputs
        try:
            config = {k: float(v.text()) for k, v in self.inputs.items()}
        except ValueError:
            QMessageBox.warning(self, "Invalid input", "Please enter valid numerical values in config fields.")
            return

        config["include_ground_drag"] = self.include_ground_drag.isChecked()
        config["surface"] = self.surface_combo.currentText()

        # Prepare input params based on flight mode
        if self.flight_mode == "kml":
            if not hasattr(self, "kml_input_path") or not self.kml_input_path:
                QMessageBox.warning(self, "Missing input", "Please load a KML file first.")
                return

            input_kml = self.kml_input_path
            input_coords = None
            input_bearing = None

        elif self.flight_mode == "coords":
            try:
                lat1 = float(self.lat1_input.text())
                lon1 = float(self.lon1_input.text())
                lat2 = float(self.lat2_input.text())
                lon2 = float(self.lon2_input.text())
            except ValueError:
                QMessageBox.warning(self, "Invalid input", "Please enter valid coordinates for Lat 1, Lon 1, Lat 2, Lon 2.")
                return
            input_kml = None
            input_coords = (lat1, lon1, lat2, lon2)
            input_bearing = None

        elif self.flight_mode == "bearing":
            try:
                lat = float(self.bearing_lat_input.text())
                lon = float(self.bearing_lon_input.text())
                azimuth = float(self.azimuth_input.text())
            except ValueError:
                QMessageBox.warning(self, "Invalid input", "Please enter valid latitude, longitude, and azimuth.")
                return
            input_kml = None
            input_coords = None
            input_bearing = (lat, lon, azimuth)
        else:
            QMessageBox.warning(self, "Invalid mode", "Unknown flight input mode selected.")
            return

        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save output KML",
            "debris_trajectory.kml",
            "KML Files (*.kml)"
        )
        if not save_path:
            return

        self.run_debris_calculator(
            input_kml=input_kml,
            input_coords=input_coords,
            input_bearing=input_bearing,
            output_kml=save_path,
            config=config,
            altitude_m=altitude_m,
            terrain_m=terrain_m,
        )

    def run_debris_calculator(self, input_kml, input_coords, input_bearing, output_kml, config, altitude_m, terrain_m):
        """
        Hook point for Debris_Trajectory_Calculator.py
        """
        try:
            simulation = DebrisTrajectoryCalculator(
                mass_kg=config["Mass (kg)"],
                area_m2=config["Frontal area A (m²)"],
                Cd=config["Drag Coefficient Cd"],
                rho=config["Air Density ρ (kg/m³)"],
                g=config["Gravity g (m/s²)"],
                dt=config["Time step (s)"],
                ktas=config["KTAS (knots true airspeed)"],
                surface=config.get("surface", "asphalt"),
                input_file=input_kml,
                output_file=output_kml,
                include_ground_drag=config["include_ground_drag"],
                terrain_ft=terrain_m * 3.28084,
                altitude_m=altitude_m,
                input_coords=input_coords,
                input_bearing=input_bearing,
            )
            simulation.run_debris_trajectory_simulation()
        except Exception as e:
            QMessageBox.critical(self, "Simulation Error", str(e))
        else:
            QMessageBox.information(self, "Simulation Complete", "Debris trajectory simulation completed successfully.")

class App(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Farnborough Tools")
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

        self.rb_transpose = QRadioButton("Transpose to Farnborough")
        self.rb_debris = QRadioButton("Debris Trajectory")

        self.rb_transpose.setChecked(True)

        self.mode_group.addButton(self.rb_transpose)
        self.mode_group.addButton(self.rb_debris)

        self.rb_transpose.toggled.connect(self.switch_mode)

        bar.addWidget(self.rb_transpose)
        bar.addWidget(self.rb_debris)
        bar.addStretch()

        self.root_layout.addLayout(bar)

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


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = App()
    window.show()
    sys.exit(app.exec())