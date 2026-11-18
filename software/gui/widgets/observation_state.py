"""
Reusable Observation State preset UI (select, load, save changes, save as, delete).

Embed or place this widget anywhere in the layout; wire ``on_state_changed`` to
refresh parent UI (e.g. illumination sliders, channel lists) after a successful
save or load.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from qtpy.QtWidgets import QComboBox, QGroupBox, QHBoxLayout, QPushButton, QSizePolicy

from control.core.config.repository import ConfigRepository
from control.core.live_controller import LiveController
from control.core.objective_store import ObjectiveStore


class ObservationStateWidget(QGroupBox):
    """Explicit actions for editing Observation State YAML presets."""

    def __init__(
        self,
        parent=None,
        *,
        config_repo: ConfigRepository,
        live_controller: LiveController,
        objective_store: ObjectiveStore,
        emission_filter_wheel: Optional[Any] = None,
        on_state_changed: Optional[Callable[[], None]] = None,
        title: str = "Observation State",
    ) -> None:
        super().__init__(title, parent)
        self._config_repo = config_repo
        self._live_controller = live_controller
        self._objective_store = objective_store
        self._emission_filter_wheel = emission_filter_wheel
        self._on_state_changed = on_state_changed

        row = QHBoxLayout(self)
        row.setSpacing(4)

        self._combo_presets = QComboBox()
        self._combo_presets.setMinimumWidth(140)
        self._combo_presets.setToolTip("Select a saved preset, then Load to apply its settings")

        self._btn_save = QPushButton("Save Changes")
        self._btn_save.setToolTip("Replace the selected preset with the current imaging settings")
        self._btn_save.clicked.connect(self._on_save)

        self._btn_save_as = QPushButton("Save As…")
        self._btn_save_as.setToolTip(
            "Save current imaging settings as a new preset with a different name"
        )
        self._btn_save_as.clicked.connect(self._on_save_as)

        self._btn_load = QPushButton("Load")
        self._btn_load.setToolTip("Apply the selected preset to the current imaging settings")
        self._btn_load.clicked.connect(self._on_load)

        self._btn_delete = QPushButton("Delete…")
        self._btn_delete.setToolTip("Delete the selected saved preset")
        self._btn_delete.clicked.connect(self._on_delete)

        row.addWidget(self._combo_presets, stretch=1)
        for button in (self._btn_load, self._btn_save, self._btn_save_as, self._btn_delete):
            button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            row.addWidget(button)
        self._combo_presets.currentIndexChanged.connect(self._update_actions)
        self.refresh_presets()

    def _update_actions(self) -> None:
        has_profile = bool(self._config_repo.current_profile)
        has_selection = has_profile and self._combo_presets.currentIndex() >= 0
        for button in (self._btn_load, self._btn_save, self._btn_delete):
            button.setEnabled(has_selection)
        self._btn_save_as.setEnabled(has_profile)

    def refresh_presets(self) -> None:
        """Repopulate the preset list (e.g. after profile switch or external save)."""
        self._combo_presets.blockSignals(True)
        current = self._combo_presets.currentText()
        self._combo_presets.clear()
        for name in self._config_repo.list_observation_presets():
            self._combo_presets.addItem(name)
        idx = self._combo_presets.findText(current)
        if idx >= 0:
            self._combo_presets.setCurrentIndex(idx)
        self._combo_presets.blockSignals(False)
        self._update_actions()

    def _after_success(self) -> None:
        self.refresh_presets()
        if self._on_state_changed:
            self._on_state_changed()

    def _on_save(self) -> None:
        self._save(preset_name=self._combo_presets.currentText())

    def _on_save_as(self) -> None:
        self._save()

    def _after_named_success(self, name: str) -> None:
        self.refresh_presets()
        self._combo_presets.setCurrentText(name)
        if self._on_state_changed:
            self._on_state_changed()

    def _save(self, preset_name: Optional[str] = None) -> None:
        from gui.widgets.observation_state_dialogs import run_save_observation_state_dialog

        run_save_observation_state_dialog(
            self,
            self._config_repo,
            self._live_controller,
            self._objective_store,
            self._emission_filter_wheel,
            preset_name=preset_name,
            on_saved=self._after_named_success,
        )

    def _on_delete(self) -> None:
        from gui.widgets.observation_state_dialogs import run_delete_observation_state

        run_delete_observation_state(
            self,
            self._config_repo,
            self._combo_presets.currentText(),
            on_success=self._after_success,
        )

    def _on_load(self) -> None:
        from gui.widgets.observation_state_dialogs import run_load_observation_state

        name = self._combo_presets.currentText()
        run_load_observation_state(
            self,
            self._config_repo,
            self._live_controller,
            self._objective_store,
            self._emission_filter_wheel,
            preset_name=name.strip() if name.strip() else None,
            on_success=self._after_success,
        )
