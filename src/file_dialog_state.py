"""Persistent, workflow-specific starting locations for native file dialogs."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

from PyQt6.QtCore import QSettings, QStandardPaths


class FileDialogWorkflow(str, Enum):
    """Independent file-dialog histories used by the application."""

    TRANSPOSITION = "transposition"
    DEBRIS = "debris"
    AIRFIELD_PRESET = "airfield-preset"
    DEBRIS_PRESET = "debris-preset"


class FileDialogDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


_LEGACY_KEYS = {
    (FileDialogWorkflow.TRANSPOSITION, FileDialogDirection.OUTPUT):
        "transpose/last-output-directory",
}


def _settings_key(
    workflow: FileDialogWorkflow,
    direction: FileDialogDirection,
) -> str:
    return f"file-dialogs/{workflow.value}/{direction.value}-directory"


def _existing_directory(value: object) -> str | None:
    if not value:
        return None
    path = Path(os.fspath(value)).expanduser()
    if path.is_dir():
        return str(path)
    return None


def _default_directory() -> str:
    documents = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DocumentsLocation
    )
    existing = _existing_directory(documents)
    if existing is not None:
        return existing
    return os.getcwd()


def remembered_directory(
    workflow: FileDialogWorkflow,
    direction: FileDialogDirection,
) -> str:
    """Return a valid persisted directory, migrating a supported legacy key."""
    settings = QSettings()
    key = _settings_key(workflow, direction)
    saved = _existing_directory(settings.value(key, "", type=str))
    if saved is not None:
        return saved

    legacy_key = _LEGACY_KEYS.get((workflow, direction))
    if legacy_key is not None:
        legacy = _existing_directory(settings.value(legacy_key, "", type=str))
        if legacy is not None:
            settings.setValue(key, legacy)
            settings.sync()
            return legacy

    return _default_directory()


def suggested_save_path(
    workflow: FileDialogWorkflow,
    filename: str,
) -> str:
    """Return an editable filename suggestion inside the remembered output folder."""
    safe_filename = Path(filename).name
    return str(
        Path(remembered_directory(workflow, FileDialogDirection.OUTPUT))
        / safe_filename
    )


def remember_directory(
    workflow: FileDialogWorkflow,
    direction: FileDialogDirection,
    directory: str | os.PathLike[str],
) -> None:
    """Persist a directory selected by the user."""
    selected = _existing_directory(directory)
    if selected is None:
        return
    settings = QSettings()
    settings.setValue(_settings_key(workflow, direction), selected)
    settings.sync()


def remember_file_selection(
    workflow: FileDialogWorkflow,
    direction: FileDialogDirection,
    path: str | os.PathLike[str],
) -> None:
    """Persist the parent directory of an accepted open/save file selection."""
    if not path:
        return
    remember_directory(workflow, direction, Path(path).expanduser().parent)


def ensure_extension(path: str, extension: str) -> str:
    """Append a required extension without replacing a user-supplied filename."""
    normalized_extension = extension if extension.startswith(".") else f".{extension}"
    if path.lower().endswith(normalized_extension.lower()):
        return path
    return f"{path}{normalized_extension}"
