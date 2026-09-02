"""Phase-one KML editor workspace shell backed by one authoritative model."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSlider,
    QSplitter,
    QStackedWidget,
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
from services import (
    EditorMode,
    KmlEditorDocumentState,
    KmlEditorWorkspaceModel,
    ParseStatus,
)


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
    color: palette(bright-text);
    font-weight: 650;
}
"""


class KmlEditorPage(QWidget):
    """Multi-file KML editor shell; transforms are intentionally deferred."""

    preview_requested = pyqtSignal(object)

    def __init__(self, model: KmlEditorWorkspaceModel | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("KmlEditorPage")
        self.setStyleSheet(PAGE_STYLE)
        self.model = model or KmlEditorWorkspaceModel(parent=self)
        self._rendering = False

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
        action_row.addWidget(self.restore_btn)
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
        self.text_editor = QPlainTextEdit()
        self.text_editor.setAccessibleName("Editable KML contents")
        self.text_editor.setAccessibleDescription(
            "Raw KML text. Phase one marks edits as needing validation."
        )
        self.text_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text_editor.textChanged.connect(self._text_changed)
        layout.addWidget(self.text_editor, 1)
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
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        placeholder, self.crop_placeholder_label = self._placeholder(
            "Geographic preview",
            "Phase two will render the current validated track here.",
        )
        placeholder.setAccessibleName("Geographic preview placeholder")
        layout.addWidget(placeholder, 1)

        controls = QFrame()
        controls.setObjectName("workspacePanel")
        controls_layout = QGridLayout(controls)
        controls_layout.setContentsMargins(14, 12, 14, 12)
        controls_layout.setHorizontalSpacing(12)
        controls_layout.setVerticalSpacing(6)
        title = QLabel("Crop range")
        title.setObjectName("panelTitle")
        controls_layout.addWidget(title, 0, 0, 1, 2)
        self.crop_start_label = QLabel("Start point")
        self.crop_end_label = QLabel("End point")
        self.crop_start_slider = QSlider(Qt.Orientation.Horizontal)
        self.crop_start_slider.setAccessibleName("Crop start point")
        self.crop_end_slider = QSlider(Qt.Orientation.Horizontal)
        self.crop_end_slider.setAccessibleName("Crop end point")
        self.crop_start_slider.valueChanged.connect(self._crop_start_changed)
        self.crop_end_slider.valueChanged.connect(self._crop_end_changed)
        controls_layout.addWidget(self.crop_start_label, 1, 0)
        controls_layout.addWidget(self.crop_start_slider, 1, 1)
        controls_layout.addWidget(self.crop_end_label, 2, 0)
        controls_layout.addWidget(self.crop_end_slider, 2, 1)
        controls_layout.setColumnStretch(1, 1)
        layout.addWidget(controls)
        return page

    def _build_simplify_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        placeholder, self.simplification_result_label = self._placeholder(
            "Simplification results",
            "Phase three will compare the original and reduced tracks here.",
        )
        placeholder.setAccessibleName("Simplification results placeholder")
        layout.addWidget(placeholder, 1)

        controls = QFrame()
        controls.setObjectName("workspacePanel")
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(14, 12, 14, 12)
        title = QLabel("RDP tolerance")
        title.setObjectName("panelTitle")
        self.tolerance_input = QDoubleSpinBox()
        self.tolerance_input.setAccessibleName("Simplification tolerance metres")
        self.tolerance_input.setRange(0.0, 100000.0)
        self.tolerance_input.setDecimals(1)
        self.tolerance_input.setSuffix(" m")
        self.tolerance_input.valueChanged.connect(self._tolerance_changed)
        self.original_points_label = QLabel("Original points: —")
        self.original_points_label.setAccessibleName("Original KML point count")
        controls_layout.addWidget(title)
        controls_layout.addWidget(self.tolerance_input)
        controls_layout.addSpacing(12)
        controls_layout.addWidget(self.original_points_label)
        controls_layout.addStretch()
        layout.addWidget(controls)
        return page

    def _build_shortcuts(self) -> None:
        context = Qt.ShortcutContext.WidgetWithChildrenShortcut
        self.open_shortcut = QShortcut(QKeySequence.StandardKey.Open, self)
        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_as_shortcut = QShortcut(QKeySequence("Ctrl+Shift+S"), self)
        for shortcut in (self.open_shortcut, self.save_shortcut, self.save_as_shortcut):
            shortcut.setContext(context)
        self.open_shortcut.activated.connect(self.browse_files)
        self.save_shortcut.activated.connect(self.save_active_document)
        self.save_as_shortcut.activated.connect(self.save_active_document_as)

    def _set_tab_order(self) -> None:
        self.setTabOrder(self.file_list, self.add_files_btn)
        self.setTabOrder(self.add_files_btn, self.remove_files_btn)
        self.setTabOrder(self.remove_files_btn, self.text_mode_button)
        self.setTabOrder(self.text_mode_button, self.crop_mode_button)
        self.setTabOrder(self.crop_mode_button, self.simplify_mode_button)
        self.setTabOrder(self.simplify_mode_button, self.restore_btn)
        self.setTabOrder(self.restore_btn, self.save_btn)
        self.setTabOrder(self.save_btn, self.save_as_btn)
        self.setTabOrder(self.save_as_btn, self.text_editor)

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
                label = document.source_path.name + (" *" if document.dirty else "")
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, str(document.document_id))
                item.setToolTip(str(document.source_path))
                item.setData(
                    Qt.ItemDataRole.AccessibleDescriptionRole,
                    f"Full path: {document.source_path}. "
                    + ("Has unsaved changes." if document.dirty else "Saved."),
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
            self.model.set_active_document(self._item_document_id(current))

    def _document_changed(self, document_id: UUID) -> None:
        if document_id == self.model.active_document_id:
            self._render_active_document(document_id)

    def _render_active_document(self, _document_id) -> None:
        document = self.model.active_document
        self._rendering = True
        try:
            enabled = document is not None
            self.text_editor.setEnabled(enabled)
            self.save_btn.setEnabled(enabled and document.dirty)
            self.save_as_btn.setEnabled(enabled)
            self.restore_btn.setEnabled(enabled and document.dirty)
            self.remove_files_btn.setEnabled(bool(self.model.documents))
            if document is None:
                self.active_file_label.setText("No KML file selected")
                self.text_editor.clear()
                self._set_parse_status(None)
                self._render_crop(None)
                self._render_simplification(None)
                return
            self.active_file_label.setText(
                document.source_path.name + (" — Unsaved changes" if document.dirty else "")
            )
            self.active_file_label.setToolTip(str(document.source_path))
            if self.text_editor.toPlainText() != document.contents:
                self.text_editor.setPlainText(document.contents)
            self._set_parse_status(document)
            self._render_crop(document)
            self._render_simplification(document)
        finally:
            self._rendering = False
        self._render_document_list()

    def _set_parse_status(self, document: KmlEditorDocumentState | None) -> None:
        if document is None:
            status = "none"
            text = "Add a KML file to begin editing."
        else:
            parse = document.parse_state
            status = parse.status.value
            if parse.status == ParseStatus.VALID:
                text = f"Valid KML track — {parse.point_count} points"
            else:
                diagnostic = parse.diagnostics[0].message if parse.diagnostics else "No details available."
                prefix = "Needs validation" if parse.status == ParseStatus.STALE else "KML diagnostic"
                text = f"{prefix}: {diagnostic}"
        self.parse_status_label.setProperty("parseStatus", status)
        self.parse_status_label.setText(text)
        self.parse_status_label.style().unpolish(self.parse_status_label)
        self.parse_status_label.style().polish(self.parse_status_label)

    def _point_label(self, prefix: str, document: KmlEditorDocumentState, index: int) -> str:
        track = document.parse_state.track
        if track is None:
            return prefix
        timestamp = track.points[index].timestamp
        point = f"point {index + 1} of {len(track.points)}"
        return f"{prefix}: {timestamp} ({point})" if timestamp else f"{prefix}: {point}"

    def _render_crop(self, document: KmlEditorDocumentState | None) -> None:
        valid = document is not None and document.parse_state.status == ParseStatus.VALID
        count = document.parse_state.point_count if valid else 0
        enabled = valid and count >= 2
        for slider in (self.crop_start_slider, self.crop_end_slider):
            slider.setEnabled(enabled)
            slider.setRange(0, max(0, count - 1))
        if not enabled:
            self.crop_start_slider.setValue(0)
            self.crop_end_slider.setValue(0)
            self.crop_start_label.setText("Start point")
            self.crop_end_label.setText("End point")
            return
        start = document.crop_state.start_index or 0
        end = document.crop_state.end_index if document.crop_state.end_index is not None else count - 1
        self.crop_start_slider.setValue(start)
        self.crop_end_slider.setValue(end)
        self.crop_start_label.setText(self._point_label("Start", document, start))
        self.crop_end_label.setText(self._point_label("End", document, end))

    def _render_simplification(self, document: KmlEditorDocumentState | None) -> None:
        self.tolerance_input.setEnabled(document is not None)
        if document is None:
            self.tolerance_input.setValue(10.0)
            self.original_points_label.setText("Original points: —")
            return
        self.tolerance_input.setValue(document.simplification_state.tolerance_m)
        count = document.parse_state.point_count
        self.original_points_label.setText(
            f"Original points: {count}" if count else "Original points: unavailable"
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

    def _text_changed(self) -> None:
        document = self.model.active_document
        if not self._rendering and document is not None:
            self.model.update_contents(document.document_id, self.text_editor.toPlainText())

    def _crop_start_changed(self, value: int) -> None:
        if self._rendering:
            return
        document = self.model.active_document
        if document is None or document.parse_state.status != ParseStatus.VALID:
            return
        end = max(value, self.crop_end_slider.value())
        if end != self.crop_end_slider.value():
            self.crop_end_slider.setValue(end)
        self.model.update_crop(document.document_id, value, end)

    def _crop_end_changed(self, value: int) -> None:
        if self._rendering:
            return
        document = self.model.active_document
        if document is None or document.parse_state.status != ParseStatus.VALID:
            return
        start = min(value, self.crop_start_slider.value())
        if start != self.crop_start_slider.value():
            self.crop_start_slider.setValue(start)
        self.model.update_crop(document.document_id, start, value)

    def _tolerance_changed(self, value: float) -> None:
        document = self.model.active_document
        if not self._rendering and document is not None:
            self.model.update_simplification_tolerance(document.document_id, value)

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
        document = self.model.active_document
        return False if document is None else self._save_document_to_source(document.document_id)

    def save_active_document_as(self) -> bool:
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
        return self._resolve_unsaved(self.model.dirty_document_ids, "closing the application")


__all__ = ["KmlEditorPage"]
