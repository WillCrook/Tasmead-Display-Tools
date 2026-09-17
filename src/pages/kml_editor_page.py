"""KML editor workspace backed by one authoritative per-file model."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from PyQt6.QtCore import QSettings, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut, QTextCursor, QTextDocument
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from file_dialog_state import (
    FileDialogDirection,
    FileDialogWorkflow,
    ensure_extension,
    remember_file_selection,
    remembered_directory,
    suggested_save_path,
)
from icon_utils import AppIcon, set_button_icon
from google_maps_settings import GoogleMapsSettings
from map_preview_widget import MapPreviewWidget, WEBENGINE_AVAILABLE
from pages.crop_range_slider import CropRangeSlider
from pages.kml_text_editor import KmlCodeEditor
from services.kml_text_formatting import format_kml_coordinates
from services import (
    EditorMode,
    KmlEditorDocumentState,
    KmlEditorWorkspaceModel,
    OperationStatus,
    ParseStatus,
    PreviewPresentation,
)
from services.kml_editor_operations import timestamp_info


PAGE_STYLE = """
KmlEditorPage {
    background: palette(window);
}
KmlEditorPage QFrame#workspacePanel,
KmlEditorPage QFrame#editorPlaceholder {
    background: palette(base);
    border: 1px solid palette(mid);
    border-radius: 12px;
}
KmlEditorPage QLabel#panelTitle {
    font-size: 15px;
    font-weight: 650;
}
KmlEditorPage QFrame#editorModeControl {
    background: palette(alternate-base);
    border: 1px solid palette(mid);
    border-radius: 9px;
}
KmlEditorPage QRadioButton#editorModeSegment {
    min-height: 30px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 2px 10px;
    spacing: 0;
    font-weight: 500;
}
KmlEditorPage QRadioButton#editorModeSegment::indicator {
    width: 0;
    height: 0;
    margin: 0;
    padding: 0;
    image: none;
}
KmlEditorPage QRadioButton#editorModeSegment:hover:!checked {
    background: palette(base);
}
KmlEditorPage QRadioButton#editorModeSegment:checked {
    background: palette(highlight);
    color: palette(highlighted-text);
    border-color: palette(accent);
    font-weight: 650;
}
KmlEditorPage QRadioButton#editorModeSegment:focus {
    border: 2px solid palette(accent);
}
KmlEditorPage QLabel#editorStatus[parseStatus="valid"] {
    color: palette(link);
    font-weight: 650;
}
KmlEditorPage QLabel#editorStatus[parseStatus="invalid"],
KmlEditorPage QLabel#editorStatus[parseStatus="stale"] {
    color: palette(text);
    font-weight: 650;
}
KmlEditorPage QLabel#editorStatus[parseStatus="validating"] {
    color: palette(link);
    font-weight: 650;
}
KmlEditorPage QFrame#searchBar,
KmlEditorPage QFrame#diagnosticPanel {
    background: palette(alternate-base);
    border: 1px solid palette(mid);
    border-radius: 7px;
}
"""

KML_EDITOR_FONT_SIZE_SETTING = "kml-editor/font-size"
KML_EDITOR_FONT_SIZE_MIN = 8
KML_EDITOR_FONT_SIZE_MAX = 32


class KmlEditorPage(QWidget):
    """Multi-file text, crop and resolution-reduction workspace."""

    preview_requested = pyqtSignal(object)
    maps_settings_requested = pyqtSignal()

    def __init__(
        self,
        model: KmlEditorWorkspaceModel | None = None,
        parent=None,
        *,
        settings: QSettings | None = None,
        maps_settings: GoogleMapsSettings | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("KmlEditorPage")
        self.setStyleSheet(PAGE_STYLE)
        self.model = model or KmlEditorWorkspaceModel(parent=self)
        self._settings = settings if settings is not None else QSettings()
        self._maps_settings = maps_settings
        self._default_editor_font_size = min(
            KML_EDITOR_FONT_SIZE_MAX,
            max(KML_EDITOR_FONT_SIZE_MIN, KmlCodeEditor.default_font_point_size()),
        )
        self._editor_font_size = self._read_editor_font_size()
        self._rendering = False
        self._formatting_coordinates = False
        self._diagnostics = ()
        self._rendered_document_id = None
        self._simplification_timer = QTimer(self)
        self._simplification_timer.setSingleShot(True)
        self._simplification_timer.setInterval(180)
        self._simplification_timer.timeout.connect(self._calculate_simplification)
        self._crop_preview_timer = QTimer(self)
        self._crop_preview_timer.setSingleShot(True)
        self._crop_preview_timer.setInterval(100)
        self._crop_preview_timer.timeout.connect(self._request_crop_preview)
        self._pending_crop_scene = None
        self._crop_preview_document_id = None
        self._crop_preview_refresh_pending = False

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        root.addWidget(self.splitter, 1)
        self._build_sidebar()
        self._build_workspace()
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 4)
        self.splitter.setSizes((235, 815))

        self.model.documents_changed.connect(self._render_document_list)
        self.model.active_document_changed.connect(self._render_active_document)
        self.model.document_changed.connect(self._document_changed)
        self.model.mode_changed.connect(self._render_mode)
        self.model.preview_ready.connect(self._preview_ready)
        self.model.operation_finished.connect(self._operation_finished)
        if self._maps_settings is not None:
            self._maps_settings.api_key_changed.connect(self._maps_api_key_changed)

        self._build_shortcuts()
        self._set_tab_order()
        self._render_document_list()
        self._render_active_document(None)
        self._render_mode(self.model.mode)

    def _build_sidebar(self) -> None:
        self.sidebar = QFrame()
        self.sidebar.setObjectName("workspacePanel")
        self.sidebar.setMinimumWidth(205)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(14, 14, 14, 14)
        sidebar_layout.setSpacing(10)

        heading_row = QHBoxLayout()
        heading = QLabel("Input files")
        heading.setObjectName("panelTitle")
        self.file_count_label = QLabel("0 files")
        self.file_count_label.setObjectName("mutedText")
        heading_row.addWidget(heading)
        heading_row.addStretch()
        heading_row.addWidget(self.file_count_label)
        sidebar_layout.addLayout(heading_row)

        self.file_list = QListWidget()
        self.file_list.setAccessibleName("KML Editor input files")
        self.file_list.setAccessibleDescription(
            "Select the active KML file. An asterisk marks unsaved changes."
        )
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list.currentItemChanged.connect(self._active_item_changed)
        sidebar_layout.addWidget(self.file_list, 1)

        file_actions = QHBoxLayout()
        self.add_files_btn = QPushButton("Add files")
        self.add_files_btn.setAccessibleName("Add KML files")
        set_button_icon(self.add_files_btn, AppIcon.FOLDER_PLUS)
        self.remove_files_btn = QPushButton("Remove")
        self.remove_files_btn.setAccessibleName("Remove selected KML files")
        set_button_icon(self.remove_files_btn, AppIcon.TRASH)
        self.add_files_btn.clicked.connect(self.browse_files)
        self.remove_files_btn.clicked.connect(self.remove_selected_files)
        file_actions.addWidget(self.add_files_btn)
        file_actions.addWidget(self.remove_files_btn)
        sidebar_layout.addLayout(file_actions)

        mode_heading = QLabel("Mode")
        mode_heading.setObjectName("panelTitle")
        sidebar_layout.addWidget(mode_heading)
        self.mode_control = QFrame()
        self.mode_control.setObjectName("editorModeControl")
        self.mode_control.setAccessibleName("KML editor mode")
        mode_layout = QVBoxLayout(self.mode_control)
        mode_layout.setContentsMargins(3, 3, 3, 3)
        mode_layout.setSpacing(0)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.text_mode_button = QRadioButton("Text Editor")
        self.crop_mode_button = QRadioButton("Crop KML")
        self.simplify_mode_button = QRadioButton("Reduce Resolution")
        self.mode_buttons = {
            EditorMode.TEXT: self.text_mode_button,
            EditorMode.CROP: self.crop_mode_button,
            EditorMode.SIMPLIFY: self.simplify_mode_button,
        }
        for mode, button in self.mode_buttons.items():
            button.setObjectName("editorModeSegment")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.toggled.connect(
                lambda checked, selected=mode: checked and self.model.set_mode(selected)
            )
            self.mode_group.addButton(button)
            mode_layout.addWidget(button)
        self.text_mode_button.setChecked(True)
        sidebar_layout.addWidget(self.mode_control)
        self.splitter.addWidget(self.sidebar)

    def _build_workspace(self) -> None:
        self.workspace = QWidget()
        workspace_layout = QVBoxLayout(self.workspace)
        workspace_layout.setContentsMargins(7, 0, 0, 0)
        workspace_layout.setSpacing(10)

        action_row = QHBoxLayout()
        self.active_file_label = QLabel("No KML file selected")
        self.active_file_label.setObjectName("panelTitle")
        self.active_file_label.setAccessibleName("Active KML file")
        action_row.addWidget(self.active_file_label)
        action_row.addStretch()
        self.restore_btn = QPushButton("Restore saved")
        self.restore_btn.setAccessibleName("Restore active KML to last saved contents")
        self.save_btn = QPushButton("Save")
        self.save_btn.setAccessibleName("Save active KML file")
        self.save_as_btn = QPushButton("Save As…")
        self.save_as_btn.setAccessibleName("Save active KML file as")
        self.restore_btn.clicked.connect(self.restore_active_document)
        self.save_btn.clicked.connect(self.save_active_document)
        self.save_as_btn.clicked.connect(self.save_active_document_as)
        self.validate_btn = QPushButton("Validate now")
        self.validate_btn.setAccessibleName("Validate current KML text now")
        self.validate_btn.clicked.connect(self.validate_active_document)
        action_row.addWidget(self.restore_btn)
        action_row.addWidget(self.validate_btn)
        action_row.addWidget(self.save_btn)
        action_row.addWidget(self.save_as_btn)
        workspace_layout.addLayout(action_row)

        self.workspace_stack = QStackedWidget()
        self.text_page = self._build_text_page()
        self.crop_page = self._build_crop_page()
        self.simplify_page = self._build_simplify_page()
        self.workspace_stack.addWidget(self.text_page)
        self.workspace_stack.addWidget(self.crop_page)
        self.workspace_stack.addWidget(self.simplify_page)
        workspace_layout.addWidget(self.workspace_stack, 1)
        self.splitter.addWidget(self.workspace)

    def _build_text_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("workspacePanel")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)
        self.parse_status_label = QLabel("No file selected")
        self.parse_status_label.setObjectName("editorStatus")
        self.parse_status_label.setAccessibleName("KML parse status")
        self.parse_status_label.setWordWrap(True)
        layout.addWidget(self.parse_status_label)
        self.search_bar = QFrame()
        self.search_bar.setObjectName("searchBar")
        search_layout = QHBoxLayout(self.search_bar)
        search_layout.setContentsMargins(8, 5, 8, 5)
        search_layout.setSpacing(6)
        search_label = QLabel("Find")
        self.search_input = QLineEdit()
        self.search_input.setAccessibleName("Find in KML text")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._search_text_changed)
        self.search_input.returnPressed.connect(self.find_next)
        self.search_previous_btn = QPushButton("Previous")
        self.search_previous_btn.clicked.connect(lambda: self.find_next(backward=True))
        self.search_next_btn = QPushButton("Next")
        self.search_next_btn.clicked.connect(self.find_next)
        self.search_status_label = QLabel("")
        self.search_status_label.setAccessibleName("KML search result")
        self.search_close_btn = QPushButton("Close")
        self.search_close_btn.clicked.connect(self.close_search)
        search_layout.addWidget(search_label)
        search_layout.addWidget(self.search_input, 1)
        search_layout.addWidget(self.search_previous_btn)
        search_layout.addWidget(self.search_next_btn)
        search_layout.addWidget(self.search_status_label)
        search_layout.addWidget(self.search_close_btn)
        self.search_bar.hide()
        layout.addWidget(self.search_bar)

        self.text_editor = KmlCodeEditor()
        self.text_editor.set_font_point_size(self._editor_font_size)
        self.text_editor.setAccessibleName("Editable KML contents")
        self.text_editor.setAccessibleDescription(
            "Raw KML text with line numbers, XML highlighting and background validation."
        )
        self.text_editor.textChanged.connect(self._text_changed)
        self.text_editor.cursorPositionChanged.connect(self._cursor_changed)

        editor_toolbar = QHBoxLayout()
        editor_toolbar.setSpacing(6)
        self.format_coordinates_btn = QPushButton("One coordinate per line")
        self.format_coordinates_btn.setAccessibleName(
            "Arrange KML coordinates one per line"
        )
        self.format_coordinates_btn.setToolTip(
            "Changes coordinate whitespace in the active editor; saving remains explicit."
        )
        self.format_coordinates_btn.clicked.connect(self.format_active_coordinates)
        self.format_status_label = QLabel("")
        self.format_status_label.setObjectName("mutedText")
        self.format_status_label.setAccessibleName("Coordinate formatting result")
        self.format_status_label.setMaximumWidth(320)
        self.format_status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        editor_toolbar.addWidget(self.format_coordinates_btn)
        editor_toolbar.addWidget(self.format_status_label, 1)

        font_label = QLabel("Text size")
        self.font_decrease_btn = QPushButton("−")
        self.font_decrease_btn.setAccessibleName("Decrease KML editor text size")
        self.font_decrease_btn.setToolTip("Decrease text size (Ctrl+-)")
        self.font_decrease_btn.setMaximumWidth(36)
        self.font_size_input = QSpinBox()
        self.font_size_input.setAccessibleName("KML editor text size in points")
        self.font_size_input.setRange(
            KML_EDITOR_FONT_SIZE_MIN,
            KML_EDITOR_FONT_SIZE_MAX,
        )
        self.font_size_input.setSuffix(" pt")
        self.font_size_input.setValue(self._editor_font_size)
        self.font_increase_btn = QPushButton("+")
        self.font_increase_btn.setAccessibleName("Increase KML editor text size")
        self.font_increase_btn.setToolTip("Increase text size (Ctrl+=)")
        self.font_increase_btn.setMaximumWidth(36)
        self.font_reset_btn = QPushButton("Reset")
        self.font_reset_btn.setAccessibleName("Reset KML editor text size")
        self.font_reset_btn.setToolTip("Reset text size (Ctrl+0)")
        self.font_decrease_btn.clicked.connect(self.font_size_input.stepDown)
        self.font_increase_btn.clicked.connect(self.font_size_input.stepUp)
        self.font_reset_btn.clicked.connect(
            lambda: self.font_size_input.setValue(self._default_editor_font_size)
        )
        self.font_size_input.valueChanged.connect(self._set_editor_font_size)
        editor_toolbar.addWidget(font_label)
        editor_toolbar.addWidget(self.font_decrease_btn)
        editor_toolbar.addWidget(self.font_size_input)
        editor_toolbar.addWidget(self.font_increase_btn)
        editor_toolbar.addWidget(self.font_reset_btn)
        layout.addLayout(editor_toolbar)
        layout.addWidget(self.text_editor, 1)

        self.diagnostic_panel = QFrame()
        self.diagnostic_panel.setObjectName("diagnosticPanel")
        diagnostics_layout = QVBoxLayout(self.diagnostic_panel)
        diagnostics_layout.setContentsMargins(8, 8, 8, 8)
        diagnostics_layout.setSpacing(6)
        diagnostics_title_row = QHBoxLayout()
        diagnostics_title = QLabel("Diagnostics")
        diagnostics_title.setObjectName("panelTitle")
        self.go_to_diagnostic_btn = QPushButton("Go to location")
        self.go_to_diagnostic_btn.setAccessibleName("Go to selected KML diagnostic")
        self.go_to_diagnostic_btn.clicked.connect(self.go_to_selected_diagnostic)
        diagnostics_title_row.addWidget(diagnostics_title)
        diagnostics_title_row.addStretch()
        diagnostics_title_row.addWidget(self.go_to_diagnostic_btn)
        diagnostics_layout.addLayout(diagnostics_title_row)
        self.diagnostic_list = QTreeWidget()
        self.diagnostic_list.setAccessibleName("KML diagnostics")
        self.diagnostic_list.setHeaderLabels(("Severity", "Location", "Problem"))
        self.diagnostic_list.setRootIsDecorated(False)
        self.diagnostic_list.setMaximumHeight(125)
        self.diagnostic_list.currentItemChanged.connect(self._diagnostic_selected)
        self.diagnostic_list.itemActivated.connect(
            lambda *_args: self.go_to_selected_diagnostic()
        )
        self.diagnostic_list.header().setStretchLastSection(True)
        self.diagnostic_list.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.diagnostic_list.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        diagnostics_layout.addWidget(self.diagnostic_list)
        self.diagnostic_explanation_label = QLabel("No diagnostics.")
        self.diagnostic_explanation_label.setWordWrap(True)
        self.diagnostic_explanation_label.setAccessibleName("KML diagnostic explanation")
        self.diagnostic_suggestion_label = QLabel("")
        self.diagnostic_suggestion_label.setWordWrap(True)
        self.diagnostic_suggestion_label.setAccessibleName("KML diagnostic suggested correction")
        diagnostics_layout.addWidget(self.diagnostic_explanation_label)
        diagnostics_layout.addWidget(self.diagnostic_suggestion_label)
        layout.addWidget(self.diagnostic_panel)
        return page

    @staticmethod
    def _placeholder(title: str, message: str) -> tuple[QFrame, QLabel]:
        frame = QFrame()
        frame.setObjectName("editorPlaceholder")
        layout = QVBoxLayout(frame)
        layout.addStretch()
        title_label = QLabel(title)
        title_label.setObjectName("panelTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_label = QLabel(message)
        message_label.setObjectName("mutedText")
        message_label.setWordWrap(True)
        message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)
        layout.addWidget(message_label)
        layout.addStretch()
        return frame, message_label

    def _build_crop_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("workspacePanel")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        title = QLabel("Crop flight path")
        title.setObjectName("panelTitle")
        layout.addWidget(title)
        self.crop_timing_label = QLabel("Select a valid KML flight path.")
        self.crop_timing_label.setObjectName("mutedText")
        self.crop_timing_label.setWordWrap(True)
        self.crop_timing_label.setAccessibleName("Crop timeline availability")
        layout.addWidget(self.crop_timing_label)

        self.crop_preview_stack = QStackedWidget()
        self.crop_preview_stack.setMinimumHeight(280)
        self.crop_preview_placeholder = QFrame()
        self.crop_preview_placeholder.setObjectName("editorPlaceholder")
        placeholder_layout = QVBoxLayout(self.crop_preview_placeholder)
        placeholder_layout.addStretch()
        self.crop_preview_placeholder_title = QLabel("Crop preview")
        self.crop_preview_placeholder_title.setObjectName("panelTitle")
        self.crop_preview_placeholder_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addWidget(self.crop_preview_placeholder_title)
        self.crop_preview_placeholder_message = QLabel(
            "Select a valid KML flight path to prepare the map."
        )
        self.crop_preview_placeholder_message.setObjectName("mutedText")
        self.crop_preview_placeholder_message.setWordWrap(True)
        self.crop_preview_placeholder_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop_preview_placeholder_message.setAccessibleName("Crop preview status")
        placeholder_layout.addWidget(self.crop_preview_placeholder_message)
        self.crop_preview_settings_btn = QPushButton("Open Maps Settings")
        self.crop_preview_settings_btn.clicked.connect(
            lambda _checked=False: self.maps_settings_requested.emit()
        )
        placeholder_layout.addWidget(
            self.crop_preview_settings_btn,
            alignment=Qt.AlignmentFlag.AlignHCenter,
        )
        self.crop_preview_settings_btn.hide()
        placeholder_layout.addStretch()
        self.crop_map_preview = MapPreviewWidget()
        self.crop_map_preview.settings_requested.connect(
            self.maps_settings_requested.emit
        )
        self.crop_preview_stack.addWidget(self.crop_preview_placeholder)
        self.crop_preview_stack.addWidget(self.crop_map_preview)
        layout.addWidget(self.crop_preview_stack, 1)

        controls_layout = QGridLayout()
        controls_layout.setHorizontalSpacing(12)
        controls_layout.setVerticalSpacing(10)
        self.crop_start_label = QLabel("Start point")
        self.crop_end_label = QLabel("End point")
        self.crop_start_label.setWordWrap(True)
        self.crop_end_label.setWordWrap(True)
        self.crop_range_slider = CropRangeSlider()
        self.crop_range_slider.setAccessibleName("Retained crop range")
        self.crop_range_slider.setAccessibleDescription(
            "One range with start and end handles. Press Space to choose the other "
            "handle, then use the arrow keys to adjust it."
        )
        self.crop_range_slider.range_changed.connect(self._crop_range_changed)
        self.crop_range_slider.interaction_finished.connect(
            self._crop_range_interaction_finished
        )
        controls_layout.addWidget(self.crop_range_slider, 0, 0, 1, 2)
        controls_layout.addWidget(self.crop_start_label, 1, 0)
        controls_layout.addWidget(self.crop_end_label, 1, 1)
        controls_layout.setColumnStretch(0, 1)
        controls_layout.setColumnStretch(1, 1)
        layout.addLayout(controls_layout)

        self.crop_count_label = QLabel("Retained points: —")
        self.crop_count_label.setAccessibleName("Retained crop point count")
        layout.addWidget(self.crop_count_label)
        self.crop_status_label = QLabel("")
        self.crop_status_label.setObjectName("mutedText")
        self.crop_status_label.setWordWrap(True)
        self.crop_status_label.setAccessibleName("Crop operation status")
        layout.addWidget(self.crop_status_label)
        actions = QHBoxLayout()
        self.crop_reset_btn = QPushButton("Reset range")
        self.crop_apply_btn = QPushButton("Apply Crop")
        self.crop_apply_btn.setObjectName("primaryButton")
        self.crop_cancel_btn = QPushButton("Cancel task")
        self.crop_reset_btn.clicked.connect(self.reset_crop)
        self.crop_apply_btn.clicked.connect(self.apply_crop)
        self.crop_cancel_btn.clicked.connect(self.cancel_active_operations)
        actions.addWidget(self.crop_reset_btn)
        actions.addStretch()
        actions.addWidget(self.crop_cancel_btn)
        actions.addWidget(self.crop_apply_btn)
        layout.addLayout(actions)
        return page

    def _build_simplify_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("workspacePanel")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        title = QLabel("Maximum path deviation")
        title.setObjectName("panelTitle")
        layout.addWidget(title)
        help_label = QLabel(
            "Ramer–Douglas–Peucker reduction in WGS84 local metres. Horizontal "
            "deviation always applies; explicit absolute/relative-to-ground altitude "
            "and reliable timing profiles also participate. Clamp-to-ground paths are "
            "horizontal-only because terrain height is unavailable."
        )
        help_label.setObjectName("mutedText")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        presets = QGridLayout()
        presets.setHorizontalSpacing(12)
        presets.setVerticalSpacing(6)
        self.simplification_preset_group = QButtonGroup(self)
        self.simplification_preset_group.setExclusive(True)
        self.simplification_preset_buttons = {}
        for column, (key, text, tolerance) in enumerate((
            ("high", "High accuracy — 1 m", 1.0),
            ("balanced", "Balanced — 5 m", 5.0),
            ("aggressive", "Aggressive — 20 m", 20.0),
        )):
            button = QRadioButton(text)
            button.setAccessibleName(text)
            button.toggled.connect(
                lambda checked, selected=key, value=tolerance: checked
                and self._set_simplification_preset(selected, value)
            )
            self.simplification_preset_group.addButton(button)
            self.simplification_preset_buttons[key] = button
            presets.addWidget(button, 0, column)
        self.custom_tolerance_button = QRadioButton("Custom")
        self.custom_tolerance_button.setAccessibleName("Custom maximum path deviation")
        self.custom_tolerance_button.toggled.connect(self._custom_tolerance_toggled)
        self.simplification_preset_group.addButton(self.custom_tolerance_button)
        self.simplification_preset_buttons["custom"] = self.custom_tolerance_button
        presets.addWidget(self.custom_tolerance_button, 1, 0)
        self.tolerance_input = QDoubleSpinBox()
        self.tolerance_input.setAccessibleName("Custom maximum path deviation metres")
        self.tolerance_input.setRange(0.01, 100000.0)
        self.tolerance_input.setDecimals(2)
        self.tolerance_input.setSuffix(" m")
        self.tolerance_input.valueChanged.connect(self._tolerance_changed)
        presets.addWidget(self.tolerance_input, 1, 1)
        presets.setColumnStretch(3, 1)
        layout.addLayout(presets)

        counts = QHBoxLayout()
        self.original_points_label = QLabel("Original points: —")
        self.original_points_label.setAccessibleName("Original KML point count")
        self.result_points_label = QLabel("Resulting points: —")
        self.result_points_label.setAccessibleName("Reduced KML point count")
        self.reduction_percent_label = QLabel("Reduction: —")
        self.reduction_percent_label.setAccessibleName("KML point reduction percentage")
        counts.addWidget(self.original_points_label)
        counts.addWidget(self.result_points_label)
        counts.addWidget(self.reduction_percent_label)
        counts.addStretch()
        layout.addLayout(counts)
        self.simplification_status_label = QLabel("")
        self.simplification_status_label.setObjectName("mutedText")
        self.simplification_status_label.setWordWrap(True)
        self.simplification_status_label.setAccessibleName("Resolution reduction status")
        layout.addWidget(self.simplification_status_label)
        layout.addStretch()
        actions = QHBoxLayout()
        self.simplification_reset_btn = QPushButton("Reset settings")
        self.simplification_preview_btn = QPushButton("Preview reduction")
        self.simplification_apply_btn = QPushButton("Apply Reduction")
        self.simplification_apply_btn.setObjectName("primaryButton")
        self.simplification_cancel_btn = QPushButton("Cancel task")
        self.simplification_reset_btn.clicked.connect(self.reset_simplification)
        self.simplification_preview_btn.clicked.connect(self.preview_simplification)
        self.simplification_apply_btn.clicked.connect(self.apply_simplification)
        self.simplification_cancel_btn.clicked.connect(self.cancel_active_operations)
        actions.addWidget(self.simplification_reset_btn)
        actions.addStretch()
        actions.addWidget(self.simplification_cancel_btn)
        actions.addWidget(self.simplification_preview_btn)
        actions.addWidget(self.simplification_apply_btn)
        layout.addLayout(actions)
        return page

    def _build_shortcuts(self) -> None:
        context = Qt.ShortcutContext.WidgetWithChildrenShortcut
        self.open_shortcut = QShortcut(QKeySequence.StandardKey.Open, self)
        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_as_shortcut = QShortcut(QKeySequence("Ctrl+Shift+S"), self)
        self.find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self.find_next_shortcut = QShortcut(QKeySequence("F3"), self)
        self.find_previous_shortcut = QShortcut(QKeySequence("Shift+F3"), self)
        self.escape_search_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.font_increase_shortcut = QShortcut(QKeySequence("Ctrl+="), self)
        self.font_decrease_shortcut = QShortcut(QKeySequence("Ctrl+-"), self)
        self.font_reset_shortcut = QShortcut(QKeySequence("Ctrl+0"), self)
        for shortcut in (
            self.open_shortcut,
            self.save_shortcut,
            self.save_as_shortcut,
            self.find_shortcut,
            self.find_next_shortcut,
            self.find_previous_shortcut,
            self.escape_search_shortcut,
            self.font_increase_shortcut,
            self.font_decrease_shortcut,
            self.font_reset_shortcut,
        ):
            shortcut.setContext(context)
        self.open_shortcut.activated.connect(self.browse_files)
        self.save_shortcut.activated.connect(self.save_active_document)
        self.save_as_shortcut.activated.connect(self.save_active_document_as)
        self.find_shortcut.activated.connect(self.show_search)
        self.find_next_shortcut.activated.connect(self.find_next)
        self.find_previous_shortcut.activated.connect(
            lambda: self.find_next(backward=True)
        )
        self.escape_search_shortcut.activated.connect(self.close_search)
        self.font_increase_shortcut.activated.connect(self.font_size_input.stepUp)
        self.font_decrease_shortcut.activated.connect(self.font_size_input.stepDown)
        self.font_reset_shortcut.activated.connect(
            lambda: self.font_size_input.setValue(self._default_editor_font_size)
        )

    def _set_tab_order(self) -> None:
        self.setTabOrder(self.file_list, self.add_files_btn)
        self.setTabOrder(self.add_files_btn, self.remove_files_btn)
        self.setTabOrder(self.remove_files_btn, self.text_mode_button)
        self.setTabOrder(self.text_mode_button, self.crop_mode_button)
        self.setTabOrder(self.crop_mode_button, self.simplify_mode_button)
        self.setTabOrder(self.simplify_mode_button, self.restore_btn)
        self.setTabOrder(self.restore_btn, self.validate_btn)
        self.setTabOrder(self.validate_btn, self.save_btn)
        self.setTabOrder(self.save_btn, self.save_as_btn)
        self.setTabOrder(self.save_as_btn, self.format_coordinates_btn)
        self.setTabOrder(self.format_coordinates_btn, self.font_decrease_btn)
        self.setTabOrder(self.font_decrease_btn, self.font_size_input)
        self.setTabOrder(self.font_size_input, self.font_increase_btn)
        self.setTabOrder(self.font_increase_btn, self.font_reset_btn)
        self.setTabOrder(self.font_reset_btn, self.text_editor)
        self.setTabOrder(self.text_editor, self.crop_range_slider)
        self.setTabOrder(self.crop_range_slider, self.crop_reset_btn)
        self.setTabOrder(self.crop_reset_btn, self.crop_apply_btn)
        self.setTabOrder(
            self.crop_apply_btn,
            self.simplification_preset_buttons["high"],
        )
        self.setTabOrder(
            self.simplification_preset_buttons["high"],
            self.simplification_preset_buttons["balanced"],
        )
        self.setTabOrder(
            self.simplification_preset_buttons["balanced"],
            self.simplification_preset_buttons["aggressive"],
        )
        self.setTabOrder(
            self.simplification_preset_buttons["aggressive"],
            self.custom_tolerance_button,
        )
        self.setTabOrder(self.custom_tolerance_button, self.tolerance_input)
        self.setTabOrder(self.tolerance_input, self.simplification_reset_btn)
        self.setTabOrder(self.simplification_reset_btn, self.simplification_preview_btn)
        self.setTabOrder(self.simplification_preview_btn, self.simplification_apply_btn)

    def _read_editor_font_size(self) -> int:
        try:
            value = int(
                self._settings.value(
                    KML_EDITOR_FONT_SIZE_SETTING,
                    self._default_editor_font_size,
                )
            )
        except (TypeError, ValueError):
            value = self._default_editor_font_size
        return min(KML_EDITOR_FONT_SIZE_MAX, max(KML_EDITOR_FONT_SIZE_MIN, value))

    def _set_editor_font_size(self, point_size: int) -> None:
        value = min(KML_EDITOR_FONT_SIZE_MAX, max(KML_EDITOR_FONT_SIZE_MIN, int(point_size)))
        self._editor_font_size = value
        self.text_editor.set_font_point_size(value)
        self._settings.setValue(KML_EDITOR_FONT_SIZE_SETTING, value)
        self._settings.sync()

    def format_active_coordinates(self) -> bool:
        document = self.model.active_document
        if document is None:
            return False
        result = format_kml_coordinates(self.text_editor.toPlainText())
        if result.changed:
            cursor = QTextCursor(self.text_editor.document())
            cursor.beginEditBlock()
            cursor.select(QTextCursor.SelectionType.Document)
            self._formatting_coordinates = True
            try:
                cursor.insertText(result.contents)
            finally:
                self._formatting_coordinates = False
                cursor.endEditBlock()
        self.format_status_label.setText(result.message)
        self.format_status_label.setToolTip(result.message)
        return result.changed

    @staticmethod
    def _item_document_id(item: QListWidgetItem | None) -> UUID | None:
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return UUID(str(value)) if value else None

    def _render_document_list(self) -> None:
        active = self.model.active_document_id
        selected = {
            self._item_document_id(item)
            for item in self.file_list.selectedItems()
        }
        self._rendering = True
        try:
            self.file_list.clear()
            active_item = None
            for document in self.model.documents:
                state_label = {
                    ParseStatus.INVALID: "Invalid",
                    ParseStatus.STALE: "Needs validation",
                    ParseStatus.VALIDATING: "Validating",
                }.get(document.parse_state.status)
                label = document.source_path.name
                if state_label:
                    label += f" — {state_label}"
                if document.dirty:
                    label += " *"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, str(document.document_id))
                item.setToolTip(str(document.source_path))
                item.setData(
                    Qt.ItemDataRole.AccessibleDescriptionRole,
                    f"Full path: {document.source_path}. "
                    + ("Has unsaved changes. " if document.dirty else "Saved. ")
                    + f"Validation status: {document.parse_state.status.value}.",
                )
                self.file_list.addItem(item)
                if document.document_id in selected:
                    item.setSelected(True)
                if document.document_id == active:
                    active_item = item
            if active_item is not None:
                self.file_list.setCurrentItem(active_item)
        finally:
            self._rendering = False
        count = len(self.model.documents)
        self.file_count_label.setText(f"{count} file" if count == 1 else f"{count} files")

    def _active_item_changed(self, current, _previous) -> None:
        if not self._rendering:
            self._flush_active_editor()
            self.model.set_active_document(self._item_document_id(current))

    def _document_changed(self, document_id: UUID) -> None:
        if document_id == self.model.active_document_id:
            self._render_active_document(document_id)
            document = self.model.active_document
            if (
                self.model.mode == EditorMode.CROP
                and document is not None
                and document.parse_state.status == ParseStatus.VALID
                and document.crop_state.status == OperationStatus.IDLE
            ):
                self._schedule_crop_preview()
            if (
                self.model.mode == EditorMode.SIMPLIFY
                and document is not None
                and document.parse_state.status == ParseStatus.VALID
                and document.simplification_state.status == OperationStatus.IDLE
                and document.simplification_state.result_revision != document.revision
            ):
                self._simplification_timer.start()

    def _render_active_document(self, _document_id) -> None:
        document = self.model.active_document
        changing_document = (
            document is not None
            and document.document_id != self._rendered_document_id
        )
        if changing_document:
            self._pending_crop_scene = None
            self._crop_preview_document_id = None
            if self.model.mode == EditorMode.CROP:
                self._set_crop_preview_placeholder(
                    "Preparing the crop comparison…"
                )
        self._rendering = True
        try:
            enabled = document is not None
            self.text_editor.setEnabled(enabled)
            self.save_btn.setEnabled(enabled and document.dirty)
            self.save_as_btn.setEnabled(enabled)
            self.restore_btn.setEnabled(enabled and document.dirty)
            self.validate_btn.setEnabled(enabled)
            self.format_coordinates_btn.setEnabled(enabled)
            self.remove_files_btn.setEnabled(bool(self.model.documents))
            if document is None:
                self._rendered_document_id = None
                self.active_file_label.setText("No KML file selected")
                self.text_editor.clear()
                self.format_status_label.clear()
                self._set_parse_status(None)
                self._render_diagnostics(None)
                self._render_crop(None)
                self._render_simplification(None)
                return
            self.active_file_label.setText(
                document.source_path.name + (" — Unsaved changes" if document.dirty else "")
            )
            self.active_file_label.setToolTip(str(document.source_path))
            if changing_document:
                self.format_status_label.clear()
                self.format_status_label.setToolTip("")
            contents_changed = self.text_editor.toPlainText() != document.contents
            if contents_changed:
                self.text_editor.setPlainText(document.contents)
            if changing_document or contents_changed:
                cursor = self.text_editor.textCursor()
                maximum = max(0, self.text_editor.document().characterCount() - 1)
                cursor.setPosition(min(maximum, document.cursor_anchor))
                cursor.setPosition(
                    min(maximum, document.cursor_position),
                    QTextCursor.MoveMode.KeepAnchor,
                )
                self.text_editor.setTextCursor(cursor)
            self._rendered_document_id = document.document_id
            self._set_parse_status(document)
            self._render_diagnostics(document)
            self._render_crop(document)
            self._render_simplification(document)
        finally:
            self._rendering = False
        self._render_document_list()
        if changing_document and self.model.mode == EditorMode.CROP:
            self._schedule_crop_preview(immediate=True, conceal_existing=True)

    def _set_parse_status(self, document: KmlEditorDocumentState | None) -> None:
        if document is None:
            status = "none"
            text = "Add a KML file to begin editing."
        else:
            parse = document.parse_state
            status = parse.status.value
            if parse.status == ParseStatus.VALID:
                text = f"Valid KML track — {parse.point_count} points"
            elif parse.status == ParseStatus.VALIDATING:
                text = "Validating current KML text…"
            elif parse.status == ParseStatus.STALE:
                text = "Needs validation — waiting for edits to pause."
            else:
                diagnostic = parse.diagnostics[0].message if parse.diagnostics else "No details available."
                text = f"KML diagnostic: {diagnostic}"
        self.parse_status_label.setProperty("parseStatus", status)
        self.parse_status_label.setText(text)
        self.parse_status_label.style().unpolish(self.parse_status_label)
        self.parse_status_label.style().polish(self.parse_status_label)

    def _render_diagnostics(self, document: KmlEditorDocumentState | None) -> None:
        self._diagnostics = () if document is None else document.parse_state.diagnostics
        self.diagnostic_list.clear()
        for index, diagnostic in enumerate(self._diagnostics):
            location = diagnostic.location.display if diagnostic.location else "Unavailable"
            item = QTreeWidgetItem(
                (diagnostic.severity.value.title(), location, diagnostic.message)
            )
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            self.diagnostic_list.addTopLevelItem(item)
        if self._diagnostics:
            self.diagnostic_list.setCurrentItem(self.diagnostic_list.topLevelItem(0))
        else:
            self.diagnostic_explanation_label.setText("No diagnostics.")
            self.diagnostic_suggestion_label.clear()
            self.go_to_diagnostic_btn.setEnabled(False)

    def _selected_diagnostic(self):
        item = self.diagnostic_list.currentItem()
        if item is None:
            return None
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(index, int) or not 0 <= index < len(self._diagnostics):
            return None
        return self._diagnostics[index]

    def _diagnostic_selected(self, _current, _previous) -> None:
        diagnostic = self._selected_diagnostic()
        if diagnostic is None:
            self.diagnostic_explanation_label.setText("No diagnostics.")
            self.diagnostic_suggestion_label.clear()
            self.go_to_diagnostic_btn.setEnabled(False)
            return
        self.diagnostic_explanation_label.setText(
            f"Explanation: {diagnostic.explanation}"
        )
        self.diagnostic_suggestion_label.setText(
            f"Suggested correction: {diagnostic.suggestion}"
        )
        self.go_to_diagnostic_btn.setEnabled(diagnostic.location is not None)

    def go_to_selected_diagnostic(self) -> bool:
        diagnostic = self._selected_diagnostic()
        if diagnostic is None or diagnostic.location is None:
            return False
        return self.text_editor.go_to_location(diagnostic.location)

    def show_search(self) -> None:
        self.search_bar.show()
        selected = self.text_editor.textCursor().selectedText()
        if selected and "\u2029" not in selected and len(selected) <= 200:
            self.search_input.setText(selected)
        self.search_input.selectAll()
        self.search_input.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def close_search(self) -> None:
        if not self.search_bar.isVisible():
            return
        self.search_bar.hide()
        self.search_status_label.clear()
        self.text_editor.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _search_text_changed(self, text: str) -> None:
        self.search_status_label.clear()
        if text:
            self.find_next()

    def find_next(self, *, backward: bool = False) -> bool:
        query = self.search_input.text()
        if not query:
            self.search_status_label.setText("Enter text")
            return False
        flags = (
            QTextDocument.FindFlag.FindBackward
            if backward
            else QTextDocument.FindFlag(0)
        )
        cursor = self.text_editor.document().find(
            query,
            self.text_editor.textCursor(),
            flags,
        )
        if cursor.isNull():
            start = QTextCursor(self.text_editor.document())
            start.movePosition(
                QTextCursor.MoveOperation.End
                if backward
                else QTextCursor.MoveOperation.Start
            )
            cursor = self.text_editor.document().find(query, start, flags)
        if cursor.isNull():
            self.search_status_label.setText("No matches")
            return False
        self.text_editor.setTextCursor(cursor)
        self.text_editor.ensureCursorVisible()
        self.search_status_label.setText("Match")
        return True

    def _point_label(self, prefix: str, document: KmlEditorDocumentState, index: int) -> str:
        track = document.parse_state.track
        if track is None:
            return prefix
        selected = track.points[index]
        progress = 100.0 * index / max(1, len(track.points) - 1)
        position = f"{selected.latitude:.6f}, {selected.longitude:.6f}"
        if selected.altitude_m is not None:
            position += f", {selected.altitude_m:.1f} m"
        point = f"point {index + 1} of {len(track.points)}, {progress:.1f}%"
        timing = timestamp_info(track)
        detail = f"{selected.timestamp}; {point}" if timing.reliable else point
        return f"{prefix}: {position} ({detail})"

    def _render_crop(self, document: KmlEditorDocumentState | None) -> None:
        valid = document is not None and document.parse_state.status == ParseStatus.VALID
        count = document.parse_state.point_count if valid else 0
        enabled = valid and count >= 2
        self.crop_range_slider.setEnabled(enabled)
        self.crop_range_slider.set_range(0, max(1, count - 1))
        self.crop_range_slider.set_page_step(max(1, count // 100))
        for button in (self.crop_reset_btn, self.crop_apply_btn):
            button.setEnabled(enabled)
        if not enabled:
            self._crop_preview_timer.stop()
            self._pending_crop_scene = None
            self._crop_preview_document_id = None
            self.crop_range_slider.set_values(0, 1)
            self.crop_range_slider.setAccessibleDescription(
                "Select a valid KML flight path before adjusting the retained range."
            )
            self.crop_start_label.setText("Start point")
            self.crop_end_label.setText("End point")
            self.crop_count_label.setText("Retained points: —")
            self.crop_timing_label.setText("Select a current valid KML flight path.")
            self.crop_status_label.setText("")
            self.crop_cancel_btn.setEnabled(False)
            message = (
                "Select a current valid KML flight path to prepare the map."
                if document is None
                else "The map will update when the current KML is valid."
            )
            self._set_crop_preview_placeholder(message)
            return
        start = document.crop_state.start_index or 0
        end = document.crop_state.end_index if document.crop_state.end_index is not None else count - 1
        self.crop_range_slider.set_values(start, end)
        self.crop_range_slider.setAccessibleDescription(
            f"Retains points {start + 1} through {end + 1} of {count}. "
            "Press Space to choose the other handle, then use the arrow keys to adjust it."
        )
        self.crop_start_label.setText(self._point_label("Start", document, start))
        self.crop_end_label.setText(self._point_label("End", document, end))
        self.crop_count_label.setText(f"Retained points: {end - start + 1} of {count}")
        timing = timestamp_info(document.parse_state.track)
        self.crop_timing_label.setText(
            "Aligned timestamps are available; point indexes remain the exact crop boundaries."
            if timing.reliable
            else f"Using point index and progress because {timing.reason.lower()}"
        )
        crop = document.crop_state
        binding = document.parse_state.track.source_binding
        source_safe = binding is not None and not binding.unsafe_reason
        busy = crop.status in {OperationStatus.PREPARING, OperationStatus.APPLYING}
        self.crop_cancel_btn.setEnabled(busy)
        self.crop_apply_btn.setEnabled(enabled and not busy and start < end and source_safe)
        status = {
            OperationStatus.PREPARING: "Preparing the complete crop comparison…",
            OperationStatus.APPLYING: "Applying the crop to in-memory editor contents…",
            OperationStatus.CANCELLED: "Crop task cancelled.",
            OperationStatus.ERROR: crop.error,
            OperationStatus.READY: "Crop preview is ready.",
        }.get(crop.status, "")
        details = " ".join(
            (*crop.warnings, binding.unsafe_reason if binding and binding.unsafe_reason else "")
        ).strip()
        self.crop_status_label.setText(" ".join(value for value in (status, details) if value))

    def _render_simplification(self, document: KmlEditorDocumentState | None) -> None:
        valid = document is not None and document.parse_state.status == ParseStatus.VALID
        self.tolerance_input.setEnabled(valid and self.custom_tolerance_button.isChecked())
        for button in self.simplification_preset_buttons.values():
            button.setEnabled(valid)
        for button in (
            self.simplification_reset_btn,
            self.simplification_preview_btn,
            self.simplification_apply_btn,
        ):
            button.setEnabled(valid)
        if document is None:
            self.tolerance_input.setValue(5.0)
            self.original_points_label.setText("Original points: —")
            self.result_points_label.setText("Resulting points: —")
            self.reduction_percent_label.setText("Reduction: —")
            self.simplification_status_label.clear()
            self.simplification_cancel_btn.setEnabled(False)
            return
        state = document.simplification_state
        binding = document.parse_state.track.source_binding if document.parse_state.track else None
        source_safe = binding is not None and not binding.unsafe_reason
        button = self.simplification_preset_buttons.get(state.preset, self.custom_tolerance_button)
        button.setChecked(True)
        self.tolerance_input.setValue(state.tolerance_m)
        self.tolerance_input.setEnabled(valid and state.preset == "custom")
        count = document.parse_state.point_count
        self.original_points_label.setText(
            f"Original points: {count}" if count else "Original points: unavailable"
        )
        if state.result_point_count is None:
            self.result_points_label.setText("Resulting points: —")
            self.reduction_percent_label.setText("Reduction: —")
        else:
            self.result_points_label.setText(f"Resulting points: {state.result_point_count}")
            reduction = 100.0 * (count - state.result_point_count) / count if count else 0.0
            self.reduction_percent_label.setText(f"Reduction: {reduction:.1f}%")
        busy = state.status in {OperationStatus.PREPARING, OperationStatus.APPLYING}
        self.simplification_cancel_btn.setEnabled(busy)
        self.simplification_preview_btn.setEnabled(valid and not busy)
        self.simplification_apply_btn.setEnabled(
            valid
            and not busy
            and source_safe
            and state.result_revision == document.revision
        )
        status = {
            OperationStatus.PREPARING: "Calculating resolution reduction without changing the KML…",
            OperationStatus.APPLYING: "Applying retained points to in-memory editor contents…",
            OperationStatus.CANCELLED: "Resolution reduction cancelled.",
            OperationStatus.ERROR: state.error,
            OperationStatus.READY: (
                "Reduction result is current. Reliable timing is included in the deviation policy."
                if state.timestamps_reliable
                else "Reduction result is current; timing was not reliable enough to affect reduction."
            ),
        }.get(state.status, "")
        details = " ".join(
            (*state.warnings, binding.unsafe_reason if binding and binding.unsafe_reason else "")
        ).strip()
        self.simplification_status_label.setText(
            " ".join(value for value in (status, details) if value)
        )

    def _render_mode(self, mode: EditorMode) -> None:
        selected = EditorMode(mode)
        button = self.mode_buttons[selected]
        if not button.isChecked():
            button.setChecked(True)
        index = {
            EditorMode.TEXT: 0,
            EditorMode.CROP: 1,
            EditorMode.SIMPLIFY: 2,
        }[selected]
        self.workspace_stack.setCurrentIndex(index)
        if selected == EditorMode.CROP:
            self._schedule_crop_preview(immediate=True)
        else:
            self._crop_preview_timer.stop()
        if selected == EditorMode.SIMPLIFY:
            self._simplification_timer.start()

    def _set_crop_preview_placeholder(
        self,
        message: str,
        *,
        show_settings: bool = False,
    ) -> None:
        self.crop_preview_placeholder_message.setText(message)
        self.crop_preview_settings_btn.setVisible(
            bool(show_settings and self._maps_settings is not None)
        )
        self.crop_preview_stack.setCurrentWidget(self.crop_preview_placeholder)

    def _maps_api_key(self) -> str:
        return "" if self._maps_settings is None else self._maps_settings.api_key

    def _schedule_crop_preview(
        self,
        *,
        immediate: bool = False,
        conceal_existing: bool = False,
    ) -> None:
        self._crop_preview_timer.stop()
        if self.model.mode != EditorMode.CROP:
            return
        document = self.model.active_document
        if (
            document is None
            or document.parse_state.status != ParseStatus.VALID
            or document.parse_state.point_count < 2
        ):
            self._set_crop_preview_placeholder(
                "Select a current valid KML flight path to prepare the map."
            )
            return
        if not WEBENGINE_AVAILABLE:
            self._set_crop_preview_placeholder(
                "PyQt6-WebEngine is unavailable. Cropping and Apply Crop remain available."
            )
            return
        if not self._maps_api_key():
            self._set_crop_preview_placeholder(
                "Add a Google Maps API key to display the crop comparison. "
                "Cropping and Apply Crop remain available without it.",
                show_settings=True,
            )
            return
        if conceal_existing:
            self._set_crop_preview_placeholder("Preparing the crop comparison…")
        if not self.isVisible():
            self._crop_preview_refresh_pending = True
            return
        self._crop_preview_refresh_pending = False
        if immediate:
            self._request_crop_preview()
        else:
            self._crop_preview_timer.start()

    def _request_crop_preview(self) -> bool:
        self._crop_preview_timer.stop()
        document = self.model.active_document
        if (
            self.model.mode != EditorMode.CROP
            or document is None
            or document.parse_state.status != ParseStatus.VALID
            or document.parse_state.point_count < 2
            or document.crop_state.status == OperationStatus.APPLYING
        ):
            return False
        if not WEBENGINE_AVAILABLE or not self._maps_api_key():
            self._schedule_crop_preview()
            return False
        if not self.isVisible():
            self._crop_preview_refresh_pending = True
            return False
        self._crop_preview_refresh_pending = False
        return self.model.request_crop_preview(document.document_id)

    def _present_crop_scene(self, document_id, scene) -> None:
        if document_id != self.model.active_document_id:
            return
        self._pending_crop_scene = (document_id, scene)
        if self.model.mode != EditorMode.CROP or not self.isVisible():
            self._crop_preview_refresh_pending = True
            return
        api_key = self._maps_api_key()
        if not api_key:
            self._schedule_crop_preview()
            return
        presentation = PreviewPresentation(
            read_only=True,
            embedded=True,
            measurement_enabled=False,
            title="Crop comparison",
            ready_message="The map matches the current crop range.",
            legend=(
                "Bright line — retained path",
                "Grey line — excluded path",
            ),
        )
        self.crop_preview_stack.setCurrentWidget(self.crop_map_preview)
        try:
            self.crop_map_preview.set_scene(scene, api_key, presentation)
        except Exception as error:
            self._set_crop_preview_placeholder(
                str(error) or "The crop comparison could not be displayed."
            )
            return
        self._crop_preview_document_id = document_id
        self._crop_preview_refresh_pending = False

    def _maps_api_key_changed(self, api_key: str) -> None:
        self._crop_preview_timer.stop()
        if not str(api_key).strip():
            self.crop_map_preview.shutdown()
            self._set_crop_preview_placeholder(
                "Add a Google Maps API key to display the crop comparison. "
                "Cropping and Apply Crop remain available without it.",
                show_settings=True,
            )
            return
        self._schedule_crop_preview(immediate=True)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        super().showEvent(event)
        if self.model.mode != EditorMode.CROP:
            return
        if self._pending_crop_scene is not None:
            document_id, scene = self._pending_crop_scene
            self._present_crop_scene(document_id, scene)
        elif self._crop_preview_refresh_pending:
            self._schedule_crop_preview(immediate=True)

    def _text_changed(self) -> None:
        document = self.model.active_document
        if not self._rendering and document is not None:
            if not self._formatting_coordinates:
                self.format_status_label.clear()
            self.model.update_contents(document.document_id, self.text_editor.toPlainText())

    def _cursor_changed(self) -> None:
        document = self.model.active_document
        if self._rendering or document is None:
            return
        cursor = self.text_editor.textCursor()
        self.model.update_cursor(document.document_id, cursor.position(), cursor.anchor())

    def _flush_active_editor(self) -> None:
        document = self.model.active_document
        if document is None:
            return
        contents = self.text_editor.toPlainText()
        if contents != document.contents:
            self.model.update_contents(document.document_id, contents)
        cursor = self.text_editor.textCursor()
        self.model.update_cursor(document.document_id, cursor.position(), cursor.anchor())

    def validate_active_document(self) -> bool:
        self._flush_active_editor()
        document = self.model.active_document
        return False if document is None else self.model.validate_document_now(document.document_id)

    def _crop_range_changed(self, start: int, end: int) -> None:
        if self._rendering:
            return
        document = self.model.active_document
        if document is None or document.parse_state.status != ParseStatus.VALID:
            return
        self.model.update_crop(document.document_id, int(start), int(end))

    def _crop_range_interaction_finished(self) -> None:
        self._crop_preview_timer.stop()
        self._request_crop_preview()

    def _tolerance_changed(self, value: float) -> None:
        document = self.model.active_document
        if not self._rendering and document is not None:
            self.custom_tolerance_button.setChecked(True)
            self.model.update_simplification_tolerance(
                document.document_id,
                value,
                preset="custom",
            )
            self._simplification_timer.start()

    def _set_simplification_preset(self, preset: str, tolerance_m: float) -> None:
        if self._rendering:
            return
        document = self.model.active_document
        if document is None:
            return
        self._rendering = True
        try:
            self.tolerance_input.setValue(tolerance_m)
        finally:
            self._rendering = False
        self.model.update_simplification_tolerance(
            document.document_id,
            tolerance_m,
            preset=preset,
        )
        self._simplification_timer.start()

    def _custom_tolerance_toggled(self, checked: bool) -> None:
        self.tolerance_input.setEnabled(bool(checked and self.model.active_document))
        if checked and not self._rendering:
            self._tolerance_changed(self.tolerance_input.value())

    def _calculate_simplification(self) -> None:
        document = self.model.active_document
        if (
            document is not None
            and document.parse_state.status == ParseStatus.VALID
            and document.simplification_state.status
            not in {OperationStatus.PREPARING, OperationStatus.APPLYING}
            and document.simplification_state.result_revision != document.revision
        ):
            self.model.request_simplification(document.document_id)

    def reset_crop(self) -> None:
        document = self.model.active_document
        if document is not None:
            self.model.reset_crop(document.document_id)
            self._crop_preview_timer.stop()
            self._request_crop_preview()

    def preview_crop(self) -> bool:
        document = self.model.active_document
        return bool(document is not None and self.model.request_crop_preview(document.document_id))

    def apply_crop(self) -> bool:
        document = self.model.active_document
        return bool(document is not None and self.model.apply_crop(document.document_id))

    def reset_simplification(self) -> None:
        document = self.model.active_document
        if document is not None:
            self.model.reset_simplification(document.document_id)
            self._simplification_timer.start()

    def preview_simplification(self) -> bool:
        document = self.model.active_document
        return bool(
            document is not None
            and self.model.request_simplification(document.document_id, purpose="preview")
        )

    def apply_simplification(self) -> bool:
        document = self.model.active_document
        return bool(
            document is not None
            and self.model.request_simplification(document.document_id, purpose="apply")
        )

    def cancel_active_operations(self) -> None:
        document = self.model.active_document
        if document is not None:
            self.model.cancel_operations(document.document_id)

    def _preview_ready(self, document_id, kind, scene, _warnings) -> None:
        if document_id != self.model.active_document_id:
            return
        if kind == "crop":
            self._present_crop_scene(document_id, scene)
        else:
            self.preview_requested.emit(scene)

    def _operation_finished(self, document_id, _kind, _purpose) -> None:
        if document_id == self.model.active_document_id:
            self._render_active_document(document_id)

    def browse_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add KML Files",
            remembered_directory(FileDialogWorkflow.KML_EDITOR, FileDialogDirection.INPUT),
            "KML Files (*.kml)",
        )
        if not paths:
            return
        remember_file_selection(
            FileDialogWorkflow.KML_EDITOR,
            FileDialogDirection.INPUT,
            paths[0],
        )
        result = self.model.add_paths(paths)
        if result.errors:
            QMessageBox.warning(
                self,
                "Some KML files could not be opened",
                "\n".join(f"{error.path.name}: {error.message}" for error in result.errors),
            )

    def _confirm_unvalidated_source_save(self, document: KmlEditorDocumentState) -> str:
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("KML needs validation")
        message.setText(
            f'"{document.source_path.name}" has not passed validation in its current form.'
        )
        message.setInformativeText(
            "Save anyway, restore the last saved snapshot, or cancel without writing the file."
        )
        save_button = message.addButton("Save Anyway", QMessageBox.ButtonRole.AcceptRole)
        restore_button = message.addButton("Restore Saved", QMessageBox.ButtonRole.DestructiveRole)
        message.addButton(QMessageBox.StandardButton.Cancel)
        message.exec()
        if message.clickedButton() is save_button:
            return "save"
        if message.clickedButton() is restore_button:
            return "restore"
        return "cancel"

    def _save_document_to_source(self, document_id: UUID) -> bool:
        document = self.model.document(document_id)
        if document.parse_state.status != ParseStatus.VALID:
            choice = self._confirm_unvalidated_source_save(document)
            if choice == "cancel":
                return False
            if choice == "restore":
                self.model.restore_document(document_id)
                return True
        try:
            self.model.save_document(document_id)
        except (OSError, UnicodeError, ValueError) as error:
            QMessageBox.critical(self, "KML could not be saved", str(error))
            return False
        return True

    def save_active_document(self) -> bool:
        self._flush_active_editor()
        document = self.model.active_document
        return False if document is None else self._save_document_to_source(document.document_id)

    def save_active_document_as(self) -> bool:
        self._flush_active_editor()
        document = self.model.active_document
        if document is None:
            return False
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save KML As",
            suggested_save_path(FileDialogWorkflow.KML_EDITOR, document.source_path.name),
            "KML Files (*.kml)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path:
            return False
        path = ensure_extension(path, ".kml")
        destination = Path(path).expanduser().resolve(strict=False)
        if destination == document.source_path:
            return self._save_document_to_source(document.document_id)
        if destination.exists() and destination != document.source_path:
            answer = QMessageBox.question(
                self,
                "Replace existing file?",
                f'"{destination.name}" already exists. Replace it?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        try:
            self.model.save_document(document.document_id, destination)
        except (OSError, UnicodeError, ValueError, FileExistsError) as error:
            QMessageBox.critical(self, "KML could not be saved", str(error))
            return False
        remember_file_selection(
            FileDialogWorkflow.KML_EDITOR,
            FileDialogDirection.OUTPUT,
            destination,
        )
        return True

    def restore_active_document(self) -> bool:
        self._flush_active_editor()
        document = self.model.active_document
        if document is None or not document.dirty:
            return False
        answer = QMessageBox.question(
            self,
            "Restore saved contents?",
            f'Discard the unsaved changes to "{document.source_path.name}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.model.restore_document(document.document_id)
        return True

    def _ask_unsaved(self, dirty, action: str) -> str:
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("Unsaved KML changes")
        message.setText(
            f"Save changes to {len(dirty)} KML file"
            + ("s" if len(dirty) != 1 else "")
            + f" before {action}?"
        )
        message.setInformativeText("\n".join(document.source_path.name for document in dirty))
        save_button = message.addButton("Save All", QMessageBox.ButtonRole.AcceptRole)
        discard_button = message.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        message.addButton(QMessageBox.StandardButton.Cancel)
        message.exec()
        clicked = message.clickedButton()
        if clicked is discard_button:
            return "discard"
        if clicked is not save_button:
            return "cancel"
        return "save"

    def _resolve_unsaved(self, document_ids, action: str) -> bool:
        dirty = [
            self.model.document(document_id)
            for document_id in document_ids
            if self.model.document(document_id).dirty
        ]
        if not dirty:
            return True
        choice = self._ask_unsaved(dirty, action)
        if choice == "discard":
            return True
        if choice == "cancel":
            return False
        return all(self._save_document_to_source(document.document_id) for document in dirty)

    def remove_selected_files(self) -> bool:
        self._flush_active_editor()
        selected = [
            document_id
            for item in self.file_list.selectedItems()
            if (document_id := self._item_document_id(item)) is not None
        ]
        if not selected:
            return False
        if not self._resolve_unsaved(selected, "removing them"):
            return False
        self.model.remove_documents(selected)
        return True

    def confirm_close(self) -> bool:
        self._flush_active_editor()
        return self._resolve_unsaved(self.model.dirty_document_ids, "closing the application")

    def shutdown(self) -> None:
        """Cooperatively stop editor work before the application is destroyed."""
        self._crop_preview_timer.stop()
        self._simplification_timer.stop()
        self.crop_map_preview.shutdown()
        self.model.shutdown()


__all__ = ["KmlEditorPage"]
