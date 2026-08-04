"""Reusable Qt field for latitude/longitude coordinate pairs."""

from PyQt6.QtWidgets import QLineEdit

from services import (
    CoordinateInputError,
    CoordinatePair,
    format_coordinate_pair,
    format_coordinate_value,
    parse_coordinate_pair,
)


class CoordinatePairInput(QLineEdit):
    """A combined latitude/longitude input with shared normalization."""

    def __init__(self, field_name: str, parent=None):
        super().__init__(parent)
        self.field_name = field_name
        self.setAccessibleName(field_name)
        self.setPlaceholderText("Latitude, Longitude or DMS")
        self.editingFinished.connect(self.normalize_if_valid)

    def coordinates(self, *, normalize: bool = True) -> CoordinatePair:
        try:
            pair = parse_coordinate_pair(self.text())
        except CoordinateInputError as error:
            raise CoordinateInputError(f"{self.field_name}: {error}") from error
        if normalize:
            self.setText(format_coordinate_pair(pair))
        return pair

    def normalize_if_valid(self) -> None:
        if not self.text().strip():
            return
        try:
            self.coordinates()
        except CoordinateInputError:
            pass

    def preset_components(self) -> tuple[str, str]:
        if not self.text().strip():
            return "", ""
        pair = self.coordinates()
        return (
            format_coordinate_value(pair.latitude),
            format_coordinate_value(pair.longitude),
        )

    def set_components(self, latitude: object, longitude: object) -> None:
        latitude_text = "" if latitude is None else str(latitude).strip()
        longitude_text = "" if longitude is None else str(longitude).strip()
        if not latitude_text and not longitude_text:
            self.clear()
            return
        self.setText(f"{latitude_text}, {longitude_text}")
        self.normalize_if_valid()
