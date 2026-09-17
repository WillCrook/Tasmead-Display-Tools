"""Accessible two-handle range control for selecting a retained crop interval."""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QMouseEvent, QPaintEvent, QPainter, QPen
from PyQt6.QtWidgets import (
    QSizePolicy,
    QStyle,
    QStyleOptionSlider,
    QWidget,
)


class CropRangeSlider(QWidget):
    """A horizontal range slider that always keeps its two values ordered."""

    range_changed = pyqtSignal(int, int)
    interaction_finished = pyqtSignal()

    _LOWER = "lower"
    _UPPER = "upper"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._minimum = 0
        self._maximum = 1
        self._lower = 0
        self._upper = 1
        self._single_step = 1
        self._page_step = 1
        self._active_handle = self._LOWER
        self._dragging_handle: str | None = None
        self._drag_offset = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.setMinimumHeight(28)

    def minimum(self) -> int:
        return self._minimum

    def maximum(self) -> int:
        return self._maximum

    def lower_value(self) -> int:
        return self._lower

    def upper_value(self) -> int:
        return self._upper

    def values(self) -> tuple[int, int]:
        return self._lower, self._upper

    def set_range(self, minimum: int, maximum: int) -> None:
        minimum = int(minimum)
        maximum = int(maximum)
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        changed = (minimum, maximum) != (self._minimum, self._maximum)
        self._minimum = minimum
        self._maximum = maximum
        self.set_values(self._lower, self._upper, emit=False)
        if changed:
            self.updateGeometry()
            self.update()

    def set_values(self, lower: int, upper: int, *, emit: bool = False) -> None:
        lower = max(self._minimum, min(self._maximum, int(lower)))
        upper = max(self._minimum, min(self._maximum, int(upper)))
        if self._maximum > self._minimum:
            lower = min(lower, self._maximum - 1)
            upper = max(upper, self._minimum + 1)
            if lower >= upper:
                if self._active_handle == self._LOWER:
                    lower = upper - 1
                else:
                    upper = lower + 1
        else:
            lower = upper = self._minimum
        if (lower, upper) == (self._lower, self._upper):
            return
        self._lower, self._upper = lower, upper
        self.update()
        if emit:
            self.range_changed.emit(lower, upper)

    def set_page_step(self, step: int) -> None:
        self._page_step = max(1, int(step))

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt virtual method
        return QSize(240, 28)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt virtual method
        return QSize(120, 28)

    def _option(self, value: int) -> QStyleOptionSlider:
        option = QStyleOptionSlider()
        option.initFrom(self)
        option.orientation = Qt.Orientation.Horizontal
        option.minimum = self._minimum
        option.maximum = self._maximum
        option.sliderPosition = value
        option.sliderValue = value
        option.singleStep = self._single_step
        option.pageStep = self._page_step
        option.upsideDown = self.layoutDirection() == Qt.LayoutDirection.RightToLeft
        return option

    def _handle_rect(self, value: int) -> QRect:
        option = self._option(value)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            option,
            QStyle.SubControl.SC_SliderHandle,
            self,
        )

    def _groove_rect(self) -> QRect:
        option = self._option(self._lower)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            option,
            QStyle.SubControl.SC_SliderGroove,
            self,
        )

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt virtual method
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        groove = self._groove_rect()
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.palette().mid())
        painter.drawRoundedRect(groove, 2.0, 2.0)
        painter.restore()
        lower_center = self._handle_rect(self._lower).center().x()
        upper_center = self._handle_rect(self._upper).center().x()
        selected = QRect(
            min(lower_center, upper_center),
            groove.center().y() - max(2, groove.height() // 2),
            abs(upper_center - lower_center) + 1,
            max(4, groove.height()),
        )
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.palette().highlight())
        painter.drawRoundedRect(selected, 2.0, 2.0)
        painter.restore()

        handles = (self._LOWER, self._UPPER)
        if self._active_handle == self._LOWER:
            handles = (self._UPPER, self._LOWER)
        for handle in handles:
            rect = self._handle_rect(
                self._lower if handle == self._LOWER else self._upper
            ).adjusted(1, 1, -1, -1)
            active = handle == self._active_handle
            border = (
                self.palette().highlight().color()
                if active and (self.hasFocus() or self._dragging_handle == handle)
                else self.palette().dark().color()
            )
            painter.setPen(QPen(border, 2 if active and self.hasFocus() else 1))
            painter.setBrush(self.palette().button())
            painter.drawRoundedRect(rect, 4.0, 4.0)

    def _handle_for_position(self, position: QPoint) -> str:
        lower_rect = self._handle_rect(self._lower)
        upper_rect = self._handle_rect(self._upper)
        lower_hit = lower_rect.contains(position)
        upper_hit = upper_rect.contains(position)
        if lower_hit and not upper_hit:
            return self._LOWER
        if upper_hit and not lower_hit:
            return self._UPPER
        lower_distance = abs(position.x() - lower_rect.center().x())
        upper_distance = abs(position.x() - upper_rect.center().x())
        if lower_distance == upper_distance:
            return self._active_handle
        return self._LOWER if lower_distance < upper_distance else self._UPPER

    def _value_from_position(self, position_x: int) -> int:
        option = self._option(self._lower)
        groove = self._groove_rect()
        handle_width = max(
            self._handle_rect(self._lower).width(),
            self._handle_rect(self._upper).width(),
        )
        span = max(1, groove.width() - handle_width)
        slider_position = max(
            0,
            min(span, int(position_x) - groove.x() - handle_width // 2),
        )
        return QStyle.sliderValueFromPosition(
            self._minimum,
            self._maximum,
            slider_position,
            span,
            option.upsideDown,
        )

    def _move_active_handle(self, value: int) -> None:
        if self._active_handle == self._LOWER:
            self.set_values(min(int(value), self._upper - 1), self._upper, emit=True)
        else:
            self.set_values(self._lower, max(int(value), self._lower + 1), emit=True)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt virtual method
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        position = event.position().toPoint()
        self._active_handle = self._handle_for_position(position)
        self._dragging_handle = self._active_handle
        active_rect = self._handle_rect(
            self._lower if self._active_handle == self._LOWER else self._upper
        )
        self._drag_offset = (
            position.x() - active_rect.center().x()
            if active_rect.contains(position)
            else 0
        )
        if not active_rect.contains(position):
            self._move_active_handle(self._value_from_position(position.x()))
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt virtual method
        if self._dragging_handle is None:
            super().mouseMoveEvent(event)
            return
        self._move_active_handle(
            self._value_from_position(round(event.position().x()) - self._drag_offset)
        )
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt virtual method
        if event.button() != Qt.MouseButton.LeftButton or self._dragging_handle is None:
            super().mouseReleaseEvent(event)
            return
        self._dragging_handle = None
        self._drag_offset = 0
        self.update()
        self.interaction_finished.emit()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt virtual method
        key = event.key()
        if key == Qt.Key.Key_Space:
            self._active_handle = (
                self._UPPER if self._active_handle == self._LOWER else self._LOWER
            )
            self.update()
            event.accept()
            return
        increment = {
            Qt.Key.Key_Left: -self._single_step,
            Qt.Key.Key_Down: -self._single_step,
            Qt.Key.Key_Right: self._single_step,
            Qt.Key.Key_Up: self._single_step,
            Qt.Key.Key_PageDown: -self._page_step,
            Qt.Key.Key_PageUp: self._page_step,
        }.get(key)
        if increment is not None:
            current = self._lower if self._active_handle == self._LOWER else self._upper
            self._move_active_handle(current + increment)
            self.interaction_finished.emit()
            event.accept()
            return
        if key in {Qt.Key.Key_Home, Qt.Key.Key_End}:
            target = self._minimum if key == Qt.Key.Key_Home else self._maximum
            self._move_active_handle(target)
            self.interaction_finished.emit()
            event.accept()
            return
        super().keyPressEvent(event)


__all__ = ["CropRangeSlider"]
