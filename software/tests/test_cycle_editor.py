"""Cycle library editing keeps save targets explicit and protects unsaved work."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from qtpy.QtCore import Qt
from qtpy.QtWidgets import QInputDialog, QMessageBox

from control.core.config.repository import ConfigRepository
from control.models.acquisition_cycle import AcquisitionCycle, CycleGroup, CycleStep, CycleWait
from control.models.observation_state import IlluminatorState, ObservationState
from gui.widgets.multipoint import CycleEditorDialog


@pytest.fixture
def editor(tmp_path, qtbot, monkeypatch):
    (tmp_path / "machine_configs").mkdir()
    (tmp_path / "user_profiles" / "p1").mkdir(parents=True)
    repo = ConfigRepository(base_path=tmp_path)
    repo.set_profile("p1")
    repo.save_observation_preset(
        "BF", ObservationState(illuminator_states=[IlluminatorState(illumination_channel="BF", intensity=50)])
    )
    cycle = AcquisitionCycle(
        name="First cycle", repeat=2,
        items=[CycleStep(observation_state="BF", n_frames=3, acquire_z_stack=False),
               CycleGroup(repeat=4, steps=[CycleWait(duration_ms=10), CycleStep(observation_state="BF")])],
    )
    repo.save_acquisition_cycle("First cycle", cycle)
    repo.save_acquisition_cycle("Second", cycle)
    monkeypatch.setattr(QMessageBox, "warning", MagicMock())
    monkeypatch.setattr(QMessageBox, "question", MagicMock(return_value=QMessageBox.Cancel))
    dialog = CycleEditorDialog(SimpleNamespace(config_repo=repo))
    qtbot.addWidget(dialog)
    yield dialog
    # Avoid a prompt during Qt test teardown, including after failed assertions.
    dialog._saved_snapshot = dialog._snapshot()


def select(editor, name):
    editor.list_existing.setCurrentItem(editor.list_existing.findItems(name, Qt.MatchExactly)[0])


def test_initial_draft_and_keyboard_selection(editor, qtbot):
    assert editor.edit_name.isReadOnly()
    assert not editor.btn_save_cycle.isEnabled()
    assert not editor.btn_delete_cycle.isEnabled()
    assert editor.btn_save_cycle_as.isEnabled()
    editor.list_existing.setFocus()
    qtbot.keyClick(editor.list_existing, Qt.Key_Down)
    assert editor.edit_name.text() == "First cycle"
    assert editor.spin_repeat.value() == 2
    assert editor.btn_save_cycle.isEnabled()
    assert editor.btn_delete_cycle.isEnabled()
    QMessageBox.question.assert_not_called()


def test_save_changes_updates_loaded_cycle_and_keeps_selection(editor, monkeypatch):
    select(editor, "First cycle")
    editor.spin_repeat.setValue(7)
    name_prompt = MagicMock()
    monkeypatch.setattr(QInputDialog, "getText", name_prompt)
    monkeypatch.setattr(QMessageBox, "question", MagicMock(return_value=QMessageBox.Save))
    editor.btn_save_cycle.click()
    saved = editor.repo.load_acquisition_cycle("First cycle")
    assert saved.repeat == 7
    assert saved.items[0].n_frames == 3
    assert saved.items[0].acquire_z_stack is False
    assert saved.items[1].repeat == 4
    assert saved.items[1].steps[0].duration_ms == 10
    assert editor.repo.load_acquisition_cycle("Second").repeat == 2
    assert editor.list_existing.currentItem().text() == "First cycle"
    assert editor._snapshot() == editor._saved_snapshot
    assert editor.label_cycle_status.text() == "Saved 'First cycle'"
    name_prompt.assert_not_called()


def test_cancel_overwrite_preserves_draft_and_file(editor):
    select(editor, "First cycle")
    editor.spin_repeat.setValue(7)
    editor.btn_save_cycle.click()
    assert editor.repo.load_acquisition_cycle("First cycle").repeat == 2
    assert editor.spin_repeat.value() == 7


@pytest.mark.parametrize("collision", ["First cycle", "First_cycle", "bad/name", ""])
def test_save_as_retries_invalid_or_existing_names(editor, monkeypatch, collision):
    select(editor, "First cycle")
    editor.spin_repeat.setValue(7)
    answers = iter([(collision, True), ("Copy", True)])
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: next(answers))
    editor.btn_save_cycle_as.click()
    assert editor.repo.load_acquisition_cycle("First cycle").repeat == 2
    assert editor.repo.load_acquisition_cycle("Copy").repeat == 7
    assert editor.list_existing.currentItem().text() == "Copy"
    assert editor.edit_name.text() == "Copy"
    QMessageBox.warning.assert_called_once()


def test_new_cycle_can_be_saved_as(editor, monkeypatch):
    editor._make_leaf_item(editor.tree, {"type": "wait", "duration_ms": 42})
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("New cycle", True))
    editor.btn_save_cycle_as.click()
    assert editor.repo.load_acquisition_cycle("New cycle").items[0].duration_ms == 42
    assert editor.btn_save_cycle.isEnabled()
    assert editor.btn_delete_cycle.isEnabled()


@pytest.mark.parametrize("loaded", [True, False])
def test_cancel_switch_restores_selection_and_edits(editor, loaded):
    if loaded:
        select(editor, "First cycle")
    editor.spin_repeat.setValue(7)
    select(editor, "Second")
    assert editor.spin_repeat.value() == 7
    if loaded:
        assert editor.list_existing.currentItem().text() == "First cycle"
    else:
        assert editor.list_existing.currentItem() is None
    QMessageBox.question.assert_called_once()


def test_save_on_switch_saves_previous_cycle_once(editor, monkeypatch):
    select(editor, "First cycle")
    editor.spin_repeat.setValue(7)
    monkeypatch.setattr(QMessageBox, "question", MagicMock(return_value=QMessageBox.Save))
    select(editor, "Second")
    assert editor.repo.load_acquisition_cycle("First cycle").repeat == 7
    assert editor.edit_name.text() == "Second"
    assert editor.spin_repeat.value() == 2
    QMessageBox.question.assert_called_once()


def test_new_cycle_cancel_and_discard(editor, monkeypatch):
    select(editor, "First cycle")
    editor.spin_repeat.setValue(7)
    editor.btn_new_cycle.click()
    assert editor.edit_name.text() == "First cycle"
    assert editor.spin_repeat.value() == 7
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Discard)
    editor.btn_new_cycle.click()
    assert editor.edit_name.text() == ""
    assert editor.tree.topLevelItemCount() == 0
    assert editor.spin_repeat.value() == 1
    assert editor.list_existing.currentItem() is None


def test_delete_keeps_draft_and_requires_save_as(editor, monkeypatch):
    select(editor, "First cycle")
    editor.spin_repeat.setValue(7)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    editor.btn_delete_cycle.click()
    assert editor.repo.list_acquisition_cycles() == ["Second"]
    assert editor.spin_repeat.value() == 7
    assert editor.tree.topLevelItemCount() == 2
    assert not editor.btn_save_cycle.isEnabled()
    assert not editor.btn_delete_cycle.isEnabled()
    assert editor.btn_save_cycle_as.isEnabled()
    assert editor._snapshot() != editor._saved_snapshot


@pytest.mark.parametrize("failure", [False, True])
def test_cancel_or_failed_delete_preserves_selection(editor, monkeypatch, failure):
    select(editor, "First cycle")
    if failure:
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
        monkeypatch.setattr(editor.repo, "delete_acquisition_cycle", MagicMock(side_effect=OSError("read only")))
    editor.btn_delete_cycle.click()
    assert editor.list_existing.currentItem().text() == "First cycle"
    assert editor.repo.list_acquisition_cycles() == ["First cycle", "Second"]


@pytest.mark.parametrize("close_action", ["accept", "reject", "close"])
def test_cancel_close_preserves_visible_draft(editor, close_action):
    editor.show()
    editor.spin_repeat.setValue(7)
    getattr(editor, close_action)()
    assert editor.isVisible()
    assert editor.spin_repeat.value() == 7


def test_failed_save_during_switch_keeps_original_draft(editor, monkeypatch):
    select(editor, "First cycle")
    editor.spin_repeat.setValue(7)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Save)
    monkeypatch.setattr(editor.repo, "save_acquisition_cycle", MagicMock(side_effect=OSError("disk full")))
    select(editor, "Second")
    assert editor.list_existing.currentItem().text() == "First cycle"
    assert editor.spin_repeat.value() == 7
    assert editor.repo.load_acquisition_cycle("First cycle").repeat == 2
    assert "disk full" in QMessageBox.warning.call_args.args[2]
