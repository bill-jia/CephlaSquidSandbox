"""
Reusable Observation State preset UI (combo + Save / Load).

Embed or place this widget anywhere in the layout; wire ``on_state_changed`` to
refresh parent UI (e.g. illumination sliders, channel lists) after a successful
save or load.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from qtpy.QtWidgets import QComboBox, QGroupBox, QHBoxLayout, QPushButton

from control.core.config.repository import ConfigRepository
from control.core.live_controller import LiveController
from control.core.objective_store import ObjectiveStore


class ObservationStateWidget(QGroupBox):
    """Group box: preset dropdown, Save…, and Load for Observation State YAML presets."""

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
        row.setSpacing(6)

        self._combo_presets = QComboBox()
        self._combo_presets.setMinimumWidth(140)
        self.refresh_presets()

        btn_save = QPushButton("Save…")
        btn_save.setToolTip(
            "Save current imaging settings as a named preset (channels, exposure/gain, filters, confocal)"
        )
        btn_save.clicked.connect(self._on_save)

        btn_load = QPushButton("Load")
        btn_load.setToolTip("Load the selected Observation State preset")
        btn_load.clicked.connect(self._on_load)

        row.addWidget(self._combo_presets, stretch=1)
        row.addWidget(btn_save)
        row.addWidget(btn_load)

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

    def _after_success(self) -> None:
        self.refresh_presets()
        if self._on_state_changed:
            self._on_state_changed()

    def _on_save(self) -> None:
        from gui.observation_state_gui import run_save_observation_state_dialog

        run_save_observation_state_dialog(
            self,
            self._config_repo,
            self._live_controller,
            self._objective_store,
            self._emission_filter_wheel,
            on_success=self._after_success,
        )

    def _on_load(self) -> None:
        from gui.observation_state_gui import run_load_observation_state

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
