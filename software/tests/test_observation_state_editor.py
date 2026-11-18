"""Exercise preset editing through the Qt controls and real YAML repository."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from qtpy.QtWidgets import QInputDialog, QMessageBox

from control.core.config.repository import ConfigRepository
from control.models.observation_state import CameraSettings, IlluminatorState, ObservationState
from gui.widgets.observation_state import ObservationStateWidget
from gui.widgets.observation_state_dialogs import run_load_observation_state, run_save_observation_state_dialog


@pytest.fixture
def editor(tmp_path, qtbot, monkeypatch):
    (tmp_path / "machine_configs").mkdir()
    (tmp_path / "user_profiles" / "p1").mkdir(parents=True)
    repo = ConfigRepository(base_path=tmp_path)
    repo.set_profile("p1")
    old = ObservationState(
        name="old", camera_settings=CameraSettings(exposure_time_ms=10, gain_mode=0),
        illuminator_states=[IlluminatorState(illumination_channel="BF", intensity=50, on=True)],
    )
    repo.save_observation_preset("BF full", old)
    repo.save_observation_preset("GFP", old)
    current = old.model_copy(update={"camera_settings": CameraSettings(exposure_time_ms=25, gain_mode=0)})
    controller = SimpleNamespace(
        microscope=SimpleNamespace(addons=None),
        collect_observation_state=MagicMock(return_value=current),
        apply_observation_state_preset=MagicMock(),
    )
    changed = MagicMock()
    widget = ObservationStateWidget(
        config_repo=repo, live_controller=SimpleNamespace(obs_controller=controller),
        objective_store=None, on_state_changed=changed,
    )
    qtbot.addWidget(widget)
    monkeypatch.setattr(QMessageBox, "warning", MagicMock())
    return SimpleNamespace(widget=widget, repo=repo, controller=controller, changed=changed)


def test_save_changes_uses_selection_without_name_entry(editor, qtbot, monkeypatch):
    w = editor.widget
    w._combo_presets.setCurrentText("BF full")
    prompt = MagicMock(return_value=QMessageBox.Save)
    monkeypatch.setattr(QMessageBox, "question", prompt)
    name_prompt = MagicMock()
    monkeypatch.setattr(QInputDialog, "getText", name_prompt)
    w._btn_save.click()
    qtbot.waitUntil(lambda: editor.changed.call_count == 1)
    assert editor.repo.load_observation_preset("BF full").camera_settings.exposure_time_ms == 25
    assert editor.repo.load_observation_preset("GFP").camera_settings.exposure_time_ms == 10
    assert "BF full" in prompt.call_args.args[2]
    name_prompt.assert_not_called()
    assert w._combo_presets.currentText() == "BF full"


@pytest.mark.parametrize("action", ["_btn_save", "_btn_delete"])
def test_cancel_preserves_saved_state(editor, monkeypatch, action):
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Cancel)
    getattr(editor.widget, action).click()
    assert editor.repo.list_observation_presets() == ["BF full", "GFP"]
    assert editor.repo.load_observation_preset("BF full").camera_settings.exposure_time_ms == 10
    editor.controller.collect_observation_state.assert_not_called()
    editor.changed.assert_not_called()


@pytest.mark.parametrize("collision", ["BF full", "BF_full"])
def test_save_as_rejects_existing_name_and_filename_alias(editor, qtbot, monkeypatch, collision):
    answers = iter([(collision, True), ("New state", True)])
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: next(answers))
    editor.widget._btn_save_as.click()
    qtbot.waitUntil(lambda: editor.changed.call_count == 1)
    assert editor.widget._combo_presets.currentText() == "New state"
    assert editor.repo.load_observation_preset("New state").camera_settings.exposure_time_ms == 25
    assert editor.repo.load_observation_preset("BF full").camera_settings.exposure_time_ms == 10
    QMessageBox.warning.assert_called_once()


def test_delete_refreshes_list_without_applying_live_settings(editor, qtbot, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    editor.widget._combo_presets.setCurrentText("BF full")
    editor.widget._btn_delete.click()
    qtbot.waitUntil(lambda: editor.changed.call_count == 1)
    assert editor.repo.list_observation_presets() == ["GFP"]
    assert editor.widget._combo_presets.currentText() == "GFP"
    editor.widget._btn_delete.click()
    qtbot.waitUntil(lambda: editor.changed.call_count == 2)
    assert editor.repo.list_observation_presets() == []
    assert not editor.widget._btn_save.isEnabled()
    assert not editor.widget._btn_load.isEnabled()
    assert not editor.widget._btn_delete.isEnabled()
    assert editor.widget._btn_save_as.isEnabled()
    editor.controller.apply_observation_state_preset.assert_not_called()
    editor.controller.collect_observation_state.assert_not_called()


def test_actions_disabled_without_profile(editor):
    editor.repo._current_profile = None
    editor.widget.refresh_presets()
    assert not editor.widget._btn_save_as.isEnabled()
    assert not editor.widget._btn_save.isEnabled()
    assert not editor.widget._btn_delete.isEnabled()
    assert not editor.widget._btn_load.isEnabled()


def test_failed_save_does_not_report_success(editor, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("New state", True))
    monkeypatch.setattr(editor.repo, "save_observation_preset", MagicMock(side_effect=OSError("disk full")))
    assert not run_save_observation_state_dialog(
        editor.widget, editor.repo, SimpleNamespace(obs_controller=editor.controller), None,
        on_success=editor.changed,
    )
    editor.changed.assert_not_called()
    assert editor.repo.list_observation_presets() == ["BF full", "GFP"]
    assert "disk full" in QMessageBox.warning.call_args.args[2]


def test_delete_rejects_missing_preset_and_other_profiles(editor):
    with pytest.raises(ValueError):
        editor.repo.delete_observation_preset("../BF full")
    (editor.repo.user_profiles_path / "p2").mkdir()
    with pytest.raises(ValueError):
        editor.repo.delete_observation_preset("BF full", profile="p2")
    assert editor.repo.list_observation_presets() == ["BF full", "GFP"]


def test_menu_load_selects_loaded_state_for_subsequent_save(editor, qtbot, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args: ("GFP", True))
    assert run_load_observation_state(
        editor.widget, editor.repo, SimpleNamespace(obs_controller=editor.controller), None,
        on_loaded=editor.widget._after_named_success,
    )
    qtbot.waitUntil(lambda: editor.changed.call_count == 1)
    assert editor.widget._combo_presets.currentText() == "GFP"
    assert editor.controller.apply_observation_state_preset.call_args.args[0].name == "GFP"


def test_cancel_save_as_does_not_collect_or_save(editor, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("", False))
    editor.widget._btn_save_as.click()
    assert editor.repo.list_observation_presets() == ["BF full", "GFP"]
    editor.controller.collect_observation_state.assert_not_called()
    editor.changed.assert_not_called()


def test_failed_delete_keeps_selection_and_reports_error(editor, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    monkeypatch.setattr(editor.repo, "delete_observation_preset", MagicMock(side_effect=OSError("read only")))
    editor.widget._btn_delete.click()
    assert editor.widget._combo_presets.currentText() == "BF full"
    assert editor.repo.list_observation_presets() == ["BF full", "GFP"]
    assert "read only" in QMessageBox.warning.call_args.args[2]
    editor.changed.assert_not_called()
