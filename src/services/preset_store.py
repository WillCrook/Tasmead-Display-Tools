"""Shared JSON-backed preset persistence."""

import json
import os
from pathlib import Path


class PresetStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def load_all(self):
        presets = {}
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            presets[path.stem] = {"data": data, "path": str(path)}
        return presets

    def save(self, name, data):
        path = self.directory / f"{name}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return {"data": data, "path": str(path)}

    @staticmethod
    def load_file(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    @staticmethod
    def delete(entry):
        path = entry.get("path")
        if path and os.path.isfile(path):
            os.remove(path)
