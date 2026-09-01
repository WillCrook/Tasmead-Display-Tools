"""Modern airfield cards and the managed-airfield preset dialog."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from file_dialog_state import (
    FileDialogDirection,
    FileDialogWorkflow,
    remember_file_selection,
    remembered_directory,
)
from icon_utils import AppIcon, set_button_icon
from pages.coordinate_input import CoordinatePairInput
from pages.info_popup import PersistentInfoPopupController
from pages.preset_ui import PresetUiMixin
from pages.unit_fields import MetreFeetFieldPair
from services import (
    AirfieldPresetError,
    CoordinateInputError,
    PresetError,
    PresetImportExportService,
    PresetRepository,
    RunwayPresetSection,
    TranspositionPresetData,
)


@dataclass(frozen=True, slots=True)
class AirfieldFormValues:
    airfield_name: str = ""
    runway: str = ""
    threshold: str = ""
    true_heading: str = ""
    elevation_m: str = ""


def format_optional_number(value: float | None, places: int = 8) -> str:
    if value is None:
        return ""
    return f"{value:.{places}f}".rstrip("0").rstrip(".")


def friendly_altitude_mode(mode: str | None) -> str:
    """Return a concise user-facing label for a KML altitude mode."""
    return {
        "absolute": "Absolute",
        "relativeToGround": "Relative to ground",
        "clampToGround": "Clamped to ground",
        "relativeToSeaFloor": "Relative to sea floor (unsupported)",
        "clampToSeaFloor": "Clamped to sea floor (unsupported)",
    }.get(mode, mode or "Unknown")


def confirm_nonstandard_runways(
    parent: QWidget,
    entries: Sequence[tuple[str, str, QWidget]],
    *,
    action: str,
) -> bool:
    """Request one explicit confirmation for every non-standard value in an action."""
    if not entries:
        return True
    details = "\n".join(f"• {context}: {value}" for context, value, _ in entries)
    answer = QMessageBox.question(
        parent,
        "Confirm non-standard runway identifiers",
        "The following runway identifiers are outside the conventional 01–36 "
        "format with an optional L, C, or R suffix:\n\n"
        f"{details}\n\nContinue and {action}?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Cancel,
    )
    if answer == QMessageBox.StandardButton.Yes:
        return True
    entries[0][2].setFocus(Qt.FocusReason.OtherFocusReason)
    return False


class AirfieldCard(QFrame):
    """Reusable card for an original or target directional runway."""

    user_edited = pyqtSignal()
    preset_apply_requested = pyqtSignal()
    preset_save_requested = pyqtSignal()
    restore_auto_requested = pyqtSignal()

    def __init__(
        self,
        title: str,
        subtitle: str,
        *,
        include_elevation: bool,
        show_inference: bool,
        include_altitude_mode: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("airfieldCard")
        self.include_elevation = include_elevation
        self._details_text = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 16, 10, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("mutedText")
        subtitle_label.setWordWrap(True)
        titles.addWidget(title_label)
        titles.addWidget(subtitle_label)
        header.addLayout(titles, 1)
        root.addLayout(header)

        self.preset_label = QLabel("Preset name")
        root.addWidget(self.preset_label)
        self.preset_combo = QComboBox()
        self.preset_combo.setAccessibleName(f"{title} preset name")
        self.preset_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.preset_combo.setMinimumContentsLength(4)
        self.preset_combo.activated.connect(self._apply_activated_preset)
        self.save_preset_btn = QPushButton("Save preset")
        self.save_preset_btn.setAccessibleName(f"Save {title} inputs as preset")
        self.save_preset_btn.clicked.connect(self.preset_save_requested)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(self.save_preset_btn)
        root.addLayout(preset_row)

        fields = QGridLayout()
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(7)
        fields.setColumnStretch(1, 1)

        self.coordinate_input = CoordinatePairInput(
            f"{title} departure runway threshold coordinates"
        )
        self.heading_input = QLineEdit()
        self.heading_input.setPlaceholderText("0–360° true")
        self.heading_input.setAccessibleName(f"{title} true heading")

        fields.addWidget(QLabel("Runway coordinates"), 0, 0)
        fields.addWidget(self.coordinate_input, 0, 1)
        fields.addWidget(QLabel("True heading"), 1, 0)
        fields.addWidget(self.heading_input, 1, 1)

        next_row = 2
        self.altitude_mode_label: QLabel | None = None
        self.altitude_mode_output: QLineEdit | None = None
        if include_altitude_mode:
            self.altitude_mode_label = QLabel("Altitude mode")
            self.altitude_mode_output = QLineEdit()
            self.altitude_mode_output.setReadOnly(True)
            self.altitude_mode_output.setAccessibleName(
                f"{title} KML altitude mode"
            )
            fields.addWidget(self.altitude_mode_label, next_row, 0)
            fields.addWidget(self.altitude_mode_output, next_row, 1)
            next_row += 1

        self.elevation_m_input: QLineEdit | None = None
        self.elevation_ft_input: QLineEdit | None = None
        self.elevation_units: MetreFeetFieldPair | None = None
        self.elevation_label: QLabel | None = None
        if include_elevation:
            elevation_row = QHBoxLayout()
            self.elevation_m_input = QLineEdit()
            self.elevation_m_input.setPlaceholderText("metres")
            self.elevation_m_input.setAccessibleName(f"{title} elevation metres")
            self.elevation_ft_input = QLineEdit()
            self.elevation_ft_input.setPlaceholderText("feet")
            self.elevation_ft_input.setAccessibleName(f"{title} elevation feet")
            elevation_row.addWidget(self.elevation_m_input)
            elevation_row.addWidget(self.elevation_ft_input)
            self.elevation_label = QLabel("Elevation (m / ft)")
            fields.addWidget(self.elevation_label, next_row, 0)
            fields.addLayout(elevation_row, next_row, 1)
            self.elevation_units = MetreFeetFieldPair(
                self.elevation_m_input,
                self.elevation_ft_input,
            )
        root.addLayout(fields)

        self.status_frame = QFrame()
        self.status_frame.setObjectName("statusPanel")
        status_layout = QVBoxLayout(self.status_frame)
        status_layout.setContentsMargins(10, 8, 10, 8)
        status_layout.setSpacing(5)
        status_top = QHBoxLayout()
        self.status_label = QLabel("Waiting for a KML file")
        self.status_label.setObjectName("statusBadge")
        status_top.addWidget(self.status_label)
        self.detection_status_dot = QLabel()
        self.detection_status_dot.setObjectName("runwayDetectionStatusDot")
        self.detection_status_dot.setFixedSize(10, 10)
        self.detection_status_dot.hide()
        status_top.addWidget(self.detection_status_dot)
        self.details_button = QToolButton()
        self.details_button.setAutoRaise(True)
        self.details_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.details_button.setAccessibleName("Runway detection details")
        set_button_icon(self.details_button, AppIcon.INFO_CIRCLE)
        self.details_popup = PersistentInfoPopupController(self.details_button)
        self.details_button.hide()
        status_top.addWidget(self.details_button)
        self.restore_button = QPushButton("Restore auto-detected")
        self.restore_button.clicked.connect(self.restore_auto_requested)
        self.restore_button.hide()
        status_top.addWidget(self.restore_button)
        status_top.addStretch()
        status_layout.addLayout(status_top)
        self.status_frame.setVisible(show_inference)
        root.addWidget(self.status_frame)

        self.error_label = QLabel()
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        self.error_label.setAccessibleName(f"{title} validation error")
        root.addWidget(self.error_label)

        for field in self.editable_fields():
            field.textEdited.connect(self.user_edited)

    def editable_fields(self) -> tuple[QLineEdit, ...]:
        fields: list[QLineEdit] = [
            self.coordinate_input,
            self.heading_input,
        ]
        if self.elevation_m_input is not None and self.elevation_ft_input is not None:
            fields.extend((self.elevation_m_input, self.elevation_ft_input))
        return tuple(fields)

    def values(self) -> AirfieldFormValues:
        return AirfieldFormValues(
            threshold=self.coordinate_input.text(),
            true_heading=self.heading_input.text(),
            elevation_m=(
                self.elevation_m_input.text()
                if self.elevation_m_input is not None
                else ""
            ),
        )

    def set_values(self, values: AirfieldFormValues) -> None:
        self.coordinate_input.setText(values.threshold)
        self.heading_input.setText(values.true_heading)
        if self.elevation_units is not None:
            self.elevation_units.set_metres_text(
                values.elevation_m,
                notify_dependents=False,
            )
        self.error_label.clear()

    def clear_values(self) -> None:
        self.set_values(AirfieldFormValues())
        if self.altitude_mode_output is not None:
            self.altitude_mode_output.clear()

    def set_altitude_mode(self, mode: str | None) -> None:
        if self.altitude_mode_output is not None:
            self.altitude_mode_output.setText(friendly_altitude_mode(mode))

    def set_fields_enabled(self, enabled: bool) -> None:
        for field in self.editable_fields():
            field.setEnabled(enabled)
        self.preset_combo.setEnabled(enabled)
        self.save_preset_btn.setEnabled(enabled)

    def _apply_activated_preset(self, index: int) -> None:
        """Apply only explicit user selections, not programmatic refreshes."""
        if self.preset_combo.itemData(index, Qt.ItemDataRole.UserRole) is not None:
            self.preset_apply_requested.emit()

    def set_status(
        self,
        status: str,
        *,
        details: str = "",
        detection_state: str | None = None,
        detection_description: str = "",
        can_restore: bool = False,
    ) -> None:
        self.details_popup.close()
        self.status_label.setText(status)
        self.status_label.setProperty("status", status.casefold().replace(" ", "-"))
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self._details_text = details
        self.detection_status_dot.setVisible(detection_state is not None)
        self.detection_status_dot.setProperty(
            "detectionState",
            detection_state or "unavailable",
        )
        self.detection_status_dot.setAccessibleName(
            detection_description or "Runway detection confidence unavailable"
        )
        self.detection_status_dot.setAccessibleDescription(detection_description)
        self.detection_status_dot.setToolTip("")
        dot_style = self.detection_status_dot.style()
        dot_style.unpolish(self.detection_status_dot)
        dot_style.polish(self.detection_status_dot)
        self.detection_status_dot.update()
        self.details_button.setToolTip(details)
        self.details_button.setAccessibleDescription(details)
        self.details_popup.set_text(details)
        self.details_button.setVisible(bool(details) and not can_restore)
        self.details_button.setEnabled(bool(details))
        self.restore_button.setVisible(can_restore)

    def set_error(self, message: str) -> None:
        self.error_label.setText(message)

class AirfieldPresetManagerDialog(QDialog, PresetUiMixin):
    """Create, edit, transfer, and delete shared transposition presets."""

    def __init__(
        self,
        repository: PresetRepository,
        transfer: PresetImportExportService,
        parent: QWidget | None = None,
    ) -> None:
        QDialog.__init__(self, parent)
        self.setWindowTitle("Manage Presets")
        self.resize(900, 590)
        self.preset_repository = repository
        self.preset_store = repository
        self.preset_transfer = transfer
        self.preset_dialog_workflow = FileDialogWorkflow.AIRFIELD_PRESET
        self.presets = {}
        self._editing_id: UUID | None = None

        root = QVBoxLayout(self)
        heading = QLabel("Manage Transposition Presets")
        heading.setObjectName("dialogTitle")
        root.addWidget(heading)
        explanation = QLabel(
            "Presets can contain runway, original-trace, and target-trace inputs. "
            "This editor updates the runway section without changing other sections."
        )
        explanation.setObjectName("mutedText")
        explanation.setWordWrap(True)
        root.addWidget(explanation)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        library = QWidget()
        library_layout = QVBoxLayout(library)
        library_layout.setContentsMargins(0, 0, 8, 0)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search presets…")
        self.search_input.setAccessibleName("Search transposition presets")
        self.search_input.textChanged.connect(self._filter_changed)
        library_layout.addWidget(self.search_input)
        self.preset_list = QListWidget()
        self.preset_list.setAccessibleName("Managed transposition presets")
        self.preset_list.currentItemChanged.connect(self._selection_changed)
        library_layout.addWidget(self.preset_list, 1)
        list_actions = QHBoxLayout()
        new_btn = QPushButton("New")
        import_btn = QPushButton("Import…")
        new_btn.clicked.connect(self._new_preset)
        import_btn.clicked.connect(self._import_preset)
        list_actions.addWidget(new_btn)
        list_actions.addWidget(import_btn)
        library_layout.addLayout(list_actions)
        splitter.addWidget(library)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(8, 0, 0, 0)
        self.editor_form = AirfieldCard(
            "Runway details",
            "Edit the runway geometry stored in this preset.",
            include_elevation=True,
            show_inference=False,
        )
        self.editor_form.preset_combo.hide()
        self.editor_form.preset_label.hide()
        self.editor_form.save_preset_btn.hide()
        editor_layout.addWidget(self.editor_form)
        self.editor_note = QLabel()
        self.editor_note.setObjectName("warningText")
        self.editor_note.setWordWrap(True)
        editor_layout.addWidget(self.editor_note)
        editor_actions = QHBoxLayout()
        self.save_btn = QPushButton("Save")
        self.rename_preset_btn = QPushButton("Rename…")
        self.delete_preset_btn = QPushButton("Delete")
        self.export_preset_btn = QPushButton("Export…")
        self.save_btn.clicked.connect(self._save_editor)
        self.rename_preset_btn.clicked.connect(self.rename_preset)
        self.delete_preset_btn.clicked.connect(self.delete_preset)
        self.export_preset_btn.clicked.connect(self.export_preset)
        for button in (
            self.save_btn,
            self.rename_preset_btn,
            self.delete_preset_btn,
            self.export_preset_btn,
        ):
            editor_actions.addWidget(button)
        editor_layout.addLayout(editor_actions)
        editor_layout.addStretch()
        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.reject)
        root.addWidget(close_buttons)

        self.load_presets_from_disk()
        self._new_preset()

    def refresh_preset_list(self, *, select_id: UUID | None = None) -> None:
        selected = select_id or self._editing_id
        query = self.search_input.text().strip().casefold()
        self.preset_list.clear()
        for record in sorted(
            self.presets.values(), key=lambda item: item.preset.name.casefold()
        ):
            searchable = record.preset.name
            if query and query not in searchable.casefold():
                continue
            item = QListWidgetItem(record.preset.name)
            item.setData(Qt.ItemDataRole.UserRole, str(record.preset.id))
            self.preset_list.addItem(item)
            if record.preset.id == selected:
                self.preset_list.setCurrentItem(item)
        self.update_preset_actions()

    def apply_preset_data(self, data: Mapping[str, object]) -> None:
        try:
            payload, warnings = TranspositionPresetData.from_mapping(data)
        except AirfieldPresetError as error:
            self.editor_form.clear_values()
            self.editor_note.setText(str(error))
            return
        runway = payload.runway
        if runway is None:
            self.editor_form.clear_values()
            self.editor_note.setText(
                "This preset does not yet contain runway inputs. Enter them and save to add that section."
            )
            return
        threshold = ""
        threshold = (
            f"{format_optional_number(runway.threshold_latitude)}, "
            f"{format_optional_number(runway.threshold_longitude)}"
        )
        self.editor_form.set_values(
            AirfieldFormValues(
                threshold=threshold,
                true_heading=format_optional_number(runway.true_heading_deg),
                elevation_m=format_optional_number(runway.elevation_m, 2),
            )
        )
        notes = list(warnings)
        if runway.elevation_m is None:
            notes.append("Enter an elevation before using this preset as an original airfield.")
        self.editor_note.setText("\n".join(notes))

    def _selection_changed(self, item: QListWidgetItem | None, _previous=None) -> None:
        record = self.preset_record_for_item(item)
        self._editing_id = record.preset.id if record is not None else None
        if record is not None:
            self.apply_preset_data(record.preset.data)
        self.update_preset_actions()

    def update_preset_actions(self, *_: object) -> None:
        selected = self.preset_list.currentItem() is not None
        self.rename_preset_btn.setEnabled(selected)
        self.delete_preset_btn.setEnabled(selected)
        self.export_preset_btn.setEnabled(selected)

    def _filter_changed(self, _text: str) -> None:
        self.refresh_preset_list()

    def _new_preset(self) -> None:
        self._editing_id = None
        self.preset_list.clearSelection()
        self.preset_list.setCurrentItem(None)
        self.editor_form.clear_values()
        self.editor_note.clear()
        self.update_preset_actions()

    def _save_editor(self) -> None:
        values = self.editor_form.values()
        try:
            threshold = self.editor_form.coordinate_input.coordinates()
            runway = RunwayPresetSection.validated(
                threshold=threshold,
                true_heading_deg=values.true_heading,
                elevation_m=values.elevation_m,
                elevation_required=True,
            )
        except (AirfieldPresetError, CoordinateInputError) as error:
            self.editor_form.set_error(str(error))
            return
        try:
            if self._editing_id is None:
                label, accepted = QInputDialog.getText(
                    self,
                    "Save Transposition Preset",
                    "Preset name:",
                )
                if not accepted:
                    return
                payload = TranspositionPresetData(runway=runway)
                record = self.preset_repository.create(label, payload.to_mapping())
            else:
                existing = self.presets.get(self._editing_id)
                if existing is None:
                    raise AirfieldPresetError("The selected preset is no longer available.")
                payload, _ = TranspositionPresetData.from_mapping(
                    existing.preset.data
                )
                record = self.preset_repository.update_data(
                    self._editing_id,
                    payload.with_runway(runway).to_mapping(),
                )
        except (AirfieldPresetError, PresetError) as error:
            QMessageBox.critical(self, "Preset Error", str(error))
            return
        self.presets = self.preset_repository.load_all()
        self._editing_id = record.preset.id
        self.refresh_preset_list(select_id=record.preset.id)
        self.editor_note.clear()

    def _import_preset(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Transposition Preset",
            remembered_directory(
                FileDialogWorkflow.AIRFIELD_PRESET,
                FileDialogDirection.INPUT,
            ),
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        remember_file_selection(
            FileDialogWorkflow.AIRFIELD_PRESET,
            FileDialogDirection.INPUT,
            path,
        )
        record = self.import_preset_path(path, error_title="Import Error")
        if record is not None:
            self._editing_id = record.preset.id
            self.refresh_preset_list(select_id=record.preset.id)

    def delete_preset(self) -> None:
        preset_id, record = self.selected_preset_record()
        if preset_id is None or record is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete transposition preset?",
            f'Delete "{record.preset.name}" from managed presets?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.preset_repository.delete(preset_id)
        except PresetError as error:
            QMessageBox.critical(self, "Delete Error", str(error))
            return
        self.presets = self.preset_repository.load_all()
        self._new_preset()
        self.refresh_preset_list()


__all__ = [
    "AirfieldCard",
    "AirfieldFormValues",
    "AirfieldPresetManagerDialog",
    "confirm_nonstandard_runways",
    "format_optional_number",
]
