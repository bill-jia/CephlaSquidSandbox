"""
Qt dialogs for Observation State save, load, and deletion.

Shared by the HCS main window (Settings menu) and IlluminationWidget.
"""

from __future__ import annotations

from typing import Callable, Optional

from qtpy.QtCore import QTimer
from qtpy.QtWidgets import QInputDialog, QMessageBox

from control.core.observation_state_service import collect_emission_filter_positions, observation_preset_path


def run_save_observation_state_dialog(
    parent,
    config_repo,
    live_controller,
    objective_store,
    emission_filter_wheel=None,
    *,
    preset_name: Optional[str] = None,
    on_success: Optional[Callable[[], None]] = None,
    on_saved: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Save current settings over ``preset_name``, or prompt for a new name.

    Save As never overwrites an existing file, including filename aliases.

    Returns True if a preset was saved successfully.
    """
    repo = config_repo
    if not repo.current_profile:
        QMessageBox.warning(
            parent,
            "Observation State",
            "Load a user profile before saving an Observation State preset.",
        )
        return False
    try:
        if preset_name is None:
            while True:
                name, ok = QInputDialog.getText(parent, "Save Observation State As", "New preset name:")
                if not ok or not name.strip():
                    return False
                name = name.strip()
                try:
                    path = observation_preset_path(repo, name)
                except ValueError as e:
                    QMessageBox.warning(parent, "Observation State", str(e))
                    continue
                if name in repo.list_observation_presets() or path.exists():
                    QMessageBox.warning(
                        parent,
                        "Preset already exists",
                        "That name is already in use. Choose another name, or select the existing "
                        "preset and use Save Changes to replace it.",
                    )
                    continue
                break
        else:
            name = preset_name.strip()
            if name not in repo.list_observation_presets():
                QMessageBox.warning(parent, "Observation State", "Select an existing preset to save changes.")
                return False
            if (
                QMessageBox.question(
                    parent,
                    "Save Observation State Changes",
                    f"Replace the saved settings for '{name}' with the current imaging settings?",
                    QMessageBox.Save | QMessageBox.Cancel,
                    QMessageBox.Cancel,
                )
                != QMessageBox.Save
            ):
                return False
        obs_controller = live_controller.obs_controller
        xlight = getattr(getattr(obs_controller.microscope, "addons", None), "xlight", None)
        emission = collect_emission_filter_positions(emission_filter_wheel, xlight=xlight)
        state = obs_controller.collect_observation_state(
            emission_filter_positions=emission or None,
        )
        repo.save_observation_preset(name.strip(), state)
    except (ValueError, OSError) as e:
        QMessageBox.warning(parent, "Observation State", str(e))
        return False
    if on_success:
        QTimer.singleShot(0, on_success)
    if on_saved:
        QTimer.singleShot(0, lambda: on_saved(name))
    return True


def run_delete_observation_state(
    parent,
    config_repo,
    preset_name: str,
    *,
    on_success: Optional[Callable[[], None]] = None,
) -> bool:
    """Confirm deletion of the selected saved state; leave live settings alone."""
    if not config_repo.current_profile or preset_name not in config_repo.list_observation_presets():
        QMessageBox.warning(parent, "Observation State", "Select an existing preset to delete.")
        return False
    if (
        QMessageBox.question(
            parent,
            "Delete Observation State",
            f"Delete the saved preset '{preset_name}'? This cannot be undone.\n\n"
            "Current imaging settings will be kept. Acquisition channels using this preset "
            "will need another state selected.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        != QMessageBox.Yes
    ):
        return False
    try:
        config_repo.delete_observation_preset(preset_name)
    except (ValueError, OSError) as e:
        QMessageBox.warning(parent, "Observation State", str(e))
        return False
    if on_success:
        QTimer.singleShot(0, on_success)
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
    on_loaded: Optional[Callable[[str], None]] = None,
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
            "Load a user profile before loading an Observation State preset.",
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
        live_controller.obs_controller.apply_observation_state_preset(
            state,
            emission_filter_wheel=emission_filter_wheel,
        )
    except Exception as e:
        QMessageBox.warning(parent, "Observation State", str(e))
        return False
    if on_success:
        QTimer.singleShot(0, on_success)
    if on_loaded:
        QTimer.singleShot(0, lambda: on_loaded(name))
    return True
