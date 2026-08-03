"""Filesystem-safe names for managed preset and exported JSON files."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from .preset_model import validate_preset_name


_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_INVALID_EXPORT_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_MANAGED_STEM_LENGTH = 96
MAX_EXPORT_STEM_LENGTH = 120


def canonical_stem(name: str) -> str:
    """Convert a display name to a readable lowercase ASCII slug."""
    display_name = validate_preset_name(name)
    ascii_name = unicodedata.normalize("NFKD", display_name).encode(
        "ascii", "ignore"
    ).decode("ascii")
    stem = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-.")
    stem = stem[:MAX_MANAGED_STEM_LENGTH].rstrip("-.") or "preset"
    if stem.casefold() in _WINDOWS_RESERVED:
        stem = f"preset-{stem}"
    return stem


def canonical_filename(name: str, existing_filenames: Iterable[str] = ()) -> str:
    """Return the first case-insensitively available canonical JSON filename."""
    stem = canonical_stem(name)
    existing = {filename.casefold() for filename in existing_filenames}
    filename = f"{stem}.json"
    suffix = 2
    while filename.casefold() in existing:
        suffix_text = f"-{suffix}"
        candidate_stem = stem[: MAX_MANAGED_STEM_LENGTH - len(suffix_text)].rstrip("-.")
        filename = f"{candidate_stem}{suffix_text}.json"
        suffix += 1
    return filename


def readable_export_filename(name: str) -> str:
    """Return a readable OS-safe filename suggestion, retaining spaces and case."""
    stem = _INVALID_EXPORT_CHARACTERS.sub("-", validate_preset_name(name))
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:MAX_EXPORT_STEM_LENGTH].rstrip(" .")
    stem = stem or "Preset"
    if stem.casefold() in _WINDOWS_RESERVED:
        stem = f"Preset {stem}"
    return f"{stem}.json"
