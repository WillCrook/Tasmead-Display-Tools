"""Authoritative phase-one state and file lifecycle for the KML editor."""

from __future__ import annotations

import codecs
from dataclasses import dataclass, replace
from enum import Enum
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Callable
from uuid import UUID, uuid4

from PyQt6.QtCore import QObject, pyqtSignal

from .kml_file_handling import KmlTrack, parse_kml_track


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


@dataclass(frozen=True, slots=True)
class KmlDiagnostic:
    message: str


@dataclass(frozen=True, slots=True)
class ParseState:
    status: ParseStatus
    diagnostics: tuple[KmlDiagnostic, ...] = ()
    track: KmlTrack | None = None

    @property
    def point_count(self) -> int:
        return len(self.track.points) if self.track is not None else 0


@dataclass(frozen=True, slots=True)
class CropState:
    start_index: int | None = None
    end_index: int | None = None


@dataclass(frozen=True, slots=True)
class SimplificationState:
    tolerance_m: float = 10.0
    result_point_count: int | None = None


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

    def __init__(
        self,
        *,
        repository: KmlEditorFileRepository | None = None,
        parser: Callable[[Path], KmlTrack] = parse_kml_track,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository or KmlEditorFileRepository()
        self.parser = parser
        self._documents: dict[UUID, KmlEditorDocumentState] = {}
        self._order: list[UUID] = []
        self._active_document_id: UUID | None = None
        self._mode = EditorMode.TEXT

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

    def _parse(self, path: Path) -> ParseState:
        try:
            track = self.parser(path)
        except Exception as error:
            return ParseState(
                ParseStatus.INVALID,
                (KmlDiagnostic(str(error) or "The KML could not be parsed."),),
            )
        return ParseState(ParseStatus.VALID, (), track)

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
            parse_state = self._parse(path)
            document_id = uuid4()
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
        if accepted:
            self.documents_changed.emit()
            self.set_active_document(accepted[0])
        return AddDocumentsResult(tuple(accepted), tuple(errors))

    def set_active_document(self, document_id: UUID | None) -> None:
        if document_id is not None and document_id not in self._documents:
            raise KeyError(document_id)
        if document_id == self._active_document_id:
            return
        self._active_document_id = document_id
        self.active_document_changed.emit(document_id)

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

    def update_contents(self, document_id: UUID, contents: str) -> None:
        document = self.document(document_id)
        if contents == document.contents:
            return
        if contents == document.saved_contents:
            parse_state = document.saved_parse_state
        else:
            parse_state = ParseState(
                ParseStatus.STALE,
                (KmlDiagnostic("Current text has not been parsed."),),
            )
        self._replace_document(
            replace(document, contents=contents, parse_state=parse_state),
            list_changed=document.dirty != (contents != document.saved_contents),
        )

    def update_crop(self, document_id: UUID, start_index: int, end_index: int) -> None:
        document = self.document(document_id)
        count = document.parse_state.point_count
        if document.parse_state.status != ParseStatus.VALID or count < 2:
            raise ValueError("Crop range requires a current valid track.")
        if not (0 <= start_index <= end_index < count):
            raise ValueError("Crop range is outside the current track.")
        crop = CropState(start_index, end_index)
        if crop != document.crop_state:
            self._replace_document(replace(document, crop_state=crop))

    def update_simplification_tolerance(self, document_id: UUID, tolerance_m: float) -> None:
        tolerance = float(tolerance_m)
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("Simplification tolerance must be a non-negative number.")
        document = self.document(document_id)
        state = replace(
            document.simplification_state,
            tolerance_m=tolerance,
            result_point_count=None,
        )
        if state != document.simplification_state:
            self._replace_document(replace(document, simplification_state=state))

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
        parse_state = self._parse(path)
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
            ),
        )
        self._replace_document(updated, list_changed=True)

    def restore_document(self, document_id: UUID) -> None:
        document = self.document(document_id)
        if not document.dirty:
            return
        updated = replace(
            document,
            contents=document.saved_contents,
            parse_state=document.saved_parse_state,
            crop_state=self._reconciled_crop(
                document.crop_state,
                document.saved_parse_state,
            ),
            simplification_state=replace(
                document.simplification_state,
                result_point_count=None,
            ),
        )
        self._replace_document(updated, list_changed=True)

    def remove_documents(self, document_ids) -> None:
        removing = {document_id for document_id in document_ids if document_id in self._documents}
        if not removing:
            return
        prior_active = self._active_document_id
        self._order = [document_id for document_id in self._order if document_id not in removing]
        for document_id in removing:
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
    "ParseState",
    "ParseStatus",
    "SimplificationState",
]
