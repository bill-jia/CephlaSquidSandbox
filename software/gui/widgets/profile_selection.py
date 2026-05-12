"""Startup user-profile selection dialog.

Shown before the main microscope/window is built. Lets the user pick which
``user_profiles/{profile}/`` directory the session will load configs from. The
chosen profile is returned via :meth:`ProfileSelectionDialog.selected_profile`
and recorded as the last-active profile through ``ConfigRepository``.
"""

from typing import Optional

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

import squid.logging
from control.core.config.repository import ConfigRepository

log = squid.logging.get_logger(__name__)


class ProfileSelectionDialog(QDialog):
    """Modal dialog for choosing (or creating) a user profile at startup."""

    def __init__(self, config_repo: ConfigRepository, parent=None):
        super().__init__(parent)
        self.config_repo = config_repo
        self._selected_profile: Optional[str] = None

        self.setWindowTitle("Select User Profile")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setMinimumHeight(360)

        self._setup_ui()
        self._populate_profiles()
        self._connect_signals()
        self._update_load_enabled()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(
            QLabel(
                "Choose the user profile to load for this session. "
                "Each profile keeps its own channel configs, observation-state "
                "presets, laser AF calibrations, and saved GUI state."
            )
        )

        self.list_profiles = QListWidget()
        self.list_profiles.setSelectionMode(QListWidget.SingleSelection)
        layout.addWidget(self.list_profiles, 1)

        new_row = QHBoxLayout()
        self.btn_new_empty = QPushButton("New empty profile…")
        self.btn_new_copy = QPushButton("Duplicate selected…")
        new_row.addWidget(self.btn_new_empty)
        new_row.addWidget(self.btn_new_copy)
        new_row.addStretch()
        layout.addLayout(new_row)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Load profile")
        layout.addWidget(self.buttons)

    def _connect_signals(self):
        self.list_profiles.itemSelectionChanged.connect(self._update_load_enabled)
        self.list_profiles.itemDoubleClicked.connect(self._on_double_click)
        self.btn_new_empty.clicked.connect(self._create_empty_profile)
        self.btn_new_copy.clicked.connect(self._duplicate_selected_profile)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)

    def _populate_profiles(self):
        self.list_profiles.clear()
        profiles = self.config_repo.get_available_profiles()
        last_active = self.config_repo.get_last_active_profile()

        for name in profiles:
            item = QListWidgetItem(name)
            self.list_profiles.addItem(item)

        # Preselect the last-active profile, falling back to the first entry.
        if profiles:
            target = last_active if last_active in profiles else profiles[0]
            matches = self.list_profiles.findItems(target, Qt.MatchExactly)
            if matches:
                self.list_profiles.setCurrentItem(matches[0])
        # Disable "duplicate" when there's nothing to copy from.
        self.btn_new_copy.setEnabled(bool(profiles))

    def _update_load_enabled(self):
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(
            self.list_profiles.currentItem() is not None
        )

    def _on_double_click(self, _item):
        self._accept()

    def _prompt_new_name(self, title: str) -> Optional[str]:
        existing = set(self.config_repo.get_available_profiles())
        while True:
            name, ok = QInputDialog.getText(
                self, title, "Profile name:", QLineEdit.Normal, ""
            )
            if not ok:
                return None
            name = name.strip()
            if not name:
                QMessageBox.warning(self, title, "Profile name cannot be empty.")
                continue
            if name in existing:
                QMessageBox.warning(
                    self, title, f"A profile named '{name}' already exists."
                )
                continue
            return name

    def _create_empty_profile(self):
        name = self._prompt_new_name("New profile")
        if name is None:
            return
        try:
            self.config_repo.create_profile(name)
        except ValueError as exc:
            QMessageBox.warning(self, "New profile", str(exc))
            return
        self._populate_profiles()
        matches = self.list_profiles.findItems(name, Qt.MatchExactly)
        if matches:
            self.list_profiles.setCurrentItem(matches[0])

    def _duplicate_selected_profile(self):
        item = self.list_profiles.currentItem()
        if item is None:
            return
        source = item.text()
        name = self._prompt_new_name(f"Duplicate '{source}'")
        if name is None:
            return
        try:
            self.config_repo.copy_profile(source, name)
        except ValueError as exc:
            QMessageBox.warning(self, "Duplicate profile", str(exc))
            return
        self._populate_profiles()
        matches = self.list_profiles.findItems(name, Qt.MatchExactly)
        if matches:
            self.list_profiles.setCurrentItem(matches[0])

    def _accept(self):
        item = self.list_profiles.currentItem()
        if item is None:
            return
        self._selected_profile = item.text()
        self.accept()

    def selected_profile(self) -> Optional[str]:
        return self._selected_profile


def prompt_for_profile(parent=None) -> Optional[str]:
    """Run the startup profile selector and return the chosen profile name.

    Auto-creates a ``default`` profile (without showing the dialog) when no
    profiles exist on disk — this matches the legacy behavior so first-run
    users aren't blocked by an empty list. Returns ``None`` if the user
    cancels the dialog.
    """
    repo = ConfigRepository()
    if not repo.get_available_profiles():
        log.info("No profiles found, creating 'default' before showing selector")
        repo.create_profile("default")

    dialog = ProfileSelectionDialog(repo, parent=parent)
    if dialog.exec_() != QDialog.Accepted:
        return None
    return dialog.selected_profile()
