import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from resource_paths import resource_path
from services import PresetStore, run_transposition

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
        self.preset_store = PresetStore(self.presets_dir)
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
        self.presets = self.preset_store.load_all()
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

        try:
            self.presets[name] = self.preset_store.save(name, data)
            self.refresh_preset_list()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save preset: {e}")

    def load_preset_from_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Preset", self.presets_dir, "JSON Files (*.json)")
        if not path:
            return
        
        try:
            data = self.preset_store.load_file(path)
            
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
                self.preset_store.delete(entry)
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
