"""Application-wide colour themes and persistent appearance selection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PyQt6.QtCore import QObject, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QFrame, QGraphicsDropShadowEffect, QWidget


class ThemeMode(str, Enum):
    """User-selectable application appearance modes."""

    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


@dataclass(frozen=True, slots=True)
class ThemeTokens:
    primary: str
    primary_hover: str
    primary_pressed: str
    primary_soft: str
    background: str
    surface: str
    surface_alt: str
    border: str
    heading_text: str
    body_text: str
    muted_text: str
    accent_blue: str
    success: str
    warning: str
    error: str
    on_primary: str
    accent_text: str
    success_text: str
    warning_text: str
    error_text: str


LIGHT_TOKENS = ThemeTokens(
    primary="#0F766E",
    primary_hover="#115E59",
    primary_pressed="#134E4A",
    primary_soft="#CCFBF1",
    background="#F8FAFC",
    surface="#FFFFFF",
    surface_alt="#F1F5F9",
    border="#CBD5E1",
    heading_text="#0F172A",
    body_text="#334155",
    muted_text="#64748B",
    accent_blue="#3B82F6",
    success="#22C55E",
    warning="#F59E0B",
    error="#EF4444",
    on_primary="#FFFFFF",
    accent_text="#1D4ED8",
    success_text="#15803D",
    warning_text="#B45309",
    error_text="#B91C1C",
)

DARK_TOKENS = ThemeTokens(
    primary="#2DD4BF",
    primary_hover="#5EEAD4",
    primary_pressed="#14B8A6",
    primary_soft="#134E4A",
    background="#0B1120",
    surface="#111827",
    surface_alt="#1E293B",
    border="#334155",
    heading_text="#F8FAFC",
    body_text="#E2E8F0",
    muted_text="#94A3B8",
    accent_blue="#60A5FA",
    success="#4ADE80",
    warning="#FBBF24",
    error="#F87171",
    on_primary="#0B1120",
    accent_text="#60A5FA",
    success_text="#4ADE80",
    warning_text="#FBBF24",
    error_text="#F87171",
)

THEME_SETTING_KEY = "appearance/theme-mode"


def tokens_for(mode: ThemeMode) -> ThemeTokens:
    """Return tokens for an effective light or dark mode."""
    if mode == ThemeMode.DARK:
        return DARK_TOKENS
    return LIGHT_TOKENS


def build_palette(tokens: ThemeTokens) -> QPalette:
    """Map semantic theme tokens onto Qt's standard palette roles."""
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: tokens.background,
        QPalette.ColorRole.WindowText: tokens.body_text,
        QPalette.ColorRole.Base: tokens.surface,
        QPalette.ColorRole.AlternateBase: tokens.surface_alt,
        QPalette.ColorRole.ToolTipBase: tokens.surface_alt,
        QPalette.ColorRole.ToolTipText: tokens.body_text,
        QPalette.ColorRole.Text: tokens.body_text,
        QPalette.ColorRole.Button: tokens.surface,
        QPalette.ColorRole.ButtonText: tokens.body_text,
        QPalette.ColorRole.BrightText: tokens.error_text,
        QPalette.ColorRole.Highlight: tokens.primary_soft,
        QPalette.ColorRole.HighlightedText: tokens.heading_text,
        QPalette.ColorRole.Link: tokens.accent_text,
        QPalette.ColorRole.LinkVisited: tokens.accent_text,
        QPalette.ColorRole.PlaceholderText: tokens.muted_text,
        QPalette.ColorRole.Light: tokens.surface,
        QPalette.ColorRole.Midlight: tokens.surface_alt,
        QPalette.ColorRole.Mid: tokens.border,
        QPalette.ColorRole.Dark: tokens.border,
        QPalette.ColorRole.Shadow: tokens.background,
        QPalette.ColorRole.Accent: tokens.primary,
    }
    for role, colour in roles.items():
        palette.setColor(role, QColor(colour))

    disabled = QPalette.ColorGroup.Disabled
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.PlaceholderText,
    ):
        palette.setColor(disabled, role, QColor(tokens.muted_text))
    palette.setColor(disabled, QPalette.ColorRole.Highlight, QColor(tokens.border))
    palette.setColor(
        disabled,
        QPalette.ColorRole.HighlightedText,
        QColor(tokens.muted_text),
    )
    return palette


def build_stylesheet(tokens: ThemeTokens) -> str:
    """Generate the application stylesheet from one semantic token set."""
    return f"""
QWidget {{
    color: {tokens.body_text};
}}
QMainWindow, QDialog, QMessageBox {{
    background: {tokens.background};
    color: {tokens.body_text};
}}
QToolTip {{
    background: {tokens.surface_alt};
    color: {tokens.body_text};
    border: 1px solid {tokens.border};
    border-radius: 6px;
    padding: 4px 7px;
}}
QDialog#persistentInfoPopup {{
    background: {tokens.surface_alt};
    color: {tokens.body_text};
    border: 1px solid {tokens.border};
    border-radius: 6px;
}}
QLabel#persistentInfoPopupText {{
    background: transparent;
    color: {tokens.body_text};
    border: none;
}}
QFrame#appHeader {{
    background: {tokens.surface};
    border: none;
    border-bottom: 1px solid {tokens.border};
}}
QFrame#previewLoadingScreen {{
    background: {tokens.background};
    border: none;
}}
QFrame#previewLoadingCard {{
    background: {tokens.surface};
    border: 1px solid {tokens.border};
    border-radius: 12px;
}}
QFrame#modeSwitch, QFrame#themeSwitch {{
    background: {tokens.surface_alt};
    border: 1px solid {tokens.border};
    border-radius: 26px;
}}
QFrame#modeSelection {{
    background: {tokens.primary_soft};
    border: none;
    border-radius: 22px;
}}
QPushButton#modeSegment, QPushButton#themeSegment {{
    background: transparent;
    color: {tokens.muted_text};
    border: 2px solid transparent;
    border-radius: 22px;
    padding: 0 18px;
    font-size: 15px;
    font-weight: 500;
}}
QPushButton#modeSegment:hover:!checked,
QPushButton#themeSegment:hover:!checked {{
    background: {tokens.surface};
    color: {tokens.body_text};
}}
QPushButton#modeSegment:checked {{
    background: transparent;
    color: {tokens.primary};
    font-weight: 650;
}}
QPushButton#themeSegment:checked {{
    background: {tokens.primary_soft};
    color: {tokens.primary};
    border-radius: 22px;
    font-weight: 650;
}}
QPushButton#modeSegment:focus,
QPushButton#themeSegment:focus {{
    border: 2px solid {tokens.primary};
}}
QPushButton#modeSegment:disabled,
QPushButton#themeSegment:disabled {{
    background: transparent;
    color: {tokens.muted_text};
}}
QToolButton#settingsButton {{
    min-width: 40px;
    max-width: 40px;
    min-height: 40px;
    max-height: 40px;
    background: transparent;
    color: {tokens.body_text};
    border: 2px solid transparent;
    border-radius: 20px;
    padding: 0;
    font-size: 25px;
}}
QToolButton#settingsButton:hover {{
    background: {tokens.surface_alt};
}}
QToolButton#settingsButton:pressed {{
    background: {tokens.primary_soft};
    color: {tokens.primary_pressed};
}}
QToolButton#settingsButton:focus {{
    border-color: {tokens.primary};
}}
QLabel#settingsTitle, QLabel#dialogTitle {{
    color: {tokens.heading_text};
    font-size: 22px;
    font-weight: 700;
}}
QLabel#cardTitle, QLabel#panelTitle, QLabel#popupHeading,
QLabel#sectionTitle, QLabel#statusBadge {{
    color: {tokens.heading_text};
    font-weight: 650;
}}
QLabel#mutedText {{
    color: {tokens.muted_text};
}}
QLabel[dataRole="coordinate"] {{
    color: {tokens.accent_text};
    border-left: 2px solid {tokens.accent_blue};
    padding-left: 5px;
}}
QLineEdit, QComboBox, QListWidget, QTableWidget {{
    min-height: 28px;
    background: {tokens.surface};
    color: {tokens.body_text};
    border: 1px solid {tokens.border};
    border-radius: 8px;
    padding: 3px 7px;
    selection-background-color: {tokens.primary_soft};
    selection-color: {tokens.heading_text};
}}
QLineEdit:focus, QComboBox:focus, QListWidget:focus, QTableWidget:focus {{
    border: 2px solid {tokens.primary};
}}
QListWidget::item, QTableWidget::item {{
    border-left: 3px solid transparent;
    padding: 4px 6px;
}}
QListWidget::item:hover, QTableWidget::item:hover {{
    background: {tokens.surface_alt};
}}
QListWidget::item:selected, QTableWidget::item:selected {{
    background: {tokens.primary_soft};
    color: {tokens.heading_text};
    border-left: 3px solid {tokens.primary};
}}
QPushButton, QToolButton {{
    min-height: 28px;
    background: transparent;
    color: {tokens.body_text};
    border: 1px solid {tokens.border};
    border-radius: 8px;
    padding: 4px 10px;
}}
QPushButton:hover, QToolButton:hover {{
    background: {tokens.surface_alt};
    border-color: {tokens.primary};
}}
QPushButton:pressed, QToolButton:pressed {{
    background: {tokens.primary_soft};
    border-color: {tokens.primary_pressed};
}}
QPushButton:focus, QToolButton:focus {{
    border: 2px solid {tokens.primary};
}}
QPushButton:disabled, QToolButton:disabled {{
    background: {tokens.surface_alt};
    color: {tokens.muted_text};
    border-color: {tokens.border};
}}
QPushButton#primaryButton {{
    min-height: 38px;
    background: {tokens.primary};
    color: {tokens.on_primary};
    border-color: {tokens.primary};
    font-weight: 700;
}}
QPushButton#primaryButton:hover {{
    background: {tokens.primary_hover};
    border-color: {tokens.primary_hover};
}}
QPushButton#primaryButton:pressed {{
    background: {tokens.primary_pressed};
    border-color: {tokens.primary_pressed};
}}
QPushButton#dangerButton {{
    color: {tokens.error_text};
    border-color: {tokens.error};
}}
QPushButton#dangerButton:hover {{
    background: {tokens.surface_alt};
    border-color: {tokens.error};
}}
QLabel[status="error"] {{
    color: {tokens.error_text};
    border-left: 3px solid {tokens.error};
    padding-left: 6px;
    font-weight: 650;
}}
QLabel#errorText {{
    color: {tokens.error_text};
}}
QLabel[status="success"] {{
    color: {tokens.success_text};
    border-left: 3px solid {tokens.success};
    padding-left: 6px;
    font-weight: 650;
}}
QLabel[status="warning"] {{
    color: {tokens.warning_text};
    border-left: 3px solid {tokens.warning};
    padding-left: 6px;
    font-weight: 650;
}}
QLabel#warningText {{
    color: {tokens.warning_text};
    font-weight: 650;
}}
QLabel#previewOffsetStatusDot {{
    background: {tokens.muted_text};
    border: none;
    border-radius: 5px;
}}
QLabel#previewOffsetStatusDot[offsetState="active"] {{
    background: {tokens.success};
}}
QLabel#previewOffsetStatusDot[offsetState="mismatch"] {{
    background: {tokens.error};
}}
QLabel#runwayDetectionStatusDot {{
    background: {tokens.muted_text};
    border: none;
    border-radius: 5px;
}}
QLabel#runwayDetectionStatusDot[detectionState="high"] {{
    background: {tokens.success};
}}
QLabel#runwayDetectionStatusDot[detectionState="moderate"] {{
    background: {tokens.warning};
}}
QLabel#runwayDetectionStatusDot[detectionState="low"] {{
    background: {tokens.error};
}}
QFrame#dropZone[status="ready"] {{
    border: 2px solid {tokens.success};
}}
QFrame#dropZone[status="error"] {{
    border: 2px solid {tokens.error};
}}
QLineEdit[nonstandard="true"] {{
    border: 2px solid {tokens.warning};
}}
QProgressBar {{
    min-height: 16px;
    background: {tokens.surface};
    color: {tokens.body_text};
    border: 1px solid {tokens.border};
    border-radius: 8px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {tokens.primary};
    border-radius: 7px;
}}
QSplitter::handle {{
    background: {tokens.border};
    width: 3px;
    margin: 8px 4px;
}}
QTabWidget::pane {{
    background: {tokens.surface};
    border: 1px solid {tokens.border};
    border-radius: 8px;
    top: -1px;
}}
QTabBar::tab {{
    background: {tokens.surface_alt};
    color: {tokens.muted_text};
    border: 1px solid {tokens.border};
    padding: 9px 18px;
}}
QTabBar::tab:selected {{
    background: {tokens.surface};
    color: {tokens.primary};
    border-bottom: 2px solid {tokens.primary};
    font-weight: 650;
}}
QTabBar::tab:hover:!selected {{
    color: {tokens.body_text};
}}
QTableWidget {{
    alternate-background-color: {tokens.background};
    gridline-color: {tokens.border};
}}
QHeaderView::section, QTableCornerButton::section {{
    background: {tokens.surface_alt};
    color: {tokens.heading_text};
    border: none;
    border-right: 1px solid {tokens.border};
    border-bottom: 1px solid {tokens.border};
    padding: 6px 8px;
    font-weight: 650;
}}
QDialogButtonBox QPushButton {{
    min-width: 80px;
}}
"""


CARD_OBJECT_NAMES = frozenset(
    {
        "workspacePanel",
        "presetToolbar",
        "resultsCard",
        "previewHost",
        "airfieldCard",
    }
)


def apply_card_shadows(root: QWidget, mode: ThemeMode) -> None:
    """Apply restrained depth only to the application's top-level cards."""
    shadow_colour = (
        QColor(0, 0, 0, 89)
        if mode == ThemeMode.DARK
        else QColor(15, 23, 42, 26)
    )
    for card in root.findChildren(QFrame):
        if card.objectName() not in CARD_OBJECT_NAMES:
            continue
        effect = QGraphicsDropShadowEffect(card)
        effect.setBlurRadius(18.0)
        effect.setOffset(0.0, 2.0)
        effect.setColor(shadow_colour)
        card.setGraphicsEffect(effect)


class ThemeController(QObject):
    """Resolve, persist, and apply the application's selected appearance."""

    mode_changed = pyqtSignal(object)
    effective_mode_changed = pyqtSignal(object)

    def __init__(
        self,
        application: QApplication,
        *,
        settings: QSettings | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.application = application
        self.settings = settings if settings is not None else QSettings()
        self._mode = self._read_mode()
        self._effective_mode = self.resolve_effective_mode(self._system_colour_scheme())
        self.application.styleHints().colorSchemeChanged.connect(
            self._on_system_colour_scheme_changed
        )
        self.apply_theme()

    @property
    def mode(self) -> ThemeMode:
        return self._mode

    @property
    def effective_mode(self) -> ThemeMode:
        return self._effective_mode

    @property
    def tokens(self) -> ThemeTokens:
        return tokens_for(self._effective_mode)

    def _read_mode(self) -> ThemeMode:
        raw_mode = self.settings.value(
            THEME_SETTING_KEY,
            ThemeMode.SYSTEM.value,
            type=str,
        )
        try:
            return ThemeMode(raw_mode)
        except (TypeError, ValueError):
            return ThemeMode.SYSTEM

    def _system_colour_scheme(self) -> Qt.ColorScheme:
        return self.application.styleHints().colorScheme()

    def resolve_effective_mode(self, scheme: Qt.ColorScheme) -> ThemeMode:
        if self._mode != ThemeMode.SYSTEM:
            return self._mode
        if scheme == Qt.ColorScheme.Dark:
            return ThemeMode.DARK
        return ThemeMode.LIGHT

    def set_mode(self, mode: ThemeMode | str) -> None:
        try:
            selected = ThemeMode(mode)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Unknown theme mode: {mode!r}") from error
        if selected == self._mode:
            return
        self._mode = selected
        self.settings.setValue(THEME_SETTING_KEY, selected.value)
        self.settings.sync()
        self.apply_theme()
        self.mode_changed.emit(selected)

    def apply_theme(self) -> None:
        effective = self.resolve_effective_mode(self._system_colour_scheme())
        changed = effective != self._effective_mode
        self._effective_mode = effective
        tokens = tokens_for(effective)
        self.application.setPalette(build_palette(tokens))
        self.application.setStyleSheet(build_stylesheet(tokens))
        if changed:
            self.effective_mode_changed.emit(effective)

    def _on_system_colour_scheme_changed(self, scheme: Qt.ColorScheme) -> None:
        if self._mode != ThemeMode.SYSTEM:
            return
        effective = self.resolve_effective_mode(scheme)
        if effective == self._effective_mode:
            return
        self._effective_mode = effective
        tokens = tokens_for(effective)
        self.application.setPalette(build_palette(tokens))
        self.application.setStyleSheet(build_stylesheet(tokens))
        self.effective_mode_changed.emit(effective)
