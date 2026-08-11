"""Persist the user-supplied Google Maps JavaScript API key."""

from __future__ import annotations

from PyQt6.QtCore import QObject, QSettings, pyqtSignal


GOOGLE_MAPS_API_KEY_SETTING = "maps/google-maps-api-key"


class GoogleMapsSettings(QObject):
    """Small injectable wrapper around the application's ``QSettings``."""

    api_key_changed = pyqtSignal(str)

    def __init__(
        self,
        *,
        settings: QSettings | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings if settings is not None else QSettings()

    @property
    def api_key(self) -> str:
        value = self.settings.value(GOOGLE_MAPS_API_KEY_SETTING, "", type=str)
        return str(value or "").strip()

    def set_api_key(self, api_key: str) -> None:
        value = str(api_key).strip()
        if value == self.api_key:
            return
        if value:
            self.settings.setValue(GOOGLE_MAPS_API_KEY_SETTING, value)
        elif hasattr(self.settings, "remove"):
            self.settings.remove(GOOGLE_MAPS_API_KEY_SETTING)
        else:
            # Keep simple injected settings doubles usable in unit tests.
            self.settings.setValue(GOOGLE_MAPS_API_KEY_SETTING, "")
        self.settings.sync()
        self.api_key_changed.emit(value)

    def clear(self) -> None:
        self.set_api_key("")


__all__ = ["GOOGLE_MAPS_API_KEY_SETTING", "GoogleMapsSettings"]
