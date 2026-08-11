"""Tabbed application settings and about information."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from google_maps_settings import GoogleMapsSettings
from theme import ThemeController, ThemeMode


class SettingsDialog(QDialog):
    """Application settings with appearance and about tabs."""

    def __init__(
        self,
        theme_controller: ThemeController,
        parent=None,
        *,
        maps_settings: GoogleMapsSettings | None = None,
    ) -> None:
        super().__init__(parent)
        self.theme_controller = theme_controller
        self.maps_settings = maps_settings or GoogleMapsSettings(
            settings=theme_controller.settings,
            parent=self,
        )
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(680, 470)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        heading = QLabel("Settings")
        heading.setObjectName("settingsTitle")
        root.addWidget(heading)

        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("Settings sections")
        self.tabs.addTab(self._build_appearance_tab(), "Appearance")
        self.maps_tab = self._build_maps_tab()
        self.tabs.addTab(self.maps_tab, "Google Maps")
        self.tabs.addTab(self._build_about_tab(), "About")
        root.addWidget(self.tabs, 1)

        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.reject)
        root.addWidget(close_buttons)

        self.theme_controller.mode_changed.connect(self._sync_theme_buttons)
        self._sync_theme_buttons(self.theme_controller.mode)

    def _build_maps_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(12)

        title = QLabel("Google Maps 3D preview")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        explanation = QLabel(
            "Enter your Google Maps JavaScript API key. The project must have "
            "billing enabled and the Maps JavaScript API available. Restrict the "
            "key to that API and to http://127.0.0.1/* where possible."
        )
        explanation.setObjectName("mutedText")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        key_row = QHBoxLayout()
        self.maps_api_key_input = QLineEdit(self.maps_settings.api_key)
        self.maps_api_key_input.setObjectName("mapsApiKeyInput")
        self.maps_api_key_input.setAccessibleName("Google Maps API key")
        self.maps_api_key_input.setPlaceholderText("Paste API key")
        self.maps_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        key_row.addWidget(self.maps_api_key_input, 1)

        self.show_maps_key_button = QPushButton("Show")
        self.show_maps_key_button.setCheckable(True)
        self.show_maps_key_button.setAccessibleName("Show Google Maps API key")
        self.show_maps_key_button.toggled.connect(self._toggle_maps_key_visibility)
        key_row.addWidget(self.show_maps_key_button)
        layout.addLayout(key_row)

        actions = QHBoxLayout()
        self.save_maps_key_button = QPushButton("Save key")
        self.save_maps_key_button.setObjectName("primaryButton")
        self.clear_maps_key_button = QPushButton("Clear key")
        self.save_maps_key_button.clicked.connect(self._save_maps_key)
        self.clear_maps_key_button.clicked.connect(self._clear_maps_key)
        actions.addWidget(self.save_maps_key_button)
        actions.addWidget(self.clear_maps_key_button)
        actions.addStretch()
        layout.addLayout(actions)

        self.maps_key_status = QLabel()
        self.maps_key_status.setObjectName("mutedText")
        self.maps_key_status.setWordWrap(True)
        layout.addWidget(self.maps_key_status)

        storage_note = QLabel(
            "The key is stored in your operating-system application settings. "
            "It is masked here, but browser API keys are not secrets and are "
            "visible in requests made to Google. Keys are never included in KML, "
            "presets, logs, or error details."
        )
        storage_note.setObjectName("mutedText")
        storage_note.setWordWrap(True)
        layout.addWidget(storage_note)
        layout.addStretch()
        return page

    def _build_appearance_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(10)

        title = QLabel("Colour theme")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        explanation = QLabel(
            "Choose how Tasmead Display Tools appears. System follows your "
            "operating system and updates automatically when it changes."
        )
        explanation.setObjectName("mutedText")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.theme_switch = QFrame()
        self.theme_switch.setObjectName("themeSwitch")
        self.theme_switch.setFixedSize(390, 52)
        switch_layout = QHBoxLayout(self.theme_switch)
        switch_layout.setContentsMargins(4, 4, 4, 4)
        switch_layout.setSpacing(0)

        self.theme_group = QButtonGroup(self)
        self.theme_group.setExclusive(True)
        self.theme_buttons: dict[ThemeMode, QPushButton] = {}
        labels = {
            ThemeMode.SYSTEM: "System",
            ThemeMode.LIGHT: "Light",
            ThemeMode.DARK: "Dark",
        }
        for index, mode in enumerate(ThemeMode):
            button = QPushButton(labels[mode])
            button.setObjectName("themeSegment")
            button.setCheckable(True)
            button.setAccessibleName(f"Use {labels[mode].lower()} theme")
            button.setMinimumWidth(126)
            self.theme_group.addButton(button, index)
            self.theme_buttons[mode] = button
            switch_layout.addWidget(button, 1)

        self.theme_group.idClicked.connect(self._theme_clicked)
        layout.addWidget(
            self.theme_switch,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )

        note = QLabel("Changes are applied immediately and remembered for next time.")
        note.setObjectName("mutedText")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        return page

    def _build_about_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(12)

        product = QLabel("Tasmead Display Tools")
        product.setObjectName("sectionTitle")
        product.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(product)

        purpose = QLabel(
            "Planning and visualisation tools for aircraft display workflows."
        )
        purpose.setObjectName("mutedText")
        purpose.setWordWrap(True)
        purpose.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(purpose)

        authors = QLabel(
            "<b>Authors</b><br><br>"
            "Tasmead Display Tool created by Will Crook<br>"
            '<a href="https://github.com/WillCrook">github.com/WillCrook</a>'
            "<br><br>"
            "Debris Trajectory Calculations created by mkarachalios-1<br>"
            '<a href="https://github.com/mkarachalios-1/'
            'airshow-trajectory-app/blob/main/streamlit_app.py">'
            "View the original project on GitHub</a>"
        )
        authors.setOpenExternalLinks(True)
        authors.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        authors.setWordWrap(True)
        layout.addWidget(authors)

        contact = QLabel(
            '<b>Contact</b><br><a href="mailto:rich.pillans@tasmead.com">'
            "rich.pillans@tasmead.com</a>"
        )
        contact.setOpenExternalLinks(True)
        contact.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(contact)
        layout.addStretch()
        return page

    def _theme_clicked(self, button_id: int) -> None:
        mode = tuple(ThemeMode)[button_id]
        self.theme_controller.set_mode(mode)

    def _sync_theme_buttons(self, mode: ThemeMode) -> None:
        for candidate, button in self.theme_buttons.items():
            button.setChecked(candidate == mode)

    def _toggle_maps_key_visibility(self, visible: bool) -> None:
        self.maps_api_key_input.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self.show_maps_key_button.setText("Hide" if visible else "Show")

    def _save_maps_key(self) -> None:
        key = self.maps_api_key_input.text().strip()
        if not key:
            self.maps_key_status.setText("Enter a key, or choose Clear key.")
            return
        self.maps_settings.set_api_key(key)
        self.maps_api_key_input.setText(key)
        self.maps_key_status.setText("Google Maps API key saved locally.")

    def _clear_maps_key(self) -> None:
        self.maps_settings.clear()
        self.maps_api_key_input.clear()
        self.maps_key_status.setText("Google Maps API key cleared.")

    def select_tab(self, name: str) -> None:
        """Select a settings section by stable, case-insensitive name."""
        wanted = name.strip().casefold()
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index).casefold() == wanted:
                self.tabs.setCurrentIndex(index)
                if wanted == "google maps":
                    self.maps_api_key_input.setText(self.maps_settings.api_key)
                return
