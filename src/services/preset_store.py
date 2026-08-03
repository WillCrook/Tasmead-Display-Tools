"""Shared JSON-backed preset persistence."""

import json
import os
from pathlib import Path


class PresetStore:
    def __init__(self, directory, legacy_directory=None):
        self.directory = Path(directory)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Keep the app usable. A later save will retry and surface its
            # failure through the page's existing error dialog.
            return

        if legacy_directory is not None:
            self.migrate_legacy(legacy_directory)

    def migrate_legacy(self, legacy_directory):
        """Copy valid legacy JSON presets without changing their sources."""
        legacy_directory = Path(legacy_directory)
        if legacy_directory == self.directory:
            return

        try:
            legacy_paths = list(legacy_directory.glob("*.json"))
        except OSError:
            return

        for source in legacy_paths:
            destination = self.directory / source.name
            if destination.exists():
                continue

            created = False
            try:
                contents = source.read_bytes()
                json.loads(contents)
                with destination.open("xb") as file:
                    created = True
                    file.write(contents)
            except (OSError, UnicodeError, json.JSONDecodeError):
                if created:
                    try:
                        destination.unlink()
                    except OSError:
                        pass

    def load_all(self):
        presets = {}
        try:
            paths = list(self.directory.glob("*.json"))
        except OSError:
            return presets

        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            presets[path.stem] = {"data": data, "path": str(path)}
        return presets

    def save(self, name, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{name}.json"
        self.write_file(path, data)
        return {"data": data, "path": str(path)}

    @staticmethod
    def write_file(path, data):
        """Write a preset JSON copy to an explicitly supplied destination."""
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def load_file(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    @staticmethod
    def delete(entry):
        path = entry.get("path")
        if path and os.path.isfile(path):
            os.remove(path)
