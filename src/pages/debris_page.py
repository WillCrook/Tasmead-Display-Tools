import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QRadioButton, QSplitter, QStyle, QVBoxLayout,
    QWidget,
)

from file_dialog_state import (
    FileDialogDirection, FileDialogWorkflow, ensure_extension,
    remember_file_selection, remembered_directory, suggested_save_path,
)
from resource_paths import app_data_path, resource_path
from services import (
    CoordinateInputError, DebrisSimulationRequest, DebrisSimulationResult,
    PresetType, SimulationProgress, parse_kml_track,
)
from workers import CancellationToken, DebrisSimulationWorker, SimulationFailure
from pages.coordinate_input import CoordinatePairInput
from pages.debris_ui import DebrisPresetManagerDialog, DebrisPreviewDialog
from pages.preset_ui import PresetUiMixin
from pages.unit_fields import MetreFeetFieldPair


PAGE_STYLE = """
DebrisPage {
    background: palette(window);
}
DebrisPage QFrame#workspacePanel,
DebrisPage QFrame#presetToolbar,
DebrisPage QFrame#resultsCard,
DebrisPage QFrame#previewHost,
DebrisPage QDialog#debrisPresetManager,
DebrisPage QDialog#debrisPreviewDialog {
    background: palette(base);
    border: 1px solid palette(mid);
    border-radius: 10px;
}
DebrisPage QLabel#pageTitle,
DebrisPage QLabel#dialogTitle {
    font-size: 22px;
    font-weight: 700;
}
DebrisPage QLabel#cardTitle,
DebrisPage QLabel#panelTitle {
    font-size: 15px;
    font-weight: 650;
}
DebrisPage QLabel#sectionTitle {
    font-weight: 650;
    padding-top: 4px;
}
DebrisPage QLabel#mutedText {
    color: palette(window-text);
}
DebrisPage QFrame#statusPanel,
DebrisPage QFrame#inputPanel {
    background: palette(alternate-base);
    border: 1px solid palette(midlight);
    border-radius: 7px;
}
DebrisPage QFrame#dropZone {
    background: palette(alternate-base);
    border: 1px dashed palette(mid);
    border-radius: 8px;
}
DebrisPage QFrame#dropZone[status="ready"] {
    border: 2px solid #2e7d32;
}
DebrisPage QFrame#dropZone[status="error"] {
    border: 2px solid #b3261e;
}
DebrisPage QLabel[status="success"] {
    color: #2e7d32;
    font-weight: 650;
}
DebrisPage QLabel[status="warning"] {
    color: #a65f00;
    font-weight: 650;
}
DebrisPage QLabel[status="error"] {
    color: #b3261e;
    font-weight: 650;
}
DebrisPage QLineEdit,
DebrisPage QComboBox,
DebrisPage QListWidget {
    min-height: 28px;
    border: 1px solid palette(mid);
    border-radius: 6px;
    padding: 3px 7px;
    background: palette(base);
    color: palette(text);
    selection-background-color: palette(highlight);
    selection-color: palette(highlighted-text);
}
DebrisPage QLineEdit:focus,
DebrisPage QComboBox:focus,
DebrisPage QListWidget:focus {
    border: 2px solid palette(highlight);
}
DebrisPage QPushButton {
    min-height: 28px;
    border: 1px solid palette(mid);
    border-radius: 6px;
    padding: 4px 10px;
    background: palette(button);
    color: palette(button-text);
}
DebrisPage QPushButton:hover {
    background: palette(midlight);
}
DebrisPage QPushButton#primaryButton {
    min-height: 38px;
    background: palette(highlight);
    color: palette(highlighted-text);
    border-color: palette(highlight);
    font-weight: 700;
}
DebrisPage QPushButton#dangerButton {
    color: #b3261e;
}
DebrisPage QPushButton#dangerButton:disabled {
    color: palette(mid);
}
DebrisPage QRadioButton {
    min-height: 28px;
    padding: 3px 6px;
}
DebrisPage QProgressBar {
    min-height: 16px;
    border: 1px solid palette(mid);
    border-radius: 6px;
    text-align: center;
    background: palette(base);
}
DebrisPage QProgressBar::chunk {
    background: palette(highlight);
    border-radius: 5px;
}
DebrisPage QSplitter::handle {
    background: palette(midlight);
    width: 3px;
    margin: 8px 4px;
}
DebrisPage QLabel#previewIcon {
    font-size: 54px;
    color: palette(mid);
}
DebrisPage QLabel#previewPath {
    background: palette(alternate-base);
    border: 1px solid palette(midlight);
    border-radius: 6px;
    padding: 10px;
}
"""


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

    @staticmethod
    def _set_widget_status(widget, status):
        widget.setProperty("status", status)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def update_preset_actions(self, *args):
        if not hasattr(self, "preset_combo"):
            return
        busy = self.has_active_simulation()
        selected = self.preset_combo.currentData(Qt.ItemDataRole.UserRole) is not None
        self.apply_preset_btn.setEnabled(selected and not busy)
        self.save_preset_btn.setEnabled(not busy)
        self.manage_presets_btn.setEnabled(not busy)

    def refresh_preset_list(self, *, select_id=None):
        """Populate the compact preset selector used by the debris workspace."""
        current = select_id or self.preset_combo.currentData(Qt.ItemDataRole.UserRole)
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Choose a debris preset", None)
        for record in sorted(
            self.presets.values(), key=lambda item: item.preset.name.casefold()
        ):
            self.preset_combo.addItem(record.preset.name, str(record.preset.id))
        if current:
            index = self.preset_combo.findData(str(current), Qt.ItemDataRole.UserRole)
            if index >= 0:
                self.preset_combo.setCurrentIndex(index)
        self.preset_combo.blockSignals(False)
        self.update_preset_actions()

    def selected_preset_record(self):
        raw_id = self.preset_combo.currentData(Qt.ItemDataRole.UserRole)
        if not raw_id:
            return None, None
        try:
            preset_id = UUID(str(raw_id))
        except ValueError:
            return None, None
        return preset_id, self.presets.get(preset_id)

    def apply_selected_preset(self):
        _, record = self.selected_preset_record()
        if record is not None:
            self.apply_preset_data(record.preset.data)

    def open_preset_manager(self):
        current_id, _ = self.selected_preset_record()
        dialog = DebrisPresetManagerDialog(
            self.preset_repository,
            self.preset_transfer,
            self,
        )
        if current_id is not None:
            dialog.refresh_preset_list(select_id=current_id)
        dialog.exec()
        selected_id = dialog.selected_preset_id or current_id
        self.presets = self.preset_repository.load_all()
        self.refresh_preset_list(select_id=selected_id)

    def open_preview(self):
        if not self._last_successful_output:
            return
        self._preview_dialog = DebrisPreviewDialog(
            self._last_successful_output,
            self,
        )
        self._preview_dialog.showFullScreen()

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
        self.file_label.setToolTip(state.path)

        if not state.path:
            self.kml_status_label.setText("No KML selected.")
            self._set_widget_status(self.kml_status_label, "idle")
            self._set_widget_status(self.kml_drop_zone, "idle")
            return

        if state.error:
            self.kml_status_label.setText(f"KML error: {state.error}")
            self._set_widget_status(self.kml_status_label, "error")
            self._set_widget_status(self.kml_drop_zone, "error")
            return

        if not state.ready:
            self.kml_status_label.setText("Loading KML…")
            self._set_widget_status(self.kml_status_label, "warning")
            self._set_widget_status(self.kml_drop_zone, "idle")
            return

        penultimate_lat, penultimate_lon, final_lat, final_lon = state.coordinates
        self.kml_meta_pen_lat.setText(f"Penultimate latitude: {penultimate_lat}")
        self.kml_meta_pen_lon.setText(f"Penultimate longitude: {penultimate_lon}")
        self.kml_meta_fin_lat.setText(f"Final latitude: {final_lat}")
        self.kml_meta_fin_lon.setText(f"Final longitude: {final_lon}")
        self._set_widget_status(self.kml_drop_zone, "ready")
        if state.final_altitude_m is not None:
            self.kml_status_label.setText("KML ready.")
            self._set_widget_status(self.kml_status_label, "success")
        elif self.alt_m.text():
            self.kml_status_label.setText("KML ready — using entered altitude.")
            self._set_widget_status(self.kml_status_label, "success")
        else:
            self.kml_status_label.setText("KML ready — enter altitude in metres.")
            self._set_widget_status(self.kml_status_label, "warning")

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
        self.setObjectName("DebrisPage")
        self.setStyleSheet(PAGE_STYLE)

        self._simulation_state = SimulationUiState.IDLE
        self._simulation_thread = None
        self._simulation_worker = None
        self._cancellation_token = None
        self._terminal_outcome = None
        self._suppress_terminal_dialogs = False
        self._last_successful_output = None
        self._preview_dialog = None

        self.initialize_preset_management(
            preset_type=PresetType.DEBRIS,
            managed_directory=app_data_path("presets/debris"),
            legacy_managed_directory=app_data_path("debris-presets"),
            legacy_readonly_directory=resource_path("data/presets"),
            backup_directory=app_data_path("presets/legacy-backup/debris"),
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 14)
        root.setSpacing(10)

        title = QLabel("Debris trajectory")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "Configure the debris model, define the flight state, then generate a "
            "Google Earth-ready trajectory KML."
        )
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        self.presets_widget = QFrame()
        self.presets_widget.setObjectName("presetToolbar")
        self._build_preset_toolbar(QHBoxLayout(self.presets_widget))
        root.addWidget(self.presets_widget)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        root.addWidget(self.splitter, 1)

        self.config_widget = QFrame()
        self.config_widget.setObjectName("workspacePanel")
        config = QVBoxLayout(self.config_widget)
        config.setContentsMargins(18, 16, 18, 16)
        config.setSpacing(10)
        self.build_config(config)

        self.file_widget = QWidget()
        right = QVBoxLayout(self.file_widget)
        right.setContentsMargins(7, 0, 0, 0)
        right.setSpacing(10)

        self.flight_input_card = QFrame()
        self.flight_input_card.setObjectName("workspacePanel")
        file_panel = QVBoxLayout(self.flight_input_card)
        file_panel.setContentsMargins(18, 16, 18, 16)
        file_panel.setSpacing(10)
        self.build_file_panel(file_panel)
        right.addWidget(self.flight_input_card)

        self.results_widget = QFrame()
        self.results_widget.setObjectName("resultsCard")
        results_layout = QVBoxLayout(self.results_widget)
        results_layout.setContentsMargins(18, 16, 18, 16)
        results_layout.setSpacing(9)
        self.build_results_panel(results_layout)
        right.addWidget(self.results_widget)
        right.addStretch()

        self.splitter.addWidget(self.config_widget)
        self.splitter.addWidget(self.file_widget)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setSizes((410, 490))

        self.load_presets_from_disk()

    def _build_preset_toolbar(self, layout):
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)
        title = QLabel("Preset")
        title.setObjectName("cardTitle")
        layout.addWidget(title)

        self.preset_combo = QComboBox()
        self.preset_combo.setAccessibleName("Debris preset")
        self.preset_combo.setMinimumContentsLength(18)
        self.preset_combo.currentIndexChanged.connect(self.update_preset_actions)
        layout.addWidget(self.preset_combo, 1)

        self.apply_preset_btn = QPushButton("Apply")
        self.apply_preset_btn.clicked.connect(self.apply_selected_preset)
        layout.addWidget(self.apply_preset_btn)

        self.save_preset_btn = QPushButton("Save current…")
        self.save_preset_btn.clicked.connect(self.save_preset)
        layout.addWidget(self.save_preset_btn)

        self.manage_presets_btn = QPushButton("Manage presets…")
        self.manage_presets_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView)
        )
        self.manage_presets_btn.clicked.connect(self.open_preset_manager)
        layout.addWidget(self.manage_presets_btn)

    def save_preset(self):
        try:
            preset = self.capture_preset_data()
        except CoordinateInputError as error:
            QMessageBox.warning(self, "Invalid coordinate", str(error))
            return

        name, ok = QInputDialog.getText(
            self,
            "Save Preset",
            "Enter preset name:"
        )
        if not ok or not name:
            return

        self.save_preset_data(name, preset, error_title="Preset Error")

    def capture_preset_data(self) -> dict[str, object]:
        lat1, lon1 = self.coordinate1_input.preset_components()
        lat2, lon2 = self.coordinate2_input.preset_components()
        bearing_lat, bearing_lon = self.bearing_coordinate_input.preset_components()
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
                    "lat1": lat1,
                    "lon1": lon1,
                    "lat2": lat2,
                    "lon2": lon2,
                },
                "bearing": {
                    "lat": bearing_lat,
                    "lon": bearing_lon,
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
            self.coordinate1_input.set_components(
                coords.get("lat1", ""),
                coords.get("lon1", ""),
            )
            self.coordinate2_input.set_components(
                coords.get("lat2", ""),
                coords.get("lon2", ""),
            )

        elif self.flight_mode == "bearing":
            bearing = flight_inputs.get("bearing", {})
            self.bearing_coordinate_input.set_components(
                bearing.get("lat", ""),
                bearing.get("lon", ""),
            )
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

        title = QLabel("Debris model")
        title.setObjectName("cardTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Set the physical object, atmosphere, integration, and ground-contact parameters."
        )
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.inputs = {}

        def add_fields(section_title, keys):
            heading = QLabel(section_title)
            heading.setObjectName("sectionTitle")
            layout.addWidget(heading)
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(7)
            grid.setColumnStretch(1, 1)
            for row, key in enumerate(keys):
                label = QLabel(key)
                edit = QLineEdit(defaults[key])
                edit.setPlaceholderText(key)
                edit.setAccessibleName(key)
                grid.addWidget(label, row, 0)
                grid.addWidget(edit, row, 1)
                self.inputs[key] = edit
            layout.addLayout(grid)

        add_fields(
            "Object and release",
            ("Mass (kg)", "Frontal area A (m²)", "KTAS (knots true airspeed)"),
        )
        add_fields(
            "Atmosphere and integration",
            (
                "Drag Coefficient Cd",
                "Air Density ρ (kg/m³)",
                "Gravity g (m/s²)",
                "Time step (s)",
            ),
        )

        ground_heading = QLabel("Ground interaction")
        ground_heading.setObjectName("sectionTitle")
        layout.addWidget(ground_heading)
        ground_grid = QGridLayout()
        ground_grid.setHorizontalSpacing(12)
        ground_grid.setVerticalSpacing(7)
        ground_grid.setColumnStretch(1, 1)
        impact_key = "Impact / slide physics"
        impact_input = QLineEdit(defaults[impact_key])
        impact_input.setPlaceholderText(impact_key)
        impact_input.setAccessibleName(impact_key)
        self.inputs[impact_key] = impact_input
        ground_grid.addWidget(QLabel(impact_key), 0, 0)
        ground_grid.addWidget(impact_input, 0, 1)

        self.surface_combo = QComboBox()
        self.surface_combo.addItems(["concrete", "asphalt", "grass"])
        self.surface_combo.setAccessibleName("Surface type")
        ground_grid.addWidget(QLabel("Surface type"), 1, 0)
        ground_grid.addWidget(self.surface_combo, 1, 1)
        layout.addLayout(ground_grid)

        self.include_ground_drag = QCheckBox("Include ground drag")
        self.include_ground_drag.setChecked(True)
        layout.addWidget(self.include_ground_drag)

        layout.addStretch()

    def build_file_panel(self, layout):
        title = QLabel("Flight input")
        title.setObjectName("cardTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Choose how the release position and direction should be resolved."
        )
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.mode_group = QButtonGroup(self)
        self.rb_kml = QRadioButton("KML file")
        self.rb_coords = QRadioButton("Two coordinates")
        self.rb_bearing = QRadioButton("Coordinate + track")

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
        self.mode_stack_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.mode_stack)

        # KML drop area (only for KML mode)
        self.kml_container = QFrame()
        self.kml_container.setObjectName("inputPanel")
        kml_layout = QVBoxLayout(self.kml_container)
        kml_layout.setContentsMargins(12, 12, 12, 12)
        kml_layout.setSpacing(8)

        # Drag & drop area
        self.kml_drop_zone = QFrame()
        self.kml_drop_zone.setObjectName("dropZone")
        drop_layout = QVBoxLayout(self.kml_drop_zone)
        drop_layout.setContentsMargins(12, 12, 12, 12)
        self.file_label = QLabel("Drop KML file here")
        self.file_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.file_label.setMinimumHeight(72)
        self.file_label.setWordWrap(True)
        self.file_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.file_label.setAcceptDrops(True)

        self.file_label.mousePressEvent = self.browse_file
        self.file_label.dragEnterEvent = self.drag_enter
        self.file_label.dropEvent = self.drop_event

        drop_layout.addWidget(self.file_label)
        kml_layout.addWidget(self.kml_drop_zone)

        self.load_kml_btn = QPushButton("Reload selected KML")
        self.load_kml_btn.clicked.connect(self.reload_selected_kml)
        kml_layout.addWidget(self.load_kml_btn)

        self.kml_status_label = QLabel("No KML selected.")
        self.kml_status_label.setWordWrap(True)
        kml_layout.addWidget(self.kml_status_label)

        # Metadata display labels
        metadata = QFrame()
        metadata.setObjectName("statusPanel")
        metadata_layout = QGridLayout(metadata)
        metadata_layout.setContentsMargins(10, 8, 10, 8)
        metadata_layout.setHorizontalSpacing(12)
        metadata_layout.setVerticalSpacing(4)
        self.kml_meta_pen_lat = QLabel("Penultimate latitude: —")
        self.kml_meta_pen_lon = QLabel("Penultimate longitude: —")
        self.kml_meta_fin_lat = QLabel("Final latitude: —")
        self.kml_meta_fin_lon = QLabel("Final longitude: —")

        metadata_layout.addWidget(self.kml_meta_pen_lat, 0, 0)
        metadata_layout.addWidget(self.kml_meta_pen_lon, 0, 1)
        metadata_layout.addWidget(self.kml_meta_fin_lat, 1, 0)
        metadata_layout.addWidget(self.kml_meta_fin_lon, 1, 1)
        kml_layout.addWidget(metadata)

        # Coordinates mode inputs
        self.coords_container = QFrame()
        self.coords_container.setObjectName("inputPanel")
        coords_layout = QVBoxLayout(self.coords_container)
        coords_layout.setContentsMargins(12, 12, 12, 12)
        coordinate1_label = QLabel("Coordinate 1 (Latitude, Longitude)")
        self.coordinate1_input = CoordinatePairInput("Coordinate 1")
        coordinate2_label = QLabel("Coordinate 2 (Latitude, Longitude)")
        self.coordinate2_input = CoordinatePairInput("Coordinate 2")

        coords_layout.addWidget(coordinate1_label)
        coords_layout.addWidget(self.coordinate1_input)
        coords_layout.addWidget(coordinate2_label)
        coords_layout.addWidget(self.coordinate2_input)

        # Bearing mode inputs
        self.bearing_container = QFrame()
        self.bearing_container.setObjectName("inputPanel")
        bearing_layout = QVBoxLayout(self.bearing_container)
        bearing_layout.setContentsMargins(12, 12, 12, 12)
        coordinate_label = QLabel("Coordinate (Latitude, Longitude)")
        self.bearing_coordinate_input = CoordinatePairInput("Track coordinate")
        azimuth_label = QLabel("Track (degrees)")
        self.azimuth_input = QLineEdit()

        bearing_layout.addWidget(coordinate_label)
        bearing_layout.addWidget(self.bearing_coordinate_input)
        bearing_layout.addWidget(azimuth_label)
        bearing_layout.addWidget(self.azimuth_input)

        # Store all flight inputs
        self.flight_inputs = {
            "kml": {},
            "coords": {
                "coordinate1": self.coordinate1_input,
                "coordinate2": self.coordinate2_input,
            },
            "bearing": {
                "coordinate": self.bearing_coordinate_input,
                "azimuth": self.azimuth_input,
            }
        }

        layout.addWidget(self.kml_container)

        height_heading = QLabel("Altitude and terrain")
        height_heading.setObjectName("sectionTitle")
        layout.addWidget(height_heading)
        height_grid = QGridLayout()
        height_grid.setHorizontalSpacing(10)
        height_grid.setVerticalSpacing(7)
        height_grid.setColumnStretch(1, 1)
        height_grid.setColumnStretch(2, 1)
        metres_heading = QLabel("metres")
        metres_heading.setObjectName("mutedText")
        feet_heading = QLabel("feet")
        feet_heading.setObjectName("mutedText")
        height_grid.addWidget(metres_heading, 0, 1)
        height_grid.addWidget(feet_heading, 0, 2)

        self.alt_m = QLineEdit()
        self.alt_ft = QLineEdit()
        self.terrain_m = QLineEdit()
        self.terrain_ft = QLineEdit()
        self.height_m = QLineEdit()
        self.height_ft = QLineEdit()
        unit_rows = (
            ("Altitude", self.alt_m, self.alt_ft),
            ("Terrain", self.terrain_m, self.terrain_ft),
            ("Height above ground", self.height_m, self.height_ft),
        )
        for row, (name, metres, feet) in enumerate(unit_rows, start=1):
            metres.setAccessibleName(f"{name} metres")
            feet.setAccessibleName(f"{name} feet")
            height_grid.addWidget(QLabel(name), row, 0)
            height_grid.addWidget(metres, row, 1)
            height_grid.addWidget(feet, row, 2)
        layout.addLayout(height_grid)

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

        layout.addStretch()
        self.render_kml_state()
        self.update_flight_mode_ui()

    def build_results_panel(self, layout):
        header = QHBoxLayout()
        title = QLabel("Run and results")
        title.setObjectName("cardTitle")
        header.addWidget(title)
        header.addStretch()
        self.preview_btn = QPushButton("Open 3D preview")
        self.preview_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        )
        self.preview_btn.setEnabled(False)
        self.preview_btn.setToolTip(
            "Available after a debris trajectory is generated successfully."
        )
        self.preview_btn.clicked.connect(self.open_preview)
        header.addWidget(self.preview_btn)
        layout.addLayout(header)

        self.run_btn = QPushButton("Run Simulation")
        self.run_btn.setObjectName("primaryButton")
        self.run_btn.clicked.connect(self.run_simulation)
        layout.addWidget(self.run_btn)

        self.cancel_simulation_btn = QPushButton("Cancel Simulation")
        self.cancel_simulation_btn.setObjectName("dangerButton")
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

        status_panel = QFrame()
        status_panel.setObjectName("statusPanel")
        status_layout = QVBoxLayout(status_panel)
        status_layout.setContentsMargins(10, 8, 10, 8)
        self.simulation_status_label = QLabel("Ready.")
        self.simulation_status_label.setWordWrap(True)
        status_layout.addWidget(self.simulation_status_label)
        layout.addWidget(status_panel)

        # --- Simulation summary UI elements ---
        summary_title = QLabel("Simulation summary")
        summary_title.setObjectName("sectionTitle")
        layout.addWidget(summary_title)

        self.summary_heading = QLabel("Track used (deg): —")
        self.summary_air = QLabel("Air distance to first impact (m): —")
        self.summary_ground = QLabel("Ground distance to rest (m): —")
        self.summary_total = QLabel("Total ground‑planar distance (m): —")
        self.summary_impacts = QLabel("Impacts (incl. first): —")

        summary_grid = QGridLayout()
        summary_grid.setHorizontalSpacing(14)
        summary_grid.setVerticalSpacing(5)
        summary_grid.addWidget(self.summary_heading, 0, 0)
        summary_grid.addWidget(self.summary_impacts, 0, 1)
        summary_grid.addWidget(self.summary_air, 1, 0, 1, 2)
        summary_grid.addWidget(self.summary_ground, 2, 0, 1, 2)
        summary_grid.addWidget(self.summary_total, 3, 0, 1, 2)
        layout.addLayout(summary_grid)

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
                coordinate1 = self.coordinate1_input.coordinates()
                coordinate2 = self.coordinate2_input.coordinates()
            except CoordinateInputError as error:
                QMessageBox.warning(self, "Invalid coordinate", str(error))
                return

            input_coords = (
                coordinate1.latitude,
                coordinate1.longitude,
                coordinate2.latitude,
                coordinate2.longitude,
            )
            input_bearing = None

        elif self.flight_mode == "bearing":
            try:
                coordinate = self.bearing_coordinate_input.coordinates()
            except CoordinateInputError as error:
                QMessageBox.warning(self, "Invalid coordinate", str(error))
                return
            try:
                azimuth = float(self.azimuth_input.text())
            except ValueError:
                QMessageBox.warning(self, "Invalid input", "Please enter a valid Track.")
                return

            input_coords = None
            input_bearing = (
                coordinate.latitude,
                coordinate.longitude,
                azimuth,
            )
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
        self.preview_btn.setEnabled(
            inputs_enabled and bool(self._last_successful_output)
        )

        self.cancel_simulation_btn.setVisible(busy)
        self.cancel_simulation_btn.setEnabled(
            state is SimulationUiState.RUNNING
        )
        self.simulation_progress_bar.setVisible(busy)
        if state is SimulationUiState.RUNNING:
            self.simulation_progress_bar.setValue(0)
            self.simulation_status_label.setText("Starting simulation…")
            self._set_widget_status(self.simulation_status_label, "warning")
        elif state is SimulationUiState.CANCELLING:
            self.simulation_status_label.setText("Cancelling simulation…")
            self._set_widget_status(self.simulation_status_label, "warning")

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
            self._set_widget_status(self.simulation_status_label, "warning")

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
            self._set_widget_status(self.simulation_status_label, "success")
            self._last_successful_output = payload.output_file
            self.preview_btn.setEnabled(True)
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
            self._set_widget_status(self.simulation_status_label, "warning")
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
            self._set_widget_status(self.simulation_status_label, "error")
            if not self._suppress_terminal_dialogs:
                dialog = QMessageBox(self)
                dialog.setIcon(QMessageBox.Icon.Critical)
                dialog.setWindowTitle("Simulation Error")
                dialog.setText(failure.message)
                if failure.traceback:
                    dialog.setDetailedText(failure.traceback)
                dialog.exec()

        self._suppress_terminal_dialogs = False
