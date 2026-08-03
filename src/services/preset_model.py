"""Typed, versioned preset documents shared by the application."""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


CURRENT_FORMAT_VERSION = 1
MAX_PRESET_NAME_LENGTH = 120
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_DOCUMENT_FIELDS = {"formatVersion", "presetType", "id", "name", "data"}


class PresetError(Exception):
    """Base class for user-facing preset failures."""


class PresetValidationError(PresetError):
    """Raised when a preset document does not match the supported schema."""


class UnsupportedPresetVersionError(PresetValidationError):
    """Raised when a preset uses an unsupported format version."""


class PresetTypeMismatchError(PresetValidationError):
    """Raised when a preset belongs to another part of the application."""


class PresetNameError(PresetValidationError):
    """Raised when a display name is missing or unsafe."""


class PresetNameConflictError(PresetError):
    """Raised when a repository already contains the requested display name."""

    def __init__(self, name: str):
        self.display_name = name
        super().__init__(f'A preset named "{name}" already exists.')


class PresetNotFoundError(PresetError):
    """Raised when a requested UUID is not present in a repository."""


class PresetIOError(PresetError):
    """Raised when a preset file cannot be read or written."""


class PresetMalformedJsonError(PresetValidationError):
    """Raised when an imported file is not valid JSON."""


class PresetDestinationExistsError(PresetError):
    """Raised when an export would overwrite a file without confirmation."""


class UnsafePresetPathError(PresetError):
    """Raised when a managed path escapes the repository or uses a symlink."""


# Compatibility for callers using the previous exception name.
PresetAlreadyExistsError = PresetNameConflictError
PresetStoreError = PresetError


class PresetType(str, Enum):
    """The application feature that owns a preset payload."""

    AIRFIELD = "airfield"
    DEBRIS = "debris"


def validate_preset_name(value: object) -> str:
    """Return a normalized display name or raise a clear validation error."""
    if not isinstance(value, str):
        raise PresetNameError("Preset name must be text.")
    name = unicodedata.normalize("NFC", value.strip())
    if not name or name in {".", ".."}:
        raise PresetNameError("Enter a preset name.")
    if len(name) > MAX_PRESET_NAME_LENGTH:
        raise PresetNameError(
            f"Preset names must be {MAX_PRESET_NAME_LENGTH} characters or fewer."
        )
    if "/" in name or "\\" in name or _WINDOWS_ABSOLUTE.match(name):
        raise PresetNameError("Preset names must not contain file paths.")
    if any(unicodedata.category(character).startswith("C") for character in name):
        raise PresetNameError("Preset names cannot contain control characters.")
    return name


def preset_name_key(value: object) -> str:
    """Return the normalized, case-insensitive uniqueness key for a name."""
    return validate_preset_name(value).casefold()


@dataclass(frozen=True)
class Preset:
    """A validated preset document whose UUID cannot be edited in place."""

    format_version: int
    preset_type: PresetType
    id: UUID
    name: str
    data: dict[str, Any]

    @classmethod
    def create(
        cls,
        preset_type: PresetType | str,
        name: str,
        data: dict[str, Any],
        *,
        preset_id: UUID | None = None,
    ) -> "Preset":
        """Create a new validated preset, generating an identity by default."""
        try:
            resolved_type = PresetType(preset_type)
        except (TypeError, ValueError) as error:
            raise PresetValidationError("Preset type must be airfield or debris.") from error
        if not isinstance(data, dict):
            raise PresetValidationError("Preset data must be a JSON object.")
        try:
            json.dumps(data, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise PresetValidationError(
                "Preset data contains values that cannot be stored as JSON."
            ) from error
        return cls(
            format_version=CURRENT_FORMAT_VERSION,
            preset_type=resolved_type,
            id=preset_id or uuid4(),
            name=validate_preset_name(name),
            data=copy.deepcopy(data),
        )

    @classmethod
    def from_dict(
        cls,
        document: object,
        *,
        expected_type: PresetType | str | None = None,
    ) -> "Preset":
        """Validate and deserialize a version-1 preset JSON object."""
        if not isinstance(document, dict):
            raise PresetValidationError("Preset file must contain a JSON object.")
        missing = _DOCUMENT_FIELDS - set(document)
        extra = set(document) - _DOCUMENT_FIELDS
        if missing:
            raise PresetValidationError(
                "Preset file is missing required fields: " + ", ".join(sorted(missing)) + "."
            )
        if extra:
            raise PresetValidationError(
                "Preset file contains unsupported fields: " + ", ".join(sorted(extra)) + "."
            )
        version = document["formatVersion"]
        if type(version) is not int or version != CURRENT_FORMAT_VERSION:
            raise UnsupportedPresetVersionError(
                f"Unsupported preset formatVersion {version!r}; expected {CURRENT_FORMAT_VERSION}."
            )
        try:
            preset_type = PresetType(document["presetType"])
        except (TypeError, ValueError) as error:
            raise PresetValidationError("Preset type must be airfield or debris.") from error
        if expected_type is not None and preset_type != PresetType(expected_type):
            raise PresetTypeMismatchError(
                f'This is a {preset_type.value} preset, not a {PresetType(expected_type).value} preset.'
            )
        try:
            preset_id = UUID(str(document["id"]))
        except (AttributeError, TypeError, ValueError) as error:
            raise PresetValidationError("Preset id must be a valid UUID.") from error
        data = document["data"]
        if not isinstance(data, dict):
            raise PresetValidationError("Preset data must be a JSON object.")
        return cls.create(
            preset_type,
            validate_preset_name(document["name"]),
            data,
            preset_id=preset_id,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the complete public JSON envelope."""
        return {
            "formatVersion": self.format_version,
            "presetType": self.preset_type.value,
            "id": str(self.id),
            "name": self.name,
            "data": copy.deepcopy(self.data),
        }

    def renamed(self, name: str) -> "Preset":
        """Return a renamed preset while retaining the immutable UUID."""
        return replace(self, name=validate_preset_name(name))

    def with_data(self, data: dict[str, Any]) -> "Preset":
        """Return an updated preset while retaining UUID and name."""
        if not isinstance(data, dict):
            raise PresetValidationError("Preset data must be a JSON object.")
        return replace(self, data=copy.deepcopy(data))

    def clone(self, *, name: str | None = None) -> "Preset":
        """Return an independent copy with a newly generated UUID."""
        return Preset.create(self.preset_type, name or self.name, self.data)
