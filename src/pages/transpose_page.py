from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import math
import os
import hashlib
import json
from pathlib import Path
from uuid import UUID

from PyQt6.QtCore import QSettings, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
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
    QRadioButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QStackedWidget,
    QToolButton,
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
    format_optional_number,
    friendly_altitude_mode,
)
from pages.coordinate_input import CoordinatePairInput
from pages.info_popup import PersistentInfoPopupController
from pages.unit_fields import MetreFeetFieldPair
from resource_paths import app_data_path, resource_path
from services import (
    AlignmentMethod,
    AlignmentProfile,
    AlignmentProfileStore,
    AlignmentProfileStoreError,
    AirfieldPresetError,
    CoordinateInputError,
    ManualTranspositionAlignment,
    OriginalTracePresetSection,
    PresetImportExportService,
    PresetError,
    PresetRecord,
    PresetRepository,
    PresetType,
    PreviewScene,
    PreviewTargetSnapshot,
    PreparedTranspositionFile,
    RunwayCandidate,
    RunwayInferenceResult,
    RunwayPresetSection,
    RunwayReference,
    RunwayTranspositionAlignment,
    SourceFileFingerprint,
    TargetTracePresetSection,
    TraceAdjustment,
    TranspositionPresetData,
    create_transposition_plan,
    customize_transposition_plan,
    export_prepared_transposition,
    infer_departure_runway,
    parse_coordinate_pair,
    parse_kml_track,
    prepare_transposition,
    fingerprint_source_file,
)


_INPUT_FILES_SETTING = "transpose/input-files"
_CURRENT_INPUT_FILE_SETTING = "transpose/current-input-file"
PAGE_STYLE = """
TransposePage {
    background: palette(window);
}
TransposePage QFrame#workspacePanel,
TransposePage QFrame#airfieldCard,
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
TransposePage QFrame#alignmentModeControl {
    background: palette(alternate-base);
    border: 1px solid palette(mid);
    border-radius: 9px;
}
TransposePage QRadioButton#alignmentModeSegment {
    min-height: 30px;
    background: transparent;
    color: palette(window-text);
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 2px 14px;
    spacing: 0;
    font-weight: 500;
}
TransposePage QRadioButton#alignmentModeSegment::indicator {
    width: 0;
    height: 0;
    margin: 0;
    padding: 0;
    image: none;
}
TransposePage QRadioButton#alignmentModeSegment:hover:!checked {
    background: palette(base);
}
TransposePage QRadioButton#alignmentModeSegment:checked {
    background: palette(highlight);
    color: palette(highlighted-text);
    border-color: palette(accent);
    font-weight: 650;
}
TransposePage QRadioButton#alignmentModeSegment:focus {
    border: 2px solid palette(accent);
}
TransposePage QRadioButton#alignmentModeSegment:disabled {
    background: transparent;
    color: palette(mid);
    border-color: transparent;
}
TransposePage QFrame#previewOffsetSummary {
    background: palette(alternate-base);
    border: 1px solid palette(mid);
    border-radius: 8px;
}
"""


class PreviewOffsetSummary(QFrame):
    """Compact read-only summary of one KML's committed preview offsets."""

    clear_requested = pyqtSignal()
    restore_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("previewOffsetSummary")
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 9, 12, 9)
        root.setSpacing(4)

        heading = QHBoxLayout()
        heading.setSpacing(8)
        title = QLabel("Preview offsets")
        title.setObjectName("statusBadge")
        heading.addWidget(title)
        self.status_dot = QLabel()
        self.status_dot.setObjectName("previewOffsetStatusDot")
        self.status_dot.setFixedSize(10, 10)
        heading.addWidget(self.status_dot)
        self.status_tooltip_button = QToolButton()
        self.status_tooltip_button.setObjectName("previewOffsetTooltipButton")
        self.status_tooltip_button.setAutoRaise(True)
        self.status_tooltip_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        set_button_icon(self.status_tooltip_button, AppIcon.INFO_CIRCLE)
        self.status_popup = PersistentInfoPopupController(
            self.status_tooltip_button
        )
        heading.addWidget(self.status_tooltip_button)
        heading.addStretch()
        self.restore_button = QPushButton("Restore original")
        self.restore_button.setAccessibleName("Restore original preview target inputs")
        self.restore_button.clicked.connect(self.restore_requested)
        heading.addWidget(self.restore_button)
        self.clear_button = QPushButton("Clear offsets")
        self.clear_button.setAccessibleName("Clear preview offsets")
        self.clear_button.clicked.connect(self.clear_requested)
        heading.addWidget(self.clear_button)
        root.addLayout(heading)

        self.values_label = QLabel()
        self.values_label.setAccessibleName("Committed preview offset values")
        self.values_label.setTextFormat(Qt.TextFormat.PlainText)
        self.values_label.setWordWrap(True)
        root.addWidget(self.values_label)

    def set_adjustment(
        self,
        adjustment: TraceAdjustment | None,
        *,
        active: bool,
        file_selected: bool = True,
        mismatch_tooltip: str = "",
        restore_available: bool = False,
    ) -> None:
        self.status_popup.close()
        has_adjustment = adjustment is not None and not adjustment.is_zero
        if not has_adjustment:
            self._set_status_dot(
                "none",
                "No Preview Offset",
                "No Preview Offset: No preview offset is currently applied.",
            )
        elif active:
            self._set_status_dot(
                "active",
                "Preview Offset Active",
                "Preview Offset Active: The preview offset is currently active.",
            )
        else:
            self._set_status_dot(
                "mismatch",
                "Preview Mismatch",
                mismatch_tooltip,
            )
        if not file_selected:
            self.values_label.setText("Select a KML file to review preview offsets.")
            self.restore_button.hide()
            self.clear_button.hide()
            return
        if not has_adjustment:
            self.values_label.setText("No preview offsets applied.")
            self.restore_button.hide()
            self.clear_button.hide()
            return

        self.values_label.setText(
            f"East {adjustment.east_m:+.1f} m · "
            f"North {adjustment.north_m:+.1f} m · "
            f"Up {adjustment.up_m:+.1f} m · "
            f"Yaw {adjustment.yaw_deg:+.1f}° clockwise"
        )
        self.restore_button.setEnabled(restore_available)
        self.restore_button.setToolTip(
            ""
            if restore_available
            else (
                "Restore original is unavailable for preview offsets saved by an "
                "older version. Accept the preview offsets again to capture the "
                "original target inputs."
            )
        )
        self.restore_button.show()
        self.clear_button.show()

    def _set_status_dot(self, state: str, name: str, tooltip: str) -> None:
        self.status_dot.setProperty("offsetState", state)
        self.status_dot.setAccessibleName(name)
        self.status_dot.setAccessibleDescription(tooltip)
        self.status_dot.setToolTip("")
        self.status_tooltip_button.setAccessibleName(f"{name} details")
        self.status_tooltip_button.setAccessibleDescription(tooltip)
        self.status_tooltip_button.setToolTip(tooltip)
        self.status_popup.set_text(tooltip)
        style = self.status_dot.style()
        style.unpolish(self.status_dot)
        style.polish(self.status_dot)
        self.status_dot.update()

@dataclass(slots=True)
class SourceAirfieldState:
    path: Path
    method: AlignmentMethod = AlignmentMethod.RUNWAY
    values: AirfieldFormValues = AirfieldFormValues()
    auto_values: AirfieldFormValues | None = None
    source_overridden: bool = False
    runway_target_values: AirfieldFormValues = AirfieldFormValues()
    manual_target_coordinate: str = ""
    manual_rotation_deg: str = "0"
    manual_ground_elevation_m: str = ""
    provenance: str = "Needs input"
    analysed: bool = False
    altitude_mode: str | None = None
    source_coordinate: str = ""
    source_altitude: str = ""
    inference: RunwayInferenceResult | None = None
    fingerprint: SourceFileFingerprint | None = None
    persistence_notice: str = ""
    preview_signature: str | None = None
    preview_adjustment: TraceAdjustment | None = None
    preview_target_snapshot: PreviewTargetSnapshot | None = None
    parse_error: str | None = None
    transposition_error: str | None = None
    transposition_error_correctable: bool = False
    target_error: str | None = None
    manual_source_error: str | None = None
    manual_target_error: str | None = None
    details: str = ""
    source_runway_preset_id: str | None = None
    target_runway_preset_id: str | None = None
    original_trace_preset_id: str | None = None
    target_trace_preset_id: str | None = None


def _airfield_values_to_draft(values: AirfieldFormValues) -> dict[str, str]:
    return {
        "airfieldName": values.airfield_name,
        "runway": values.runway,
        "threshold": values.threshold,
        "trueHeading": values.true_heading,
        "elevationM": values.elevation_m,
    }


def _airfield_values_from_draft(draft: Mapping[str, str]) -> AirfieldFormValues:
    return AirfieldFormValues(
        airfield_name=draft["airfieldName"],
        runway=draft["runway"],
        threshold=draft["threshold"],
        true_heading=draft["trueHeading"],
        elevation_m=draft["elevationM"],
    )


class OriginalTraceCard(QFrame):
    """Read-only source anchor plus absolute-altitude ground reference."""

    user_edited = pyqtSignal()
    preset_apply_requested = pyqtSignal()
    preset_save_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("airfieldCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title = QLabel("Original trace")
        title.setObjectName("cardTitle")
        subtitle = QLabel(
            "The first KML coordinate is the fixed source anchor for manual alignment."
        )
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        self.preset_label = QLabel("Preset name")
        root.addWidget(self.preset_label)
        self.preset_combo = QComboBox()
        self.preset_combo.setAccessibleName("Original trace preset name")
        self.preset_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.preset_combo.setMinimumContentsLength(4)
        self.preset_combo.activated.connect(self._apply_activated_preset)
        self.save_preset_btn = QPushButton("Save preset")
        self.save_preset_btn.setAccessibleName(
            "Save Original trace inputs as preset"
        )
        self.save_preset_btn.clicked.connect(self.preset_save_requested)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(self.save_preset_btn)
        root.addLayout(preset_row)

        fields = QGridLayout()
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(7)
        fields.setColumnStretch(1, 1)
        self.coordinate_output = QLineEdit()
        self.coordinate_output.setReadOnly(True)
        self.coordinate_output.setAccessibleName("Original trace source coordinates")
        self.altitude_output = QLineEdit()
        self.altitude_output.setReadOnly(True)
        self.altitude_output.setAccessibleName("Original trace first-coordinate altitude")
        self.altitude_mode_output = QLineEdit()
        self.altitude_mode_output.setReadOnly(True)
        self.altitude_mode_output.setAccessibleName("Original trace KML altitude mode")
        fields.addWidget(QLabel("Source coordinates"), 0, 0)
        fields.addWidget(self.coordinate_output, 0, 1)
        fields.addWidget(QLabel("Source altitude from file"), 1, 0)
        fields.addWidget(self.altitude_output, 1, 1)
        self.altitude_mode_label = QLabel("Altitude mode")
        fields.addWidget(self.altitude_mode_label, 2, 0)
        fields.addWidget(self.altitude_mode_output, 2, 1)

        self.ground_label = QLabel("Ground elevation (m / ft)")
        ground_row = QHBoxLayout()
        self.ground_m_input = QLineEdit()
        self.ground_m_input.setPlaceholderText("metres")
        self.ground_m_input.setAccessibleName("Manual ground reference elevation metres")
        self.ground_ft_input = QLineEdit()
        self.ground_ft_input.setPlaceholderText("feet")
        self.ground_ft_input.setAccessibleName("Manual ground reference elevation feet")
        ground_row.addWidget(self.ground_m_input)
        ground_row.addWidget(self.ground_ft_input)
        fields.addWidget(self.ground_label, 3, 0)
        fields.addLayout(ground_row, 3, 1)
        root.addLayout(fields)

        self.ground_help = QLabel(
            "Required for absolute KML. The source altitude above is the aircraft altitude, not ground elevation."
        )
        self.ground_help.setObjectName("mutedText")
        self.ground_help.setWordWrap(True)
        root.addWidget(self.ground_help)
        self.error_label = QLabel()
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        self.error_label.setAccessibleName("Original trace validation error")
        root.addWidget(self.error_label)
        root.addStretch()

        self.ground_units = MetreFeetFieldPair(
            self.ground_m_input,
            self.ground_ft_input,
            on_metres_changed=self.user_edited.emit,
        )

    def set_source(
        self,
        *,
        coordinate: str,
        altitude: str,
        altitude_mode: str | None,
        ground_elevation_m: str,
        enabled: bool,
    ) -> None:
        self.coordinate_output.setText(coordinate)
        self.altitude_output.setText(altitude)
        self.altitude_mode_output.setText(friendly_altitude_mode(altitude_mode))
        self.ground_units.set_metres_text(
            ground_elevation_m,
            notify_dependents=False,
        )
        requires_ground = altitude_mode == "absolute"
        self.ground_label.setVisible(requires_ground)
        self.ground_m_input.setVisible(requires_ground)
        self.ground_ft_input.setVisible(requires_ground)
        self.ground_help.setVisible(requires_ground)
        self.ground_m_input.setEnabled(enabled and requires_ground)
        self.ground_ft_input.setEnabled(enabled and requires_ground)
        self.preset_combo.setEnabled(enabled and requires_ground)
        self.save_preset_btn.setEnabled(enabled and requires_ground)

    def set_error(self, message: str) -> None:
        self.error_label.setText(message)

    def _apply_activated_preset(self, index: int) -> None:
        if self.preset_combo.itemData(index, Qt.ItemDataRole.UserRole) is not None:
            self.preset_apply_requested.emit()


class TargetTraceCard(QFrame):
    """Manual destination anchor and clockwise trace rotation."""

    user_edited = pyqtSignal()
    preset_apply_requested = pyqtSignal()
    preset_save_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("airfieldCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title = QLabel("Target trace")
        title.setObjectName("cardTitle")
        subtitle = QLabel(
            "Move the source anchor here, then rotate the complete trace clockwise."
        )
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        self.preset_label = QLabel("Preset name")
        root.addWidget(self.preset_label)
        self.preset_combo = QComboBox()
        self.preset_combo.setAccessibleName("Target trace preset name")
        self.preset_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.preset_combo.setMinimumContentsLength(4)
        self.preset_combo.activated.connect(self._apply_activated_preset)
        self.save_preset_btn = QPushButton("Save preset")
        self.save_preset_btn.setAccessibleName("Save Target trace inputs as preset")
        self.save_preset_btn.clicked.connect(self.preset_save_requested)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(self.save_preset_btn)
        root.addLayout(preset_row)

        fields = QGridLayout()
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(7)
        fields.setColumnStretch(1, 1)
        self.coordinate_input = CoordinatePairInput("Target trace coordinates")
        self.rotation_input = QLineEdit("0")
        self.rotation_input.setPlaceholderText("0–360° clockwise")
        self.rotation_input.setAccessibleName("Target trace clockwise rotation degrees")
        fields.addWidget(QLabel("Target coordinates"), 0, 0)
        fields.addWidget(self.coordinate_input, 0, 1)
        fields.addWidget(QLabel("Clockwise rotation"), 1, 0)
        fields.addWidget(self.rotation_input, 1, 1)
        root.addLayout(fields)

        self.error_label = QLabel()
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        self.error_label.setAccessibleName("Target trace validation error")
        root.addWidget(self.error_label)
        root.addStretch()

        self.coordinate_input.textEdited.connect(self.user_edited)
        self.rotation_input.textEdited.connect(self.user_edited)

    def set_values(self, coordinate: str, rotation: str, *, enabled: bool) -> None:
        self.coordinate_input.setText(coordinate)
        self.rotation_input.setText(rotation)
        self.coordinate_input.setEnabled(enabled)
        self.rotation_input.setEnabled(enabled)
        self.preset_combo.setEnabled(enabled)
        self.save_preset_btn.setEnabled(enabled)

    def set_error(self, message: str) -> None:
        self.error_label.setText(message)

    def _apply_activated_preset(self, index: int) -> None:
        if self.preset_combo.itemData(index, Qt.ItemDataRole.UserRole) is not None:
            self.preset_apply_requested.emit()


class TranspositionInputDialog(QDialog):
    """Choose the loaded KML files included in one transposition attempt."""

    def __init__(self, input_files, initially_selected=(), parent=None):
        super().__init__(parent)
        self.selected_paths: tuple[str, ...] = ()
        selected = {str(Path(path).resolve(strict=False)) for path in initially_selected}
        self.setWindowTitle("Choose Files to Transpose")
        self.resize(560, min(520, 180 + len(input_files) * 32))

        layout = QVBoxLayout(self)
        instruction = QLabel("Choose which loaded KML files to transpose.")
        instruction.setWordWrap(True)
        layout.addWidget(instruction)

        self.file_list = QListWidget()
        self.file_list.setAccessibleName("Files to transpose")
        for raw_path in input_files:
            path = str(Path(raw_path).resolve(strict=False))
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(
                Qt.CheckState.Checked
                if path in selected
                else Qt.CheckState.Unchecked
            )
            self.file_list.addItem(item)
        self.file_list.itemChanged.connect(self._selection_changed)
        layout.addWidget(self.file_list)

        selection_actions = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.select_none_button = QPushButton("Select none")
        self.select_all_button.clicked.connect(
            lambda: self._set_all(Qt.CheckState.Checked)
        )
        self.select_none_button.clicked.connect(
            lambda: self._set_all(Qt.CheckState.Unchecked)
        )
        selection_actions.addWidget(self.select_all_button)
        selection_actions.addWidget(self.select_none_button)
        selection_actions.addStretch()
        layout.addLayout(selection_actions)

        self.error_label = QLabel()
        self.error_label.setObjectName("errorText")
        self.error_label.setAccessibleName("Transposition file selection error")
        layout.addWidget(self.error_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.continue_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.continue_button.setText("Continue")
        self.buttons.accepted.connect(self._validate_and_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._selection_changed()

    def _checked_paths(self) -> tuple[str, ...]:
        return tuple(
            str(item.data(Qt.ItemDataRole.UserRole))
            for row in range(self.file_list.count())
            if (item := self.file_list.item(row)).checkState()
            == Qt.CheckState.Checked
        )

    def _selection_changed(self, _item=None) -> None:
        has_selection = bool(self._checked_paths())
        self.continue_button.setEnabled(has_selection)
        if has_selection:
            self.error_label.clear()

    def _set_all(self, state: Qt.CheckState) -> None:
        for row in range(self.file_list.count()):
            self.file_list.item(row).setCheckState(state)
        self._selection_changed()

    def _validate_and_accept(self) -> None:
        selected = self._checked_paths()
        if not selected:
            self.error_label.setText("Select at least one KML file to continue.")
            return
        self.selected_paths = selected
        self.accept()


class TranspositionOutputDialog(QDialog):
    """Edit and validate every output filename in a transposition batch."""

    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self._source_plan = plan
        self.validated_plan = None
        self.filename_edits = []
        self._screen_change_connected = False
        self.setWindowTitle("Choose Transposition Output Names")
        self.setSizeGripEnabled(True)

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

        self.table = QTableWidget(len(plan.jobs), 2, self)
        self.table.setHorizontalHeaderLabels(("Input KML", "Output filename"))
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)
        for row, job in enumerate(plan.jobs):
            input_item = QTableWidgetItem(job.input_path.name)
            input_item.setToolTip(str(job.input_path))
            input_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            self.table.setItem(row, 0, input_item)

            filename_edit = QLineEdit(job.output_path.name)
            filename_edit.setAccessibleName(
                f"Output filename for {job.input_path.name}"
            )
            self.table.setCellWidget(row, 1, filename_edit)
            self.filename_edits.append(filename_edit)
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table, 1)

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

        self._size_table_rows()
        self._fit_to_available_screen()

    def _size_table_rows(self) -> None:
        """Keep embedded editors fully visible at the active font and DPI."""
        header = self.table.verticalHeader()
        default_height = header.defaultSectionSize()
        editor_margin = max(
            2,
            self.table.style().pixelMetric(
                QStyle.PixelMetric.PM_FocusFrameVMargin
            ),
        )
        for row, edit in enumerate(self.filename_edits):
            row_height = max(
                default_height,
                edit.sizeHint().height() + (2 * editor_margin),
            )
            self.table.setRowHeight(row, row_height)

    def _table_content_height(self) -> int:
        header_height = self.table.horizontalHeader().sizeHint().height()
        rows_height = sum(
            self.table.rowHeight(row) for row in range(self.table.rowCount())
        )
        return header_height + rows_height + (2 * self.table.frameWidth())

    def _fit_to_available_screen(self, *_args) -> None:
        """Choose a useful initial size without exceeding the current screen."""
        self._size_table_rows()
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        outer_margin = max(
            16,
            self.style().pixelMetric(QStyle.PixelMetric.PM_LayoutTopMargin),
        )
        maximum_width = max(1, available.width() - (2 * outer_margin))
        maximum_height = max(1, available.height() - (2 * outer_margin))

        self.layout().activate()
        non_table_height = max(
            0,
            self.sizeHint().height() - self.table.sizeHint().height(),
        )
        one_row_height = (
            self.table.horizontalHeader().sizeHint().height()
            + (self.table.rowHeight(0) if self.table.rowCount() else 0)
            + (2 * self.table.frameWidth())
        )
        table_height = min(
            self._table_content_height(),
            max(one_row_height, maximum_height - non_table_height),
        )
        self.table.setMinimumHeight(table_height)
        self.layout().activate()

        preferred_width = max(720, self.sizeHint().width())
        preferred_height = self.sizeHint().height()
        self.resize(
            min(preferred_width, maximum_width),
            min(preferred_height, maximum_height),
        )

    def showEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        super().showEvent(event)
        self._fit_to_available_screen()
        handle = self.windowHandle()
        if handle is not None and not self._screen_change_connected:
            handle.screenChanged.connect(self._fit_to_available_screen)
            self._screen_change_connected = True

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
    """Two-column workspace for reviewed runway-to-runway transposition."""

    preview_requested = pyqtSignal(object)

    def __init__(self, *, settings: QSettings | None = None):
        super().__init__()
        self.setObjectName("TransposePage")
        self.setStyleSheet(PAGE_STYLE)
        self.input_files: list[str] = []
        self.source_states: dict[str, SourceAirfieldState] = {}
        self._current_source_path: str | None = None
        self._rendering_source = False
        self._prepared_batch = None
        self._prepared_signature = None
        self._prepared_alignments = None
        self._prepared_target_snapshots = None
        self._accepted_signature = None
        self._last_transposition_selection: tuple[str, ...] | None = None
        self._pending_profile_paths: set[str] = set()
        self._profile_save_error = ""
        self._settings = settings if settings is not None else QSettings()
        self._restoring_input_session = False
        self.alignment_profile_store = AlignmentProfileStore(
            app_data_path("transposition-alignments")
        )
        self._profile_save_timer = QTimer(self)
        self._profile_save_timer.setSingleShot(True)
        self._profile_save_timer.setInterval(300)
        self._profile_save_timer.timeout.connect(self._flush_pending_profiles)

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
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes((235, 715))

        self._load_presets(show_issues=True)
        self._restore_input_session()
        if not self.input_files:
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

        self.manage_airfields_btn = QPushButton("Manage presets…")
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

        self.alignment_method_panel = QFrame()
        self.alignment_method_panel.setObjectName("workspacePanel")
        method_layout = QVBoxLayout(self.alignment_method_panel)
        method_layout.setContentsMargins(16, 12, 16, 12)
        method_layout.setSpacing(8)
        method_title = QLabel("Alignment mode")
        method_title.setObjectName("panelTitle")
        method_layout.addWidget(method_title)

        self.alignment_mode_control = QFrame()
        self.alignment_mode_control.setObjectName("alignmentModeControl")
        self.alignment_mode_control.setAccessibleName("Alignment mode")
        method_row = QHBoxLayout(self.alignment_mode_control)
        method_row.setContentsMargins(3, 3, 3, 3)
        method_row.setSpacing(0)
        self.runway_alignment_button = QRadioButton("Runway alignment")
        self.manual_alignment_button = QRadioButton("Manual alignment")
        for button in (
            self.runway_alignment_button,
            self.manual_alignment_button,
        ):
            button.setObjectName("alignmentModeSegment")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.alignment_method_group = QButtonGroup(self)
        self.alignment_method_group.setExclusive(True)
        self.alignment_method_group.addButton(self.runway_alignment_button)
        self.alignment_method_group.addButton(self.manual_alignment_button)
        self.runway_alignment_button.setChecked(True)
        self.runway_alignment_button.toggled.connect(
            self._alignment_method_changed
        )
        self.manual_alignment_button.toggled.connect(
            self._alignment_method_changed
        )
        method_row.addWidget(self.runway_alignment_button, 1)
        method_row.addWidget(self.manual_alignment_button, 1)
        method_layout.addWidget(self.alignment_mode_control)
        self.alignment_notice_label = QLabel()
        self.alignment_notice_label.setObjectName("warningText")
        self.alignment_notice_label.setWordWrap(True)
        self.alignment_notice_label.setAccessibleName("Alignment persistence notice")
        method_layout.addWidget(self.alignment_notice_label)
        layout.addWidget(self.alignment_method_panel)

        self.source_card = AirfieldCard(
            "Original airfield",
            "Threshold, heading, and elevation are inferred from the selected KML when possible.",
            include_elevation=True,
            show_inference=True,
            include_altitude_mode=True,
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
        self.source_card.preset_save_requested.connect(
            lambda: self._save_card_preset("sourceRunway")
        )
        self.target_card.user_edited.connect(self._target_form_edited)
        self.target_card.preset_apply_requested.connect(self._apply_target_preset)
        self.target_card.preset_save_requested.connect(
            lambda: self._save_card_preset("targetRunway")
        )
        (
            self.runway_offset_summary,
            self.runway_preview_btn,
            self.runway_run_btn,
        ) = self._add_target_actions(self.target_card)

        runway_page = QWidget()
        runway_layout = QHBoxLayout(runway_page)
        runway_layout.setContentsMargins(0, 0, 0, 0)
        runway_layout.setSpacing(12)
        runway_layout.addWidget(self.source_card, 1, Qt.AlignmentFlag.AlignTop)
        runway_layout.addWidget(self.target_card, 1, Qt.AlignmentFlag.AlignTop)

        self.original_trace_card = OriginalTraceCard()
        self.target_trace_card = TargetTraceCard()
        self.original_trace_card.user_edited.connect(self._manual_form_edited)
        self.original_trace_card.preset_apply_requested.connect(
            self._apply_original_trace_preset
        )
        self.original_trace_card.preset_save_requested.connect(
            lambda: self._save_card_preset("originalTrace")
        )
        self.target_trace_card.user_edited.connect(self._manual_form_edited)
        self.target_trace_card.preset_apply_requested.connect(
            self._apply_manual_target_preset
        )
        self.target_trace_card.preset_save_requested.connect(
            lambda: self._save_card_preset("targetTrace")
        )
        (
            self.manual_offset_summary,
            self.manual_preview_btn,
            self.manual_run_btn,
        ) = self._add_target_actions(self.target_trace_card)
        manual_page = QWidget()
        manual_layout = QHBoxLayout(manual_page)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.setSpacing(12)
        manual_layout.addWidget(
            self.original_trace_card,
            13,
            Qt.AlignmentFlag.AlignTop,
        )
        manual_layout.addWidget(
            self.target_trace_card,
            10,
            Qt.AlignmentFlag.AlignTop,
        )

        self.alignment_stack = QStackedWidget()
        self.alignment_stack.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.alignment_stack.addWidget(runway_page)
        self.alignment_stack.addWidget(manual_page)
        layout.addWidget(self.alignment_stack)

        # Preserve the long-standing aliases used by extensions and tests.
        self.preview_btn = self.runway_preview_btn
        self.run_btn = self.runway_run_btn
        layout.addStretch(1)
        self.splitter.addWidget(column)

        # Stable aliases for the runway geometry widgets.
        self.coordinate_input = self.target_card.coordinate_input
        self.heading_input = self.target_card.heading_input
        self.orig_height_input = self.source_card.elevation_m_input
        self.orig_height_ft_input = self.source_card.elevation_ft_input

    def _add_target_actions(
        self,
        card: QFrame,
    ) -> tuple[PreviewOffsetSummary, QPushButton, QPushButton]:
        card_layout = card.layout()
        if not isinstance(card_layout, QVBoxLayout):
            raise TypeError("Target cards require a vertical layout.")

        offset_summary = PreviewOffsetSummary()
        offset_summary.clear_requested.connect(self._clear_preview_offsets)
        offset_summary.restore_requested.connect(self._restore_preview_target)
        card_layout.addWidget(offset_summary)

        actions = QHBoxLayout()
        actions.addStretch()
        preview_button = QPushButton("View preview")
        set_button_icon(preview_button, AppIcon.MONITOR)
        preview_button.clicked.connect(self.open_preview)
        run_button = QPushButton("Transpose files")
        run_button.setObjectName("primaryButton")
        run_button.clicked.connect(self.run_transposition_ui)
        actions.addWidget(preview_button)
        actions.addWidget(run_button)
        card_layout.addLayout(actions)
        return offset_summary, preview_button, run_button

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
        for card in (
            self.source_card,
            self.target_card,
            self.original_trace_card,
            self.target_trace_card,
        ):
            card.preset_combo.clear()
            card.preset_combo.addItem("No preset selected", None)
            for record in sorted(
                self.presets.values(), key=lambda item: item.preset.name.casefold()
            ):
                card.preset_combo.addItem(record.preset.name, str(record.preset.id))
        if self._current_source_path is not None:
            state = self.source_states.get(self._current_source_path)
            if state is not None:
                self._render_preset_selections(state)

    def open_airfield_manager(self) -> None:
        dialog = AirfieldPresetManagerDialog(
            self.preset_repository,
            self.preset_transfer,
            self,
        )
        dialog.exec()
        self._load_presets()

    def _selected_preset(self, card: QWidget) -> PresetRecord | None:
        raw_id = card.preset_combo.currentData(Qt.ItemDataRole.UserRole)
        if not raw_id:
            card.set_error("Choose a preset first.")
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
    def _values_from_runway(runway: RunwayPresetSection) -> AirfieldFormValues:
        return AirfieldFormValues(
            threshold=(
                f"{format_optional_number(runway.threshold_latitude)}, "
                f"{format_optional_number(runway.threshold_longitude)}"
            ),
            true_heading=format_optional_number(runway.true_heading_deg),
            elevation_m=format_optional_number(runway.elevation_m, 2),
        )

    def _decode_selected_preset(
        self, card: QWidget
    ) -> tuple[PresetRecord, TranspositionPresetData, tuple[str, ...]] | None:
        record = self._selected_preset(card)
        if record is None:
            return None
        try:
            payload, warnings = TranspositionPresetData.from_mapping(
                record.preset.data
            )
        except AirfieldPresetError as error:
            card.set_error(str(error))
            return None
        return record, payload, warnings

    @staticmethod
    def _set_combo_preset(combo: QComboBox, preset_id: str | None) -> None:
        index = -1
        if preset_id:
            index = combo.findData(preset_id, Qt.ItemDataRole.UserRole)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _render_preset_selections(self, state: SourceAirfieldState) -> None:
        self._set_combo_preset(
            self.source_card.preset_combo, state.source_runway_preset_id
        )
        self._set_combo_preset(
            self.target_card.preset_combo, state.target_runway_preset_id
        )
        self._set_combo_preset(
            self.original_trace_card.preset_combo,
            state.original_trace_preset_id,
        )
        self._set_combo_preset(
            self.target_trace_card.preset_combo,
            state.target_trace_preset_id,
        )

    @staticmethod
    def _preset_id_for_role(state: SourceAirfieldState, role: str) -> str | None:
        return {
            "sourceRunway": state.source_runway_preset_id,
            "targetRunway": state.target_runway_preset_id,
            "originalTrace": state.original_trace_preset_id,
            "targetTrace": state.target_trace_preset_id,
        }[role]

    @staticmethod
    def _set_preset_id_for_role(
        state: SourceAirfieldState, role: str, preset_id: str | None
    ) -> None:
        attribute = {
            "sourceRunway": "source_runway_preset_id",
            "targetRunway": "target_runway_preset_id",
            "originalTrace": "original_trace_preset_id",
            "targetTrace": "target_trace_preset_id",
        }[role]
        setattr(state, attribute, preset_id)

    def _card_for_role(self, role: str) -> QWidget:
        return {
            "sourceRunway": self.source_card,
            "targetRunway": self.target_card,
            "originalTrace": self.original_trace_card,
            "targetTrace": self.target_trace_card,
        }[role]

    def _apply_source_preset(self) -> None:
        if self._current_source_path is None:
            self.source_card.set_error("Select an input KML file first.")
            return
        decoded = self._decode_selected_preset(self.source_card)
        if decoded is None:
            return
        record, payload, warnings = decoded
        if payload.runway is None:
            self.source_card.set_error(
                "This preset does not contain runway inputs."
            )
            return
        state = self.source_states[self._current_source_path]
        if state.altitude_mode == "absolute" and payload.runway.elevation_m is None:
            self.source_card.set_error(
                "This preset has no elevation for an absolute-altitude KML."
            )
            return
        state.values = self._values_from_runway(payload.runway)
        state.source_runway_preset_id = str(record.preset.id)
        state.source_overridden = True
        self._refresh_correctable_source_error(state)
        state.provenance = "Preset"
        preset_notes = "\n".join(warnings)
        if preset_notes:
            state.details = "\n\n".join(filter(None, (state.details, preset_notes)))
        self._sync_file_item_error(self._current_source_path)
        self._render_source_state(state)
        self._schedule_profile_save(immediate=True)

    def _apply_target_preset(self) -> None:
        if self._current_source_path is None:
            self.target_card.set_error("Select an input KML file first.")
            return
        decoded = self._decode_selected_preset(self.target_card)
        if decoded is None:
            return
        record, payload, warnings = decoded
        if payload.runway is None:
            self.target_card.set_error(
                "This preset does not contain runway inputs."
            )
            return
        values = self._values_from_runway(payload.runway)
        state = self.source_states[self._current_source_path]
        state.runway_target_values = AirfieldFormValues(
            threshold=values.threshold,
            true_heading=values.true_heading,
        )
        state.target_runway_preset_id = str(record.preset.id)
        self.target_card.set_values(state.runway_target_values)
        if warnings:
            self.target_card.set_error(" ".join(warnings))
        self._render_preview_offset_summaries(state)
        self._schedule_profile_save(immediate=True)

    def _apply_original_trace_preset(self) -> None:
        if self._current_source_path is None:
            self.original_trace_card.set_error("Select an input KML file first.")
            return
        decoded = self._decode_selected_preset(self.original_trace_card)
        if decoded is None:
            return
        record, payload, warnings = decoded
        if payload.original_trace is None:
            self.original_trace_card.set_error(
                "This preset does not contain Original trace inputs."
            )
            return
        state = self.source_states[self._current_source_path]
        state.manual_ground_elevation_m = format_optional_number(
            payload.original_trace.ground_elevation_m, 2
        )
        state.original_trace_preset_id = str(record.preset.id)
        self.original_trace_card.ground_units.set_metres_text(
            state.manual_ground_elevation_m,
            notify_dependents=False,
        )
        self.original_trace_card.set_error(" ".join(warnings))
        self._sync_file_item_error(self._current_source_path)
        self._render_preview_offset_summaries(state)
        self._schedule_profile_save(immediate=True)

    def _apply_manual_target_preset(self) -> None:
        if self._current_source_path is None:
            self.target_trace_card.set_error("Select an input KML file first.")
            return
        decoded = self._decode_selected_preset(self.target_trace_card)
        if decoded is None:
            return
        record, payload, warnings = decoded
        if payload.target_trace is None:
            self.target_trace_card.set_error(
                "This preset does not contain Target trace inputs."
            )
            return

        coordinate = (
            f"{format_optional_number(payload.target_trace.target_latitude)}, "
            f"{format_optional_number(payload.target_trace.target_longitude)}"
        )
        state = self.source_states[self._current_source_path]
        state.manual_target_coordinate = coordinate
        state.manual_rotation_deg = format_optional_number(
            payload.target_trace.rotation_deg, 2
        )
        state.target_trace_preset_id = str(record.preset.id)
        state.manual_target_error = None
        self.target_trace_card.coordinate_input.setText(coordinate)
        self.target_trace_card.rotation_input.setText(state.manual_rotation_deg)
        self.target_trace_card.set_error(" ".join(warnings))
        self._sync_file_item_error(self._current_source_path)
        self._render_preview_offset_summaries(state)
        self._schedule_profile_save(immediate=True)

    def _save_card_preset(self, role: str) -> None:
        if self._current_source_path is None:
            self._card_for_role(role).set_error("Select an input KML file first.")
            return
        self._commit_current_source()
        state = self.source_states[self._current_source_path]
        card = self._card_for_role(role)
        raw_id = card.preset_combo.currentData(Qt.ItemDataRole.UserRole)
        record: PresetRecord | None = None
        payload = TranspositionPresetData()
        if raw_id:
            try:
                record = self.presets.get(UUID(str(raw_id)))
            except ValueError:
                record = None
            if record is None:
                card.set_error("The selected preset is no longer available.")
                return
            try:
                payload, _ = TranspositionPresetData.from_mapping(
                    record.preset.data
                )
            except AirfieldPresetError as error:
                card.set_error(str(error))
                return

        try:
            if role in {"sourceRunway", "targetRunway"}:
                values = (
                    state.values
                    if role == "sourceRunway"
                    else state.runway_target_values
                )
                threshold = parse_coordinate_pair(values.threshold)
                elevation: object = values.elevation_m
                if role == "targetRunway" and payload.runway is not None:
                    elevation = payload.runway.elevation_m
                runway = RunwayPresetSection.validated(
                    threshold=threshold,
                    true_heading_deg=values.true_heading,
                    elevation_m=elevation,
                    elevation_required=(
                        role == "sourceRunway" and state.altitude_mode == "absolute"
                    ),
                )
                payload = payload.with_runway(runway)
            elif role == "originalTrace":
                if state.altitude_mode != "absolute":
                    raise AirfieldPresetError(
                        "Original trace has no editable ground elevation for this KML altitude mode."
                    )
                payload = payload.with_original_trace(
                    OriginalTracePresetSection.validated(
                        state.manual_ground_elevation_m
                    )
                )
            else:
                target = parse_coordinate_pair(state.manual_target_coordinate)
                payload = payload.with_target_trace(
                    TargetTracePresetSection.validated(
                        target=target,
                        rotation_deg=state.manual_rotation_deg,
                    )
                )
        except (AirfieldPresetError, CoordinateInputError) as error:
            card.set_error(str(error))
            return

        try:
            if record is None:
                name, accepted = QInputDialog.getText(
                    self,
                    "Save Transposition Preset",
                    "Preset name:",
                )
                if not accepted:
                    return
                saved = self.preset_repository.create(name, payload.to_mapping())
            else:
                answer = QMessageBox.question(
                    self,
                    "Update preset?",
                    f'Update "{record.preset.name}" with the current card inputs?',
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                saved = self.preset_repository.update_data(
                    record.preset.id,
                    payload.to_mapping(),
                )
        except PresetError as error:
            card.set_error(str(error))
            return

        self._load_presets()
        preset_id = str(saved.preset.id)
        self._set_preset_id_for_role(state, role, preset_id)
        self._render_preset_selections(state)
        card.set_error("")
        self._schedule_profile_save(immediate=True)

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

    def _persist_input_session(self) -> None:
        if self._restoring_input_session:
            return
        self._settings.setValue(_INPUT_FILES_SETTING, list(self.input_files))
        self._settings.setValue(
            _CURRENT_INPUT_FILE_SETTING,
            self._current_source_path or "",
        )
        self._settings.sync()

    def _restore_input_session(self) -> None:
        raw_files = self._settings.value(_INPUT_FILES_SETTING, [])
        if isinstance(raw_files, str):
            candidates = [raw_files]
        elif isinstance(raw_files, (list, tuple)):
            candidates = list(raw_files)
        else:
            candidates = []
        restored: list[str] = []
        seen: set[str] = set()
        for raw_path in candidates:
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            path = Path(raw_path).expanduser()
            if path.suffix.lower() != ".kml":
                continue
            resolved = str(path.resolve(strict=False))
            key = self._path_key(resolved)
            if key in seen:
                continue
            seen.add(key)
            restored.append(resolved)
        if not restored:
            self._persist_input_session()
            return

        current = self._settings.value(
            _CURRENT_INPUT_FILE_SETTING,
            "",
            type=str,
        )
        current_key = self._path_key(current) if current else None
        self._restoring_input_session = True
        try:
            self.add_files_to_list(restored)
            for path in restored:
                state = self.source_states[path]
                if not state.analysed:
                    self._analyse_source(state)
            if current_key is not None:
                for row in range(self.file_list.count()):
                    item = self.file_list.item(row)
                    if self._path_key(
                        str(item.data(Qt.ItemDataRole.UserRole))
                    ) == current_key:
                        self.file_list.setCurrentItem(item)
                        break
        finally:
            self._restoring_input_session = False
        self._persist_input_session()

    def _new_source_state(self, path: str | Path) -> SourceAirfieldState:
        resolved = Path(path).resolve(strict=False)
        state = SourceAirfieldState(resolved)
        try:
            state.fingerprint = fingerprint_source_file(resolved)
        except OSError:
            return state
        result = self.alignment_profile_store.load(resolved, state.fingerprint)
        if result.notice:
            state.persistence_notice = result.notice
        profile = result.profile
        if profile is None:
            return state
        state.method = profile.method
        state.runway_target_values = _airfield_values_from_draft(
            profile.runway_target
        )
        if profile.runway_source_override is not None:
            state.values = _airfield_values_from_draft(
                profile.runway_source_override
            )
            state.source_overridden = True
            state.provenance = "Manual override"
        state.manual_target_coordinate = profile.manual["targetCoordinate"]
        state.manual_rotation_deg = profile.manual["rotationDeg"]
        state.manual_ground_elevation_m = profile.manual["groundElevationM"]
        selections = profile.preset_selections
        state.source_runway_preset_id = selections["sourceRunway"]
        state.target_runway_preset_id = selections["targetRunway"]
        state.original_trace_preset_id = selections["originalTrace"]
        state.target_trace_preset_id = selections["targetTrace"]
        state.preview_signature = profile.preview_signature
        state.preview_adjustment = profile.preview_adjustment
        state.preview_target_snapshot = profile.preview_target_snapshot
        return state

    def _profile_for_state(self, state: SourceAirfieldState) -> AlignmentProfile:
        preview_complete = (
            state.preview_signature is not None
            and state.preview_adjustment is not None
        )
        return AlignmentProfile(
            method=state.method,
            runway_source_override=(
                _airfield_values_to_draft(state.values)
                if state.source_overridden
                else None
            ),
            runway_target=_airfield_values_to_draft(
                state.runway_target_values
            ),
            manual={
                "targetCoordinate": state.manual_target_coordinate,
                "rotationDeg": state.manual_rotation_deg,
                "groundElevationM": state.manual_ground_elevation_m,
            },
            preset_selections={
                "sourceRunway": state.source_runway_preset_id,
                "targetRunway": state.target_runway_preset_id,
                "originalTrace": state.original_trace_preset_id,
                "targetTrace": state.target_trace_preset_id,
            },
            preview_signature=(
                state.preview_signature if preview_complete else None
            ),
            preview_adjustment=(
                state.preview_adjustment if preview_complete else None
            ),
            preview_target_snapshot=(
                state.preview_target_snapshot if preview_complete else None
            ),
        )

    def _schedule_profile_save(
        self,
        path: str | Path | None = None,
        *,
        immediate: bool = False,
    ) -> None:
        selected = path or self._current_source_path
        if selected is None:
            return
        resolved = str(Path(selected).resolve(strict=False))
        self._pending_profile_paths.add(resolved)
        if immediate:
            self._flush_pending_profiles()
        else:
            self._profile_save_timer.start()

    def _flush_pending_profiles(self) -> None:
        if self._profile_save_timer.isActive():
            self._profile_save_timer.stop()
        pending = tuple(self._pending_profile_paths)
        self._pending_profile_paths.clear()
        for path in pending:
            state = self.source_states.get(path)
            if state is None:
                continue
            fingerprint = state.fingerprint
            if fingerprint is None:
                try:
                    fingerprint = fingerprint_source_file(state.path)
                    state.fingerprint = fingerprint
                except OSError as error:
                    self._profile_save_error = (
                        f"Alignment settings could not be saved: {error}"
                    )
                    continue
            try:
                self.alignment_profile_store.save(
                    state.path,
                    fingerprint,
                    self._profile_for_state(state),
                )
            except (AlignmentProfileStoreError, TypeError, ValueError) as error:
                self._profile_save_error = str(error)
            else:
                self._profile_save_error = ""
                state.persistence_notice = ""
        self._render_alignment_notice()

    def add_files_to_list(self, files) -> None:
        existing = {self._path_key(path) for path in self.input_files}
        first_new: QListWidgetItem | None = None
        for raw_path in files:
            path = str(Path(raw_path).resolve(strict=False))
            if Path(path).suffix.lower() != ".kml":
                continue
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
            self.source_states[path] = self._new_source_state(path)
            existing.add(key)
            if first_new is None:
                first_new = item
        self._update_file_count()
        if self.file_list.currentItem() is None and first_new is not None:
            self.file_list.setCurrentItem(first_new)
        self._persist_input_session()

    def remove_selected_files(self) -> None:
        self._commit_current_source()
        self._flush_pending_profiles()
        items = list(self.file_list.selectedItems())
        for item in items:
            path = str(item.data(Qt.ItemDataRole.UserRole))
            self.source_states.pop(path, None)
            if path in self.input_files:
                self.input_files.remove(path)
            self.file_list.takeItem(self.file_list.row(item))
        if self._last_transposition_selection is not None:
            remaining = set(self.input_files)
            self._last_transposition_selection = tuple(
                path
                for path in self._last_transposition_selection
                if path in remaining
            )
        self._update_file_count()
        if self.file_list.currentItem() is None and self.file_list.count():
            self.file_list.setCurrentRow(0)
        elif not self.file_list.count():
            self._current_source_path = None
            self._render_source_state(None)
        self._persist_input_session()

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
        self._flush_pending_profiles()
        self._current_source_path = (
            str(current.data(Qt.ItemDataRole.UserRole)) if current is not None else None
        )
        if self._current_source_path is None:
            self._render_source_state(None)
            self._persist_input_session()
            return
        state = self.source_states.get(self._current_source_path)
        if state is None:
            state = self._new_source_state(self._current_source_path)
            self.source_states[self._current_source_path] = state
        if not state.analysed:
            self._analyse_source(state)
        self._render_source_state(state)
        self._persist_input_session()

    def _analyse_source(self, state: SourceAirfieldState) -> None:
        state.analysed = True
        state.parse_error = None
        try:
            track = parse_kml_track(state.path)
            state.altitude_mode = track.altitude_mode
            first_point = track.points[0]
            state.source_coordinate = (
                f"{format_optional_number(first_point.latitude)}, "
                f"{format_optional_number(first_point.longitude)}"
            )
            state.source_altitude = (
                "Not present"
                if first_point.altitude_m is None
                else f"{format_optional_number(first_point.altitude_m, 2)} m"
            )
            inference = infer_departure_runway(track)
            state.inference = inference
        except Exception as error:
            state.parse_error = str(error)
            state.transposition_error = None
            state.transposition_error_correctable = False
            state.provenance = "File error"
            state.details = str(error)
            self._sync_file_item_error(str(state.path))
            return

        candidate = inference.candidate
        warnings = list(inference.warnings)
        if candidate is None:
            state.provenance = (
                "Manual override" if state.source_overridden else "Needs input"
            )
            state.details = "\n".join(
                filter(None, (inference.error or "", *warnings))
            )
            self._sync_file_item_error(str(state.path))
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
        if state.source_overridden:
            state.provenance = "Manual override"
        else:
            state.values = auto_values
            state.provenance = "Auto-detected"
        evidence = ["Evidence:", *candidate.evidence]
        combined_warnings = list(dict.fromkeys((*warnings, *candidate.warnings)))
        if combined_warnings:
            evidence.extend(("", "Warnings:", *combined_warnings))
        state.details = "\n".join(evidence)
        self._sync_file_item_error(str(state.path))

    def _commit_current_source(self) -> None:
        if self._rendering_source or self._current_source_path is None:
            return
        state = self.source_states.get(self._current_source_path)
        if state is not None and state.parse_error is None:
            state.values = self.source_card.values()
            state.runway_target_values = self.target_card.values()
            state.manual_target_coordinate = (
                self.target_trace_card.coordinate_input.text()
            )
            state.manual_rotation_deg = self.target_trace_card.rotation_input.text()
            state.manual_ground_elevation_m = (
                self.original_trace_card.ground_m_input.text()
            )

    def _source_form_edited(self) -> None:
        if self._rendering_source or self._current_source_path is None:
            return
        state = self.source_states.get(self._current_source_path)
        if state is None or state.parse_error is not None:
            return
        state.values = self.source_card.values()
        state.source_overridden = True
        self._refresh_correctable_source_error(state)
        state.provenance = "Manual override"
        self.source_card.set_error(self._source_error(state))
        self._sync_file_item_error(self._current_source_path)
        self._render_source_status(state)
        self._render_preview_offset_summaries(state)
        self._schedule_profile_save()

    def _target_form_edited(self) -> None:
        if self._rendering_source or self._current_source_path is None:
            return
        state = self.source_states.get(self._current_source_path)
        if state is None or state.parse_error is not None:
            return
        state.runway_target_values = self.target_card.values()
        state.target_error = None
        self.target_card.set_error("")
        self._sync_file_item_error(self._current_source_path)
        self._render_preview_offset_summaries(state)
        self._schedule_profile_save()

    def _manual_form_edited(self) -> None:
        if self._rendering_source or self._current_source_path is None:
            return
        state = self.source_states.get(self._current_source_path)
        if state is None or state.parse_error is not None:
            return
        state.manual_target_coordinate = self.target_trace_card.coordinate_input.text()
        state.manual_rotation_deg = self.target_trace_card.rotation_input.text()
        state.manual_ground_elevation_m = (
            self.original_trace_card.ground_m_input.text()
        )
        state.manual_source_error = None
        state.manual_target_error = None
        self.original_trace_card.set_error("")
        self.target_trace_card.set_error("")
        self._sync_file_item_error(self._current_source_path)
        self._render_preview_offset_summaries(state)
        self._schedule_profile_save()

    def _alignment_method_changed(self, checked: bool) -> None:
        if not checked or self._rendering_source or self._current_source_path is None:
            return
        state = self.source_states.get(self._current_source_path)
        if state is None:
            return
        self._commit_current_source()
        state.method = (
            AlignmentMethod.MANUAL
            if self.manual_alignment_button.isChecked()
            else AlignmentMethod.RUNWAY
        )
        self._render_source_state(state)
        self._sync_file_item_error(self._current_source_path)
        self._schedule_profile_save(immediate=True)

    def _restore_auto_source(self) -> None:
        if self._current_source_path is None:
            return
        state = self.source_states.get(self._current_source_path)
        if state is None or state.auto_values is None:
            return
        state.values = state.auto_values
        state.source_overridden = False
        self._refresh_correctable_source_error(state)
        state.provenance = "Auto-detected"
        self._sync_file_item_error(self._current_source_path)
        self._render_source_state(state)
        self._schedule_profile_save(immediate=True)

    def _render_source_state(self, state: SourceAirfieldState | None) -> None:
        self._rendering_source = True
        try:
            if state is None:
                for card in (
                    self.source_card,
                    self.target_card,
                    self.original_trace_card,
                    self.target_trace_card,
                ):
                    self._set_combo_preset(card.preset_combo, None)
                self.source_card.clear_values()
                self.source_card.set_fields_enabled(False)
                self.source_card.set_status("Waiting for a KML file")
                self.source_card.set_error("")
                self.target_card.clear_values()
                self.target_card.set_fields_enabled(False)
                self.target_card.set_error("")
                self.original_trace_card.set_source(
                    coordinate="",
                    altitude="",
                    altitude_mode=None,
                    ground_elevation_m="",
                    enabled=False,
                )
                self.original_trace_card.set_error("")
                self.target_trace_card.set_values("", "0", enabled=False)
                self.target_trace_card.set_error("")
                self.runway_alignment_button.setChecked(True)
                self.runway_alignment_button.setEnabled(False)
                self.manual_alignment_button.setEnabled(False)
                self.alignment_stack.setCurrentIndex(0)
                self._render_preview_offset_summaries(None)
                self._render_alignment_notice()
                return
            self._render_preset_selections(state)
            self.source_card.set_values(state.values)
            self.source_card.set_altitude_mode(state.altitude_mode)
            self.target_card.set_values(state.runway_target_values)
            self.source_card.set_fields_enabled(state.parse_error is None)
            self.target_card.set_fields_enabled(state.parse_error is None)
            self.source_card.set_error(
                state.transposition_error or state.parse_error or ""
            )
            self.target_card.set_error(state.target_error or "")
            self.original_trace_card.set_source(
                coordinate=state.source_coordinate,
                altitude=state.source_altitude,
                altitude_mode=state.altitude_mode,
                ground_elevation_m=state.manual_ground_elevation_m,
                enabled=state.parse_error is None,
            )
            self.original_trace_card.set_error(
                state.manual_source_error
                or (
                    state.transposition_error
                    if state.method is AlignmentMethod.MANUAL
                    else None
                )
                or state.parse_error
                or ""
            )
            self.target_trace_card.set_values(
                state.manual_target_coordinate,
                state.manual_rotation_deg,
                enabled=state.parse_error is None,
            )
            self.target_trace_card.set_error(state.manual_target_error or "")
            self.runway_alignment_button.setEnabled(True)
            self.manual_alignment_button.setEnabled(True)
            self.runway_alignment_button.setChecked(
                state.method is AlignmentMethod.RUNWAY
            )
            self.manual_alignment_button.setChecked(
                state.method is AlignmentMethod.MANUAL
            )
            self.alignment_stack.setCurrentIndex(
                0 if state.method is AlignmentMethod.RUNWAY else 1
            )
            self._render_source_status(state)
            self._render_preview_offset_summaries(state)
            self._render_alignment_notice()
        finally:
            self._rendering_source = False

    def _render_preview_offset_summaries(
        self,
        state: SourceAirfieldState | None,
    ) -> None:
        adjustment = state.preview_adjustment if state is not None else None
        active = bool(
            state is not None
            and adjustment is not None
            and self._preview_adjustment_is_active(state)
        )
        mismatch_tooltip = (
            self._preview_mismatch_tooltip(state)
            if state is not None and adjustment is not None and not active
            else ""
        )
        for summary in (
            self.runway_offset_summary,
            self.manual_offset_summary,
        ):
            summary.set_adjustment(
                adjustment,
                active=active,
                file_selected=state is not None,
                mismatch_tooltip=mismatch_tooltip,
                restore_available=bool(
                    state is not None
                    and state.preview_target_snapshot is not None
                ),
            )

    def _preview_mismatch_tooltip(self, state: SourceAirfieldState) -> str:
        snapshot = state.preview_target_snapshot
        if snapshot is not None and state.method is not snapshot.method:
            return (
                "Preview Mismatch: The alignment mode no longer matches these preview "
                "offsets. Clear the offsets or restore the original target inputs and "
                "alignment mode."
            )
        if snapshot is not None and not self._preview_target_matches(state, snapshot):
            if snapshot.method is AlignmentMethod.RUNWAY:
                return (
                    "Preview Mismatch: The current target runway coordinates or true "
                    "heading no longer match these preview offsets. Clear the offsets "
                    "or restore the original runway values."
                )
            return (
                "Preview Mismatch: The current target trace coordinates or clockwise "
                "rotation no longer match these preview offsets. Clear the offsets or "
                "restore the original manual target values."
            )
        if state.fingerprint != self._source_fingerprint(state.path):
            return (
                "Preview Mismatch: The source KML has changed, so these preview offsets "
                "are not active. Clear the offsets or restore the original source file."
            )
        if snapshot is None:
            return (
                "Preview Mismatch: The alignment inputs or source file no longer match "
                "these preview offsets. Clear the offsets, or accept the preview again "
                "to capture restorable target inputs."
            )
        return (
            "Preview Mismatch: Source-side alignment inputs no longer match these "
            "preview offsets. Restore original changes target inputs only; restore the "
            "source-side inputs or clear the offsets."
        )

    @staticmethod
    def _preview_target_matches(
        state: SourceAirfieldState,
        snapshot: PreviewTargetSnapshot,
    ) -> bool:
        if state.method is not snapshot.method:
            return False
        try:
            saved_coordinate = parse_coordinate_pair(snapshot.coordinate)
            if snapshot.method is AlignmentMethod.RUNWAY:
                current_coordinate = parse_coordinate_pair(
                    state.runway_target_values.threshold
                )
                current_direction = float(state.runway_target_values.true_heading)
                saved_direction = float(snapshot.true_heading)
            else:
                current_coordinate = parse_coordinate_pair(
                    state.manual_target_coordinate
                )
                current_direction = float(state.manual_rotation_deg)
                saved_direction = float(snapshot.clockwise_rotation)
            if not all(math.isfinite(value) for value in (current_direction, saved_direction)):
                return False
        except (CoordinateInputError, TypeError, ValueError):
            return False
        return (
            current_coordinate.latitude == saved_coordinate.latitude
            and current_coordinate.longitude == saved_coordinate.longitude
            and current_direction % 360.0 == saved_direction % 360.0
        )

    def _invalidate_prepared_preview(self) -> None:
        self._prepared_batch = None
        self._prepared_signature = None
        self._prepared_alignments = None
        self._prepared_target_snapshots = None
        self._accepted_signature = None

    def _clear_preview_offsets(self) -> None:
        if self._current_source_path is None:
            return
        state = self.source_states.get(self._current_source_path)
        if state is None:
            return
        state.preview_signature = None
        state.preview_adjustment = None
        state.preview_target_snapshot = None
        self._invalidate_prepared_preview()
        self._render_preview_offset_summaries(state)
        self._schedule_profile_save(self._current_source_path, immediate=True)

    def _restore_preview_target(self) -> None:
        if self._current_source_path is None:
            return
        state = self.source_states.get(self._current_source_path)
        if state is None or state.preview_target_snapshot is None:
            return
        snapshot = state.preview_target_snapshot
        state.method = snapshot.method
        if snapshot.method is AlignmentMethod.RUNWAY:
            state.runway_target_values = replace(
                state.runway_target_values,
                threshold=snapshot.coordinate,
                true_heading=snapshot.true_heading or "",
            )
            state.target_error = None
        else:
            state.manual_target_coordinate = snapshot.coordinate
            state.manual_rotation_deg = snapshot.clockwise_rotation or ""
            state.manual_target_error = None
        self._invalidate_prepared_preview()
        self._render_source_state(state)
        self._sync_file_item_error(self._current_source_path)
        self._schedule_profile_save(self._current_source_path, immediate=True)

    @staticmethod
    def _source_error(state: SourceAirfieldState) -> str:
        messages = (
            state.parse_error,
            state.transposition_error,
            state.target_error,
            state.manual_source_error,
            state.manual_target_error,
        )
        return " ".join(dict.fromkeys(message for message in messages if message))

    def _render_alignment_notice(self) -> None:
        state = (
            self.source_states.get(self._current_source_path)
            if self._current_source_path is not None
            else None
        )
        notices = []
        if state is not None and state.persistence_notice:
            notices.append(state.persistence_notice)
        if self._profile_save_error:
            notices.append(self._profile_save_error)
        self.alignment_notice_label.setText("\n".join(notices))

    def _file_item_for_path(self, path: str | Path) -> QListWidgetItem | None:
        key = self._path_key(path)
        for row in range(self.file_list.count()):
            item = self.file_list.item(row)
            if self._path_key(str(item.data(Qt.ItemDataRole.UserRole))) == key:
                return item
        return None

    def _sync_file_item_error(self, path: str | Path) -> None:
        item = self._file_item_for_path(path)
        state = self.source_states.get(str(Path(path).resolve(strict=False)))
        if item is None or state is None:
            return
        message = self._source_error(state)
        if message:
            item.setIcon(
                self.style().standardIcon(
                    QStyle.StandardPixmap.SP_MessageBoxWarning
                )
            )
            item.setToolTip(f"{state.path}\n\nError: {message}")
            accessible = f"Full path: {state.path}. Error: {message}"
        else:
            item.setIcon(QIcon())
            item.setToolTip(str(state.path))
            accessible = f"Full path: {state.path}"
        item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, accessible)

    def _set_transposition_error(
        self,
        path: str | Path,
        message: str,
        *,
        correctable: bool = False,
    ) -> None:
        resolved = str(Path(path).resolve(strict=False))
        state = self.source_states.get(resolved)
        if state is None:
            return
        state.transposition_error = str(message).strip()
        state.transposition_error_correctable = correctable
        self._sync_file_item_error(resolved)

    def _clear_transposition_error(self, path: str | Path) -> None:
        resolved = str(Path(path).resolve(strict=False))
        state = self.source_states.get(resolved)
        if state is None:
            return
        state.transposition_error = None
        state.transposition_error_correctable = False
        self._sync_file_item_error(resolved)

    def _refresh_correctable_source_error(self, state: SourceAirfieldState) -> None:
        if not state.transposition_error_correctable:
            return
        try:
            self._reference_from_values(
                state.values,
                label=state.path.name,
                elevation_required=state.altitude_mode == "absolute",
            )
        except ValueError as error:
            state.transposition_error = str(error)
            return
        state.transposition_error = None
        state.transposition_error_correctable = False

    def _render_source_status(self, state: SourceAirfieldState) -> None:
        candidate = state.inference.candidate if state.inference is not None else None
        detection_state = None
        detection_description = ""
        details = state.details
        can_restore = bool(
            candidate is not None
            and state.auto_values is not None
            and state.provenance != "Auto-detected"
        )
        if candidate is not None and can_restore:
            detection_state = "inactive"
            detection_description = (
                "Auto-detected runway confidence is inactive because the displayed "
                "runway values have been overridden."
            )
            details = ""
        elif candidate is not None:
            assessment = candidate.detection_assessment
            if assessment is not None:
                detection_state = (
                    "moderate"
                    if assessment.rating.value == "medium"
                    else assessment.rating.value
                )
                rating = (
                    "Moderate"
                    if assessment.rating.value == "medium"
                    else assessment.rating.value.title()
                )
                detection_description = (
                    f"Runway detection: {rating} Confidence, "
                    f"{assessment.overall_percent} percent. Weakest detection "
                    f"signal: {assessment.weakest_signal.name}, "
                    f"{assessment.weakest_signal.percent} percent."
                )
                details = self._runway_detection_tooltip(candidate, state.details)
            else:
                legacy_rating = (
                    "Moderate"
                    if candidate.confidence.value == "medium"
                    else candidate.confidence.value.title()
                )
                detection_state = (
                    "moderate"
                    if candidate.confidence.value == "medium"
                    else candidate.confidence.value
                )
                detection_description = (
                    f"Runway detection: {legacy_rating} Confidence."
                )
        self.source_card.set_status(
            state.provenance,
            details=details,
            detection_state=detection_state,
            detection_description=detection_description,
            can_restore=can_restore,
        )

    @staticmethod
    def _runway_detection_tooltip(
        candidate: RunwayCandidate,
        inference_details: str,
    ) -> str:
        assessment = candidate.detection_assessment
        if assessment is None:
            return inference_details
        rating = (
            "Moderate"
            if assessment.rating.value == "medium"
            else assessment.rating.value.title()
        )
        lines = [
            "Runway Detection",
            f"{rating} Confidence — {assessment.overall_percent}%",
            "",
            "Detection Criteria:",
            f"Heading detection — {assessment.heading_percent}%",
            (
                f"Threshold detection — {assessment.threshold_percent}% "
                f"({assessment.signals[-2].detail})"
            ),
            (
                f"Ground elevation — {assessment.ground_elevation_percent}% "
                f"({assessment.signals[-1].detail})"
            ),
            "",
            "Heading Detection Signals:",
        ]
        lines.extend(
            f"{signal.name} — {signal.percent}% ({signal.detail})"
            for signal in assessment.signals[:4]
        )
        lines.extend(
            (
                "",
                "Weakest detection signal:",
                (
                    f"{assessment.weakest_signal.name} — "
                    f"{assessment.weakest_signal.percent}%"
                ),
            )
        )
        if assessment.cap_reason:
            lines.extend(("", assessment.cap_reason))
        if inference_details:
            lines.extend(("", inference_details))
        return "\n".join(lines)

    def _ensure_source_states(self) -> None:
        for raw_path in self.input_files:
            path = str(Path(raw_path).resolve(strict=False))
            if path != raw_path:
                index = self.input_files.index(raw_path)
                self.input_files[index] = path
            if path not in self.source_states:
                self.source_states[path] = self._new_source_state(path)

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

    def _review_source_runways(self, _fallback_elevation_m=None, *, paths=None):
        """Return one inline-reviewed reference per requested input."""
        self._commit_current_source()
        self._ensure_source_states()
        requested_paths = tuple(paths) if paths is not None else tuple(self.input_files)
        reviewed: list[RunwayReference | None] = []
        for raw_path in requested_paths:
            path = str(Path(raw_path).resolve(strict=False))
            state = self.source_states[path]
            if not state.analysed:
                self._analyse_source(state)
            if state.parse_error is not None:
                reviewed.append(None)
                self._sync_file_item_error(path)
                continue
            try:
                reference = self._reference_from_values(
                    state.values,
                    label=Path(path).name,
                    elevation_required=state.altitude_mode == "absolute",
                )
            except ValueError as error:
                self._set_transposition_error(path, str(error), correctable=True)
                reviewed.append(None)
                continue
            self._clear_transposition_error(path)
            state.values = AirfieldFormValues(
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

    def _validated_target_for_state(
        self,
        path: str,
        state: SourceAirfieldState,
    ) -> RunwayReference | None:
        values = state.runway_target_values
        try:
            reference = self._reference_from_values(
                values,
                label=f"{Path(path).name} target airfield",
                elevation_required=False,
            )
        except ValueError as error:
            state.target_error = str(error)
            return None
        state.runway_target_values = AirfieldFormValues(
            threshold=(
                f"{format_optional_number(reference.latitude)}, "
                f"{format_optional_number(reference.longitude)}"
            ),
            true_heading=format_optional_number(reference.true_heading_deg),
            elevation_m="",
        )
        state.target_error = None
        return reference

    def _validated_target(self) -> RunwayReference | None:
        """Compatibility wrapper for the active file's runway target."""
        self._commit_current_source()
        if self._current_source_path is None:
            return None
        state = self.source_states[self._current_source_path]
        target = self._validated_target_for_state(
            self._current_source_path,
            state,
        )
        self._render_source_state(state)
        return target

    def _choose_transposition_inputs(self) -> tuple[str, ...] | None:
        if not self.input_files:
            QMessageBox.warning(self, "No Files", "Please select at least one KML file.")
            return None

        available = set(self.input_files)
        remembered = tuple(
            path
            for path in (self._last_transposition_selection or ())
            if path in available
        )
        if not remembered:
            current = self._current_source_path
            remembered = (current,) if current in available else ()
        dialog = TranspositionInputDialog(self.input_files, remembered, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        self._last_transposition_selection = dialog.selected_paths
        return dialog.selected_paths

    def _validated_inputs_for_paths(self, paths):
        self._commit_current_source()
        self._ensure_source_states()
        alignments = []
        for raw_path in paths:
            path = str(Path(raw_path).resolve(strict=False))
            state = self.source_states[path]
            if not state.analysed:
                self._analyse_source(state)
            state.transposition_error = None
            state.transposition_error_correctable = False
            state.target_error = None
            state.manual_source_error = None
            state.manual_target_error = None
            if state.parse_error is not None:
                alignments.append(None)
                self._sync_file_item_error(path)
                continue

            alignment = None
            if state.method is AlignmentMethod.RUNWAY:
                source = None
                try:
                    source = self._reference_from_values(
                        state.values,
                        label=Path(path).name,
                        elevation_required=state.altitude_mode == "absolute",
                    )
                except ValueError as error:
                    state.transposition_error = str(error)
                    state.transposition_error_correctable = True
                target = self._validated_target_for_state(path, state)
                if source is not None and target is not None:
                    alignment = RunwayTranspositionAlignment(source, target)
            else:
                ground_elevation = None
                if state.altitude_mode == "absolute":
                    try:
                        ground_elevation = float(
                            state.manual_ground_elevation_m
                        )
                        if not math.isfinite(ground_elevation):
                            raise ValueError
                    except ValueError:
                        state.manual_source_error = (
                            f"{Path(path).name} ground reference elevation is required "
                            "and must be a finite number because this KML uses absolute altitude."
                        )
                elif state.altitude_mode not in {
                    "relativeToGround",
                    "clampToGround",
                }:
                    state.manual_source_error = (
                        f'KML altitude mode "{state.altitude_mode}" cannot be converted '
                        "safely to relative-to-ground output."
                    )
                coordinate = None
                rotation = None
                try:
                    coordinate = parse_coordinate_pair(
                        state.manual_target_coordinate
                    )
                except CoordinateInputError as error:
                    state.manual_target_error = (
                        f"{Path(path).name} target trace coordinates: {error}"
                    )
                try:
                    rotation = float(state.manual_rotation_deg)
                    if not math.isfinite(rotation) or not 0.0 <= rotation <= 360.0:
                        raise ValueError
                except ValueError:
                    message = (
                        f"{Path(path).name} clockwise rotation must be a finite "
                        "number between 0 and 360 degrees."
                    )
                    state.manual_target_error = " ".join(
                        filter(None, (state.manual_target_error, message))
                    )
                if (
                    state.manual_source_error is None
                    and state.manual_target_error is None
                    and coordinate is not None
                    and rotation is not None
                ):
                    alignment = ManualTranspositionAlignment(
                        coordinate.latitude,
                        coordinate.longitude,
                        rotation,
                        ground_elevation,
                    )
            alignments.append(alignment)
            self._sync_file_item_error(path)
            self._schedule_profile_save(path)
        self._flush_pending_profiles()
        if self._current_source_path:
            self._render_source_state(
                self.source_states[self._current_source_path]
            )
        return tuple(alignments)

    @staticmethod
    def _alignment_signature(alignment) -> str | None:
        if isinstance(alignment, RunwayTranspositionAlignment):
            payload = (
                alignment.method.value,
                alignment.source_runway.latitude,
                alignment.source_runway.longitude,
                alignment.source_runway.true_heading_deg,
                alignment.source_runway.elevation_m,
                alignment.target_runway.latitude,
                alignment.target_runway.longitude,
                alignment.target_runway.true_heading_deg,
            )
        elif isinstance(alignment, ManualTranspositionAlignment):
            payload = (
                alignment.method.value,
                alignment.target_latitude,
                alignment.target_longitude,
                alignment.clockwise_rotation_deg,
                alignment.ground_reference_elevation_m,
            )
        else:
            return None
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _preview_target_snapshot_for_state(
        state: SourceAirfieldState | None,
    ) -> PreviewTargetSnapshot | None:
        if state is None:
            return None
        if state.method is AlignmentMethod.RUNWAY:
            return PreviewTargetSnapshot(
                method=state.method,
                coordinate=state.runway_target_values.threshold,
                true_heading=state.runway_target_values.true_heading,
            )
        return PreviewTargetSnapshot(
            method=state.method,
            coordinate=state.manual_target_coordinate,
            clockwise_rotation=state.manual_rotation_deg,
        )

    def _preview_target_snapshots_for_paths(self, input_files):
        return tuple(
            self._preview_target_snapshot_for_state(
                self.source_states.get(
                    str(Path(path).resolve(strict=False))
                )
            )
            for path in input_files
        )

    def _current_alignment_signature(
        self,
        state: SourceAirfieldState,
    ) -> str | None:
        """Build the current geometric signature without changing validation state."""

        if state.parse_error is not None:
            return None
        try:
            if state.method is AlignmentMethod.RUNWAY:
                source = self._reference_from_values(
                    state.values,
                    label=state.path.name,
                    elevation_required=state.altitude_mode == "absolute",
                )
                target = self._reference_from_values(
                    state.runway_target_values,
                    label=f"{state.path.name} target airfield",
                    elevation_required=False,
                )
                alignment = RunwayTranspositionAlignment(source, target)
            else:
                ground_elevation = None
                if state.altitude_mode == "absolute":
                    ground_elevation = float(state.manual_ground_elevation_m)
                    if not math.isfinite(ground_elevation):
                        return None
                elif state.altitude_mode not in {
                    "relativeToGround",
                    "clampToGround",
                }:
                    return None
                coordinate = parse_coordinate_pair(
                    state.manual_target_coordinate
                )
                rotation = float(state.manual_rotation_deg)
                if not math.isfinite(rotation) or not 0.0 <= rotation <= 360.0:
                    return None
                alignment = ManualTranspositionAlignment(
                    coordinate.latitude,
                    coordinate.longitude,
                    rotation,
                    ground_elevation,
                )
        except (CoordinateInputError, TypeError, ValueError):
            return None
        return self._alignment_signature(alignment)

    def _preview_adjustment_is_active(self, state: SourceAirfieldState) -> bool:
        if (
            state.preview_adjustment is None
            or state.preview_signature is None
        ):
            return False
        if state.preview_signature != self._current_alignment_signature(state):
            return False
        return state.fingerprint == self._source_fingerprint(state.path)

    def _preparation_signature(self, input_files, alignments):
        fingerprints = []
        for raw_path in input_files:
            path = Path(raw_path).resolve(strict=False)
            file_signature = self._source_fingerprint(path)
            fingerprints.append((self._path_key(path), file_signature))
        return (
            tuple(fingerprints),
            tuple(self._alignment_signature(item) for item in alignments),
        )

    @staticmethod
    def _source_fingerprint(path: str | Path):
        """Return a content-backed identity for stale-scene and offset checks."""

        try:
            return fingerprint_source_file(path)
        except OSError:
            return None

    def _apply_committed_adjustments(self, batch, alignments=None):
        active_alignments = tuple(alignments or self._prepared_alignments or ())
        items = []
        for index, item in enumerate(batch.items):
            if isinstance(item, PreparedTranspositionFile):
                path = str(item.input_path.resolve(strict=False))
                state = self.source_states.get(path)
                fingerprint = self._source_fingerprint(item.input_path)
                alignment = (
                    active_alignments[index]
                    if index < len(active_alignments)
                    else None
                )
                signature = self._alignment_signature(alignment)
                if (
                    state is not None
                    and state.preview_adjustment is not None
                    and state.preview_signature == signature
                    and state.fingerprint == fingerprint
                ):
                    item = replace(
                        item,
                        trace=item.trace.with_adjustment(
                            state.preview_adjustment
                        ),
                    )
            items.append(item)
        return replace(batch, items=tuple(items))

    def _prepare_current_batch(
        self,
        input_files,
        alignments,
        signature,
    ):
        if (
            self._prepared_batch is not None
            and self._accepted_signature == signature
            and self._prepared_signature == signature
        ):
            return self._prepared_batch
        batch = prepare_transposition(
            input_files=input_files,
            alignments=alignments,
        )
        batch = self._apply_committed_adjustments(batch, alignments)
        self._prepared_batch = batch
        self._prepared_signature = signature
        self._prepared_alignments = tuple(alignments)
        self._prepared_target_snapshots = self._preview_target_snapshots_for_paths(
            input_files
        )
        return batch

    def _record_preparation_failures(self, batch):
        failures = []
        for item in batch.failed_items:
            path = str(item.input_path.resolve(strict=False))
            state = self.source_states.get(path)
            message = self._source_error(state) if state is not None else ""
            message = message or item.message
            correctable = bool(
                state is not None
                and state.transposition_error
                and state.transposition_error_correctable
            )
            self._set_transposition_error(
                path,
                message,
                correctable=correctable,
            )
            failures.append((path, message))
        for item in batch.prepared:
            self._clear_transposition_error(item.input_path)
        if self._current_source_path:
            self._render_source_state(self.source_states[self._current_source_path])
        return tuple(failures)

    def _show_source_failures(self, failures, *, title: str, introduction: str) -> None:
        if not failures:
            return
        first_path = str(Path(failures[0][0]).resolve(strict=False))
        self._select_source_path(first_path)
        state = self.source_states.get(first_path)
        if state is not None:
            self._render_source_state(state)
        details = "\n".join(
            f"• {Path(path).name}: {message}" for path, message in failures
        )
        QMessageBox.warning(
            self,
            title,
            f"{introduction}\n\n{details}\n\n"
            "Select a marked input file to review its error.",
        )

    def open_preview(self) -> None:
        current_path = self._current_source_path
        if current_path is None:
            QMessageBox.warning(
                self,
                "No file selected",
                "Select one KML file in the input list to preview it.",
            )
            return
        input_files = (current_path,)
        alignments = self._validated_inputs_for_paths(input_files)
        signature = self._preparation_signature(input_files, alignments)
        try:
            batch = prepare_transposition(
                input_files=input_files,
                alignments=alignments,
            )
            batch = self._apply_committed_adjustments(batch, alignments)
        except Exception as error:
            QMessageBox.critical(
                self,
                "Preview preparation failed",
                str(error) or "The transposition preview could not be prepared.",
            )
            return
        failures = self._record_preparation_failures(batch)
        if failures:
            self._show_source_failures(
                failures,
                title="Preview file needs attention",
                introduction="The selected file cannot be previewed:",
            )
            return
        self._prepared_batch = batch
        self._prepared_signature = signature
        self._prepared_alignments = tuple(alignments)
        self._prepared_target_snapshots = self._preview_target_snapshots_for_paths(
            input_files
        )
        self.preview_requested.emit(
            PreviewScene(tuple(item.trace for item in batch.prepared))
        )

    def accept_preview_scene(self, scene) -> None:
        if self._prepared_batch is None or not isinstance(scene, PreviewScene):
            return
        traces = {trace.trace_id: trace for trace in scene.traces}
        items = []
        for index, item in enumerate(self._prepared_batch.items):
            if isinstance(item, PreparedTranspositionFile):
                trace = traces.get(item.trace.trace_id)
                if trace is not None:
                    item = replace(item, trace=trace)
                    path = str(item.input_path.resolve(strict=False))
                    state = self.source_states.get(path)
                    alignment = (
                        self._prepared_alignments[index]
                        if self._prepared_alignments is not None
                        and index < len(self._prepared_alignments)
                        else None
                    )
                    target_snapshot = (
                        self._prepared_target_snapshots[index]
                        if self._prepared_target_snapshots is not None
                        and index < len(self._prepared_target_snapshots)
                        else self._preview_target_snapshot_for_state(state)
                    )
                    if state is not None:
                        state.fingerprint = self._source_fingerprint(
                            item.input_path
                        )
                        state.preview_signature = self._alignment_signature(
                            alignment
                        )
                        state.preview_adjustment = trace.adjustment
                        state.preview_target_snapshot = target_snapshot
                        self._schedule_profile_save(path)
            items.append(item)
        self._prepared_batch = replace(
            self._prepared_batch,
            items=tuple(items),
        )
        self._accepted_signature = self._prepared_signature
        self._flush_pending_profiles()
        if self._current_source_path is not None:
            state = self.source_states.get(self._current_source_path)
            if state is not None:
                self._render_preview_offset_summaries(state)

    def export_committed_scene(self) -> None:
        if self._prepared_batch is None:
            return
        input_files = tuple(
            str(item.input_path.resolve(strict=False))
            for item in self._prepared_batch.items
        )
        if input_files:
            self._run_transposition_for_paths(input_files)

    def run_transposition_ui(self) -> None:
        input_files = self._choose_transposition_inputs()
        if input_files is None:
            return
        self._run_transposition_for_paths(input_files)

    def _output_preset_name(self, path: str, alignment: object) -> str | None:
        state = self.source_states.get(str(Path(path).resolve(strict=False)))
        if state is None:
            return None
        preset_id = (
            state.target_runway_preset_id
            if isinstance(alignment, RunwayTranspositionAlignment)
            else state.target_trace_preset_id
            if isinstance(alignment, ManualTranspositionAlignment)
            else None
        )
        if not preset_id:
            return None
        try:
            record = self.presets.get(UUID(preset_id))
        except ValueError:
            return None
        return record.preset.name if record is not None else None

    def _run_transposition_for_paths(self, input_files) -> None:
        alignments = self._validated_inputs_for_paths(input_files)
        signature = self._preparation_signature(input_files, alignments)
        try:
            prepared_batch = self._prepare_current_batch(
                input_files,
                alignments,
                signature,
            )
        except Exception as error:
            QMessageBox.critical(
                self,
                "Error",
                f"Could not prepare transposition: {error}",
            )
            return

        failures = self._record_preparation_failures(prepared_batch)
        if failures:
            self._show_source_failures(
                failures,
                title="Selected files need attention",
                introduction=(
                    "No files were transposed because the following selected "
                    "inputs need attention:"
                ),
            )
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
            target_airfields = tuple(
                self._output_preset_name(path, alignment)
                for path, alignment in zip(input_files, alignments, strict=True)
            )
            plan = create_transposition_plan(
                input_files=input_files,
                output_directory=output_dir,
                target_airfields=target_airfields,
            )
        except Exception as error:
            QMessageBox.critical(self, "Error", f"Could not plan outputs: {error}")
            return

        plan = self._edit_output_plan(
            plan,
            writable_inputs={
                item.input_path.resolve(strict=False)
                for item in prepared_batch.prepared
            },
        )
        if plan is None:
            return

        try:
            result = export_prepared_transposition(prepared_batch, plan)
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
        """Return structured target-runway data for compatibility callers."""
        self._commit_current_source()
        values = self.target_card.values()
        coordinate = self.target_card.coordinate_input.coordinates()
        runway = RunwayPresetSection.validated(
            threshold=coordinate,
            true_heading_deg=values.true_heading,
        )
        return TranspositionPresetData(runway=runway).to_mapping()

    def apply_preset_data(self, data: Mapping[str, object]) -> None:
        """Compatibility hook: apply a runway section to the target card."""
        payload, warnings = TranspositionPresetData.from_mapping(data)
        if payload.runway is None:
            raise AirfieldPresetError("This preset does not contain runway inputs.")
        values = self._values_from_runway(payload.runway)
        target_values = AirfieldFormValues(
            threshold=values.threshold,
            true_heading=values.true_heading,
        )
        self.target_card.set_values(target_values)
        if self._current_source_path is not None:
            state = self.source_states[self._current_source_path]
            state.runway_target_values = target_values
            self._render_preview_offset_summaries(state)
            self._schedule_profile_save(immediate=True)
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

    def _edit_output_plan(self, plan, *, writable_inputs=None):
        dialog = TranspositionOutputDialog(plan, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        candidate = dialog.validated_plan
        if candidate is None:
            return None
        existing_paths = tuple(
            job.output_path
            for job in candidate.jobs
            if job.output_path.exists()
            and (
                writable_inputs is None
                or job.input_path.resolve(strict=False) in writable_inputs
            )
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

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        self._commit_current_source()
        self._persist_input_session()
        if self._current_source_path is not None:
            self._schedule_profile_save(self._current_source_path)
        self._flush_pending_profiles()
        super().closeEvent(event)


__all__ = [
    "OriginalTraceCard",
    "SourceAirfieldState",
    "TargetTraceCard",
    "TransposePage",
    "TranspositionInputDialog",
    "TranspositionOutputDialog",
]
