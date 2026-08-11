"""Focused dialogs used by the modern debris trajectory workspace."""

from __future__ import annotations

from uuid import UUID

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from file_dialog_state import (
    FileDialogDirection,
    FileDialogWorkflow,
    remember_file_selection,
    remembered_directory,
)
from pages.preset_ui import PresetUiMixin
from services import PresetImportExportService, PresetRecord, PresetRepository

class DebrisPresetManagerDialog(QDialog, PresetUiMixin):
    """Search and manage debris presets without duplicating the page form."""

    def __init__(
        self,
        repository: PresetRepository,
        transfer: PresetImportExportService,
        parent: QWidget | None = None,
    ) -> None:
        QDialog.__init__(self, parent)
        self.setObjectName("debrisPresetManager")
        self.setWindowTitle("Manage Debris Presets")
        self.resize(720, 520)
        self.preset_repository = repository
        self.preset_store = repository
        self.preset_transfer = transfer
        self.preset_dialog_workflow = FileDialogWorkflow.DEBRIS_PRESET
        self.presets: dict[UUID, PresetRecord] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title = QLabel("Manage debris presets")
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        description = QLabel(
            "Import, rename, export, or delete saved debris configurations. "
            "Create new presets from the main workspace."
        )
        description.setObjectName("mutedText")
        description.setWordWrap(True)
        root.addWidget(description)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search debris presets…")
        self.search_input.setAccessibleName("Search debris presets")
        self.search_input.textChanged.connect(lambda _: self.refresh_preset_list())
        root.addWidget(self.search_input)

        self.preset_list = QListWidget()
        self.preset_list.setAccessibleName("Managed debris presets")
        self.preset_list.currentItemChanged.connect(self.update_preset_actions)
        root.addWidget(self.preset_list, 1)

        actions = QHBoxLayout()
        self.import_btn = QPushButton("Import…")
        self.rename_preset_btn = QPushButton("Rename…")
        self.export_preset_btn = QPushButton("Export…")
        self.delete_preset_btn = QPushButton("Delete")
        self.delete_preset_btn.setObjectName("dangerButton")
        self.import_btn.clicked.connect(self.load_preset_from_file)
        self.rename_preset_btn.clicked.connect(self.rename_preset)
        self.export_preset_btn.clicked.connect(self.export_preset)
        self.delete_preset_btn.clicked.connect(self.delete_preset)
        actions.addWidget(self.import_btn)
        actions.addWidget(self.rename_preset_btn)
        actions.addWidget(self.export_preset_btn)
        actions.addWidget(self.delete_preset_btn)
        actions.addStretch()
        root.addLayout(actions)

        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.reject)
        root.addWidget(close_buttons)

        self.load_presets_from_disk()

    @property
    def selected_preset_id(self) -> UUID | None:
        preset_id, _ = self.selected_preset_record()
        return preset_id

    def refresh_preset_list(self, *, select_id: UUID | None = None) -> None:
        selected = select_id or self.selected_preset_id
        query = self.search_input.text().strip().casefold()
        self.preset_list.clear()
        for record in sorted(
            self.presets.values(), key=lambda item: item.preset.name.casefold()
        ):
            if query and query not in record.preset.name.casefold():
                continue
            item = QListWidgetItem(record.preset.name)
            item.setData(Qt.ItemDataRole.UserRole, str(record.preset.id))
            self.preset_list.addItem(item)
            if record.preset.id == selected:
                self.preset_list.setCurrentItem(item)
        self.update_preset_actions()

    def update_preset_actions(self, *_: object) -> None:
        enabled = self.preset_list.currentItem() is not None
        self.rename_preset_btn.setEnabled(enabled)
        self.export_preset_btn.setEnabled(enabled)
        self.delete_preset_btn.setEnabled(enabled)

    def apply_preset_data(self, data) -> None:
        """The manager never edits or applies page configuration payloads."""

    def load_preset_from_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Debris Preset",
            remembered_directory(
                FileDialogWorkflow.DEBRIS_PRESET,
                FileDialogDirection.INPUT,
            ),
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        remember_file_selection(
            FileDialogWorkflow.DEBRIS_PRESET,
            FileDialogDirection.INPUT,
            path,
        )
        self.import_preset_path(path, error_title="Preset Error")

    def delete_preset(self) -> None:
        _, record = self.selected_preset_record()
        if record is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete debris preset?",
            f'Delete "{record.preset.name}" permanently?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        super().delete_preset()
