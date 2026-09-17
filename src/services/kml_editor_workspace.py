"""Authoritative document state, validation and file lifecycle for the KML editor."""

from __future__ import annotations

import codecs
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from enum import Enum
import math
import os
from pathlib import Path
import re
import tempfile
from threading import Event, Lock
import time
from typing import Callable
from uuid import UUID, uuid4

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, pyqtSignal

from .kml_file_handling import (
    KmlDiagnostic,
    KmlDiagnosticCode,
    KmlDiagnosticSeverity,
    KmlParseError,
    KmlTrack,
    parse_kml_text,
)
from .kml_editor_operations import (
    KmlEditorOperationCancelled,
    SimplificationResult,
    apply_retained_indices,
    build_crop_preview_scene,
    build_simplification_preview_scene,
    crop_indices,
    simplify_track,
)


_XML_ENCODING_RE = re.compile(
    br"<\?xml[^>]*\bencoding\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)


class EditorMode(str, Enum):
    TEXT = "text"
    CROP = "crop"
    SIMPLIFY = "simplify"


class ParseStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    STALE = "stale"
    VALIDATING = "validating"


class OperationStatus(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    READY = "ready"
    APPLYING = "applying"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ParseState:
    status: ParseStatus
    diagnostics: tuple[KmlDiagnostic, ...] = ()
    track: KmlTrack | None = None
    revision: int = 0

    @property
    def point_count(self) -> int:
        return len(self.track.points) if self.track is not None else 0


@dataclass(frozen=True, slots=True)
class CropState:
    start_index: int | None = None
    end_index: int | None = None
    status: OperationStatus = OperationStatus.IDLE
    operation_revision: int = 0
    warnings: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True, slots=True)
class SimplificationState:
    tolerance_m: float = 5.0
    preset: str = "balanced"
    result_point_count: int | None = None
    kept_indices: tuple[int, ...] = ()
    result_revision: int | None = None
    status: OperationStatus = OperationStatus.IDLE
    operation_revision: int = 0
    timestamps_reliable: bool = False
    warnings: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True, slots=True)
class KmlEditorDocumentState:
    document_id: UUID
    source_path: Path
    contents: str
    saved_contents: str
    encoding: str
    newline: str
    parse_state: ParseState
    saved_parse_state: ParseState
    crop_state: CropState
    simplification_state: SimplificationState
    revision: int = 0
    cursor_position: int = 0
    cursor_anchor: int = 0

    @property
    def dirty(self) -> bool:
        return self.contents != self.saved_contents


@dataclass(frozen=True, slots=True)
class LoadedKmlText:
    contents: str
    encoding: str
    newline: str


@dataclass(frozen=True, slots=True)
class DocumentLoadError:
    path: Path
    message: str


@dataclass(frozen=True, slots=True)
class AddDocumentsResult:
    document_ids: tuple[UUID, ...]
    errors: tuple[DocumentLoadError, ...]


@dataclass(frozen=True, slots=True)
class _ValidationResult:
    document_id: UUID
    revision: int
    generation: int
    track: KmlTrack | None
    diagnostics: tuple[KmlDiagnostic, ...]
    cancelled: bool = False


class _ValidationSignals(QObject):
    completed = pyqtSignal(object)


class _ValidationTask(QRunnable):
    def __init__(
        self,
        document_id: UUID,
        revision: int,
        generation: int,
        contents: str,
        source_name: str,
        validator: Callable[..., KmlTrack],
    ) -> None:
        super().__init__()
        self.document_id = document_id
        self.revision = revision
        self.generation = generation
        self.contents = contents
        self.source_name = source_name
        self.validator = validator
        self.cancel_event = Event()
        self.signals = _ValidationSignals()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        track = None
        diagnostics: tuple[KmlDiagnostic, ...] = ()
        try:
            if self.validator is parse_kml_text:
                track = self.validator(
                    self.contents,
                    source_name=self.source_name,
                    cancellation_check=self.cancel_event.is_set,
                )
            else:
                track = self.validator(self.contents, source_name=self.source_name)
            if self.cancel_event.is_set():
                self.signals.completed.emit(
                    _ValidationResult(
                        self.document_id,
                        self.revision,
                        self.generation,
                        None,
                        (),
                        True,
                    )
                )
                return
        except KmlParseError as error:
            diagnostics = (error.diagnostic,)
        except Exception:
            if self.cancel_event.is_set():
                self.signals.completed.emit(
                    _ValidationResult(
                        self.document_id,
                        self.revision,
                        self.generation,
                        None,
                        (),
                        True,
                    )
                )
                return
            diagnostics = (
                KmlDiagnostic(
                    code=KmlDiagnosticCode.STRUCTURE_UNSUPPORTED,
                    severity=KmlDiagnosticSeverity.ERROR,
                    message="The KML could not be validated.",
                    explanation="An unexpected error occurred while validating this document.",
                    suggestion="Try validating again. If the problem persists, reopen the file.",
                ),
            )
        self.signals.completed.emit(
            _ValidationResult(
                document_id=self.document_id,
                revision=self.revision,
                generation=self.generation,
                track=track,
                diagnostics=diagnostics,
            )
        )


@dataclass(frozen=True, slots=True)
class _OperationResult:
    document_id: UUID
    document_revision: int
    operation_revision: int
    kind: str
    purpose: str
    value: object | None = None
    error: str = ""
    cancelled: bool = False


class _OperationSignals(QObject):
    completed = pyqtSignal(object)


class _OperationTask:
    def __init__(
        self,
        document_id: UUID,
        document_revision: int,
        operation_revision: int,
        kind: str,
        purpose: str,
        operation,
    ) -> None:
        self.document_id = document_id
        self.document_revision = document_revision
        self.operation_revision = operation_revision
        self.kind = kind
        self.purpose = purpose
        self.operation = operation
        self.cancel_event = Event()
        self.signals = _OperationSignals()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        value = None
        error = ""
        cancelled = False
        try:
            if self.cancel_event.is_set():
                raise KmlEditorOperationCancelled()
            value = self.operation(self.cancel_event.is_set)
        except KmlEditorOperationCancelled:
            cancelled = True
        except Exception as exception:
            error = str(exception) or "The KML editor operation failed."
        self.signals.completed.emit(
            _OperationResult(
                self.document_id,
                self.document_revision,
                self.operation_revision,
                self.kind,
                self.purpose,
                value,
                error,
                cancelled,
            )
        )


@dataclass(frozen=True, slots=True)
class _OperationHandle:
    task: _OperationTask
    future: Future[None]


class _OperationExecutor:
    """Run editor operations on one persistent Python-managed worker thread."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="kml-editor-operation",
        )
        self._futures: set[Future[None]] = set()
        self._lock = Lock()
        self._shutdown = False

    @property
    def is_shutdown(self) -> bool:
        with self._lock:
            return self._shutdown

    def start(self, task: _OperationTask) -> Future[None]:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("The KML editor operation executor is shut down.")
            future = self._executor.submit(task.run)
            self._futures.add(future)
        future.add_done_callback(self._forget)
        return future

    def _forget(self, future: Future[None]) -> None:
        with self._lock:
            self._futures.discard(future)

    def wait_for_done(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                futures = tuple(self._futures)
            if not futures:
                return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0.0:
                return False
            _done, pending = wait(futures, timeout=remaining)
            if pending:
                return False

    def shutdown(self, *, wait_for_running: bool = False) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._executor.shutdown(
            wait=wait_for_running,
            cancel_futures=True,
        )


class KmlEditorFileRepository:
    """Read KML text without mutation and publish explicit saves atomically."""

    @staticmethod
    def _encoding_for(data: bytes) -> str:
        if data.startswith(codecs.BOM_UTF8):
            return "utf-8-sig"
        if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
            return "utf-32"
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return "utf-16"
        match = _XML_ENCODING_RE.search(data[:512])
        if match is None:
            return "utf-8"
        try:
            declared = match.group(1).decode("ascii")
            return codecs.lookup(declared).name
        except (UnicodeDecodeError, LookupError) as error:
            raise UnicodeError("The XML declaration names an unsupported encoding.") from error

    def load(self, path: Path) -> LoadedKmlText:
        data = path.read_bytes()
        encoding = self._encoding_for(data)
        try:
            decoded = data.decode(encoding)
        except UnicodeError as error:
            raise UnicodeError(
                f'{path.name} could not be decoded using its XML encoding "{encoding}".'
            ) from error
        newline = "\r\n" if "\r\n" in decoded else ("\r" if "\r" in decoded else "\n")
        contents = decoded.replace("\r\n", "\n").replace("\r", "\n")
        return LoadedKmlText(contents, encoding, newline)

    def save(
        self,
        path: Path,
        contents: str,
        *,
        encoding: str,
        newline: str,
    ) -> None:
        if newline not in {"\n", "\r\n", "\r"}:
            raise ValueError("Unsupported newline convention.")
        rendered = contents if newline == "\n" else contents.replace("\n", newline)
        data = rendered.encode(encoding)
        destination = path.resolve(strict=False)
        existing_mode = None
        try:
            existing_mode = destination.stat().st_mode
        except FileNotFoundError:
            pass
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            if existing_mode is not None:
                os.fchmod(descriptor, existing_mode)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, destination)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise


class KmlEditorWorkspaceModel(QObject):
    """Single source of truth shared by every KML editor mode."""

    documents_changed = pyqtSignal()
    active_document_changed = pyqtSignal(object)
    document_changed = pyqtSignal(object)
    mode_changed = pyqtSignal(object)
    validation_finished = pyqtSignal(object, int)
    operation_finished = pyqtSignal(object, str, str)
    preview_ready = pyqtSignal(object, str, object, object)

    VALIDATION_DEBOUNCE_MS = 300

    def __init__(
        self,
        *,
        repository: KmlEditorFileRepository | None = None,
        validator: Callable[..., KmlTrack] = parse_kml_text,
        validation_debounce_ms: int | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository or KmlEditorFileRepository()
        self.validator = validator
        self.validation_debounce_ms = (
            self.VALIDATION_DEBOUNCE_MS
            if validation_debounce_ms is None
            else max(0, int(validation_debounce_ms))
        )
        self._documents: dict[UUID, KmlEditorDocumentState] = {}
        self._order: list[UUID] = []
        self._active_document_id: UUID | None = None
        self._mode = EditorMode.TEXT
        self._validation_timers: dict[UUID, QTimer] = {}
        self._validation_tasks: dict[tuple[UUID, int, int], _ValidationTask] = {}
        self._validation_generations: dict[UUID, int] = {}
        self._validation_pool = QThreadPool(self)
        self._validation_pool.setMaxThreadCount(2)
        self._operation_executor = _OperationExecutor()
        self._operation_tasks: dict[
            tuple[UUID, str, int],
            _OperationHandle,
        ] = {}
        self._shutting_down = False

    @staticmethod
    def path_key(path: str | os.PathLike[str]) -> str:
        return os.path.normcase(str(Path(path).resolve(strict=False)))

    @property
    def documents(self) -> tuple[KmlEditorDocumentState, ...]:
        return tuple(self._documents[document_id] for document_id in self._order)

    @property
    def active_document_id(self) -> UUID | None:
        return self._active_document_id

    @property
    def active_document(self) -> KmlEditorDocumentState | None:
        if self._active_document_id is None:
            return None
        return self._documents.get(self._active_document_id)

    @property
    def mode(self) -> EditorMode:
        return self._mode

    @property
    def dirty_document_ids(self) -> tuple[UUID, ...]:
        return tuple(document.document_id for document in self.documents if document.dirty)

    def document(self, document_id: UUID) -> KmlEditorDocumentState:
        return self._documents[document_id]

    def document_id_for_path(self, path: str | os.PathLike[str]) -> UUID | None:
        key = self.path_key(path)
        for document in self.documents:
            if self.path_key(document.source_path) == key:
                return document.document_id
        return None

    @staticmethod
    def _validating_state(revision: int) -> ParseState:
        return ParseState(ParseStatus.VALIDATING, revision=revision)

    def _timer_for(self, document_id: UUID) -> QTimer:
        timer = self._validation_timers.get(document_id)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda selected=document_id: self.validate_document(selected))
            self._validation_timers[document_id] = timer
        return timer

    def _schedule_validation(self, document_id: UUID) -> None:
        timer = self._timer_for(document_id)
        timer.start(self.validation_debounce_ms)

    def validate_document(self, document_id: UUID) -> bool:
        document = self._documents.get(document_id)
        if document is None:
            return False
        timer = self._validation_timers.get(document_id)
        if timer is not None:
            timer.stop()
        revision = document.revision
        for (selected_id, selected_revision, _generation), selected_task in tuple(
            self._validation_tasks.items()
        ):
            if selected_id == document_id and selected_revision != revision:
                selected_task.cancel()
        if any(
            selected_id == document_id
            and selected_revision == revision
            and selected_generation == self._validation_generations.get(document_id)
            for selected_id, selected_revision, selected_generation in self._validation_tasks
        ):
            if document.parse_state.status != ParseStatus.VALIDATING:
                self._replace_document(
                    replace(document, parse_state=self._validating_state(revision)),
                    list_changed=True,
                )
            return False
        generation = self._validation_generations.get(document_id, 0) + 1
        self._validation_generations[document_id] = generation
        key = (document_id, revision, generation)
        if document.parse_state.status != ParseStatus.VALIDATING:
            document = replace(document, parse_state=self._validating_state(revision))
            self._replace_document(document, list_changed=True)
        task = _ValidationTask(
            document_id,
            revision,
            generation,
            document.contents,
            document.source_path.name,
            self.validator,
        )
        task.signals.completed.connect(
            self._validation_completed,
            Qt.ConnectionType.QueuedConnection,
        )
        self._validation_tasks[key] = task
        self._validation_pool.start(task)
        return True

    def validate_document_now(self, document_id: UUID) -> bool:
        return self.validate_document(document_id)

    def _validation_completed(self, result: _ValidationResult) -> None:
        self._validation_tasks.pop(
            (result.document_id, result.revision, result.generation),
            None,
        )
        document = self._documents.get(result.document_id)
        if (
            result.cancelled
            or document is None
            or document.revision != result.revision
            or self._validation_generations.get(result.document_id) != result.generation
        ):
            return
        if result.track is None:
            parse_state = ParseState(
                ParseStatus.INVALID,
                result.diagnostics,
                revision=result.revision,
            )
        else:
            parse_state = ParseState(
                ParseStatus.VALID,
                (),
                result.track,
                result.revision,
            )
        saved_parse_state = (
            parse_state if document.contents == document.saved_contents else document.saved_parse_state
        )
        updated = replace(
            document,
            parse_state=parse_state,
            saved_parse_state=saved_parse_state,
            crop_state=self._reconciled_crop(document.crop_state, parse_state),
        )
        self._replace_document(updated, list_changed=True)
        self.validation_finished.emit(result.document_id, result.revision)

    @staticmethod
    def _default_crop(parse_state: ParseState) -> CropState:
        if parse_state.status != ParseStatus.VALID or parse_state.point_count < 2:
            return CropState()
        return CropState(0, parse_state.point_count - 1)

    @staticmethod
    def _reconciled_crop(crop: CropState, parse_state: ParseState) -> CropState:
        if parse_state.status != ParseStatus.VALID or parse_state.point_count < 2:
            return crop
        maximum = parse_state.point_count - 1
        if crop.start_index is None or crop.end_index is None:
            return CropState(0, maximum)
        start = min(maximum, max(0, crop.start_index))
        end = min(maximum, max(start, crop.end_index))
        return CropState(start, end)

    def add_paths(self, paths) -> AddDocumentsResult:
        accepted: list[UUID] = []
        errors: list[DocumentLoadError] = []
        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve(strict=False)
            if path.suffix.lower() != ".kml":
                errors.append(DocumentLoadError(path, "Only .kml files are supported."))
                continue
            existing = self.document_id_for_path(path)
            if existing is not None:
                accepted.append(existing)
                continue
            try:
                loaded = self.repository.load(path)
            except (OSError, UnicodeError, ValueError) as error:
                errors.append(DocumentLoadError(path, str(error)))
                continue
            document_id = uuid4()
            parse_state = self._validating_state(0)
            document = KmlEditorDocumentState(
                document_id=document_id,
                source_path=path,
                contents=loaded.contents,
                saved_contents=loaded.contents,
                encoding=loaded.encoding,
                newline=loaded.newline,
                parse_state=parse_state,
                saved_parse_state=parse_state,
                crop_state=self._default_crop(parse_state),
                simplification_state=SimplificationState(),
            )
            self._documents[document_id] = document
            self._order.append(document_id)
            accepted.append(document_id)
            self.validate_document(document_id)
        if accepted:
            self.documents_changed.emit()
            self.set_active_document(accepted[0])
        return AddDocumentsResult(tuple(accepted), tuple(errors))

    def set_active_document(self, document_id: UUID | None) -> None:
        if document_id is not None and document_id not in self._documents:
            raise KeyError(document_id)
        if document_id == self._active_document_id:
            return
        previous_document_id = self._active_document_id
        if previous_document_id is not None:
            self._cancel_validation(previous_document_id)
            self.cancel_operations(previous_document_id)
        self._active_document_id = document_id
        self.active_document_changed.emit(document_id)
        if document_id is not None:
            document = self._documents[document_id]
            if document.parse_state.status == ParseStatus.STALE:
                self._schedule_validation(document_id)

    def set_mode(self, mode: EditorMode | str) -> None:
        selected = EditorMode(mode)
        if selected == self._mode:
            return
        self._mode = selected
        self.mode_changed.emit(selected)

    def _replace_document(self, document: KmlEditorDocumentState, *, list_changed=False) -> None:
        self._documents[document.document_id] = document
        if list_changed:
            self.documents_changed.emit()
        self.document_changed.emit(document.document_id)

    def _cancel_operation_tasks(self, document_id: UUID, kind: str | None = None) -> None:
        for key, handle in tuple(self._operation_tasks.items()):
            selected_id, selected_kind, _operation_revision = key
            if selected_id == document_id and (kind is None or selected_kind == kind):
                handle.task.cancel()
                if handle.future.cancel():
                    self._operation_tasks.pop(key, None)

    def _cancel_validation(self, document_id: UUID) -> None:
        cancelled = False
        for (selected_id, _revision, _generation), task in tuple(
            self._validation_tasks.items()
        ):
            if selected_id == document_id:
                task.cancel()
                cancelled = True
        timer = self._validation_timers.get(document_id)
        if timer is not None and timer.isActive():
            timer.stop()
            cancelled = True
        if not cancelled:
            return
        self._validation_generations[document_id] = (
            self._validation_generations.get(document_id, 0) + 1
        )
        document = self._documents.get(document_id)
        if document is not None and document.parse_state.status == ParseStatus.VALIDATING:
            self._replace_document(
                replace(
                    document,
                    parse_state=ParseState(ParseStatus.STALE, revision=document.revision),
                ),
                list_changed=True,
            )

    def cancel_operations(self, document_id: UUID) -> None:
        self._cancel_operation_tasks(document_id)
        document = self._documents.get(document_id)
        if document is None:
            return
        crop = document.crop_state
        simplify = document.simplification_state
        if crop.status in {OperationStatus.PREPARING, OperationStatus.APPLYING}:
            crop = replace(
                crop,
                status=OperationStatus.CANCELLED,
                operation_revision=crop.operation_revision + 1,
            )
        if simplify.status in {OperationStatus.PREPARING, OperationStatus.APPLYING}:
            simplify = replace(
                simplify,
                status=OperationStatus.CANCELLED,
                operation_revision=simplify.operation_revision + 1,
            )
        self._replace_document(
            replace(document, crop_state=crop, simplification_state=simplify)
        )

    def shutdown(self) -> None:
        """Cancel queued/running work without blocking the UI thread."""
        if self._shutting_down:
            return
        self._shutting_down = True
        for timer in self._validation_timers.values():
            timer.stop()
        for task in self._validation_tasks.values():
            task.cancel()
        for key, handle in tuple(self._operation_tasks.items()):
            handle.task.cancel()
            if handle.future.cancel():
                self._operation_tasks.pop(key, None)
        self._operation_executor.shutdown(wait_for_running=False)

    def update_contents(self, document_id: UUID, contents: str) -> None:
        document = self.document(document_id)
        if contents == document.contents:
            return
        self._cancel_operation_tasks(document_id)
        revision = document.revision + 1
        if contents == document.saved_contents:
            parse_state = replace(document.saved_parse_state, revision=revision)
        else:
            parse_state = ParseState(ParseStatus.STALE, revision=revision)
        self._replace_document(
            replace(
                document,
                contents=contents,
                parse_state=parse_state,
                crop_state=replace(
                    document.crop_state,
                    status=OperationStatus.IDLE,
                    warnings=(),
                    error="",
                ),
                simplification_state=replace(
                    document.simplification_state,
                    result_point_count=None,
                    kept_indices=(),
                    result_revision=None,
                    status=OperationStatus.IDLE,
                    warnings=(),
                    error="",
                ),
                revision=revision,
            ),
            list_changed=(
                document.dirty != (contents != document.saved_contents)
                or document.parse_state.status != parse_state.status
            ),
        )
        if parse_state.status in {ParseStatus.STALE, ParseStatus.VALIDATING}:
            self._schedule_validation(document_id)

    def update_cursor(self, document_id: UUID, position: int, anchor: int | None = None) -> None:
        document = self.document(document_id)
        # QTextCursor positions are UTF-16 code-unit offsets, not Python string
        # indices. Keep them opaque here and clamp against QTextDocument when
        # the view is restored.
        safe_position = max(0, int(position))
        safe_anchor = safe_position if anchor is None else max(0, int(anchor))
        if (safe_position, safe_anchor) == (document.cursor_position, document.cursor_anchor):
            return
        self._documents[document_id] = replace(
            document,
            cursor_position=safe_position,
            cursor_anchor=safe_anchor,
        )

    def update_crop(self, document_id: UUID, start_index: int, end_index: int) -> None:
        document = self.document(document_id)
        count = document.parse_state.point_count
        if document.parse_state.status != ParseStatus.VALID or count < 2:
            raise ValueError("Crop range requires a current valid track.")
        if not (0 <= start_index < end_index < count):
            raise ValueError("Crop range must retain at least two current track points.")
        self._cancel_operation_tasks(document_id, "crop")
        crop = replace(
            document.crop_state,
            start_index=start_index,
            end_index=end_index,
            operation_revision=document.crop_state.operation_revision + 1,
            status=OperationStatus.IDLE,
            warnings=(),
            error="",
        )
        if crop != document.crop_state:
            self._replace_document(replace(document, crop_state=crop))

    def update_simplification_tolerance(
        self,
        document_id: UUID,
        tolerance_m: float,
        *,
        preset: str = "custom",
    ) -> None:
        tolerance = float(tolerance_m)
        if not math.isfinite(tolerance) or tolerance <= 0:
            raise ValueError("Maximum path deviation must be a positive finite distance.")
        document = self.document(document_id)
        self._cancel_operation_tasks(document_id, "simplify")
        state = replace(
            document.simplification_state,
            tolerance_m=tolerance,
            preset=str(preset),
            result_point_count=None,
            kept_indices=(),
            result_revision=None,
            status=OperationStatus.IDLE,
            operation_revision=document.simplification_state.operation_revision + 1,
            warnings=(),
            error="",
        )
        if state != document.simplification_state:
            self._replace_document(replace(document, simplification_state=state))

    def reset_crop(self, document_id: UUID) -> None:
        document = self.document(document_id)
        if document.parse_state.status != ParseStatus.VALID or document.parse_state.point_count < 2:
            return
        self._cancel_operation_tasks(document_id, "crop")
        state = CropState(
            0,
            document.parse_state.point_count - 1,
            operation_revision=document.crop_state.operation_revision + 1,
        )
        self._replace_document(replace(document, crop_state=state))

    def reset_simplification(self, document_id: UUID) -> None:
        document = self.document(document_id)
        self._cancel_operation_tasks(document_id, "simplify")
        state = SimplificationState(
            operation_revision=document.simplification_state.operation_revision + 1
        )
        self._replace_document(replace(document, simplification_state=state))

    def _start_operation(
        self,
        document: KmlEditorDocumentState,
        *,
        kind: str,
        purpose: str,
        operation_revision: int,
        operation,
    ) -> bool:
        if self._shutting_down or self._operation_executor.is_shutdown:
            return False
        self._cancel_operation_tasks(document.document_id, kind)
        task = _OperationTask(
            document.document_id,
            document.revision,
            operation_revision,
            kind,
            purpose,
            operation,
        )
        task.signals.completed.connect(
            self._operation_completed,
            Qt.ConnectionType.QueuedConnection,
        )
        future = self._operation_executor.start(task)
        key = (document.document_id, kind, operation_revision)
        self._operation_tasks[key] = _OperationHandle(task, future)
        return True

    def request_crop_preview(self, document_id: UUID) -> bool:
        if self._shutting_down:
            return False
        document = self.document(document_id)
        track = document.parse_state.track
        crop = document.crop_state
        if track is None or document.parse_state.status != ParseStatus.VALID:
            return False
        if crop.start_index is None or crop.end_index is None:
            return False
        crop_indices(track, crop.start_index, crop.end_index)
        operation_revision = crop.operation_revision + 1
        crop = replace(
            crop,
            status=OperationStatus.PREPARING,
            operation_revision=operation_revision,
            warnings=(),
            error="",
        )
        self._replace_document(replace(document, crop_state=crop))
        return self._start_operation(
            document,
            kind="crop",
            purpose="preview",
            operation_revision=operation_revision,
            operation=lambda cancelled: build_crop_preview_scene(
                track,
                crop.start_index,
                crop.end_index,
                trace_id=f"editor-{document.document_id}-crop",
                label=document.source_path.name,
                cancellation_check=cancelled,
            ),
        )

    def apply_crop(self, document_id: UUID) -> bool:
        if self._shutting_down:
            return False
        document = self.document(document_id)
        track = document.parse_state.track
        crop = document.crop_state
        if track is None or document.parse_state.status != ParseStatus.VALID:
            return False
        if crop.start_index is None or crop.end_index is None:
            return False
        retained = crop_indices(track, crop.start_index, crop.end_index)
        operation_revision = crop.operation_revision + 1
        crop = replace(
            crop,
            status=OperationStatus.APPLYING,
            operation_revision=operation_revision,
            warnings=(),
            error="",
        )
        self._replace_document(replace(document, crop_state=crop))
        return self._start_operation(
            document,
            kind="crop",
            purpose="apply",
            operation_revision=operation_revision,
            operation=lambda cancelled: apply_retained_indices(
                document.contents,
                track,
                retained,
                cancellation_check=cancelled,
            ),
        )

    def request_simplification(self, document_id: UUID, *, purpose: str = "calculate") -> bool:
        if purpose not in {"calculate", "preview", "apply"}:
            raise ValueError("Unsupported simplification purpose.")
        if self._shutting_down:
            return False
        document = self.document(document_id)
        track = document.parse_state.track
        state = document.simplification_state
        if track is None or document.parse_state.status != ParseStatus.VALID:
            return False
        operation_revision = state.operation_revision + 1
        state = replace(
            state,
            status=(OperationStatus.APPLYING if purpose == "apply" else OperationStatus.PREPARING),
            operation_revision=operation_revision,
            warnings=(),
            error="",
        )
        self._replace_document(replace(document, simplification_state=state))

        def operation(cancelled):
            current_result = None
            if (
                document.simplification_state.result_revision == document.revision
                and document.simplification_state.kept_indices
            ):
                current_result = SimplificationResult(
                    document.simplification_state.kept_indices,
                    len(track.points),
                    document.simplification_state.tolerance_m,
                    document.simplification_state.timestamps_reliable,
                )
            result = current_result or simplify_track(
                track,
                state.tolerance_m,
                cancellation_check=cancelled,
            )
            if cancelled():
                raise KmlEditorOperationCancelled()
            if purpose == "preview":
                return (
                    result,
                    build_simplification_preview_scene(
                        track,
                        result,
                        trace_id=f"editor-{document.document_id}-simplify",
                        label=document.source_path.name,
                        cancellation_check=cancelled,
                    ),
                )
            if purpose == "apply":
                return (
                    result,
                    apply_retained_indices(
                        document.contents,
                        track,
                        result.kept_indices,
                        cancellation_check=cancelled,
                    ),
                )
            return result

        return self._start_operation(
            document,
            kind="simplify",
            purpose=purpose,
            operation_revision=operation_revision,
            operation=operation,
        )

    def _operation_completed(self, result: _OperationResult) -> None:
        key = (result.document_id, result.kind, result.operation_revision)
        self._operation_tasks.pop(key, None)
        if self._shutting_down:
            return
        document = self._documents.get(result.document_id)
        if document is None or document.revision != result.document_revision:
            return
        state = document.crop_state if result.kind == "crop" else document.simplification_state
        if state.operation_revision != result.operation_revision:
            return
        if result.cancelled:
            updated_state = replace(state, status=OperationStatus.CANCELLED)
        elif result.error:
            updated_state = replace(state, status=OperationStatus.ERROR, error=result.error)
        elif result.kind == "crop":
            warnings = (
                document.parse_state.track.source_binding.warnings
                if document.parse_state.track is not None
                and document.parse_state.track.source_binding is not None
                else ()
            )
            updated_state = replace(state, status=OperationStatus.READY, warnings=warnings)
        else:
            simplification = result.value[0] if isinstance(result.value, tuple) else result.value
            if not isinstance(simplification, SimplificationResult):
                updated_state = replace(state, status=OperationStatus.ERROR, error="Invalid simplification result.")
            else:
                binding = document.parse_state.track.source_binding if document.parse_state.track else None
                updated_state = replace(
                    state,
                    status=OperationStatus.READY,
                    result_point_count=simplification.result_count,
                    kept_indices=simplification.kept_indices,
                    result_revision=document.revision,
                    timestamps_reliable=simplification.timestamps_reliable,
                    warnings=binding.warnings if binding is not None else (),
                )
        if result.kind == "crop":
            document = replace(document, crop_state=updated_state)
        else:
            document = replace(document, simplification_state=updated_state)
        self._replace_document(document)

        if not result.cancelled and not result.error:
            if result.purpose == "preview":
                scene = result.value if result.kind == "crop" else result.value[1]
                self.preview_ready.emit(
                    result.document_id,
                    result.kind,
                    scene,
                    updated_state.warnings,
                )
            elif result.purpose == "apply":
                edit = result.value if result.kind == "crop" else result.value[1]
                self.update_contents(result.document_id, edit.contents)
                current = self._documents.get(result.document_id)
                if current is not None:
                    if result.kind == "crop":
                        current = replace(
                            current,
                            crop_state=replace(
                                current.crop_state,
                                status=OperationStatus.READY,
                                warnings=edit.warnings,
                            ),
                        )
                    else:
                        current = replace(
                            current,
                            simplification_state=replace(
                                current.simplification_state,
                                status=OperationStatus.READY,
                                warnings=edit.warnings,
                            ),
                        )
                    self._replace_document(current)
        self.operation_finished.emit(result.document_id, result.kind, result.purpose)

    def save_document(self, document_id: UUID, destination: str | os.PathLike[str] | None = None) -> None:
        document = self.document(document_id)
        path = (
            document.source_path
            if destination is None
            else Path(destination).expanduser().resolve(strict=False)
        )
        other = self.document_id_for_path(path)
        if other is not None and other != document_id:
            raise FileExistsError("That destination is already open in the KML Editor.")
        self.repository.save(
            path,
            document.contents,
            encoding=document.encoding,
            newline=document.newline,
        )
        parse_state = document.parse_state
        updated = replace(
            document,
            source_path=path,
            saved_contents=document.contents,
            parse_state=parse_state,
            saved_parse_state=parse_state,
            crop_state=self._reconciled_crop(document.crop_state, parse_state),
            simplification_state=replace(
                document.simplification_state,
                result_point_count=None,
                kept_indices=(),
                result_revision=None,
                status=OperationStatus.IDLE,
            ),
        )
        self._replace_document(updated, list_changed=True)
        if parse_state.status in {ParseStatus.STALE, ParseStatus.VALIDATING}:
            self._schedule_validation(document_id)

    def restore_document(self, document_id: UUID) -> None:
        document = self.document(document_id)
        if not document.dirty:
            return
        revision = document.revision + 1
        parse_state = replace(document.saved_parse_state, revision=revision)
        updated = replace(
            document,
            contents=document.saved_contents,
            parse_state=parse_state,
            crop_state=self._reconciled_crop(
                document.crop_state,
                parse_state,
            ),
            simplification_state=replace(
                document.simplification_state,
                result_point_count=None,
                kept_indices=(),
                result_revision=None,
                status=OperationStatus.IDLE,
            ),
            revision=revision,
        )
        self._replace_document(updated, list_changed=True)
        if parse_state.status in {ParseStatus.STALE, ParseStatus.VALIDATING}:
            self._schedule_validation(document_id)

    def remove_documents(self, document_ids) -> None:
        removing = {document_id for document_id in document_ids if document_id in self._documents}
        if not removing:
            return
        prior_active = self._active_document_id
        self._order = [document_id for document_id in self._order if document_id not in removing]
        for document_id in removing:
            self._cancel_operation_tasks(document_id)
            for (selected_id, _revision, _generation), task in tuple(
                self._validation_tasks.items()
            ):
                if selected_id == document_id:
                    task.cancel()
            timer = self._validation_timers.pop(document_id, None)
            if timer is not None:
                timer.stop()
                timer.deleteLater()
            self._documents.pop(document_id, None)
        if prior_active in removing:
            self._active_document_id = self._order[0] if self._order else None
        self.documents_changed.emit()
        if prior_active != self._active_document_id:
            self.active_document_changed.emit(self._active_document_id)


__all__ = [
    "AddDocumentsResult",
    "CropState",
    "DocumentLoadError",
    "EditorMode",
    "KmlDiagnostic",
    "KmlEditorDocumentState",
    "KmlEditorFileRepository",
    "KmlEditorWorkspaceModel",
    "OperationStatus",
    "ParseState",
    "ParseStatus",
    "SimplificationState",
]
