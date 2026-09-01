"""Durable, content-guarded drafts for per-KML transposition alignment."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from .map_preview import TraceAdjustment
from .transpose_coordinates import AlignmentMethod


ALIGNMENT_PROFILE_FORMAT_VERSION = 3
_AIRFIELD_FIELDS = {
    "airfieldName",
    "runway",
    "threshold",
    "trueHeading",
    "elevationM",
}
_MANUAL_FIELDS = {
    "targetCoordinate",
    "rotationDeg",
    "groundElevationM",
}
_FINGERPRINT_FIELDS = {"size", "sha256"}
_ADJUSTMENT_FIELDS = {"eastM", "northM", "upM", "yawDeg"}
_PRESET_SELECTION_FIELDS = {
    "sourceRunway",
    "targetRunway",
    "originalTrace",
    "targetTrace",
}
_DOCUMENT_FIELDS = {
    "formatVersion",
    "sourcePath",
    "fingerprint",
    "method",
    "runway",
    "manual",
    "presetSelections",
    "preview",
}
_VERSION_ONE_DOCUMENT_FIELDS = _DOCUMENT_FIELDS - {"presetSelections"}
_LEGACY_PROFILE_FORMAT_VERSIONS = {1, 2}


class AlignmentProfileStoreError(OSError):
    """A profile could not be written to managed application storage."""


@dataclass(frozen=True, slots=True)
class SourceFileFingerprint:
    """Content identity that deliberately ignores modification timestamps."""

    size: int
    sha256: str

    def __post_init__(self) -> None:
        if type(self.size) is not int or self.size < 0:
            raise ValueError("Fingerprint size must be a non-negative integer.")
        digest = str(self.sha256).lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("Fingerprint SHA-256 must contain 64 hexadecimal characters.")
        object.__setattr__(self, "sha256", digest)


def fingerprint_source_file(path: str | os.PathLike[str]) -> SourceFileFingerprint:
    """Return a size and SHA-256 identity for one source file."""

    source = Path(path)
    stat = source.stat()
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return SourceFileFingerprint(stat.st_size, digest.hexdigest())


def empty_airfield_draft() -> dict[str, str]:
    return {field: "" for field in sorted(_AIRFIELD_FIELDS)}


def empty_manual_draft() -> dict[str, str]:
    draft = {field: "" for field in sorted(_MANUAL_FIELDS)}
    draft["rotationDeg"] = "0"
    return draft


def empty_preset_selections() -> dict[str, str | None]:
    return {field: None for field in sorted(_PRESET_SELECTION_FIELDS)}


def _preset_selections(value: object) -> dict[str, str | None]:
    if not isinstance(value, Mapping) or set(value) != _PRESET_SELECTION_FIELDS:
        raise ValueError("Preset selections must contain exactly the supported cards.")
    result: dict[str, str | None] = {}
    for field in _PRESET_SELECTION_FIELDS:
        item = value[field]
        if item is not None and (not isinstance(item, str) or not item):
            raise ValueError("Preset selection identities must be non-empty text or null.")
        result[field] = item
    return result


def _string_mapping(
    value: object,
    *,
    fields: set[str],
    label: str,
) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{label} must contain exactly the supported fields.")
    result = {}
    for field in fields:
        item = value[field]
        if not isinstance(item, str):
            raise ValueError(f"{label} values must be text.")
        result[field] = item
    return result


@dataclass(frozen=True, slots=True)
class PreviewTargetSnapshot:
    """Target-side inputs used when preview offsets were accepted."""

    method: AlignmentMethod
    coordinate: str
    true_heading: str | None = None
    clockwise_rotation: str | None = None

    def __post_init__(self) -> None:
        method = AlignmentMethod(self.method)
        if not isinstance(self.coordinate, str):
            raise ValueError("Preview target coordinates must be text.")
        if method is AlignmentMethod.RUNWAY:
            if not isinstance(self.true_heading, str):
                raise ValueError("Runway preview targets require a true heading.")
            if self.clockwise_rotation is not None:
                raise ValueError(
                    "Runway preview targets cannot contain clockwise rotation."
                )
        else:
            if not isinstance(self.clockwise_rotation, str):
                raise ValueError(
                    "Manual preview targets require a clockwise rotation."
                )
            if self.true_heading is not None:
                raise ValueError("Manual preview targets cannot contain a true heading.")
        object.__setattr__(self, "method", method)


@dataclass(frozen=True, slots=True)
class AlignmentProfile:
    """Raw user drafts and accepted preview state for one unchanged KML."""

    method: AlignmentMethod = AlignmentMethod.RUNWAY
    runway_source_override: dict[str, str] | None = None
    runway_target: dict[str, str] | None = None
    manual: dict[str, str] | None = None
    preset_selections: dict[str, str | None] | None = None
    preview_signature: str | None = None
    preview_adjustment: TraceAdjustment | None = None
    preview_target_snapshot: PreviewTargetSnapshot | None = None

    def __post_init__(self) -> None:
        method = AlignmentMethod(self.method)
        source_override = self.runway_source_override
        if source_override is not None:
            source_override = _string_mapping(
                source_override,
                fields=_AIRFIELD_FIELDS,
                label="Runway source override",
            )
        target = _string_mapping(
            self.runway_target or empty_airfield_draft(),
            fields=_AIRFIELD_FIELDS,
            label="Runway target draft",
        )
        manual = _string_mapping(
            self.manual or empty_manual_draft(),
            fields=_MANUAL_FIELDS,
            label="Manual alignment draft",
        )
        selections = _preset_selections(
            self.preset_selections or empty_preset_selections()
        )
        signature = self.preview_signature
        adjustment = self.preview_adjustment
        target_snapshot = self.preview_target_snapshot
        if (signature is None) != (adjustment is None):
            raise ValueError(
                "Preview signature and adjustment must either both be present or both be absent."
            )
        if signature is not None and (not isinstance(signature, str) or not signature):
            raise ValueError("Preview signature must be non-empty text.")
        if adjustment is not None and not isinstance(adjustment, TraceAdjustment):
            raise ValueError("Preview adjustment must be a TraceAdjustment.")
        if target_snapshot is not None and not isinstance(
            target_snapshot, PreviewTargetSnapshot
        ):
            raise ValueError("Preview target snapshot must be a PreviewTargetSnapshot.")
        if target_snapshot is not None and adjustment is None:
            raise ValueError(
                "Preview target snapshot cannot exist without a preview adjustment."
            )
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "runway_source_override", source_override)
        object.__setattr__(self, "runway_target", target)
        object.__setattr__(self, "manual", manual)
        object.__setattr__(self, "preset_selections", selections)


@dataclass(frozen=True, slots=True)
class AlignmentProfileLoadResult:
    profile: AlignmentProfile | None
    notice: str | None = None


class AlignmentProfileStore:
    """Store one versioned JSON profile per normalized resolved source path."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = Path(directory)

    @staticmethod
    def resolved_source_path(path: str | os.PathLike[str]) -> str:
        return str(Path(path).resolve(strict=False))

    @classmethod
    def source_key(cls, path: str | os.PathLike[str]) -> str:
        resolved = os.path.normcase(cls.resolved_source_path(path))
        return hashlib.sha256(resolved.encode("utf-8")).hexdigest()

    def record_path(self, path: str | os.PathLike[str]) -> Path:
        return self.directory / f"{self.source_key(path)}.json"

    @staticmethod
    def _fingerprint_from_document(value: object) -> SourceFileFingerprint:
        if not isinstance(value, Mapping) or set(value) != _FINGERPRINT_FIELDS:
            raise ValueError("Fingerprint must contain size and sha256.")
        return SourceFileFingerprint(value["size"], value["sha256"])

    @staticmethod
    def _adjustment_from_document(value: object) -> TraceAdjustment:
        if not isinstance(value, Mapping) or set(value) != _ADJUSTMENT_FIELDS:
            raise ValueError("Preview adjustment contains unsupported fields.")
        values = []
        for field in ("eastM", "northM", "upM", "yawDeg"):
            raw = value[field]
            if isinstance(raw, bool):
                raise ValueError("Preview adjustment values must be finite numbers.")
            try:
                number = float(raw)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "Preview adjustment values must be finite numbers."
                ) from error
            if not math.isfinite(number):
                raise ValueError("Preview adjustment values must be finite numbers.")
            values.append(number)
        return TraceAdjustment(*values)

    @staticmethod
    def _target_snapshot_from_document(value: object) -> PreviewTargetSnapshot | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("Preview target snapshot must be an object or null.")
        try:
            method = AlignmentMethod(value.get("method"))
        except (TypeError, ValueError) as error:
            raise ValueError("Preview target snapshot has an unsupported method.") from error
        if method is AlignmentMethod.RUNWAY:
            if set(value) != {"method", "runwayCoordinate", "trueHeading"}:
                raise ValueError("Runway preview target contains unsupported fields.")
            return PreviewTargetSnapshot(
                method=method,
                coordinate=value["runwayCoordinate"],
                true_heading=value["trueHeading"],
            )
        if set(value) != {"method", "targetCoordinate", "clockwiseRotation"}:
            raise ValueError("Manual preview target contains unsupported fields.")
        return PreviewTargetSnapshot(
            method=method,
            coordinate=value["targetCoordinate"],
            clockwise_rotation=value["clockwiseRotation"],
        )

    @staticmethod
    def _target_snapshot_document(
        snapshot: PreviewTargetSnapshot | None,
    ) -> dict[str, object] | None:
        if snapshot is None:
            return None
        if snapshot.method is AlignmentMethod.RUNWAY:
            return {
                "method": snapshot.method.value,
                "runwayCoordinate": snapshot.coordinate,
                "trueHeading": snapshot.true_heading,
            }
        return {
            "method": snapshot.method.value,
            "targetCoordinate": snapshot.coordinate,
            "clockwiseRotation": snapshot.clockwise_rotation,
        }

    @classmethod
    def _profile_from_document(cls, document: object) -> tuple[str, SourceFileFingerprint, AlignmentProfile]:
        if not isinstance(document, Mapping):
            raise ValueError("Alignment profile contains unsupported or missing fields.")
        version = document.get("formatVersion")
        expected_fields = (
            _VERSION_ONE_DOCUMENT_FIELDS if version == 1 else _DOCUMENT_FIELDS
        )
        if set(document) != expected_fields:
            raise ValueError("Alignment profile contains unsupported or missing fields.")
        if version not in {*_LEGACY_PROFILE_FORMAT_VERSIONS, ALIGNMENT_PROFILE_FORMAT_VERSION}:
            raise ValueError(
                f"Unsupported alignment profile formatVersion {version!r}."
            )
        source_path = document["sourcePath"]
        if not isinstance(source_path, str) or not source_path:
            raise ValueError("Alignment profile sourcePath must be non-empty text.")
        fingerprint = cls._fingerprint_from_document(document["fingerprint"])
        runway = document["runway"]
        if not isinstance(runway, Mapping) or set(runway) != {"sourceOverride", "target"}:
            raise ValueError("Runway profile contains unsupported or missing fields.")
        source_override = runway["sourceOverride"]
        if source_override is not None:
            source_override = _string_mapping(
                source_override,
                fields=_AIRFIELD_FIELDS,
                label="Runway source override",
            )
        target = _string_mapping(
            runway["target"],
            fields=_AIRFIELD_FIELDS,
            label="Runway target draft",
        )
        manual = _string_mapping(
            document["manual"],
            fields=_MANUAL_FIELDS,
            label="Manual alignment draft",
        )
        selections = (
            empty_preset_selections()
            if version == 1
            else _preset_selections(document["presetSelections"])
        )
        preview = document["preview"]
        signature = None
        adjustment = None
        target_snapshot = None
        if preview is not None:
            preview_fields = (
                {"signature", "adjustment"}
                if version in _LEGACY_PROFILE_FORMAT_VERSIONS
                else {"signature", "adjustment", "targetSnapshot"}
            )
            if not isinstance(preview, Mapping) or set(preview) != preview_fields:
                raise ValueError("Preview profile contains unsupported or missing fields.")
            signature = preview["signature"]
            if not isinstance(signature, str) or not signature:
                raise ValueError("Preview signature must be non-empty text.")
            adjustment = cls._adjustment_from_document(preview["adjustment"])
            if version == ALIGNMENT_PROFILE_FORMAT_VERSION:
                target_snapshot = cls._target_snapshot_from_document(
                    preview["targetSnapshot"]
                )
        profile = AlignmentProfile(
            method=AlignmentMethod(document["method"]),
            runway_source_override=source_override,
            runway_target=target,
            manual=manual,
            preset_selections=selections,
            preview_signature=signature,
            preview_adjustment=adjustment,
            preview_target_snapshot=target_snapshot,
        )
        return source_path, fingerprint, profile

    def load(
        self,
        path: str | os.PathLike[str],
        fingerprint: SourceFileFingerprint,
    ) -> AlignmentProfileLoadResult:
        record = self.record_path(path)
        if not record.exists():
            return AlignmentProfileLoadResult(None)
        try:
            if record.is_symlink() or not record.is_file():
                raise ValueError("Alignment profile is not a safe regular file.")
            contents = record.read_text(encoding="utf-8")
            document = json.loads(
                contents,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant {value}")
                ),
            )
            stored_path, stored_fingerprint, profile = self._profile_from_document(
                document
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            return AlignmentProfileLoadResult(
                None,
                f"Saved alignment settings could not be loaded: {error}",
            )
        resolved = self.resolved_source_path(path)
        if os.path.normcase(stored_path) != os.path.normcase(resolved):
            return AlignmentProfileLoadResult(
                None,
                "Saved alignment settings did not match this KML path and were ignored.",
            )
        if stored_fingerprint != fingerprint:
            return AlignmentProfileLoadResult(
                None,
                "Saved alignment was not restored because this KML file has changed.",
            )
        return AlignmentProfileLoadResult(profile)

    @staticmethod
    def _document(
        source_path: str,
        fingerprint: SourceFileFingerprint,
        profile: AlignmentProfile,
    ) -> dict[str, object]:
        preview = None
        if profile.preview_adjustment is not None:
            adjustment = profile.preview_adjustment
            preview = {
                "signature": profile.preview_signature,
                "adjustment": {
                    "eastM": adjustment.east_m,
                    "northM": adjustment.north_m,
                    "upM": adjustment.up_m,
                    "yawDeg": adjustment.yaw_deg,
                },
                "targetSnapshot": AlignmentProfileStore._target_snapshot_document(
                    profile.preview_target_snapshot
                ),
            }
        return {
            "formatVersion": ALIGNMENT_PROFILE_FORMAT_VERSION,
            "sourcePath": source_path,
            "fingerprint": {
                "size": fingerprint.size,
                "sha256": fingerprint.sha256,
            },
            "method": profile.method.value,
            "runway": {
                "sourceOverride": profile.runway_source_override,
                "target": profile.runway_target,
            },
            "manual": profile.manual,
            "presetSelections": profile.preset_selections,
            "preview": preview,
        }

    def save(
        self,
        path: str | os.PathLike[str],
        fingerprint: SourceFileFingerprint,
        profile: AlignmentProfile,
    ) -> None:
        resolved = self.resolved_source_path(path)
        record = self.record_path(resolved)
        temporary: Path | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if self.directory.is_symlink() or not self.directory.is_dir():
                raise OSError("Alignment profile directory is not a safe directory.")
            payload = (
                json.dumps(
                    self._document(resolved, fingerprint, profile),
                    indent=2,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            temporary = self.directory / f".alignment-{uuid4().hex}.tmp"
            with temporary.open("xb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            if record.exists() and (record.is_symlink() or not record.is_file()):
                raise OSError("Refusing to replace an unsafe alignment profile.")
            os.replace(temporary, record)
            temporary = None
        except (OSError, TypeError, ValueError) as error:
            raise AlignmentProfileStoreError(
                f"Could not save alignment settings: {error}"
            ) from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


__all__ = [
    "ALIGNMENT_PROFILE_FORMAT_VERSION",
    "AlignmentProfile",
    "AlignmentProfileLoadResult",
    "AlignmentProfileStore",
    "AlignmentProfileStoreError",
    "PreviewTargetSnapshot",
    "SourceFileFingerprint",
    "empty_airfield_draft",
    "empty_manual_draft",
    "empty_preset_selections",
    "fingerprint_source_file",
]
