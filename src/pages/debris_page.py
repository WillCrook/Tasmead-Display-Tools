import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QRadioButton, QSplitter, QVBoxLayout, QWidget,
)

from file_dialog_state import (
    FileDialogDirection, FileDialogWorkflow, ensure_extension,
    remember_file_selection, remembered_directory, suggested_save_path,
)
from resource_paths import app_data_path, resource_path
from services import (
    DebrisSimulationRequest, DebrisSimulationResult, PresetType,
    SimulationProgress, parse_kml_track,
)
from workers import CancellationToken, DebrisSimulationWorker, SimulationFailure
from pages.preset_ui import PresetPanelLabels, PresetUiMixin
from pages.unit_fields import MetreFeetFieldPair


@dataclass(frozen=True, slots=True)
class DebrisKmlState:
    """The selected debris KML and the parse result belonging to that path."""

    path: str = ""
    coordinates: tuple[float, float, float, float] | None = None
    final_altitude_m: float | None = None
    error: str | None = None

    @property
    def ready(self):
        return self.coordinates is not None and self.error is None


class SimulationUiState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"


class DebrisPage(PresetUiMixin, QWidget):
    simulation_busy_changed = pyqtSignal(bool)
    worker_class = DebrisSimulationWorker

    @property
    def kml_input_path(self):
        """Compatibility view of the path stored in the authoritative KML state."""
        return self._kml_state.path

    @kml_input_path.setter
    def kml_input_path(self, path):
        """Legacy path assignment invalidates any parse result until Run reparses it."""
        self._kml_state = DebrisKmlState(path=os.fspath(path) if path else "")

    def update_preset_actions(self, *args):
        super().update_preset_actions(*args)
        if self.has_active_simulation():
            self.rename_preset_btn.setEnabled(False)
            self.delete_preset_btn.setEnabled(False)
            self.export_preset_btn.setEnabled(False)

    def render_kml_state(self):
        """Render the complete KML state without retaining metadata from another path."""
        if not hasattr(self, "kml_meta_pen_lat"):
            return

        state = self._kml_state
        self.kml_meta_pen_lat.setText("Penultimate latitude: —")
        self.kml_meta_pen_lon.setText("Penultimate longitude: —")
        self.kml_meta_fin_lat.setText("Final latitude: —")
        self.kml_meta_fin_lon.setText("Final longitude: —")
        self.load_kml_btn.setEnabled(
            bool(state.path) and not self.has_active_simulation()
        )
        self.file_label.setText(state.path or "Drop KML file here")

        if not state.path:
            self.kml_status_label.setText("No KML selected.")
            self.kml_status_label.setStyleSheet("")
            return

        if state.error:
            self.kml_status_label.setText(f"KML error: {state.error}")
            self.kml_status_label.setStyleSheet("color: #b00020;")
            return

        if not state.ready:
            self.kml_status_label.setText("Loading KML…")
            self.kml_status_label.setStyleSheet("")
            return

        penultimate_lat, penultimate_lon, final_lat, final_lon = state.coordinates
        self.kml_meta_pen_lat.setText(f"Penultimate latitude: {penultimate_lat}")
        self.kml_meta_pen_lon.setText(f"Penultimate longitude: {penultimate_lon}")
        self.kml_meta_fin_lat.setText(f"Final latitude: {final_lat}")
        self.kml_meta_fin_lon.setText(f"Final longitude: {final_lon}")
        self.kml_status_label.setStyleSheet("")
        if state.final_altitude_m is not None:
            self.kml_status_label.setText("KML ready.")
        elif self.alt_m.text():
            self.kml_status_label.setText("KML ready — using entered altitude.")
        else:
            self.kml_status_label.setText("KML ready — enter altitude in metres.")

    def select_and_parse_kml(self, path, *, altitude_fallback=None, notify=True):
        """Select and synchronously parse one path, committing a complete result."""
        selected_path = os.fspath(path) if path else ""
        self._kml_state = DebrisKmlState(path=selected_path)
        if altitude_fallback is None:
            self.alt_m.clear()
        else:
            self.alt_m.setText(altitude_fallback)
        self.render_kml_state()

        if not selected_path:
            return False

        try:
            track = parse_kml_track(selected_path)
        except Exception as error:
            message = str(error) or error.__class__.__name__
            self._kml_state = DebrisKmlState(path=selected_path, error=message)
            self.render_kml_state()
            if notify:
                QMessageBox.critical(self, "KML Error", message)
            return False

        penultimate, final = track.points[-2:]
        coordinates = (
            penultimate.latitude,
            penultimate.longitude,
            final.latitude,
            final.longitude,
        )
        self._kml_state = DebrisKmlState(
            path=selected_path,
            coordinates=coordinates,
            final_altitude_m=final.altitude_m,
        )

        if final.altitude_m is not None:
            self.alt_m.setText(f"{final.altitude_m}")
        elif altitude_fallback is None:
            self.alt_m.clear()

        self.render_kml_state()
        if final.altitude_m is None and not altitude_fallback and notify:
            QMessageBox.warning(
                self,
                "KML altitude missing",
                "The final KML coordinate has no altitude. Enter the altitude in metres before running the simulation.",
            )
        return True

    def reload_selected_kml(self):
        if not self.kml_input_path:
            QMessageBox.warning(self, "Missing file", "Please drop or select a KML file first.")
            return False
        return self.select_and_parse_kml(
            self.kml_input_path,
            altitude_fallback=self.alt_m.text(),
        )

    def __init__(self):
        super().__init__()

        self._simulation_state = SimulationUiState.IDLE
        self._simulation_thread = None
        self._simulation_worker = None
        self._cancellation_token = None
        self._terminal_outcome = None
        self._suppress_terminal_dialogs = False

        layout = QHBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: black;
                width: 6px;
            }
        """)
        layout.addWidget(splitter)

        self.presets_widget = QWidget()
        self.config_widget = QWidget()
        self.file_widget = QWidget()

        presets = QVBoxLayout(self.presets_widget)
        config = QVBoxLayout(self.config_widget)
        file_panel = QVBoxLayout(self.file_widget)

        splitter.addWidget(self.presets_widget)
        splitter.addWidget(self.config_widget)
        splitter.addWidget(self.file_widget)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 2)

        self.initialize_preset_management(
            preset_type=PresetType.DEBRIS,
            managed_directory=app_data_path("presets/debris"),
            legacy_managed_directory=app_data_path("debris-presets"),
            legacy_readonly_directory=resource_path("data/presets"),
            backup_directory=app_data_path("presets/legacy-backup/debris"),
        )

        self.build_preset_panel(
            presets,
            PresetPanelLabels(
                title="Presets",
                save="Save preset",
                load="Load preset",
                rename="Rename preset",
                delete="Delete preset",
                export="Export preset",
            ),
        )
        self.build_config(config)
        self.build_file_panel(file_panel)

        self.load_presets_from_disk()

    def save_preset(self):
        preset = self.capture_preset_data()

        name, ok = QInputDialog.getText(
            self,
            "Save Preset",
            "Enter preset name:"
        )
        if not ok or not name:
            return

        self.save_preset_data(name, preset, error_title="Preset Error")

    def capture_preset_data(self) -> dict[str, object]:
        return {
            "config": {k: v.text() for k, v in self.inputs.items()},
            "surface": self.surface_combo.currentText(),
            "include_ground_drag": self.include_ground_drag.isChecked(),
            "altitude_m": self.alt_m.text(),
            "terrain_m": self.terrain_m.text(),
            "height_m": self.height_m.text(),
            "flight_mode": self.flight_mode,
            "flight_inputs": {
                "kml": {
                    "kml_path": self._kml_state.path
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

    def load_preset_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Aircraft Preset",
            remembered_directory(
                FileDialogWorkflow.DEBRIS_PRESET,
                FileDialogDirection.INPUT,
            ),
            "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return

        remember_file_selection(
            FileDialogWorkflow.DEBRIS_PRESET,
            FileDialogDirection.INPUT,
            path,
        )

        self.import_preset_path(path, error_title="Preset Error")

    def apply_preset_data(self, data: Mapping[str, object]) -> None:
        for k, v in data.get("config", {}).items():
            if k in self.inputs:
                self.inputs[k].setText(v)

        surface = data.get("surface")
        if surface:
            index = self.surface_combo.findText(surface)
            if index >= 0:
                self.surface_combo.setCurrentIndex(index)

        self.include_ground_drag.setChecked(data.get("include_ground_drag", True))
        saved_altitude = data.get("altitude_m", "")
        self.alt_m.setText(saved_altitude)
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
            self.select_and_parse_kml(
                kml_data.get("kml_path", ""),
                altitude_fallback=saved_altitude,
            )

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
        self._kml_state = DebrisKmlState()

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

        self.load_kml_btn = QPushButton("Reload selected KML")
        self.load_kml_btn.clicked.connect(self.reload_selected_kml)
        kml_layout.addWidget(self.load_kml_btn)

        self.kml_status_label = QLabel("No KML selected.")
        self.kml_status_label.setWordWrap(True)
        kml_layout.addWidget(self.kml_status_label)

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

        self._altitude_units = MetreFeetFieldPair(
            self.alt_m,
            self.alt_ft,
            on_metres_changed=self.update_from_alt_terrain,
        )
        self._terrain_units = MetreFeetFieldPair(
            self.terrain_m,
            self.terrain_ft,
            on_metres_changed=self.update_from_alt_terrain,
        )
        self._height_units = MetreFeetFieldPair(
            self.height_m,
            self.height_ft,
            on_metres_changed=self.update_from_height,
        )

        self.alt_m.textChanged.connect(lambda _: self.render_kml_state())

        self.run_btn = QPushButton("Run Simulation")
        self.run_btn.clicked.connect(self.run_simulation)
        layout.addWidget(self.run_btn)

        self.cancel_simulation_btn = QPushButton("Cancel Simulation")
        self.cancel_simulation_btn.clicked.connect(
            lambda: self.cancel_simulation()
        )
        self.cancel_simulation_btn.hide()
        layout.addWidget(self.cancel_simulation_btn)

        self.simulation_progress_bar = QProgressBar()
        self.simulation_progress_bar.setRange(0, 100)
        self.simulation_progress_bar.setValue(0)
        self.simulation_progress_bar.hide()
        layout.addWidget(self.simulation_progress_bar)

        self.simulation_status_label = QLabel("Ready.")
        self.simulation_status_label.setWordWrap(True)
        layout.addWidget(self.simulation_status_label)

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

        self.render_kml_state()
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

    def update_from_alt_terrain(self):
        try:
            alt_m = float(self.alt_m.text())
            terr_m = float(self.terrain_m.text())
        except ValueError:
            return

        height_m = alt_m - terr_m
        self._height_units.set_metres_value(
            height_m,
            notify_dependents=False,
        )

    def update_from_height(self):
        try:
            height_m = float(self.height_m.text())
            terr_m = float(self.terrain_m.text())
        except ValueError:
            return

        alt_m = height_m + terr_m
        self._altitude_units.set_metres_value(
            alt_m,
            notify_dependents=False,
        )

    def browse_file(self, _):
        file, _ = QFileDialog.getOpenFileName(
            self,
            "Open KML",
            remembered_directory(
                FileDialogWorkflow.DEBRIS,
                FileDialogDirection.INPUT,
            ),
            "KML Files (*.kml)",
        )
        if file:
            remember_file_selection(
                FileDialogWorkflow.DEBRIS,
                FileDialogDirection.INPUT,
                file,
            )
            self.select_and_parse_kml(file)

    def drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def drop_event(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.select_and_parse_kml(urls[0].toLocalFile())

    def run_simulation(self):
        if self.has_active_simulation():
            return

        if self.flight_mode == "kml":
            if not self.kml_input_path:
                QMessageBox.warning(self, "Missing input", "Please load a KML file first.")
                return

            if not self._kml_state.ready:
                if self._kml_state.error:
                    QMessageBox.warning(
                        self,
                        "Invalid KML",
                        "The selected KML could not be loaded. Choose another file or reload it after correcting the error.",
                    )
                    return
                if not self.select_and_parse_kml(
                    self.kml_input_path,
                    altitude_fallback=self.alt_m.text(),
                ):
                    return

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
            input_coords = self._kml_state.coordinates
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
            suggested_save_path(
                FileDialogWorkflow.DEBRIS,
                "debris_trajectory.kml",
            ),
            "KML Files (*.kml)"
        )
        if not save_path:
            return
        save_path = ensure_extension(save_path, ".kml")
        remember_file_selection(
            FileDialogWorkflow.DEBRIS,
            FileDialogDirection.OUTPUT,
            save_path,
        )

        self.run_debris_calculator(
            input_coords_hook=input_coords,
            input_bearing_hook=input_bearing,
            output_kml=save_path,
            config=config,
            altitude_m_hook=altitude_m,
            terrain_m_hook=terrain_m,
        )

    def run_debris_calculator(self, input_coords_hook, input_bearing_hook, output_kml, config, altitude_m_hook, terrain_m_hook):
        """Capture an immutable request and start its one-shot worker."""
        try:
            request = DebrisSimulationRequest(
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
        except (KeyError, TypeError, ValueError) as error:
            QMessageBox.warning(
                self,
                "Invalid input",
                f"The simulation request is incomplete: {error}",
            )
            return

        self._start_simulation(request)

    def has_active_simulation(self):
        return self._simulation_state is not SimulationUiState.IDLE

    def _start_simulation(self, request):
        if self.has_active_simulation():
            return False

        token = CancellationToken()
        thread = QThread(self)
        worker = self.worker_class(request, token)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(
            self._on_simulation_progress,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.succeeded.connect(
            self._on_simulation_succeeded,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.cancelled.connect(
            self._on_simulation_cancelled,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.failed.connect(
            self._on_simulation_failed,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_simulation_thread_finished)
        thread.finished.connect(thread.deleteLater)

        self._simulation_thread = thread
        self._simulation_worker = worker
        self._cancellation_token = token
        self._terminal_outcome = None
        self._suppress_terminal_dialogs = False
        self._set_simulation_state(SimulationUiState.RUNNING)
        thread.start()
        return True

    def cancel_simulation(self, *, silent=False):
        if not self.has_active_simulation():
            return False
        if silent:
            self._suppress_terminal_dialogs = True
        if self._simulation_state is SimulationUiState.RUNNING:
            self._set_simulation_state(SimulationUiState.CANCELLING)
            self._cancellation_token.cancel()
        return True

    def _set_simulation_state(self, state):
        previous_busy = self.has_active_simulation()
        self._simulation_state = state
        busy = self.has_active_simulation()

        inputs_enabled = not busy
        self.presets_widget.setEnabled(inputs_enabled)
        self.config_widget.setEnabled(inputs_enabled)
        for button in self.mode_group.buttons():
            button.setEnabled(inputs_enabled)
        for widget in (
            self.kml_container,
            self.coords_container,
            self.bearing_container,
            self.alt_m,
            self.alt_ft,
            self.terrain_m,
            self.terrain_ft,
            self.height_m,
            self.height_ft,
        ):
            widget.setEnabled(inputs_enabled)
        self.file_label.setAcceptDrops(inputs_enabled)
        self.run_btn.setEnabled(inputs_enabled)

        self.cancel_simulation_btn.setVisible(busy)
        self.cancel_simulation_btn.setEnabled(
            state is SimulationUiState.RUNNING
        )
        self.simulation_progress_bar.setVisible(busy)
        if state is SimulationUiState.RUNNING:
            self.simulation_progress_bar.setValue(0)
            self.simulation_status_label.setText("Starting simulation…")
        elif state is SimulationUiState.CANCELLING:
            self.simulation_status_label.setText("Cancelling simulation…")

        if not busy:
            self.render_kml_state()
            self.update_preset_actions()
        if previous_busy != busy:
            self.simulation_busy_changed.emit(busy)

    def _on_simulation_progress(self, progress):
        if not isinstance(progress, SimulationProgress):
            return
        percentage = int(progress.completed * 100 / max(progress.total, 1))
        self.simulation_progress_bar.setValue(percentage)
        if self._simulation_state is SimulationUiState.RUNNING:
            self.simulation_status_label.setText(progress.message)

    def _record_terminal_outcome(self, kind, payload=None):
        if self._terminal_outcome is None:
            self._terminal_outcome = (kind, payload)

    def _on_simulation_succeeded(self, result):
        self._record_terminal_outcome("success", result)

    def _on_simulation_cancelled(self):
        self._record_terminal_outcome("cancelled")

    def _on_simulation_failed(self, failure):
        self._record_terminal_outcome("failure", failure)

    def _on_simulation_thread_finished(self):
        outcome = self._terminal_outcome
        if outcome is None:
            outcome = (
                "failure",
                SimulationFailure(
                    exception_type="WorkerTerminalError",
                    message="The simulation thread ended without a terminal result.",
                    traceback="",
                ),
            )

        self._simulation_thread = None
        self._simulation_worker = None
        self._cancellation_token = None
        self._terminal_outcome = None
        self._set_simulation_state(SimulationUiState.IDLE)

        kind, payload = outcome
        if kind == "success" and isinstance(payload, DebrisSimulationResult):
            self.summary_heading.setText(f"Track used (deg): {payload.heading:.1f}")
            self.summary_air.setText(
                f"Air distance to first impact (m): {payload.air_distance_m:.1f}"
            )
            self.summary_ground.setText(
                f"Ground distance to rest (m): {payload.ground_distance_m:.1f}"
            )
            self.summary_total.setText(
                f"Total ground‑planar distance (m): {payload.total_distance_m:.1f}"
            )
            self.summary_impacts.setText(
                f"Impacts (incl. first): {payload.impacts}"
            )
            self.simulation_progress_bar.setValue(100)
            self.simulation_status_label.setText("Simulation complete.")
            if not self._suppress_terminal_dialogs:
                QMessageBox.information(
                    self,
                    "Simulation Complete",
                    "Debris trajectory simulation completed successfully.",
                )
        elif kind == "cancelled":
            self.simulation_status_label.setText(
                "Simulation cancelled; the output file was not changed."
            )
            if not self._suppress_terminal_dialogs:
                QMessageBox.information(
                    self,
                    "Simulation Cancelled",
                    "The simulation was cancelled and the output file was not changed.",
                )
        else:
            failure = payload
            if not isinstance(failure, SimulationFailure):
                failure = SimulationFailure(
                    exception_type="WorkerTerminalError",
                    message="The simulation returned an invalid failure result.",
                    traceback="",
                )
            self.simulation_status_label.setText("Simulation failed.")
            if not self._suppress_terminal_dialogs:
                dialog = QMessageBox(self)
                dialog.setIcon(QMessageBox.Icon.Critical)
                dialog.setWindowTitle("Simulation Error")
                dialog.setText(failure.message)
                if failure.traceback:
                    dialog.setDetailedText(failure.traceback)
                dialog.exec()

        self._suppress_terminal_dialogs = False
