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
from PyQt6.QtGui import QAction
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon

import json
import os
import platform

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def _select_icon_name():
    """Return a preferred icon filename for the current OS.

    - macOS -> .icns
    - Windows -> .ico
    - else -> .png (fallback)
    """
    if sys.platform == "darwin":
        return "app.icns"
    if sys.platform.startswith("win"):
        return "app.ico"
    return "app.png"


def find_icon_path():
    """Resolve the icon path from a list of sensible names and return the first
    one that exists (resolved via resource_path). Returns None if none found.
    """
    candidates = [_select_icon_name(), "app.ico", "app.icns", "app.png"]
    for name in candidates:
        path = resource_path(name)
        if os.path.exists(path):
            return path
    return None

from Debris_Trajectory_Calculator import DebrisTrajectoryCalculator
from KML_File_Handling import load_last_two_points_from_kml
from Transpose_Coordinates import run_transposition

class TransposePage(QWidget):
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

        presets_layout = QVBoxLayout(presets_widget)
        config_layout = QVBoxLayout(config_widget)
        file_layout = QVBoxLayout(file_widget)

        splitter.addWidget(presets_widget)
        splitter.addWidget(config_widget)
        splitter.addWidget(file_widget)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 2)

        # Presets
        self.presets_dir = resource_path("data/airfields")
        os.makedirs(self.presets_dir, exist_ok=True)
        self.presets = {}
        self.build_presets_panel(presets_layout)
        self.load_presets_from_disk()

        # Config (Lat, Lon, Heading)
        self.build_config_panel(config_layout)

        # File Drop & Run
        self.input_files = []
        self.build_file_panel(file_layout)

    def build_presets_panel(self, layout):
        title = QLabel("Airfield Presets")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(title)

        self.preset_list = QListWidget()
        self.preset_list.itemClicked.connect(self.load_selected_preset)
        layout.addWidget(self.preset_list)

        save_btn = QPushButton("Save Preset")
        load_btn = QPushButton("Load Preset from File")
        delete_btn = QPushButton("Delete Preset")

        save_btn.clicked.connect(self.save_preset)
        load_btn.clicked.connect(self.load_preset_from_file)
        delete_btn.clicked.connect(self.delete_preset)

        layout.addWidget(save_btn)
        layout.addWidget(load_btn)
        layout.addWidget(delete_btn)
        layout.addStretch()

    def build_config_panel(self, layout):
        # Original Airfield Height Section
        orig_title = QLabel("Original Airfield")
        orig_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(orig_title)

        self.orig_height_input = QLineEdit()
        self.orig_height_input.setPlaceholderText("Elevation (m)")
        self.orig_height_input.textChanged.connect(self.orig_height_m_changed)
        
        m_layout = QVBoxLayout()
        m_layout.addWidget(QLabel("Original Elevation (m)"))
        m_layout.addWidget(self.orig_height_input)

        self.orig_height_ft_input = QLineEdit()
        self.orig_height_ft_input.setPlaceholderText("Elevation (ft)")
        self.orig_height_ft_input.textChanged.connect(self.orig_height_ft_changed)

        ft_layout = QVBoxLayout()
        ft_layout.addWidget(QLabel("Original Elevation (ft)"))
        ft_layout.addWidget(self.orig_height_ft_input)
        
        # Container for both
        h_layout = QHBoxLayout()
        h_layout.addLayout(m_layout)
        h_layout.addLayout(ft_layout)

        layout.addLayout(h_layout)

        self._orig_height_updating = False
        
        layout.addSpacing(20)

        # Target Airfield Section
        title = QLabel("Target Airfield")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(title)

        self.airfield_name_input = QLineEdit()
        self.airfield_name_input.setPlaceholderText("Airfield Name")
        layout.addWidget(QLabel("Airfield Name"))
        layout.addWidget(self.airfield_name_input)

        self.lat_input = QLineEdit()
        self.lat_input.setPlaceholderText("Latitude")
        layout.addWidget(QLabel("Latitude"))
        layout.addWidget(self.lat_input)

        self.lon_input = QLineEdit()
        self.lon_input.setPlaceholderText("Longitude")
        layout.addWidget(QLabel("Longitude"))
        layout.addWidget(self.lon_input)

        self.heading_input = QLineEdit()
        self.heading_input.setPlaceholderText("Heading (degrees)")
        layout.addWidget(QLabel("Runway Heading"))
        layout.addWidget(self.heading_input)

        layout.addStretch()

    def build_file_panel(self, layout):
        title = QLabel("Transposition")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(title)

        # Replace QLabel with QListWidget for file list
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.file_list.setMinimumHeight(120)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDragDropMode(QListWidget.DragDropMode.DropOnly)
        
        # Override drop event on the list widget
        self.file_list.dragEnterEvent = self.drag_enter
        self.file_list.dragMoveEvent = self.drag_move
        self.file_list.dropEvent = self.drop_event

        layout.addWidget(self.file_list)

        # Buttons for file management
        btn_layout = QHBoxLayout()
        add_btn = QPushButton("Add Files")
        remove_btn = QPushButton("Remove Selected")
        
        add_btn.clicked.connect(self.browse_files)
        remove_btn.clicked.connect(self.remove_selected_files)
        
        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(remove_btn)
        layout.addLayout(btn_layout)

        self.run_btn = QPushButton("Run Transposition")
        self.run_btn.clicked.connect(self.run_transposition_ui)
        layout.addWidget(self.run_btn)

        layout.addStretch()

    def drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def drag_move(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def drop_event(self, event):
        urls = event.mimeData().urls()
        if urls:
            new_files = [u.toLocalFile() for u in urls if u.toLocalFile().endswith('.kml')]
            self.add_files_to_list(new_files)

    def browse_files(self, event=None):
        files, _ = QFileDialog.getOpenFileNames(self, "Select KML Files", "", "KML Files (*.kml)")
        if files:
            self.add_files_to_list(files)

    def add_files_to_list(self, files):
        existing_files = {self.file_list.item(i).text() for i in range(self.file_list.count())}
        for f in files:
            if f not in existing_files:
                self.file_list.addItem(f)
                self.input_files.append(f)

    def remove_selected_files(self):
        for item in self.file_list.selectedItems():
            row = self.file_list.row(item)
            self.file_list.takeItem(row)
            if item.text() in self.input_files:
                self.input_files.remove(item.text())

    def update_file_label(self):
        # Deprecated, logic moved to list widget
        pass

    def load_presets_from_disk(self):
        self.presets = {}
        if not os.path.exists(self.presets_dir):
            return
        
        for filename in os.listdir(self.presets_dir):
            if filename.endswith(".json"):
                path = os.path.join(self.presets_dir, filename)
                try:
                    with open(path, "r") as f:
                        data = json.load(f)
                    name = filename.replace(".json", "")
                    self.presets[name] = {"data": data, "path": path}
                except Exception:
                    pass
        self.refresh_preset_list()

    def refresh_preset_list(self):
        self.preset_list.clear()
        for name in sorted(self.presets.keys()):
            self.preset_list.addItem(name)

    def save_preset(self):
        default_name = self.airfield_name_input.text()
        name, ok = QInputDialog.getText(self, "Save Preset", "Enter preset name:", text=default_name)
        if not ok or not name:
            return

        data = {
            "name": self.airfield_name_input.text(),
            "latitude": self.lat_input.text(),
            "longitude": self.lon_input.text(),
            "heading": self.heading_input.text(),
            "original_elevation_m": self.orig_height_input.text()
        }

        filename = f"{name}.json"
        path = os.path.join(self.presets_dir, filename)
        
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            self.presets[name] = {"data": data, "path": path}
            self.refresh_preset_list()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save preset: {e}")

    def load_preset_from_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Preset", self.presets_dir, "JSON Files (*.json)")
        if not path:
            return
        
        try:
            with open(path, "r") as f:
                data = json.load(f)
            
            self.airfield_name_input.setText(data.get("name", ""))
            self.lat_input.setText(data.get("latitude", ""))
            self.lon_input.setText(data.get("longitude", ""))
            self.heading_input.setText(data.get("heading", ""))
            self.orig_height_input.setText(data.get("original_elevation_m", ""))
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load preset: {e}")

    def load_selected_preset(self, item):
        name = item.text()
        entry = self.presets.get(name)
        if entry:
            data = entry["data"]
            self.airfield_name_input.setText(data.get("name", ""))
            self.lat_input.setText(data.get("latitude", ""))
            self.lon_input.setText(data.get("longitude", ""))
            self.heading_input.setText(data.get("heading", ""))
            self.orig_height_input.setText(data.get("original_elevation_m", ""))

    def delete_preset(self):
        item = self.preset_list.currentItem()
        if not item:
            return
        
        name = item.text()
        entry = self.presets.get(name)
        if entry:
            path = entry["path"]
            try:
                os.remove(path)
                del self.presets[name]
                self.refresh_preset_list()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to delete preset: {e}")

    def run_transposition_ui(self):
        if not self.input_files:
            QMessageBox.warning(self, "No Files", "Please select at least one KML file.")
            return

        try:
            lat = float(self.lat_input.text())
            lon = float(self.lon_input.text())
            heading = float(self.heading_input.text())
        except ValueError:
            QMessageBox.warning(self, "Invalid Input", "Please enter valid numeric values for Latitude, Longitude, and Heading.")
            return

        # Get Original Height
        try:
            orig_height_text = self.orig_height_input.text()
            if not orig_height_text:
                orig_height = 0.0
            else:
                orig_height = float(orig_height_text)
        except ValueError:
            QMessageBox.warning(self, "Invalid Input", "Please enter a valid numeric value for Original Height.")
            return

        output_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Output",
            "transposed_output.kml",
            "KML Files (*.kml)"
        )
        if not output_path:
            return

        # If user selected a directory instead of a filename, append default filename
        if os.path.isdir(output_path):
            output_path = os.path.join(output_path, "transposed_output.kml")

        # Ensure directory exists
        output_dir = os.path.dirname(output_path)
        os.makedirs(output_dir, exist_ok=True)

        # Use the entered airfield name, or fallback to preset name, or "Airfield"
        airfield_name_hook = self.airfield_name_input.text()
        if not airfield_name_hook:
            item = self.preset_list.currentItem()
            if item:
                airfield_name_hook = item.text()
            else:
                airfield_name_hook = "Airfield"

        try:
            run_transposition(
                input_files=self.input_files,
                output_file=output_path,
                target_lat=lat,
                target_lon=lon,
                target_heading=heading,
                ground_reference_elevation=orig_height
            )
            QMessageBox.information(self, "Success", f"Transposition complete!\nSaved to {output_dir}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Transposition failed: {e}")

    def orig_height_m_changed(self, text):
        if self._orig_height_updating:
            return
        self._orig_height_updating = True
        try:
            val_m = float(text)
            self.orig_height_ft_input.setText(f"{val_m * 3.28084:.2f}")
        except ValueError:
            self.orig_height_ft_input.clear()
        self._orig_height_updating = False

    def orig_height_ft_changed(self, text):
        if self._orig_height_updating:
            return
        self._orig_height_updating = True
        try:
            val_ft = float(text)
            self.orig_height_input.setText(f"{val_ft / 3.28084:.2f}")
        except ValueError:
            self.orig_height_input.clear()
        self._orig_height_updating = False


class DebrisPage(QWidget):
    def load_kml_metadata(self):
        if not hasattr(self, "kml_input_path") or not self.kml_input_path:
            QMessageBox.warning(self, "Missing file", "Please drop or select a KML file first.")
            return

        try:
            (
                penultimate_lat,
                penultimate_lon,
                final_lat,
                final_lon,
                alt_m
            ) = load_last_two_points_from_kml(self.kml_input_path)
        except Exception as e:
            QMessageBox.critical(self, "KML Error", str(e))
            return

        self.kml_meta_pen_lat.setText(f"Penultimate latitude: {penultimate_lat}")
        self.kml_meta_pen_lon.setText(f"Penultimate longitude: {penultimate_lon}")
        self.kml_meta_fin_lat.setText(f"Final latitude: {final_lat}")
        self.kml_meta_fin_lon.setText(f"Final longitude: {final_lon}")

        #package up for hooking into DebrisTrajectoryCalculator
        self.kml_values = (penultimate_lat, penultimate_lon, final_lat, final_lon)

        # Populate the shared altitude field from KML
        self.alt_m.setText(f"{alt_m}")

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

        # self.presets_path = "data/presets.json"
        self.presets_dir = resource_path("data/presets")
        os.makedirs(self.presets_dir, exist_ok=True)
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
        load_btn = QPushButton("Load preset")
        delete_btn = QPushButton("Delete preset")

        save_btn.clicked.connect(self.save_preset)
        load_btn.clicked.connect(self.load_preset_from_file)
        delete_btn.clicked.connect(self.delete_preset)

        layout.addWidget(save_btn)
        layout.addWidget(load_btn)
        layout.addWidget(delete_btn)

        layout.addStretch()

    def load_presets_from_disk(self):
        self.presets = {}
        if not os.path.isdir(self.presets_dir):
            return

        for filename in os.listdir(self.presets_dir):
            if filename.endswith(".json"):
                path = os.path.join(self.presets_dir, filename)
                try:
                    with open(path, "r") as f:
                        data = json.load(f)
                    name = filename.replace(".json", "")
                    self.presets[name] = {"data": data, "path": path}
                except Exception:
                    pass

        self.refresh_preset_list()

    def refresh_preset_list(self):
        self.preset_list.clear()
        for name in self.presets:
            self.preset_list.addItem(name)

    def save_preset(self):
        preset = {
            "config": {k: v.text() for k, v in self.inputs.items()},
            "surface": self.surface_combo.currentText(),
            "include_ground_drag": self.include_ground_drag.isChecked(),
            "altitude_m": self.alt_m.text(),
            "terrain_m": self.terrain_m.text(),
            "height_m": self.height_m.text(),
            "flight_mode": self.flight_mode,
            "flight_inputs": {
                "kml": {
                    "kml_path": getattr(self, "kml_input_path", "")
                },
                "coords": {
                    "lat1": self.lat1_input.text(),
                    "lon1": self.lon1_input.text(),
                    "lat2": self.lat2_input.text(),
                    "lon2": self.lon2_input.text(),
                },
                "bearing": {
                    "lat": self.bearing_lat_input.text(),
                    "lon": self.bearing_lon_input.text(),
                    "azimuth": self.azimuth_input.text(),
                }
            }
        }

        name, ok = QInputDialog.getText(
            self,
            "Save Preset",
            "Enter preset name:"
        )
        if not ok or not name:
            return

        path = os.path.join(self.presets_dir, f"{name}.json")

        with open(path, "w") as f:
            json.dump(preset, f, indent=2)

        name = os.path.basename(path).replace(".json", "")
        self.presets[name] = {"data": preset, "path": path}
        self.refresh_preset_list()

    def load_preset_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Aircraft Preset",
            "",
            "JSON Files (*.json)"
        )
        if not path:
            return

        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Preset Error", str(e))
            return

        name = os.path.basename(path).replace(".json", "")
        self.presets[name] = {"data": data, "path": path}
        self.refresh_preset_list()

    def load_selected_preset(self, item):
        entry = self.presets.get(item.text())
        if not entry:
            return
        data = entry["data"]

        for k, v in data.get("config", {}).items():
            if k in self.inputs:
                self.inputs[k].setText(v)

        surface = data.get("surface")
        if surface:
            index = self.surface_combo.findText(surface)
            if index >= 0:
                self.surface_combo.setCurrentIndex(index)

        self.include_ground_drag.setChecked(data.get("include_ground_drag", True))
        self.alt_m.setText(data.get("altitude_m", ""))
        self.terrain_m.setText(data.get("terrain_m", ""))
        self.height_m.setText(data.get("height_m", ""))

        mode = data.get("flight_mode", "kml")
        if mode == "kml":
            self.rb_kml.setChecked(True)
        elif mode == "coords":
            self.rb_coords.setChecked(True)
        elif mode == "bearing":
            self.rb_bearing.setChecked(True)

        # Restore flight_inputs after setting radio buttons
        flight_inputs = data.get("flight_inputs", {})

        if self.flight_mode == "kml":
            kml_data = flight_inputs.get("kml", {})
            self.kml_input_path = kml_data.get("kml_path", "")
            if self.kml_input_path:
                self.file_label.setText(self.kml_input_path)

        elif self.flight_mode == "coords":
            coords = flight_inputs.get("coords", {})
            self.lat1_input.setText(coords.get("lat1", ""))
            self.lon1_input.setText(coords.get("lon1", ""))
            self.lat2_input.setText(coords.get("lat2", ""))
            self.lon2_input.setText(coords.get("lon2", ""))

        elif self.flight_mode == "bearing":
            bearing = flight_inputs.get("bearing", {})
            self.bearing_lat_input.setText(bearing.get("lat", ""))
            self.bearing_lon_input.setText(bearing.get("lon", ""))
            self.azimuth_input.setText(bearing.get("azimuth", ""))

    def delete_preset(self):
        item = self.preset_list.currentItem()
        if not item:
            return

        name = item.text()
        entry = self.presets.get(name)
        if not entry:
            return

        path = entry.get("path")
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except Exception as e:
                QMessageBox.critical(self, "Delete Error", str(e))
                return

        del self.presets[name]
        self.refresh_preset_list()

    def build_config(self, layout):
        defaults = {
            "Mass (kg)": "",
            "Frontal area A (m²)": "",
            "Drag Coefficient Cd": "1.1",
            "Air Density ρ (kg/m³)": "1.23",
            "Gravity g (m/s²)": "9.81",
            "KTAS (knots true airspeed)": "",
            "Time step (s)": "0.01",
            "Impact / slide physics": "0.5"
        }

        title = QLabel("Config")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(title)

        self.inputs = {}

        for key, value in defaults.items():
            lbl = QLabel(key)
            edit = QLineEdit()
            edit.setPlaceholderText(key)
            if value != "":
                edit.setText(value)
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
        self.rb_bearing = QRadioButton("From Track")

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

        # KML drop area (only for KML mode)
        self.kml_container = QWidget()
        kml_layout = QVBoxLayout(self.kml_container)

        # Drag & drop area
        self.file_label = QLabel("Drop KML file here")
        self.file_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.file_label.setFrameShape(QFrame.Shape.Box)
        self.file_label.setMinimumHeight(120)
        self.file_label.setAcceptDrops(True)

        self.file_label.mousePressEvent = self.browse_file
        self.file_label.dragEnterEvent = self.drag_enter
        self.file_label.dropEvent = self.drop_event

        kml_layout.addWidget(self.file_label)

        #Load KML button
        self.load_kml_btn = QPushButton("Extract Values from KML")
        self.load_kml_btn.clicked.connect(self.load_kml_metadata)
        kml_layout.addWidget(self.load_kml_btn)

        # Metadata display labels
        self.kml_meta_pen_lat = QLabel("Penultimate latitude: —")
        self.kml_meta_pen_lon = QLabel("Penultimate longitude: —")
        self.kml_meta_fin_lat = QLabel("Final latitude: —")
        self.kml_meta_fin_lon = QLabel("Final longitude: —")

        kml_layout.addWidget(self.kml_meta_pen_lat)
        kml_layout.addWidget(self.kml_meta_pen_lon)
        kml_layout.addWidget(self.kml_meta_fin_lat)
        kml_layout.addWidget(self.kml_meta_fin_lon)

        # Coordinates mode inputs
        self.coords_container = QWidget()
        coords_layout = QVBoxLayout(self.coords_container)
        lat1_label = QLabel("Latitude 1")
        self.lat1_input = QLineEdit()
        lon1_label = QLabel("Longitude 1")
        self.lon1_input = QLineEdit()
        lat2_label = QLabel("Latitude 2")
        self.lat2_input = QLineEdit()
        lon2_label = QLabel("Longitude 2")
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
        lat_label = QLabel("Latitude")
        self.bearing_lat_input = QLineEdit()
        lon_label = QLabel("Longitude")
        self.bearing_lon_input = QLineEdit()
        azimuth_label = QLabel("Track (degrees)")
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

        # Altitude inputs (shared by all modes)
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

        # Height above ground inputs
        height_layout = QHBoxLayout()
        height_m_label = QLabel("Height (m)")
        self.height_m = QLineEdit()
        height_ft_label = QLabel("Height (ft)")
        self.height_ft = QLineEdit()
        height_layout.addWidget(height_m_label)
        height_layout.addWidget(self.height_m)
        height_layout.addWidget(height_ft_label)
        height_layout.addWidget(self.height_ft)
        layout.addLayout(height_layout)

        # Update flags
        self._alt_updating = False
        self._terrain_updating = False
        self._height_updating = False

        # Connect signals
        self.alt_m.textChanged.connect(self.alt_m_changed)
        self.alt_ft.textChanged.connect(self.alt_ft_changed)
        self.terrain_m.textChanged.connect(self.terrain_m_changed)
        self.terrain_ft.textChanged.connect(self.terrain_ft_changed)
        self.height_m.textChanged.connect(self.height_m_changed)
        self.height_ft.textChanged.connect(self.height_ft_changed)

        self.run_btn = QPushButton("Run Simulation")
        self.run_btn.clicked.connect(self.run_simulation)
        layout.addWidget(self.run_btn)

        # --- Simulation summary UI elements ---
        summary_title = QLabel("Simulation Summary")
        summary_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(summary_title)

        self.summary_heading = QLabel("Track used (deg): —")
        self.summary_air = QLabel("Air distance to first impact (m): —")
        self.summary_ground = QLabel("Ground distance to rest (m): —")
        self.summary_total = QLabel("Total ground‑planar distance (m): —")
        self.summary_impacts = QLabel("Impacts (incl. first): —")

        layout.addWidget(self.summary_heading)
        layout.addWidget(self.summary_air)
        layout.addWidget(self.summary_ground)
        layout.addWidget(self.summary_total)
        layout.addWidget(self.summary_impacts)

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

    # --- Fully linked handlers ---
    def alt_m_changed(self, text):
        if self._alt_updating:
            return
        self._alt_updating = True
        try:
            m = float(text)
            self.alt_ft.setText(f"{m * 3.28084:.2f}")
        except ValueError:
            self.alt_ft.clear()
        self._alt_updating = False
        self.update_from_alt_terrain()

    def alt_ft_changed(self, text):
        if self._alt_updating:
            return
        self._alt_updating = True
        try:
            ft = float(text)
            self.alt_m.setText(f"{ft / 3.28084:.2f}")
        except ValueError:
            self.alt_m.clear()
        self._alt_updating = False
        self.update_from_alt_terrain()

    def terrain_m_changed(self, text):
        if self._terrain_updating:
            return
        self._terrain_updating = True
        try:
            m = float(text)
            self.terrain_ft.setText(f"{m * 3.28084:.2f}")
        except ValueError:
            self.terrain_ft.clear()
        self._terrain_updating = False
        self.update_from_alt_terrain()

    def terrain_ft_changed(self, text):
        if self._terrain_updating:
            return
        self._terrain_updating = True
        try:
            ft = float(text)
            self.terrain_m.setText(f"{ft / 3.28084:.2f}")
        except ValueError:
            self.terrain_m.clear()
        self._terrain_updating = False
        self.update_from_alt_terrain()

    def height_m_changed(self, text):
        if self._height_updating:
            return
        self._height_updating = True
        try:
            m = float(text)
            self.height_ft.setText(f"{m * 3.28084:.2f}")
        except ValueError:
            self.height_ft.clear()
        self._height_updating = False
        self.update_from_height()

    def height_ft_changed(self, text):
        if self._height_updating:
            return
        self._height_updating = True
        try:
            ft = float(text)
            self.height_m.setText(f"{ft / 3.28084:.2f}")
        except ValueError:
            self.height_m.clear()
        self._height_updating = False
        self.update_from_height()

    def update_from_alt_terrain(self):
        if self._height_updating:
            return
        try:
            alt_m = float(self.alt_m.text())
            terr_m = float(self.terrain_m.text())
        except ValueError:
            return

        self._height_updating = True
        height_m = alt_m - terr_m
        self.height_m.setText(f"{height_m:.2f}")
        self.height_ft.setText(f"{height_m * 3.28084:.2f}")
        self._height_updating = False

    def update_from_height(self):
        if self._alt_updating or self._terrain_updating:
            return
        try:
            height_m = float(self.height_m.text())
            terr_m = float(self.terrain_m.text())
        except ValueError:
            return

        self._alt_updating = True
        alt_m = height_m + terr_m
        self.alt_m.setText(f"{alt_m:.2f}")
        self.alt_ft.setText(f"{alt_m * 3.28084:.2f}")
        self._alt_updating = False

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
            config = {k: float(v.text()) for k, v in self.inputs.items() if v.text() != ""}
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

            input_coords = self.kml_values
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
            
            input_coords = (lat1, lon1, lat2, lon2)
            input_bearing = None

        elif self.flight_mode == "bearing":
            try:
                lat = float(self.bearing_lat_input.text())
                lon = float(self.bearing_lon_input.text())
                azimuth = float(self.azimuth_input.text())
            except ValueError:
                QMessageBox.warning(self, "Invalid input", "Please enter valid Latitude, Longitude, and Track.")
                return
            
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
            input_coords_hook=input_coords,
            input_bearing_hook=input_bearing,
            output_kml=save_path,
            config=config,
            altitude_m_hook=altitude_m,
            terrain_m_hook=terrain_m,
        )

    def run_debris_calculator(self, input_coords_hook, input_bearing_hook, output_kml, config, altitude_m_hook, terrain_m_hook):
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
                slide_physics=config["Impact / slide physics"],
                include_ground_drag=config["include_ground_drag"],
                terrain_m=terrain_m_hook,
                altitude_m=altitude_m_hook,
                input_coords=input_coords_hook,
                input_bearing=input_bearing_hook,
                output_file=output_kml
            )

            summary = simulation.run_debris_trajectory_simulation()

            if summary:
                self.summary_heading.setText(f"Track used (deg): {summary['heading']:.1f}")
                self.summary_air.setText(f"Air distance to first impact (m): {summary['air_dist_xy_m']:.1f}")
                self.summary_ground.setText(f"Ground distance to rest (m): {summary['ground_dist_xy_m']:.1f}")
                self.summary_total.setText(f"Total ground‑planar distance (m): {summary['total_dist_xy_m']:.1f}")
                self.summary_impacts.setText(f"Impacts (incl. first): {summary['impacts']}")
        except Exception as e:
            QMessageBox.critical(self, "Simulation Error", str(e))
        else:
            QMessageBox.information(self, "Simulation Complete", "Debris trajectory simulation completed successfully.")

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

if __name__ == "__main__":
    app = QApplication(sys.argv)
    # Set application icon for the running app (select per-OS icon).
    app_icon = find_icon_path()
    if app_icon:
        try:
            app.setWindowIcon(QIcon(app_icon))
        except Exception:
            pass
    window = App()
    window.show()
    sys.exit(app.exec())