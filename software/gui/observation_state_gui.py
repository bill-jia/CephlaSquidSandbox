"""
Qt dialogs for Observation State save/load.

Shared by the HCS main window (Settings menu) and IlluminationWidget.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from qtpy.QtWidgets import QInputDialog, QMessageBox

from control.core.observation_state_service import apply_observation_state, collect_observation_state


def _collect_emission_positions(emission_filter_wheel: Optional[Any]) -> dict:
    emission: dict = {}
    if emission_filter_wheel and hasattr(emission_filter_wheel, "get_filter_wheel_position"):
        try:
            pos = emission_filter_wheel.get_filter_wheel_position()
            if isinstance(pos, dict):
                emission = {
                    str(k): int(v) if isinstance(v, (int, float)) else v for k, v in pos.items()
                }
        except Exception:
            pass
    return emission


def run_save_observation_state_dialog(
    parent,
    config_repo,
    live_controller,
    objective_store,
    emission_filter_wheel=None,
    *,
    on_success: Optional[Callable[[], None]] = None,
) -> bool:
    """
    Prompt for a name and save current Observation State.

    Returns True if a preset was saved successfully.
    """
    repo = config_repo
    if not repo.current_profile:
        QMessageBox.warning(
            parent,
            "Observation State",
            "Load a configuration profile before saving an Observation State preset.",
        )
        return False
    name, ok = QInputDialog.getText(parent, "Save Observation State", "Preset name:")
    if not ok or not name.strip():
        return False
    emission = _collect_emission_positions(emission_filter_wheel)
    try:
        state = collect_observation_state(
            live_controller,
            repo,
            objective_store.current_objective,
            emission_filter_positions=emission or None,
        )
        repo.save_observation_preset(name.strip(), state)
    except ValueError as e:
        QMessageBox.warning(parent, "Observation State", str(e))
        return False
    if on_success:
        on_success()
    return True


def run_load_observation_state(
    parent,
    config_repo,
    live_controller,
    objective_store,
    emission_filter_wheel=None,
    *,
    preset_name: Optional[str] = None,
    on_success: Optional[Callable[[], None]] = None,
) -> bool:
    """
    Load an Observation State preset.

    If ``preset_name`` is None, shows a preset picker dialog. Otherwise loads that
    name (must exist in ``list_observation_presets()``).

    Returns True if a preset was loaded successfully.
    """
    repo = config_repo
    if not repo.current_profile:
        QMessageBox.warning(
            parent,
            "Observation State",
            "Load a configuration profile before loading an Observation State preset.",
        )
        return False
    names = repo.list_observation_presets()
    if not names:
        QMessageBox.information(parent, "Observation State", "No saved Observation State presets.")
        return False

    if preset_name is None:
        name, ok = QInputDialog.getItem(parent, "Load Observation State", "Preset:", names, 0, False)
        if not ok or not name:
            return False
    else:
        name = preset_name.strip()
        if not name:
            QMessageBox.information(parent, "Observation State", "Select a preset to load.")
            return False
        if name not in names:
            QMessageBox.warning(parent, "Observation State", f"Preset '{name}' was not found.")
            return False

    state = repo.load_observation_preset(name)
    if state is None:
        QMessageBox.warning(parent, "Observation State", f"Could not load preset '{name}'.")
        return False
    try:
        apply_observation_state(
            state,
            repo,
            live_controller,
            objective_store,
            emission_filter_wheel=emission_filter_wheel,
        )
    except Exception as e:
        QMessageBox.warning(parent, "Observation State", str(e))
        return False
    if on_success:
        on_success()
    return True
