"""Persistent, tooltip-styled information popups for icon buttons."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QPoint, QTimer, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)


class _InformationPopup(QDialog):
    """Small non-modal window whose dismissal is managed by its controller."""

    def __init__(self, text: str, trigger: QToolButton) -> None:
        super().__init__(
            trigger.window(),
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint,
        )
        self.setObjectName("persistentInfoPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setModal(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        self.text_label = QLabel(text)
        self.text_label.setObjectName("persistentInfoPopupText")
        self.text_label.setTextFormat(Qt.TextFormat.PlainText)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.text_label.setMaximumWidth(440)
        layout.addWidget(self.text_label)

    def show_beneath(self, trigger: QToolButton) -> None:
        """Position below the trigger, falling back above, within its screen."""
        self.adjustSize()
        screen = trigger.screen() or QApplication.primaryScreen()
        if screen is None:
            self.move(trigger.mapToGlobal(QPoint(0, trigger.height() + 4)))
        else:
            available = screen.availableGeometry()
            anchor = trigger.mapToGlobal(QPoint(0, trigger.height() + 4))
            x = max(
                available.left(),
                min(anchor.x(), available.right() - self.width() + 1),
            )
            below_y = anchor.y()
            above_y = trigger.mapToGlobal(QPoint(0, -self.height() - 4)).y()
            y = (
                below_y
                if below_y + self.height() <= available.bottom() + 1
                else above_y
            )
            y = max(
                available.top(),
                min(y, available.bottom() - self.height() + 1),
            )
            self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)


class PersistentInfoPopupController(QObject):
    """Give one tool button a persistent click-open version of its tooltip."""

    def __init__(self, trigger: QToolButton) -> None:
        super().__init__(trigger)
        self._trigger = trigger
        self._text = ""
        self._popup: _InformationPopup | None = None
        trigger.clicked.connect(self.toggle)
        trigger.destroyed.connect(self._trigger_destroyed)
        trigger.installEventFilter(self)

    @property
    def popup(self) -> QDialog | None:
        """Return the current popup, primarily for state inspection and tests."""
        return self._popup

    def set_text(self, text: str) -> None:
        if text != self._text:
            self.close()
        self._text = text

    def toggle(self) -> None:
        if self._popup is not None and self._popup.isVisible():
            self.close()
            return
        if not self._text:
            return

        self.close()
        QToolTip.hideText()
        self._popup = _InformationPopup(self._text, self._trigger)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._popup.show_beneath(self._trigger)

    def close(self, *, return_focus: bool = False) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        popup = self._popup
        self._popup = None
        if popup is not None:
            popup.hide()
            popup.deleteLater()
        if return_focus:
            QTimer.singleShot(0, self._restore_trigger_focus)

    def _restore_trigger_focus(self) -> None:
        if not self._trigger.isVisible() or not self._trigger.isEnabled():
            return
        owner = self._trigger.window()
        owner.activateWindow()
        self._trigger.setFocus(Qt.FocusReason.OtherFocusReason)

    def _trigger_destroyed(self) -> None:
        self.close()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._popup is None:
            return False
        if watched is self._trigger and event.type() == QEvent.Type.Hide:
            self.close()
            return False
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.close(return_focus=True)
            return True
        if event.type() == QEvent.Type.MouseButtonPress:
            widget = watched if isinstance(watched, QWidget) else None
            if self._contains(self._popup, widget) or self._contains(
                self._trigger, widget
            ):
                return False
            self.close()
        return False

    @staticmethod
    def _contains(container: QWidget, widget: QWidget | None) -> bool:
        return widget is not None and (
            widget is container or container.isAncestorOf(widget)
        )
