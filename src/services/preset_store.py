"""UUID-backed repository and import/export services for application presets."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from .preset_filenames import canonical_filename, readable_export_filename
from .preset_model import (
    CURRENT_FORMAT_VERSION,
    Preset,
    PresetAlreadyExistsError,
    PresetDestinationExistsError,
    PresetError,
    PresetIOError,
    PresetMalformedJsonError,
    PresetNameConflictError,
    PresetNameError,
    PresetNotFoundError,
    PresetStoreError,
    PresetType,
    PresetValidationError,
    UnsafePresetPathError,
    preset_name_key,
    validate_preset_name,
)


_MIGRATION_NAMESPACE = UUID("c9d026b2-d0b8-4e9e-bcc6-39ac19b73989")
_LEGACY_BASE64_PREFIX = "preset-v1-"


def _strict_json_loads(contents: str) -> object:
    """Parse standards-compliant JSON, rejecting NaN and infinity constants."""
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant {value}")

    try:
        return json.loads(contents, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as error:
        message = getattr(error, "msg", str(error))
        raise PresetMalformedJsonError(f"Malformed JSON: {message}.") from error


@dataclass(frozen=True)
class PresetRecord:
    """A validated preset paired with its internal managed file."""

    preset: Preset
    path: Path

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class ImportInspection:
    """A validated external preset plus any existing UUID match."""

    preset: Preset
    existing: PresetRecord | None

    @property
    def has_uuid_conflict(self) -> bool:
        return self.existing is not None


class PresetRepository:
    """Persist one preset type inside a single validated managed directory."""

    def __init__(
        self,
        directory: str | Path,
        preset_type: PresetType | str,
        *,
        legacy_managed_directories: tuple[str | Path, ...] = (),
        legacy_readonly_directories: tuple[str | Path, ...] = (),
        backup_directory: str | Path | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.preset_type = PresetType(preset_type)
        self._root = self.directory.resolve(strict=False)
        self.backup_directory = (
            Path(backup_directory) if backup_directory is not None else None
        )
        self.issues: list[str] = []
        self._available = True
        try:
            self._ensure_directory()
        except PresetError as error:
            # Presets may be unavailable without preventing the rest of the
            # application from opening. Explicit operations still fail.
            self._available = False
            self.issues.append(str(error))
            return
        self._migrate_sources(legacy_managed_directories, move_to_backup=True)
        self._migrate_sources(legacy_readonly_directories, move_to_backup=False)

    @staticmethod
    def validate_display_name(name: object) -> str:
        """Compatibility wrapper around the shared name validator."""
        return validate_preset_name(name)

    @staticmethod
    def name_key(name: object) -> str:
        """Compatibility wrapper for case-insensitive name identity."""
        return preset_name_key(name)

    @staticmethod
    def canonical_filename(name: str) -> str:
        """Return the base canonical filename without checking a repository."""
        return canonical_filename(name)

    def _ensure_directory(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise PresetIOError(
                f"Cannot create preset directory {self.directory}: {error}"
            ) from error
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise UnsafePresetPathError("Preset directory is not a safe directory.")

    def _managed_path(self, filename: str) -> Path:
        if Path(filename).name != filename or not filename.lower().endswith(".json"):
            raise UnsafePresetPathError("Preset storage rejected an unsafe filename.")
        path = self.directory / filename
        if path.parent.resolve(strict=False) != self._root:
            raise UnsafePresetPathError("Preset storage rejected an unsafe path.")
        return path

    def _require_available(self) -> None:
        if not self._available:
            raise PresetIOError(f"Preset directory is unavailable: {self.directory}")

    def _require_safe_file(self, path: Path) -> Path:
        try:
            if (
                path.parent.resolve(strict=False) != self._root
                or path.resolve(strict=False).parent != self._root
                or path.is_symlink()
                or not path.is_file()
            ):
                raise UnsafePresetPathError("Preset storage rejected an unsafe file.")
        except OSError as error:
            raise UnsafePresetPathError("Preset storage rejected an unsafe file.") from error
        return path

    @staticmethod
    def _serialized(preset: Preset) -> bytes:
        return (json.dumps(preset.to_dict(), indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )

    @staticmethod
    def _write_temporary(parent: Path, payload: bytes) -> Path:
        temporary = parent / f".preset-{uuid4().hex}.tmp"
        with temporary.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        return temporary

    @classmethod
    def _atomic_write(cls, path: Path, payload: bytes, *, overwrite: bool) -> None:
        temporary: Path | None = None
        try:
            temporary = cls._write_temporary(path.parent, payload)
            if overwrite:
                if path.exists() and (path.is_symlink() or not path.is_file()):
                    raise UnsafePresetPathError("Refusing to overwrite an unsafe file.")
                os.replace(temporary, path)
                temporary = None
            else:
                try:
                    os.link(temporary, path)
                except FileExistsError as error:
                    raise PresetDestinationExistsError(
                        f"A file already exists at {path}."
                    ) from error
                except OSError as error:
                    unsupported = {
                        errno.EACCES,
                        errno.EPERM,
                        errno.EXDEV,
                        getattr(errno, "ENOTSUP", errno.EPERM),
                        getattr(errno, "EOPNOTSUPP", errno.EPERM),
                    }
                    if error.errno not in unsupported:
                        raise
                    # Some removable/network filesystems do not support hard
                    # links. O_EXCL still guarantees no silent overwrite,
                    # though the fallback cannot make visibility atomic.
                    created = False
                    try:
                        with path.open("xb") as output:
                            created = True
                            output.write(payload)
                            output.flush()
                            os.fsync(output.fileno())
                    except Exception:
                        if created:
                            try:
                                path.unlink(missing_ok=True)
                            except OSError:
                                pass
                        raise
        except PresetError:
            raise
        except OSError as error:
            raise PresetIOError(f"Cannot write preset file {path}: {error}") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _read_record(self, path: Path) -> PresetRecord:
        self._require_safe_file(path)
        try:
            contents = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PresetIOError(f"Cannot read preset file {path}: {error}") from error
        try:
            document = _strict_json_loads(contents)
        except PresetMalformedJsonError as error:
            raise PresetMalformedJsonError(f"Malformed JSON in {path.name}: {error}") from error
        return PresetRecord(
            Preset.from_dict(document, expected_type=self.preset_type),
            path,
        )

    def load_all(self) -> dict[UUID, PresetRecord]:
        """Load valid direct-child presets keyed only by UUID."""
        records: dict[UUID, PresetRecord] = {}
        if not self._available:
            return records
        try:
            paths = sorted(self.directory.glob("*.json"), key=lambda item: item.name.casefold())
        except OSError as error:
            self.issues.append(f"Cannot list preset directory: {error}")
            return records
        for path in paths:
            try:
                record = self._read_record(path)
            except PresetError as error:
                self.issues.append(str(error))
                continue
            if record.preset.id in records:
                self.issues.append(
                    f"Duplicate preset UUID {record.preset.id} in {path.name}; the file was ignored."
                )
                continue
            records[record.preset.id] = record
        return records

    def get(self, preset_id: UUID | str) -> PresetRecord | None:
        """Return a preset by UUID, or None when it does not exist."""
        try:
            resolved_id = preset_id if isinstance(preset_id, UUID) else UUID(str(preset_id))
        except (TypeError, ValueError):
            return None
        return self.load_all().get(resolved_id)

    def find_by_name(
        self, name: str, *, excluding_id: UUID | None = None
    ) -> PresetRecord | None:
        """Find a preset using normalized case-insensitive display-name equality."""
        key = preset_name_key(name)
        for record in self.load_all().values():
            if record.preset.id != excluding_id and preset_name_key(record.preset.name) == key:
                return record
        return None

    def unique_name(self, preferred_name: str) -> str:
        """Return the first available Name, Name (2), ... display name."""
        preferred = validate_preset_name(preferred_name)
        if self.find_by_name(preferred) is None:
            return preferred
        suffix = 2
        while True:
            suffix_text = f" ({suffix})"
            base = preferred[: 120 - len(suffix_text)].rstrip()
            candidate = f"{base}{suffix_text}"
            if self.find_by_name(candidate) is None:
                return candidate
            suffix += 1

    def _new_path(self, name: str, *, excluding: Path | None = None) -> Path:
        self._require_available()
        try:
            filenames = [
                path.name
                for path in self.directory.iterdir()
                if path != excluding and path.is_file()
            ]
        except OSError as error:
            raise PresetIOError(
                f"Cannot inspect preset directory {self.directory}: {error}"
            ) from error
        return self._managed_path(canonical_filename(name, filenames))

    def _insert(self, preset: Preset) -> PresetRecord:
        self._require_available()
        if preset.preset_type != self.preset_type:
            raise PresetValidationError("Preset type does not match this repository.")
        if self.get(preset.id) is not None:
            raise PresetValidationError(f"Preset UUID {preset.id} already exists.")
        if self.find_by_name(preset.name) is not None:
            raise PresetNameConflictError(preset.name)
        path = self._new_path(preset.name)
        self._atomic_write(path, self._serialized(preset), overwrite=False)
        return PresetRecord(preset, path)

    def create(self, name: str, data: dict[str, Any]) -> PresetRecord:
        """Create a new preset with a generated UUID."""
        return self._insert(Preset.create(self.preset_type, name, data))

    def import_new(self, preset: Preset, *, name: str | None = None) -> PresetRecord:
        """Store a validated external preset while preserving its UUID."""
        candidate = preset.renamed(name) if name is not None else preset
        return self._insert(candidate)

    def import_copy(self, preset: Preset, *, name: str | None = None) -> PresetRecord:
        """Store an external preset as a logically independent UUID copy."""
        return self._insert(preset.clone(name=name))

    def _replace_record(self, existing: PresetRecord, candidate: Preset) -> PresetRecord:
        if candidate.id != existing.preset.id:
            raise PresetValidationError("A preset UUID cannot be changed during an update.")
        if candidate.preset_type != self.preset_type:
            raise PresetValidationError("Preset type does not match this repository.")
        name_conflict = self.find_by_name(candidate.name, excluding_id=candidate.id)
        if name_conflict is not None:
            raise PresetNameConflictError(candidate.name)

        old_path = self._require_safe_file(existing.path)
        new_path = self._new_path(candidate.name, excluding=old_path)
        if old_path.name.casefold() == new_path.name.casefold():
            self._atomic_write(old_path, self._serialized(candidate), overwrite=True)
            return PresetRecord(candidate, old_path)

        self._atomic_write(new_path, self._serialized(candidate), overwrite=False)
        try:
            old_path.unlink()
        except OSError as error:
            try:
                new_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise PresetIOError(f"Cannot finish replacing {old_path}: {error}") from error
        return PresetRecord(candidate, new_path)

    def update_data(self, preset_id: UUID | str, data: dict[str, Any]) -> PresetRecord:
        """Update settings while preserving UUID, name, and managed identity."""
        existing = self.get(preset_id)
        if existing is None:
            raise PresetNotFoundError(f"Preset {preset_id} was not found.")
        return self._replace_record(existing, existing.preset.with_data(data))

    def rename(self, preset_id: UUID | str, name: str) -> PresetRecord:
        """Rename a preset and recanonicalize its file without changing UUID."""
        existing = self.get(preset_id)
        if existing is None:
            raise PresetNotFoundError(f"Preset {preset_id} was not found.")
        return self._replace_record(existing, existing.preset.renamed(name))

    def replace_import(self, preset: Preset, *, name: str | None = None) -> PresetRecord:
        """Replace the matching UUID using imported metadata and data."""
        existing = self.get(preset.id)
        if existing is None:
            raise PresetNotFoundError(f"Preset {preset.id} was not found.")
        candidate = preset.renamed(name) if name is not None else preset
        return self._replace_record(existing, candidate)

    def delete(self, preset_id: UUID | str | PresetRecord | dict[str, Any]) -> None:
        """Delete only a repository record resolved by UUID."""
        self._require_available()
        if isinstance(preset_id, PresetRecord):
            resolved_id: UUID | str = preset_id.preset.id
        elif isinstance(preset_id, dict):
            raw_id = preset_id.get("id") or preset_id.get("preset_id")
            if raw_id is None:
                raise UnsafePresetPathError("Preset entry does not contain a UUID.")
            resolved_id = raw_id
        else:
            resolved_id = preset_id
        existing = self.get(resolved_id)
        if existing is None:
            raise PresetNotFoundError(f"Preset {resolved_id} was not found.")
        path = self._require_safe_file(existing.path)
        try:
            path.unlink()
        except OSError as error:
            raise PresetIOError(f"Cannot delete preset {path}: {error}") from error

    @staticmethod
    def _legacy_name(path: Path) -> str:
        stem = path.stem
        if stem.startswith(_LEGACY_BASE64_PREFIX):
            token = stem[len(_LEGACY_BASE64_PREFIX):]
            try:
                padding = "=" * (-len(token) % 4)
                decoded = base64.urlsafe_b64decode(token + padding).decode("utf-8")
                return validate_preset_name(decoded)
            except (UnicodeError, ValueError, PresetNameError):
                pass
        return validate_preset_name(stem)

    def _legacy_preset(self, source: Path, document: object) -> Preset:
        if isinstance(document, dict) and "formatVersion" in document:
            return Preset.from_dict(document, expected_type=self.preset_type)
        if not isinstance(document, dict):
            raise PresetValidationError("Legacy preset data must be a JSON object.")
        name = self._legacy_name(source)
        canonical_data = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(canonical_data.encode("utf-8")).hexdigest()
        migrated_id = uuid5(
            _MIGRATION_NAMESPACE,
            f"{self.preset_type.value}:{source.name.casefold()}:{digest}",
        )
        return Preset.create(self.preset_type, name, document, preset_id=migrated_id)

    def _backup_legacy(self, source: Path) -> None:
        if self.backup_directory is None:
            return
        try:
            self.backup_directory.mkdir(parents=True, exist_ok=True)
            destination = self.backup_directory / source.name
            suffix = 2
            while destination.exists():
                destination = self.backup_directory / f"{source.stem}-{suffix}{source.suffix}"
                suffix += 1
            shutil.move(str(source), str(destination))
        except OSError as error:
            self.issues.append(f"Preset migrated, but {source.name} could not be backed up: {error}")

    def _migrate_sources(
        self, directories: tuple[str | Path, ...], *, move_to_backup: bool
    ) -> None:
        for raw_directory in directories:
            source_directory = Path(raw_directory)
            if source_directory.resolve(strict=False) == self._root:
                continue
            try:
                sources = sorted(source_directory.glob("*.json"), key=lambda path: path.name.casefold())
            except OSError as error:
                self.issues.append(f"Cannot inspect legacy presets in {source_directory}: {error}")
                continue
            for source in sources:
                if source.is_symlink() or not source.is_file():
                    self.issues.append(f"Unsafe legacy preset {source.name} was ignored.")
                    continue
                try:
                    document = _strict_json_loads(source.read_text(encoding="utf-8"))
                    preset = self._legacy_preset(source, document)
                    existing = self.get(preset.id)
                    if existing is None:
                        unique_name = self.unique_name(preset.name)
                        self.import_new(preset, name=unique_name)
                    elif (
                        existing.preset.preset_type != preset.preset_type
                        or existing.preset.data != preset.data
                    ):
                        self.issues.append(
                            f"Legacy preset {source.name} conflicts with an existing UUID and was left untouched."
                        )
                        continue
                    if move_to_backup:
                        self._backup_legacy(source)
                except PresetMalformedJsonError as error:
                    self.issues.append(f"Malformed legacy preset {source.name}: {error}")
                except (OSError, UnicodeError, PresetError) as error:
                    self.issues.append(f"Could not migrate {source.name}: {error}")


class PresetImportExportService:
    """Validate external files and coordinate explicit import/export actions."""

    def __init__(self, repository: PresetRepository) -> None:
        self.repository = repository

    def inspect_import(self, path: str | Path) -> ImportInspection:
        """Read and validate an external file without changing repository state."""
        source = Path(path)
        try:
            contents = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PresetIOError(f"Cannot read preset file {source}: {error}") from error
        document = _strict_json_loads(contents)
        preset = Preset.from_dict(document, expected_type=self.repository.preset_type)
        return ImportInspection(preset, self.repository.get(preset.id))

    def import_new(self, preset: Preset, *, name: str | None = None) -> PresetRecord:
        """Import a new UUID after UI conflict decisions are complete."""
        return self.repository.import_new(preset, name=name)

    def import_copy(self, preset: Preset, *, name: str | None = None) -> PresetRecord:
        """Import a duplicate UUID as a newly identified copy."""
        return self.repository.import_copy(preset, name=name)

    def replace(self, preset: Preset, *, name: str | None = None) -> PresetRecord:
        """Replace an existing UUID with explicitly accepted imported content."""
        return self.repository.replace_import(preset, name=name)

    @staticmethod
    def suggested_export_filename(preset: Preset) -> str:
        """Return a readable default that is independent of the internal filename."""
        return readable_export_filename(preset.name)

    def export(
        self, preset: Preset, destination: str | Path, *, overwrite: bool = False
    ) -> Path:
        """Atomically export a complete envelope to an explicit destination."""
        path = Path(destination)
        if path.exists() and not overwrite:
            raise PresetDestinationExistsError(f"A file already exists at {path}.")
        if path.exists() and (path.is_dir() or path.is_symlink()):
            raise UnsafePresetPathError("Refusing to overwrite an unsafe export destination.")
        if not path.parent.is_dir():
            raise PresetIOError(f"Export directory does not exist: {path.parent}")
        PresetRepository._atomic_write(
            path,
            PresetRepository._serialized(preset),
            overwrite=overwrite,
        )
        return path


# Transitional compatibility name; new code should use PresetRepository.
PresetStore = PresetRepository


__all__ = [
    "CURRENT_FORMAT_VERSION",
    "ImportInspection",
    "Preset",
    "PresetAlreadyExistsError",
    "PresetDestinationExistsError",
    "PresetError",
    "PresetIOError",
    "PresetImportExportService",
    "PresetMalformedJsonError",
    "PresetNameConflictError",
    "PresetNameError",
    "PresetNotFoundError",
    "PresetRecord",
    "PresetRepository",
    "PresetStore",
    "PresetStoreError",
    "PresetType",
    "PresetValidationError",
    "UnsafePresetPathError",
]
