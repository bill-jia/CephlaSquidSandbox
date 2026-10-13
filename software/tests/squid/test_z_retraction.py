"""Working Z survives parking, startup and inter-region travel."""

from unittest.mock import Mock

import pytest

import control._def as _def
from squid.config import get_stage_config
from squid.stage.cephla import CephlaStage
from squid.stage.utils import (
    move_to_cached_or_default_startup_position,
    initialize_z_retraction,
    move_to_loading_position,
    move_to_scanning_position,
    move_xy_with_z_retract,
    retract_z,
    move_z_axis_to_safety_position,
    toggle_z_retraction,
)


@pytest.fixture
def stage():
    micro = Mock()
    micro.is_busy.return_value = False
    position = [0, 0, 0, 0]
    micro.get_pos.side_effect = lambda: tuple(position)
    for index, axis in enumerate("xyz"):
        def move_to(value, index=index):
            position[index] = value

        getattr(micro, f"move_{axis}_to_usteps").side_effect = move_to
    micro.move_z_usteps.side_effect = lambda value: position.__setitem__(2, position[2] + value)
    return CephlaStage(micro, get_stage_config().model_copy(deep=True))


def test_toggle_preserves_working_z_across_repeated_retraction(stage):
    stage.move_z_to(2.0)
    toggle_z_retraction(stage)
    assert stage.is_z_retracted
    assert stage.working_z_mm == pytest.approx(2.0)
    retract_z(stage)
    assert stage.working_z_mm == pytest.approx(2.0)
    toggle_z_retraction(stage)
    assert not stage.is_z_retracted
    assert stage.get_pos().z_mm == pytest.approx(2.0)


@pytest.mark.parametrize("relative", [False, True])
def test_software_z_move_leaves_retraction_and_updates_working_z(stage, relative):
    stage.move_z_to(2.0)
    retract_z(stage)
    if relative:
        stage.move_z(0.5, blocking=False)
    else:
        stage.move_z_to(0.6, blocking=False)
    assert not stage.is_z_retracted
    assert stage.working_z_mm == pytest.approx(stage.get_pos().z_mm)
    assert stage.working_z_mm != pytest.approx(2.0)


def test_joystick_z_move_leaves_retraction_and_keeps_tracking(stage):
    stage.move_z_to(2.0)
    retract_z(stage)
    for z_mm in (0.4, 0.5):
        axis = stage.get_config().Z_AXIS
        stage._microcontroller.move_z_to_usteps(axis.convert_real_units_to_ustep(axis.canonical_to_raw(z_mm)))
        stage.get_pos()
        assert not stage.is_z_retracted
        assert stage.working_z_mm == pytest.approx(z_mm)


@pytest.mark.parametrize("sign", [1, -1])
def test_startup_keeps_z_retracted_and_remembers_cached_working_z(stage, tmp_path, sign):
    cfg = stage.get_config()
    cfg.Z_AXIS.CANONICAL_SIGN = type(cfg.Z_AXIS.CANONICAL_SIGN)(sign)
    cache = tmp_path / "coords.txt"
    cache.write_text("1,2,3")
    move_to_cached_or_default_startup_position(stage, cfg, str(cache))
    pos = stage.get_pos()
    assert pos.x_mm == pytest.approx(1.0)
    assert pos.y_mm == pytest.approx(2.0)
    assert pos.z_mm == pytest.approx(cfg.Z_AXIS.raw_to_canonical(_def.OBJECTIVE_RETRACTED_POS_MM))
    assert stage.is_z_retracted
    assert stage.working_z_mm == pytest.approx(cfg.Z_AXIS.raw_to_canonical(3.0))
    toggle_z_retraction(stage)
    assert stage.get_pos().z_mm == pytest.approx(cfg.Z_AXIS.raw_to_canonical(3.0))


def test_inter_region_move_uses_saved_working_z_if_already_retracted(stage):
    stage.move_z_to(2.0)
    retract_z(stage)
    move_xy_with_z_retract(stage, 1.0, 2.0, retract=True, z_target_mm=None, home_z_mm=0.1)
    assert not stage.is_z_retracted
    assert stage.get_pos().z_mm == pytest.approx(2.0)
    assert stage.working_z_mm == pytest.approx(2.0)


def test_restart_retracts_without_restoring_xy(stage, monkeypatch):
    stage.move_x_to(4.0)
    stage.move_y_to(5.0)
    stage.move_z_to(2.0)
    saved_pos = stage.get_pos()
    monkeypatch.setattr("squid.stage.utils.get_cached_position", lambda **kwargs: saved_pos)
    initialize_z_retraction(stage)
    assert stage.is_z_retracted
    assert stage.working_z_mm == pytest.approx(2.0)
    pos = stage.get_pos()
    assert pos.x_mm == pytest.approx(4.0)
    assert pos.y_mm == pytest.approx(5.0)


def test_failed_inter_region_move_keeps_z_retracted_and_working_z(stage):
    stage.move_z_to(2.0)

    def fail_xy():
        raise RuntimeError("XY failed")

    with pytest.raises(RuntimeError, match="XY failed"):
        move_xy_with_z_retract(stage, 1.0, 2.0, retract=True, z_target_mm=None, home_z_mm=0.1, xy_move=fail_xy)
    assert stage.is_z_retracted
    assert stage.working_z_mm == pytest.approx(2.0)


def test_loading_moves_preserve_working_z_when_already_retracted(stage):
    stage.move_z_to(2.0)
    retract_z(stage)
    move_to_loading_position(stage)
    assert stage.is_z_retracted
    assert stage.working_z_mm == pytest.approx(2.0)
    # Manual adjustment while loading establishes a new working position.
    stage.move_z_to(0.6)
    move_to_scanning_position(stage)
    assert not stage.is_z_retracted
    assert stage.get_pos().z_mm == pytest.approx(0.6)


def test_navigation_toggle_updates_with_manual_movement(stage):
    from qtpy.QtWidgets import QApplication
    from gui.widgets.hardware_panels import NavigationWidget

    app = QApplication.instance() or QApplication([])
    stage.move_z_to(2.0)
    retract_z(stage)
    widget = NavigationWidget(stage)
    try:
        widget._update_position()
        assert widget.btn_toggle_z_retraction.text() == "Return Z to Working Position"
        widget.btn_toggle_z_retraction.click()
        widget._update_position()
        assert stage.get_pos().z_mm == pytest.approx(2.0)
        assert widget.btn_toggle_z_retraction.text() == "Retract Z"
        widget.btn_toggle_z_retraction.click()
        stage.move_z_to(0.6)
        widget._update_position()
        assert widget.btn_toggle_z_retraction.text() == "Retract Z"
        assert stage.working_z_mm == pytest.approx(0.6)
    finally:
        widget.position_update_timer.stop()
        widget.close()


def test_retraction_failure_does_not_overwrite_working_z(stage):
    stage.move_z_to(2.0)
    stage._microcontroller.move_z_to_usteps.side_effect = RuntimeError("Z failed")
    with pytest.raises(RuntimeError, match="Z failed"):
        retract_z(stage)
    assert not stage._z_retraction_in_progress
    assert not stage.is_z_retracted
    assert stage.working_z_mm == pytest.approx(2.0)


@pytest.mark.parametrize("sign", [1, -1])
def test_startup_at_rounded_retraction_position_issues_no_second_z_move(stage, sign):
    axis = stage.get_config().Z_AXIS
    axis.SCREW_PITCH = 0.3
    axis.MICROSTEPS_PER_STEP = 16
    axis.MOVEMENT_SIGN = type(axis.MOVEMENT_SIGN)(-1)
    axis.CANONICAL_SIGN = type(axis.CANONICAL_SIGN)(sign)
    retract_z(stage)
    target = axis.raw_to_canonical(_def.OBJECTIVE_RETRACTED_POS_MM)
    # This rig's retraction target is not an exact motor step.
    assert stage.get_pos().z_mm != target
    micro = stage._microcontroller
    micro.move_z_to_usteps.reset_mock()
    micro.wait_till_operation_is_completed.reset_mock()
    micro.wait_till_operation_is_completed.side_effect = TimeoutError("A redundant Z command stalled")
    initialize_z_retraction(stage, axis.raw_to_canonical(2.0))
    micro.move_z_to_usteps.assert_not_called()
    micro.wait_till_operation_is_completed.assert_not_called()
    assert stage.is_z_retracted
    assert stage.working_z_mm == pytest.approx(axis.raw_to_canonical(2.0))


def test_retraction_is_one_direct_move_without_upward_backlash_correction(stage):
    stage.move_z_to(2.0)
    micro = stage._microcontroller
    micro.move_z_to_usteps.reset_mock()
    retract_z(stage)
    axis = stage.get_config().Z_AXIS
    micro.move_z_to_usteps.assert_called_once_with(axis.convert_real_units_to_ustep(_def.OBJECTIVE_RETRACTED_POS_MM))
    assert stage.working_z_mm == pytest.approx(2.0)
    micro.move_z_to_usteps.reset_mock()
    retract_z(stage)
    micro.move_z_to_usteps.assert_not_called()


def test_working_z_moves_still_compensate_backlash(stage):
    stage.move_z_to(2.0)
    micro = stage._microcontroller
    micro.move_z_to_usteps.reset_mock()
    stage.move_z_to(1.0)
    assert micro.move_z_to_usteps.call_count == 2
    assert stage.get_pos().z_mm == pytest.approx(1.0)


@pytest.mark.parametrize("home_xy", [False, True])
def test_homing_and_safety_use_the_retraction_z_setting(stage, monkeypatch, home_xy):
    from control.microscope import Microscope

    monkeypatch.setattr(_def, "HOMING_ENABLED_Z", True)
    monkeypatch.setattr(_def, "HOMING_ENABLED_X", home_xy)
    monkeypatch.setattr(_def, "HOMING_ENABLED_Y", home_xy)
    monkeypatch.setattr(_def, "OBJECTIVE_RETRACTED_POS_MM", 0.15)
    monkeypatch.setattr(_def, "Z_HOME_SAFETY_POINT", 900)  # Legacy value must not pick a different target.
    scope = Microscope.__new__(Microscope)
    scope.stage = stage
    scope._log = Mock()
    scope.home_xyz()
    stage._microcontroller.home_z.assert_called_once()
    assert stage.is_z_retracted
    assert stage.get_pos().z_mm == pytest.approx(0.15)
    stage._microcontroller.move_z_to_usteps.reset_mock()
    initialize_z_retraction(stage, 2.0)
    move_z_axis_to_safety_position(stage)
    stage._microcontroller.move_z_to_usteps.assert_not_called()
    assert stage.working_z_mm == pytest.approx(2.0)
