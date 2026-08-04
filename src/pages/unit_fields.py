"""Small bindings for paired metre and feet text fields."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtWidgets import QLineEdit


FEET_PER_METRE = 3.28084


class MetreFeetFieldPair:
    """Synchronize one existing metre/feet field pair without owning its layout."""

    def __init__(
        self,
        metres_input: QLineEdit,
        feet_input: QLineEdit,
        *,
        decimal_places: int = 2,
        on_metres_changed: Callable[[], None] | None = None,
    ) -> None:
        self.metres_input = metres_input
        self.feet_input = feet_input
        self.decimal_places = decimal_places
        self._on_metres_changed = on_metres_changed
        self._updating = False
        self._notify_dependents = True

        self.metres_input.textChanged.connect(self._metres_changed)
        self.feet_input.textChanged.connect(self._feet_changed)

    def set_metres_text(
        self, text: str, *, notify_dependents: bool = True
    ) -> None:
        self._set_source_text(
            self.metres_input,
            text,
            notify_dependents=notify_dependents,
        )

    def set_feet_text(
        self, text: str, *, notify_dependents: bool = True
    ) -> None:
        self._set_source_text(
            self.feet_input,
            text,
            notify_dependents=notify_dependents,
        )

    def set_metres_value(
        self, value: float, *, notify_dependents: bool = True
    ) -> None:
        """Format both fields from one unrounded metre value."""
        previous_notify = self._notify_dependents
        self._notify_dependents = notify_dependents
        self._updating = True
        try:
            self.metres_input.setText(self._format(value))
            self.feet_input.setText(self._format(value * FEET_PER_METRE))
        finally:
            self._updating = False
            self._notify_dependents = previous_notify
        if notify_dependents:
            self._notify_changed()

    def _set_source_text(
        self,
        source: QLineEdit,
        text: str,
        *,
        notify_dependents: bool,
    ) -> None:
        previous_notify = self._notify_dependents
        self._notify_dependents = notify_dependents
        try:
            source.setText(text)
        finally:
            self._notify_dependents = previous_notify

    def _metres_changed(self, text: str) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            try:
                self.feet_input.setText(
                    self._format(float(text) * FEET_PER_METRE)
                )
            except ValueError:
                self.feet_input.clear()
        finally:
            self._updating = False
        self._notify_changed()

    def _feet_changed(self, text: str) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            try:
                self.metres_input.setText(
                    self._format(float(text) / FEET_PER_METRE)
                )
            except ValueError:
                self.metres_input.clear()
        finally:
            self._updating = False
        self._notify_changed()

    def _notify_changed(self) -> None:
        if self._notify_dependents and self._on_metres_changed is not None:
            self._on_metres_changed()

    def _format(self, value: float) -> str:
        return f"{value:.{self.decimal_places}f}"
