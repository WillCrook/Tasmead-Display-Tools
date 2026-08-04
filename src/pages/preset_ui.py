"""Shared PyQt orchestration for preset repositories and transfer services."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
)

from file_dialog_state import (
    FileDialogDirection, FileDialogWorkflow, ensure_extension,
    remember_file_selection, suggested_save_path,
)
from services import (
    PresetError,
    PresetImportExportService,
    PresetNameConflictError,
    PresetRecord,
    PresetRepository,
    PresetType,
)


@dataclass(frozen=True, slots=True)
class PresetPanelLabels:
    """Exact visible text for one page's shared preset panel."""

    title: str
    save: str
    load: str
    rename: str
    delete: str
    export: str


class PresetUiMixin:
    """Keep common preset UI decisions outside repository and file I/O code."""

    preset_repository: PresetRepository
    preset_transfer: PresetImportExportService
    presets: dict[UUID, PresetRecord]

    def initialize_preset_management(
        self,
        *,
        preset_type: PresetType,
        managed_directory: str,
        legacy_managed_directory: str,
        legacy_readonly_directory: str,
        backup_directory: str,
    ) -> None:
        self.presets_dir = managed_directory
        self.preset_dialog_workflow = (
            FileDialogWorkflow.AIRFIELD_PRESET
            if preset_type is PresetType.AIRFIELD
            else FileDialogWorkflow.DEBRIS_PRESET
        )
        self.preset_repository = PresetRepository(
            managed_directory,
            preset_type,
            legacy_managed_directories=(legacy_managed_directory,),
            legacy_readonly_directories=(legacy_readonly_directory,),
            backup_directory=backup_directory,
        )
        # Compatibility for older page integrations and extensions.
        self.preset_store = self.preset_repository
        self.preset_transfer = PresetImportExportService(self.preset_repository)
        self.presets = {}

    def build_preset_panel(self, layout, labels: PresetPanelLabels) -> None:
        title = QLabel(labels.title)
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(title)

        self.preset_list = QListWidget()
        self.preset_list.itemClicked.connect(self.load_selected_preset)
        self.preset_list.currentItemChanged.connect(self.update_preset_actions)
        layout.addWidget(self.preset_list)

        save_button = QPushButton(labels.save)
        load_button = QPushButton(labels.load)
        self.rename_preset_btn = QPushButton(labels.rename)
        self.delete_preset_btn = QPushButton(labels.delete)
        self.export_preset_btn = QPushButton(labels.export)
        self.rename_preset_btn.setEnabled(False)
        self.delete_preset_btn.setEnabled(False)
        self.export_preset_btn.setEnabled(False)

        save_button.clicked.connect(self.save_preset)
        load_button.clicked.connect(self.load_preset_from_file)
        self.rename_preset_btn.clicked.connect(self.rename_preset)
        self.delete_preset_btn.clicked.connect(self.delete_preset)
        self.export_preset_btn.clicked.connect(self.export_preset)

        layout.addWidget(save_button)
        layout.addWidget(load_button)
        layout.addWidget(self.rename_preset_btn)
        layout.addWidget(self.delete_preset_btn)
        layout.addWidget(self.export_preset_btn)
        layout.addStretch()

    def load_presets_from_disk(self) -> None:
        self.presets = self.preset_repository.load_all()
        if self.preset_repository.issues:
            unique_issues = list(dict.fromkeys(self.preset_repository.issues))
            QMessageBox.warning(
                self,
                "Preset files need attention",
                "Some preset files could not be loaded or migrated:\n\n"
                + "\n".join(f"• {issue}" for issue in unique_issues[:8]),
            )
            self.preset_repository.issues.clear()
        self.refresh_preset_list()

    def selected_preset_record(self) -> tuple[UUID | None, PresetRecord | None]:
        item = self.preset_list.currentItem()
        if item is None:
            return None, None
        try:
            preset_id = UUID(str(item.data(Qt.ItemDataRole.UserRole)))
        except (TypeError, ValueError):
            return None, None
        return preset_id, self.presets.get(preset_id)

    def preset_record_for_item(self, item) -> PresetRecord | None:
        if item is None:
            return None
        try:
            preset_id = UUID(str(item.data(Qt.ItemDataRole.UserRole)))
        except (TypeError, ValueError):
            return None
        return self.presets.get(preset_id)

    def load_selected_preset(self, item) -> None:
        record = self.preset_record_for_item(item)
        if record is not None:
            self.apply_preset_data(record.preset.data)

    def apply_preset_data(self, data: Mapping[str, object]) -> None:
        """Apply one validated, page-specific preset payload."""
        raise NotImplementedError

    def refresh_preset_list(self, *, select_id: UUID | None = None) -> None:
        self.preset_list.clear()
        for record in sorted(
            self.presets.values(), key=lambda item: item.preset.name.casefold()
        ):
            item = QListWidgetItem(record.preset.name)
            item.setData(Qt.ItemDataRole.UserRole, str(record.preset.id))
            self.preset_list.addItem(item)
            if record.preset.id == select_id:
                self.preset_list.setCurrentItem(item)
        self.update_preset_actions()

    def update_preset_actions(self, *_: object) -> None:
        enabled = self.preset_list.currentItem() is not None
        self.rename_preset_btn.setEnabled(enabled)
        self.delete_preset_btn.setEnabled(enabled)
        self.export_preset_btn.setEnabled(enabled)

    def save_preset_data(
        self, name: str, data: dict[str, object], *, error_title: str
    ) -> PresetRecord | None:
        """Create a preset or explicitly update an existing same-name preset."""
        try:
            existing = self.preset_repository.find_by_name(name)
            if existing is not None:
                replace = QMessageBox.question(
                    self,
                    "Replace preset?",
                    f'A preset named "{existing.preset.name}" already exists. Replace its settings?',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if replace != QMessageBox.StandardButton.Yes:
                    return None
                record = self.preset_repository.update_data(existing.preset.id, data)
            else:
                record = self.preset_repository.create(name, data)
        except PresetError as error:
            QMessageBox.critical(self, error_title, f"Failed to save preset: {error}")
            return None
        self.presets = self.preset_repository.load_all()
        self.refresh_preset_list(select_id=record.preset.id)
        return record

    def _prompt_unique_import_name(
        self, preferred_name: str, *, excluding_id: UUID | None = None
    ) -> str | None:
        if self.preset_repository.find_by_name(
            preferred_name, excluding_id=excluding_id
        ) is None:
            return preferred_name
        suggestion = self.preset_repository.unique_name(preferred_name)
        while True:
            name, accepted = QInputDialog.getText(
                self,
                "Rename imported preset",
                "Another preset already uses this name. Enter a unique name:",
                text=suggestion,
            )
            if not accepted:
                return None
            try:
                conflict = self.preset_repository.find_by_name(
                    name, excluding_id=excluding_id
                )
            except PresetError as error:
                QMessageBox.warning(self, "Invalid preset name", str(error))
                continue
            if conflict is None:
                return name
            QMessageBox.warning(
                self,
                "Preset name already used",
                f'A preset named "{conflict.preset.name}" already exists.',
            )
            suggestion = self.preset_repository.unique_name(name)

    def choose_duplicate_uuid_action(
        self, existing: PresetRecord, imported_name: str
    ) -> str:
        """Ask the only UI-owned decision in duplicate-UUID imports."""
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle("Preset already imported")
        dialog.setText(
            f'The imported preset "{imported_name}" has the same UUID as '
            f'"{existing.preset.name}".'
        )
        dialog.setInformativeText("Replace it, import an independent copy, or cancel?")
        replace_button = dialog.addButton("Replace", QMessageBox.ButtonRole.AcceptRole)
        copy_button = dialog.addButton("Import as Copy", QMessageBox.ButtonRole.ActionRole)
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is replace_button:
            return "replace"
        if clicked is copy_button:
            return "copy"
        return "cancel"

    def import_preset_path(
        self, path: str, *, error_title: str
    ) -> PresetRecord | None:
        """Validate, resolve conflicts, and copy an external preset into storage."""
        try:
            inspection = self.preset_transfer.inspect_import(path)
            preset = inspection.preset
            if inspection.existing is not None:
                action = self.choose_duplicate_uuid_action(
                    inspection.existing, preset.name
                )
                if action == "cancel":
                    return None
                if action == "replace":
                    name = self._prompt_unique_import_name(
                        preset.name, excluding_id=preset.id
                    )
                    if name is None:
                        return None
                    record = self.preset_transfer.replace(preset, name=name)
                else:
                    name = self._prompt_unique_import_name(preset.name)
                    if name is None:
                        return None
                    record = self.preset_transfer.import_copy(preset, name=name)
            else:
                name = self._prompt_unique_import_name(preset.name)
                if name is None:
                    return None
                record = self.preset_transfer.import_new(preset, name=name)
        except PresetError as error:
            QMessageBox.critical(self, error_title, str(error))
            return None
        self.presets = self.preset_repository.load_all()
        self.refresh_preset_list(select_id=record.preset.id)
        return record

    def rename_preset(self) -> None:
        preset_id, record = self.selected_preset_record()
        if preset_id is None or record is None:
            return
        suggestion = record.preset.name
        while True:
            name, accepted = QInputDialog.getText(
                self,
                "Rename Preset",
                "Enter a unique preset name:",
                text=suggestion,
            )
            if not accepted:
                return
            try:
                updated = self.preset_repository.rename(preset_id, name)
            except PresetNameConflictError as error:
                QMessageBox.warning(self, "Preset name already used", str(error))
                suggestion = self.preset_repository.unique_name(name)
                continue
            except PresetError as error:
                QMessageBox.critical(self, "Rename Error", str(error))
                return
            self.presets = self.preset_repository.load_all()
            self.refresh_preset_list(select_id=updated.preset.id)
            return

    def delete_preset(self) -> None:
        preset_id, record = self.selected_preset_record()
        if preset_id is None or record is None:
            return
        try:
            self.preset_repository.delete(preset_id)
        except PresetError as error:
            QMessageBox.critical(self, "Delete Error", str(error))
            return
        self.presets = self.preset_repository.load_all()
        self.refresh_preset_list()

    def export_preset(self) -> None:
        _, record = self.selected_preset_record()
        if record is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Preset",
            suggested_save_path(
                self.preset_dialog_workflow,
                self.preset_transfer.suggested_export_filename(record.preset),
            ),
            "JSON Files (*.json)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path:
            return
        path = ensure_extension(path, ".json")
        remember_file_selection(
            self.preset_dialog_workflow,
            FileDialogDirection.OUTPUT,
            path,
        )
        overwrite = False
        if Path(path).exists():
            answer = QMessageBox.question(
                self,
                "Replace export file?",
                f'A file already exists at "{path}". Replace it?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            overwrite = True
        try:
            self.preset_transfer.export(record.preset, path, overwrite=overwrite)
        except PresetError as error:
            QMessageBox.critical(self, "Export Error", f"Failed to export preset: {error}")
