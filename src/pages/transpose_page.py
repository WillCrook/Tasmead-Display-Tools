from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import os
from pathlib import Path
from uuid import UUID

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from file_dialog_state import (
    FileDialogDirection,
    FileDialogWorkflow,
    remember_directory,
    remember_file_selection,
    remembered_directory,
)
from icon_utils import AppIcon, set_button_icon
from pages.airfield_ui import (
    AirfieldCard,
    AirfieldFormValues,
    AirfieldPresetManagerDialog,
    confirm_nonstandard_runways,
    format_optional_number,
)
from resource_paths import app_data_path, resource_path
from services import (
    AirfieldPresetData,
    AirfieldPresetError,
    CoordinateInputError,
    PresetImportExportService,
    PresetRecord,
    PresetRepository,
    PresetType,
    RunwayInferenceResult,
    RunwayReference,
    apply_source_runways,
    create_transposition_plan,
    customize_transposition_plan,
    infer_departure_runway,
    normalise_runway_designator,
    parse_coordinate_pair,
    parse_kml_track,
    run_transposition,
)


PAGE_STYLE = """
TransposePage {
    background: palette(window);
}
TransposePage QFrame#workspacePanel,
TransposePage QFrame#airfieldCard,
TransposePage QFrame#previewHost,
TransposePage QDialog#inferencePopup {
    background: palette(base);
    border: 1px solid palette(mid);
    border-radius: 12px;
}
TransposePage QLabel#dialogTitle {
    font-size: 22px;
    font-weight: 700;
}
TransposePage QLabel#cardTitle,
TransposePage QLabel#panelTitle,
TransposePage QLabel#popupHeading {
    font-size: 15px;
    font-weight: 650;
}
TransposePage QFrame#statusPanel {
    background: palette(alternate-base);
    border: 1px solid palette(midlight);
    border-radius: 8px;
}
TransposePage QLabel#statusBadge {
    font-weight: 650;
}
"""


@dataclass(slots=True)
class SourceAirfieldState:
    path: Path
    values: AirfieldFormValues = AirfieldFormValues()
    auto_values: AirfieldFormValues | None = None
    provenance: str = "Needs input"
    analysed: bool = False
    altitude_mode: str | None = None
    inference: RunwayInferenceResult | None = None
    parse_error: str | None = None
    details: str = ""


class TranspositionOutputDialog(QDialog):
    """Edit and validate every output filename in a transposition batch."""

    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self._source_plan = plan
        self.validated_plan = None
        self.filename_edits = []
        self.setWindowTitle("Choose Transposition Output Names")
        self.resize(720, min(560, 190 + len(plan.jobs) * 42))

        layout = QVBoxLayout(self)
        instruction = QLabel(
            "Review or change each output filename before saving the batch."
        )
        instruction.setWordWrap(True)
        layout.addWidget(instruction)

        folder_label = QLabel(f"Output folder: {plan.output_directory}")
        folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        folder_label.setWordWrap(True)
        layout.addWidget(folder_label)

        table = QTableWidget(len(plan.jobs), 2, self)
        table.setHorizontalHeaderLabels(("Input KML", "Output filename"))
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setAlternatingRowColors(True)
        for row, job in enumerate(plan.jobs):
            input_item = QTableWidgetItem(job.input_path.name)
            input_item.setToolTip(str(job.input_path))
            input_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            table.setItem(row, 0, input_item)

            filename_edit = QLineEdit(job.output_path.name)
            filename_edit.setAccessibleName(
                f"Output filename for {job.input_path.name}"
            )
            table.setCellWidget(row, 1, filename_edit)
            self.filename_edits.append(filename_edit)
        table.resizeColumnsToContents()
        layout.addWidget(table)

        self.error_label = QLabel()
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        self.error_label.setAccessibleName("Output filename error")
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def output_filenames(self):
        return tuple(edit.text() for edit in self.filename_edits)

    def _validate_and_accept(self):
        try:
            self.validated_plan = customize_transposition_plan(
                self._source_plan,
                self.output_filenames(),
            )
        except (TypeError, ValueError) as error:
            self.error_label.setText(str(error))
            return
        self.error_label.clear()
        self.accept()


class TransposePage(QWidget):
    """Three-column workspace for reviewed runway-to-runway transposition."""

    def __init__(self):
        super().__init__()
        self.setObjectName("TransposePage")
        self.setStyleSheet(PAGE_STYLE)
        self.input_files: list[str] = []
        self.source_states: dict[str, SourceAirfieldState] = {}
        self._current_source_path: str | None = None
        self._rendering_source = False

        self.preset_repository = PresetRepository(
            app_data_path("presets/airfield"),
            PresetType.AIRFIELD,
            legacy_managed_directories=(app_data_path("airfields"),),
            legacy_readonly_directories=(resource_path("data/airfields"),),
            backup_directory=app_data_path("presets/legacy-backup/airfield"),
        )
        self.preset_store = self.preset_repository
        self.preset_transfer = PresetImportExportService(self.preset_repository)
        self.presets: dict[UUID, PresetRecord] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        root.addWidget(self.splitter, 1)

        self._build_file_column()
        self._build_airfield_column()
        self._build_preview_column()
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setStretchFactor(2, 1)
        self.splitter.setSizes((235, 430, 285))

        self._load_presets(show_issues=True)
        self._render_source_state(None)

    def _build_file_column(self) -> None:
        column = QWidget()
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(0)

        self.file_panel = QFrame()
        self.file_panel.setObjectName("workspacePanel")
        layout = QVBoxLayout(self.file_panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        heading_row = QHBoxLayout()
        heading = QLabel("Input files")
        heading.setObjectName("panelTitle")
        self.file_count_label = QLabel("0 files")
        self.file_count_label.setObjectName("mutedText")
        heading_row.addWidget(heading)
        heading_row.addStretch()
        heading_row.addWidget(self.file_count_label)
        layout.addLayout(heading_row)
        hint = QLabel("Drop KML files here or add them from disk.")
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.file_list = QListWidget()
        self.file_list.setAccessibleName("Input KML files")
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.file_list.dragEnterEvent = self.drag_enter
        self.file_list.dragMoveEvent = self.drag_move
        self.file_list.dropEvent = self.drop_event
        self.file_list.currentItemChanged.connect(self._current_file_changed)
        layout.addWidget(self.file_list, 1)

        file_actions = QHBoxLayout()
        self.add_files_btn = QPushButton("Add files")
        set_button_icon(self.add_files_btn, AppIcon.FOLDER_PLUS)
        self.remove_files_btn = QPushButton("Remove")
        set_button_icon(self.remove_files_btn, AppIcon.TRASH)
        self.add_files_btn.clicked.connect(self.browse_files)
        self.remove_files_btn.clicked.connect(self.remove_selected_files)
        file_actions.addWidget(self.add_files_btn)
        file_actions.addWidget(self.remove_files_btn)
        layout.addLayout(file_actions)

        self.manage_airfields_btn = QPushButton("Manage airfields…")
        set_button_icon(self.manage_airfields_btn, AppIcon.LIST)
        self.manage_airfields_btn.clicked.connect(self.open_airfield_manager)
        layout.addWidget(self.manage_airfields_btn)

        column_layout.addWidget(
            self.file_panel,
            alignment=Qt.AlignmentFlag.AlignTop,
        )
        column_layout.addStretch()
        self.splitter.addWidget(column)

    def _build_airfield_column(self) -> None:
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(7, 0, 7, 0)
        layout.setSpacing(12)
        self.source_card = AirfieldCard(
            "Original airfield",
            "Threshold, heading, and elevation are inferred from the selected KML when possible.",
            include_elevation=True,
            show_inference=True,
        )
        self.target_card = AirfieldCard(
            "Target airfield",
            "Choose a preset or enter the target runway manually.",
            include_elevation=False,
            show_inference=False,
        )
        self.source_card.user_edited.connect(self._source_form_edited)
        self.source_card.restore_auto_requested.connect(self._restore_auto_source)
        self.source_card.preset_apply_requested.connect(self._apply_source_preset)
        self.target_card.preset_apply_requested.connect(self._apply_target_preset)
        layout.addWidget(self.source_card)
        layout.addWidget(self.target_card)
        layout.addStretch()
        self.splitter.addWidget(column)

        # Stable aliases for extensions that used the former target widgets.
        self.airfield_name_input = self.target_card.name_input
        self.coordinate_input = self.target_card.coordinate_input
        self.heading_input = self.target_card.heading_input
        self.target_runway_input = self.target_card.runway_input
        self.orig_height_input = self.source_card.elevation_m_input
        self.orig_height_ft_input = self.source_card.elevation_ft_input

    def _build_preview_column(self) -> None:
        self.preview_host = QFrame()
        self.preview_host.setObjectName("previewHost")
        layout = QVBoxLayout(self.preview_host)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.addStretch()
        icon = QLabel("◫")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 42px; color: palette(mid);")
        title = QLabel("3D preview")
        title.setObjectName("panelTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description = QLabel("Reserved for the future Google Maps preview.")
        description.setObjectName("mutedText")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description.setWordWrap(True)
        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addStretch()

        actions = QHBoxLayout()
        self.run_btn = QPushButton("Transpose files")
        self.run_btn.setObjectName("primaryButton")
        self.run_btn.clicked.connect(self.run_transposition_ui)
        self.preview_btn = QPushButton("View preview")
        set_button_icon(self.preview_btn, AppIcon.MONITOR)
        actions.addWidget(self.preview_btn)
        actions.addWidget(self.run_btn)
        layout.addLayout(actions)
        self.splitter.addWidget(self.preview_host)

    def _load_presets(self, *, show_issues: bool = False) -> None:
        self.presets = self.preset_repository.load_all()
        if show_issues and self.preset_repository.issues:
            issues = list(dict.fromkeys(self.preset_repository.issues))
            QMessageBox.warning(
                self,
                "Preset files need attention",
                "Some airfield presets could not be loaded or migrated:\n\n"
                + "\n".join(f"• {issue}" for issue in issues[:8]),
            )
            self.preset_repository.issues.clear()
        for card in (self.source_card, self.target_card):
            selected = card.preset_combo.currentData(Qt.ItemDataRole.UserRole)
            card.preset_combo.clear()
            card.preset_combo.addItem("Choose an airfield preset", None)
            for record in sorted(
                self.presets.values(), key=lambda item: item.preset.name.casefold()
            ):
                card.preset_combo.addItem(record.preset.name, str(record.preset.id))
            if selected:
                index = card.preset_combo.findData(selected, Qt.ItemDataRole.UserRole)
                if index >= 0:
                    card.preset_combo.setCurrentIndex(index)

    def open_airfield_manager(self) -> None:
        dialog = AirfieldPresetManagerDialog(
            self.preset_repository,
            self.preset_transfer,
            self,
        )
        dialog.exec()
        self._load_presets()

    def _selected_preset(self, card: AirfieldCard) -> PresetRecord | None:
        raw_id = card.preset_combo.currentData(Qt.ItemDataRole.UserRole)
        if not raw_id:
            card.set_error("Choose an airfield preset first.")
            return None
        try:
            preset_id = UUID(str(raw_id))
        except ValueError:
            card.set_error("The selected preset has an invalid identity.")
            return None
        record = self.presets.get(preset_id)
        if record is None:
            card.set_error("The selected preset is no longer available.")
        return record

    @staticmethod
    def _values_from_preset(payload: AirfieldPresetData) -> AirfieldFormValues:
        threshold = ""
        if payload.threshold_latitude is not None and payload.threshold_longitude is not None:
            threshold = (
                f"{format_optional_number(payload.threshold_latitude)}, "
                f"{format_optional_number(payload.threshold_longitude)}"
            )
        return AirfieldFormValues(
            airfield_name=payload.airfield_name,
            runway=payload.runway,
            threshold=threshold,
            true_heading=format_optional_number(payload.true_heading_deg),
            elevation_m=format_optional_number(payload.elevation_m, 2),
        )

    def _decode_selected_preset(
        self, card: AirfieldCard
    ) -> tuple[AirfieldPresetData, tuple[str, ...]] | None:
        record = self._selected_preset(card)
        if record is None:
            return None
        try:
            return AirfieldPresetData.from_mapping(record.preset.data)
        except AirfieldPresetError as error:
            card.set_error(str(error))
            return None

    def _apply_source_preset(self) -> None:
        if self._current_source_path is None:
            self.source_card.set_error("Select an input KML file first.")
            return
        decoded = self._decode_selected_preset(self.source_card)
        if decoded is None:
            return
        payload, warnings = decoded
        state = self.source_states[self._current_source_path]
        state.values = self._values_from_preset(payload)
        state.provenance = "Preset"
        preset_notes = "\n".join(warnings)
        if preset_notes:
            state.details = "\n\n".join(filter(None, (state.details, preset_notes)))
        self._render_source_state(state)

    def _apply_target_preset(self) -> None:
        decoded = self._decode_selected_preset(self.target_card)
        if decoded is None:
            return
        payload, warnings = decoded
        values = self._values_from_preset(payload)
        self.target_card.set_values(
            AirfieldFormValues(
                airfield_name=values.airfield_name,
                runway=values.runway,
                threshold=values.threshold,
                true_heading=values.true_heading,
            )
        )
        if warnings:
            self.target_card.set_error(" ".join(warnings))

    def drag_enter(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def drag_move(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def drop_event(self, event) -> None:
        files = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.toLocalFile().lower().endswith(".kml")
        ]
        if files:
            self.add_files_to_list(files)
            event.acceptProposedAction()

    def browse_files(self, _event=None) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select KML Files",
            remembered_directory(
                FileDialogWorkflow.TRANSPOSITION,
                FileDialogDirection.INPUT,
            ),
            "KML Files (*.kml)",
        )
        if not files:
            return
        remember_file_selection(
            FileDialogWorkflow.TRANSPOSITION,
            FileDialogDirection.INPUT,
            files[0],
        )
        self.add_files_to_list(files)

    @staticmethod
    def _path_key(path: str | Path) -> str:
        return os.path.normcase(str(Path(path).resolve(strict=False)))

    def add_files_to_list(self, files) -> None:
        existing = {self._path_key(path) for path in self.input_files}
        first_new: QListWidgetItem | None = None
        for raw_path in files:
            path = str(Path(raw_path).resolve(strict=False))
            key = self._path_key(path)
            if key in existing:
                continue
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            item.setData(
                Qt.ItemDataRole.AccessibleDescriptionRole,
                f"Full path: {path}",
            )
            self.file_list.addItem(item)
            self.input_files.append(path)
            self.source_states[path] = SourceAirfieldState(Path(path))
            existing.add(key)
            if first_new is None:
                first_new = item
        self._update_file_count()
        if self.file_list.currentItem() is None and first_new is not None:
            self.file_list.setCurrentItem(first_new)

    def remove_selected_files(self) -> None:
        self._commit_current_source()
        items = list(self.file_list.selectedItems())
        for item in items:
            path = str(item.data(Qt.ItemDataRole.UserRole))
            self.source_states.pop(path, None)
            if path in self.input_files:
                self.input_files.remove(path)
            self.file_list.takeItem(self.file_list.row(item))
        self._update_file_count()
        if self.file_list.currentItem() is None and self.file_list.count():
            self.file_list.setCurrentRow(0)
        elif not self.file_list.count():
            self._current_source_path = None
            self._render_source_state(None)

    def _update_file_count(self) -> None:
        count = len(self.input_files)
        self.file_count_label.setText(f"{count} file" if count == 1 else f"{count} files")

    def update_file_label(self) -> None:
        """Compatibility hook retained for extensions; the list is always current."""
        self._update_file_count()

    def _current_file_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        self._commit_current_source()
        self._current_source_path = (
            str(current.data(Qt.ItemDataRole.UserRole)) if current is not None else None
        )
        if self._current_source_path is None:
            self._render_source_state(None)
            return
        state = self.source_states.setdefault(
            self._current_source_path,
            SourceAirfieldState(Path(self._current_source_path)),
        )
        if not state.analysed:
            self._analyse_source(state)
        self._render_source_state(state)

    def _analyse_source(self, state: SourceAirfieldState) -> None:
        state.analysed = True
        try:
            track = parse_kml_track(state.path)
            state.altitude_mode = track.altitude_mode
            inference = infer_departure_runway(track)
            state.inference = inference
        except Exception as error:
            state.parse_error = str(error)
            state.provenance = "File error"
            state.details = str(error)
            return

        candidate = inference.candidate
        warnings = list(inference.warnings)
        if candidate is None:
            state.provenance = "Needs input"
            state.details = "\n".join(
                filter(None, (inference.error or "", *warnings))
            )
            return
        reference = candidate.reference
        auto_values = AirfieldFormValues(
            threshold=(
                f"{format_optional_number(reference.latitude)}, "
                f"{format_optional_number(reference.longitude)}"
            ),
            true_heading=format_optional_number(reference.true_heading_deg, 2),
            elevation_m=format_optional_number(reference.elevation_m, 2),
        )
        state.auto_values = auto_values
        state.values = auto_values
        state.provenance = "Auto-detected"
        evidence = ["Evidence:", *candidate.evidence]
        combined_warnings = list(dict.fromkeys((*warnings, *candidate.warnings)))
        if combined_warnings:
            evidence.extend(("", "Warnings:", *combined_warnings))
        state.details = "\n".join(evidence)

    def _commit_current_source(self) -> None:
        if self._rendering_source or self._current_source_path is None:
            return
        state = self.source_states.get(self._current_source_path)
        if state is not None and state.parse_error is None:
            state.values = self.source_card.values()

    def _source_form_edited(self) -> None:
        if self._rendering_source or self._current_source_path is None:
            return
        state = self.source_states.get(self._current_source_path)
        if state is None or state.parse_error is not None:
            return
        state.values = self.source_card.values()
        state.provenance = "Manual override"
        self._render_source_status(state)

    def _restore_auto_source(self) -> None:
        if self._current_source_path is None:
            return
        state = self.source_states.get(self._current_source_path)
        if state is None or state.auto_values is None:
            return
        state.values = state.auto_values
        state.provenance = "Auto-detected"
        self._render_source_state(state)

    def _render_source_state(self, state: SourceAirfieldState | None) -> None:
        self._rendering_source = True
        try:
            if state is None:
                self.source_card.clear_values()
                self.source_card.set_fields_enabled(False)
                self.source_card.set_status("Waiting for a KML file")
                self.source_card.set_error("")
                return
            self.source_card.set_values(state.values)
            self.source_card.set_fields_enabled(state.parse_error is None)
            self.source_card.set_error(state.parse_error or "")
            self._render_source_status(state)
        finally:
            self._rendering_source = False

    def _render_source_status(self, state: SourceAirfieldState) -> None:
        confidence = ""
        candidate = state.inference.candidate if state.inference is not None else None
        if candidate is not None:
            confidence = (
                f"Heading {candidate.heading_confidence.value.title()} · "
                f"Threshold {candidate.threshold_confidence.value.title()}"
            )
        self.source_card.set_status(
            state.provenance,
            confidence=confidence,
            details=state.details,
            can_restore=(
                state.auto_values is not None and state.values != state.auto_values
            ),
        )

    def _ensure_source_states(self) -> None:
        for raw_path in self.input_files:
            path = str(Path(raw_path).resolve(strict=False))
            if path != raw_path:
                index = self.input_files.index(raw_path)
                self.input_files[index] = path
            self.source_states.setdefault(path, SourceAirfieldState(Path(path)))

    @staticmethod
    def _reference_from_values(
        values: AirfieldFormValues,
        *,
        label: str,
        elevation_required: bool,
    ) -> RunwayReference:
        try:
            coordinate = parse_coordinate_pair(values.threshold)
        except CoordinateInputError as error:
            raise ValueError(f"{label} departure threshold: {error}") from error
        try:
            heading = float(values.true_heading)
            if not math.isfinite(heading):
                raise ValueError
        except ValueError as error:
            raise ValueError(f"{label} true heading must be a finite number.") from error
        elevation = None
        if values.elevation_m.strip():
            try:
                elevation = float(values.elevation_m)
                if not math.isfinite(elevation):
                    raise ValueError
            except ValueError as error:
                raise ValueError(f"{label} elevation must be a finite number.") from error
        if elevation_required and elevation is None:
            raise ValueError(
                f"{label} elevation is required because this KML uses absolute altitude."
            )
        try:
            return RunwayReference(
                coordinate.latitude,
                coordinate.longitude,
                heading,
                elevation,
            )
        except ValueError as error:
            raise ValueError(f"{label}: {error}") from error

    def _select_source_path(self, path: str) -> None:
        for row in range(self.file_list.count()):
            item = self.file_list.item(row)
            if str(item.data(Qt.ItemDataRole.UserRole)) == path:
                self.file_list.setCurrentItem(item)
                break

    def _review_source_runways(self, _fallback_elevation_m=None):
        """Return one inline-reviewed reference per input; no modal review is used."""
        self._commit_current_source()
        self._ensure_source_states()
        reviewed: list[RunwayReference | None] = []
        for path in self.input_files:
            state = self.source_states[path]
            if not state.analysed:
                self._analyse_source(state)
            if state.parse_error is not None:
                reviewed.append(None)
                continue
            try:
                reference = self._reference_from_values(
                    state.values,
                    label=Path(path).name,
                    elevation_required=state.altitude_mode == "absolute",
                )
            except ValueError as error:
                self._select_source_path(path)
                self.source_card.set_error(str(error))
                QMessageBox.warning(self, "Original airfield needs attention", str(error))
                return None
            state.values = AirfieldFormValues(
                airfield_name=state.values.airfield_name,
                runway=state.values.runway,
                threshold=(
                    f"{format_optional_number(reference.latitude)}, "
                    f"{format_optional_number(reference.longitude)}"
                ),
                true_heading=format_optional_number(reference.true_heading_deg),
                elevation_m=format_optional_number(reference.elevation_m, 2),
            )
            reviewed.append(reference)
        if self._current_source_path:
            self._render_source_state(self.source_states[self._current_source_path])
        return tuple(reviewed)

    def _validated_target(self) -> RunwayReference | None:
        values = self.target_card.values()
        if not values.airfield_name.strip():
            self.target_card.set_error("Enter the target airfield name.")
            self.target_card.name_input.setFocus()
            return None
        designator = normalise_runway_designator(values.runway)
        if not designator.value:
            self.target_card.set_error("Enter the target runway identifier.")
            self.target_card.runway_input.setFocus()
            return None
        if designator.conventional:
            self.target_card.runway_input.setText(designator.value)
        try:
            reference = self._reference_from_values(
                values,
                label="Target airfield",
                elevation_required=False,
            )
        except ValueError as error:
            self.target_card.set_error(str(error))
            QMessageBox.warning(self, "Target airfield needs attention", str(error))
            return None
        self.target_card.coordinate_input.setText(
            f"{format_optional_number(reference.latitude)}, "
            f"{format_optional_number(reference.longitude)}"
        )
        self.target_card.heading_input.setText(
            format_optional_number(reference.true_heading_deg)
        )
        self.target_card.set_error("")
        return reference

    def _nonstandard_runways(self):
        entries: list[tuple[str, str, QWidget, str | None]] = []
        for path in self.input_files:
            state = self.source_states.get(path)
            if state is None or state.parse_error is not None:
                continue
            designator = normalise_runway_designator(state.values.runway)
            if designator.conventional:
                if designator.value != state.values.runway:
                    state.values = AirfieldFormValues(
                        airfield_name=state.values.airfield_name,
                        runway=designator.value,
                        threshold=state.values.threshold,
                        true_heading=state.values.true_heading,
                        elevation_m=state.values.elevation_m,
                    )
                continue
            if designator.value:
                entries.append(
                    (
                        f"Original — {Path(path).name}",
                        designator.value,
                        self.source_card.runway_input,
                        path,
                    )
                )
        target = normalise_runway_designator(self.target_card.runway_input.text())
        if target.value and not target.conventional:
            entries.append(
                (
                    "Target airfield",
                    target.value,
                    self.target_card.runway_input,
                    None,
                )
            )
        return entries

    def _confirm_runway_overrides(self) -> bool:
        entries = self._nonstandard_runways()
        if confirm_nonstandard_runways(
            self,
            tuple((context, value, widget) for context, value, widget, _ in entries),
            action="transpose these files",
        ):
            return True
        if entries and entries[0][3] is not None:
            self._select_source_path(entries[0][3])
            self.source_card.runway_input.setFocus()
        return False

    def run_transposition_ui(self) -> None:
        if not self.input_files:
            QMessageBox.warning(self, "No Files", "Please select at least one KML file.")
            return
        target_runway = self._validated_target()
        if target_runway is None:
            return
        reviewed_runways = self._review_source_runways(None)
        if reviewed_runways is None:
            return
        if not self._confirm_runway_overrides():
            return

        output_dir = QFileDialog.getExistingDirectory(
            self,
            "Select Output Folder",
            self._initial_output_directory(),
        )
        if not output_dir:
            return
        remember_directory(
            FileDialogWorkflow.TRANSPOSITION,
            FileDialogDirection.OUTPUT,
            output_dir,
        )

        try:
            plan = create_transposition_plan(
                input_files=self.input_files,
                output_directory=output_dir,
                target_airfield=self.target_card.name_input.text(),
            )
            plan = apply_source_runways(plan, reviewed_runways)
        except Exception as error:
            QMessageBox.critical(self, "Error", f"Could not plan outputs: {error}")
            return

        plan = self._edit_output_plan(plan)
        if plan is None:
            return

        try:
            result = run_transposition(plan=plan, target_runway=target_runway)
        except Exception as error:
            QMessageBox.critical(self, "Error", f"Transposition failed: {error}")
            return

        processing_warnings = "\n".join(
            f"{outcome.input_path.name}:\n"
            + "\n".join(f"- {warning}" for warning in outcome.warnings)
            for outcome in result.successful
            if outcome.warnings
        )
        if result.succeeded:
            successful_paths = "\n".join(
                str(output.output_path) for output in result.successful
            )
            message = (
                f"Transposition complete!\n"
                f"Saved {len(result.successful)} KML file(s) to:\n{output_dir}\n\n"
                f"Outputs:\n{successful_paths}"
            )
            if processing_warnings:
                QMessageBox.warning(
                    self,
                    "Transposition complete with warnings",
                    f"{message}\n\nWarnings:\n{processing_warnings}",
                )
            else:
                QMessageBox.information(self, "Success", message)
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
                f"No KML files were produced.\n\nFailed inputs:\n{failed_paths}",
            )
            return
        warning_section = (
            f"\n\nWarnings:\n{processing_warnings}"
            if processing_warnings
            else ""
        )
        QMessageBox.warning(
            self,
            "Transposition partially complete",
            f"Saved {result.success_count} of {result.total_count} KML file(s).\n\n"
            f"Successful outputs:\n{successful_paths}\n\n"
            f"Failed inputs:\n{failed_paths}{warning_section}",
        )

    def capture_preset_data(self) -> dict[str, object]:
        """Return canonical target data; target elevation is intentionally absent."""
        values = self.target_card.values()
        coordinate = self.target_card.coordinate_input.coordinates()
        try:
            heading = float(values.true_heading)
        except ValueError as error:
            raise AirfieldPresetError("True heading must be numeric.") from error
        return AirfieldPresetData(
            airfield_name=values.airfield_name.strip(),
            runway=normalise_runway_designator(values.runway).value,
            threshold_latitude=coordinate.latitude,
            threshold_longitude=coordinate.longitude,
            true_heading_deg=heading,
            elevation_m=None,
        ).to_mapping()

    def apply_preset_data(self, data: Mapping[str, object]) -> None:
        """Compatibility hook: apply an airfield payload to the target card."""
        payload, warnings = AirfieldPresetData.from_mapping(data)
        values = self._values_from_preset(payload)
        self.target_card.set_values(
            AirfieldFormValues(
                airfield_name=values.airfield_name,
                runway=values.runway,
                threshold=values.threshold,
                true_heading=values.true_heading,
            )
        )
        self.target_card.set_error(" ".join(warnings))

    def save_preset(self) -> None:
        """Compatibility hook now routes preset creation through the manager."""
        self.open_airfield_manager()

    def load_preset_from_file(self) -> None:
        """Compatibility hook now routes imports through the manager."""
        self.open_airfield_manager()

    def _initial_output_directory(self):
        return remembered_directory(
            FileDialogWorkflow.TRANSPOSITION,
            FileDialogDirection.OUTPUT,
        )

    def _edit_output_plan(self, plan):
        dialog = TranspositionOutputDialog(plan, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        candidate = dialog.validated_plan
        if candidate is None:
            return None
        existing_paths = tuple(
            job.output_path for job in candidate.jobs if job.output_path.exists()
        )
        if not existing_paths:
            return candidate
        filenames = "\n".join(f"• {path.name}" for path in existing_paths)
        answer = QMessageBox.question(
            self,
            "Replace existing output files?",
            "The following output files already exist and will be replaced:\n\n"
            f"{filenames}\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return None
        return customize_transposition_plan(
            candidate,
            tuple(job.output_path.name for job in candidate.jobs),
            approved_overwrites=existing_paths,
        )


__all__ = ["SourceAirfieldState", "TransposePage", "TranspositionOutputDialog"]
