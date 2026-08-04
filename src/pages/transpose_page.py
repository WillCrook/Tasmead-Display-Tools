import os
from collections.abc import Mapping

from PyQt6.QtCore import QSettings, QStandardPaths, Qt
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from resource_paths import app_data_path, resource_path
from services import PresetType, create_transposition_plan, run_transposition
from pages.preset_ui import PresetPanelLabels, PresetUiMixin
from pages.unit_fields import MetreFeetFieldPair


class TransposePage(PresetUiMixin, QWidget):
    LAST_OUTPUT_DIRECTORY_KEY = "transpose/last-output-directory"

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
        self.initialize_preset_management(
            preset_type=PresetType.AIRFIELD,
            managed_directory=app_data_path("presets/airfield"),
            legacy_managed_directory=app_data_path("airfields"),
            legacy_readonly_directory=resource_path("data/airfields"),
            backup_directory=app_data_path("presets/legacy-backup/airfield"),
        )
        self.build_preset_panel(
            presets_layout,
            PresetPanelLabels(
                title="Airfield Presets",
                save="Save Preset",
                load="Load Preset",
                rename="Rename Preset",
                delete="Delete Preset",
                export="Export Preset",
            ),
        )
        self.load_presets_from_disk()

        # Config (Lat, Lon, Heading)
        self.build_config_panel(config_layout)

        # File Drop & Run
        self.input_files = []
        self.build_file_panel(file_layout)

    def build_config_panel(self, layout):
        # Original Airfield Height Section
        orig_title = QLabel("Original Airfield")
        orig_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(orig_title)

        self.orig_height_input = QLineEdit()
        self.orig_height_input.setPlaceholderText("Elevation (m)")

        m_layout = QVBoxLayout()
        m_layout.addWidget(QLabel("Original Elevation (m)"))
        m_layout.addWidget(self.orig_height_input)

        self.orig_height_ft_input = QLineEdit()
        self.orig_height_ft_input.setPlaceholderText("Elevation (ft)")

        ft_layout = QVBoxLayout()
        ft_layout.addWidget(QLabel("Original Elevation (ft)"))
        ft_layout.addWidget(self.orig_height_ft_input)
        
        # Container for both
        h_layout = QHBoxLayout()
        h_layout.addLayout(m_layout)
        h_layout.addLayout(ft_layout)

        layout.addLayout(h_layout)

        self._orig_height_units = MetreFeetFieldPair(
            self.orig_height_input,
            self.orig_height_ft_input,
        )
        
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
        self.heading_input.setPlaceholderText("Rotation (degrees)")
        layout.addWidget(QLabel("Rotation"))
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
            new_files = [
                url.toLocalFile()
                for url in urls
                if url.toLocalFile().lower().endswith(".kml")
            ]
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

    def save_preset(self):
        default_name = self.airfield_name_input.text()
        name, ok = QInputDialog.getText(self, "Save Preset", "Enter preset name:", text=default_name)
        if not ok or not name:
            return

        self.save_preset_data(
            name,
            self.capture_preset_data(),
            error_title="Error",
        )

    def capture_preset_data(self) -> dict[str, object]:
        return {
            "name": self.airfield_name_input.text(),
            "latitude": self.lat_input.text(),
            "longitude": self.lon_input.text(),
            "heading": self.heading_input.text(),
            "original_elevation_m": self.orig_height_input.text(),
        }

    def load_preset_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Preset",
            self.presets_dir,
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return

        record = self.import_preset_path(path, error_title="Error")
        if record is not None:
            self.apply_preset_data(record.preset.data)

    def apply_preset_data(self, data: Mapping[str, object]) -> None:
        """Apply tolerant airfield settings from a validated preset envelope."""
        self.airfield_name_input.setText(data.get("name", ""))
        self.lat_input.setText(data.get("latitude", ""))
        self.lon_input.setText(data.get("longitude", ""))
        self.heading_input.setText(data.get("heading", ""))
        self.orig_height_input.setText(data.get("original_elevation_m", ""))

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

        output_dir = QFileDialog.getExistingDirectory(
            self,
            "Select Output Folder",
            self._initial_output_directory(),
        )
        if not output_dir:
            return

        try:
            plan = create_transposition_plan(
                input_files=self.input_files,
                output_directory=output_dir,
                target_airfield=self.airfield_name_input.text(),
            )
        except Exception as error:
            QMessageBox.critical(self, "Error", f"Could not plan outputs: {error}")
            return

        if not self._confirm_output_plan(plan):
            return

        settings = QSettings()
        settings.setValue(self.LAST_OUTPUT_DIRECTORY_KEY, output_dir)
        settings.sync()

        try:
            result = run_transposition(
                plan=plan,
                target_lat=lat,
                target_lon=lon,
                target_heading=heading,
                ground_reference_elevation=orig_height,
            )
        except Exception as error:
            QMessageBox.critical(self, "Error", f"Transposition failed: {error}")
            return

        if result.succeeded:
            successful_paths = "\n".join(
                str(output.output_path) for output in result.successful
            )
            QMessageBox.information(
                self,
                "Success",
                f"Transposition complete!\n"
                f"Saved {len(result.successful)} KML file(s) to:\n{output_dir}\n\n"
                f"Outputs:\n{successful_paths}",
            )
            return

        failed_paths = "\n".join(
            f"{outcome.input_path}: {outcome.error.message}"
            for outcome in result.failed_outcomes
        )
        successful_paths = "\n".join(
            str(output.output_path) for output in result.successful
        ) or "None"
        if result.failed:
            QMessageBox.critical(
                self,
                "Transposition failed",
                f"No KML files were produced.\n\n"
                f"Failed inputs:\n{failed_paths}",
            )
            return
        QMessageBox.warning(
            self,
            "Transposition partially complete",
            f"Saved {result.success_count} of {result.total_count} KML file(s).\n\n"
            f"Successful outputs:\n{successful_paths}\n\n"
            f"Failed inputs:\n{failed_paths}",
        )

    def _initial_output_directory(self):
        saved_directory = QSettings().value(
            self.LAST_OUTPUT_DIRECTORY_KEY,
            "",
            type=str,
        )
        if saved_directory and os.path.isdir(saved_directory):
            return saved_directory

        documents_directory = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        if documents_directory and os.path.isdir(documents_directory):
            return documents_directory
        return os.getcwd()

    def _confirm_output_plan(self, plan):
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle("Confirm Transposition Outputs")
        dialog.setText(f"Save {len(plan.jobs)} KML file(s) to this folder?")
        dialog.setInformativeText(str(plan.output_directory))
        dialog.setDetailedText(
            "\n".join(job.output_path.name for job in plan.jobs)
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.Save)
        return dialog.exec() == QMessageBox.StandardButton.Save
