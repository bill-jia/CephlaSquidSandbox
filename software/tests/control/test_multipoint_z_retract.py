"""Z retract to home for the XY moves that travel between regions.

The objective sits ~100 um from the sample while imaging, and an XY move that
leaves a region can be centimetres long over a holder that is not perfectly
flat. With ``retract_z_between_regions`` on (the default), every move that
*enters* a region is bracketed: Z to ``OBJECTIVE_RETRACTED_POS_MM`` (blocking),
then XY, then Z down to the target. Steps across a region's own Nx x Ny tile
grid are untouched, and with the flag off the stage sees exactly the same call
sequence it saw before the feature existed.

No hardware: the worker is built with ``__new__`` (like
``tests/control/core/test_timepoint_folder_layout.py``) and driven against a
stage that only records what it was asked to do.
"""

from unittest.mock import MagicMock

import pytest
import yaml

import control._def
from control.core.multi_point_controller import (
    MultiPointController,
    _save_unified_multipoint_acquisition_yaml,
)
from control.core.multi_point_utils import AcquisitionParameters, ScanPositionInformation
from control.core.multi_point_worker import MultiPointWorker
from squid.abc import Pos


HOME_Z_MM = control._def.OBJECTIVE_RETRACTED_POS_MM


class RecordingStage:
    """Records ``(call, target_mm, blocking)`` for every axis move."""

    def __init__(self, x_mm=0.0, y_mm=0.0, z_mm=2.0):
        self.calls = []
        self._pos = Pos(x_mm=x_mm, y_mm=y_mm, z_mm=z_mm, theta_rad=None)

    def get_pos(self):
        return self._pos

    def move_x_to(self, abs_mm, blocking=True):
        self.calls.append(("move_x_to", abs_mm, blocking))
        self._pos = Pos(x_mm=abs_mm, y_mm=self._pos.y_mm, z_mm=self._pos.z_mm, theta_rad=None)

    def move_y_to(self, abs_mm, blocking=True):
        self.calls.append(("move_y_to", abs_mm, blocking))
        self._pos = Pos(x_mm=self._pos.x_mm, y_mm=abs_mm, z_mm=self._pos.z_mm, theta_rad=None)

    def move_z_to(self, abs_mm, blocking=True):
        self.calls.append(("move_z_to", abs_mm, blocking))
        self._pos = Pos(x_mm=self._pos.x_mm, y_mm=self._pos.y_mm, z_mm=abs_mm, theta_rad=None)


def _worker(stage, *, retract, do_autofocus=False, time_point=0, z_pos_proposal=None):
    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker.stage = stage
    worker._log = MagicMock()
    worker._alignment_widget = None
    worker.do_autofocus = do_autofocus
    worker.do_reflection_af = False
    worker.time_point = time_point
    worker._z_pos_proposal = dict(z_pos_proposal or {})
    worker.retract_z_between_regions = retract
    worker._last_move_region_id = None
    worker._sleep = lambda seconds: None
    return worker


# A single region's 2x1 tile grid, then a second region. Y travel dominates in
# every move here, so the XY pair is always (move_x_to non-blocking, move_y_to
# blocking) -- the "else" branch of move_to_coordinate.
R0_FOV0 = (10.0, 20.0, 3.0)
R0_FOV1 = (10.5, 20.9, 3.0)
R1_FOV0 = (40.0, 60.0, 3.5)


def _xy(coord):
    return [("move_x_to", coord[0], False), ("move_y_to", coord[1], True)]


# ═══════════════════════════════════════════════════════════════════════════════
# (a) Flag off: byte-for-byte today's stage calls
# ═══════════════════════════════════════════════════════════════════════════════


def test_flag_off_keeps_todays_call_sequence():
    stage = RecordingStage()
    worker = _worker(stage, retract=False)

    worker.move_to_coordinate(R0_FOV0, "R0", 0)
    worker.move_to_coordinate(R0_FOV1, "R0", 1)
    worker.move_to_coordinate(R1_FOV0, "R1", 0)

    # Exactly what the pre-feature code did: Z issued non-blocking alongside
    # the XY pair, for every move including the first and the region change.
    assert stage.calls == (
        [("move_z_to", 3.0, False)] + _xy(R0_FOV0)
        + [("move_z_to", 3.0, False)] + _xy(R0_FOV1)
        + [("move_z_to", 3.5, False)] + _xy(R1_FOV0)
    )


def test_flag_off_with_two_element_coordinates_issues_no_z_move():
    stage = RecordingStage()
    worker = _worker(stage, retract=False)

    worker.move_to_coordinate((10.0, 20.0), "R0", 0)

    assert stage.calls == _xy((10.0, 20.0))


def test_flag_off_uses_the_af_cache_z_without_a_retract():
    stage = RecordingStage()
    worker = _worker(stage, retract=False, do_autofocus=True, time_point=1, z_pos_proposal={("R0", 0): 4.25})

    worker.move_to_coordinate(R0_FOV0, "R0", 0)

    assert stage.calls == [("move_z_to", 4.25, False)] + _xy(R0_FOV0)


# ═══════════════════════════════════════════════════════════════════════════════
# (b, c, d) Flag on
# ═══════════════════════════════════════════════════════════════════════════════


def test_first_move_of_the_run_is_bracketed():
    stage = RecordingStage()
    worker = _worker(stage, retract=True)

    worker.move_to_coordinate(R0_FOV0, "R0", 0)

    assert stage.calls == (
        [("move_z_to", HOME_Z_MM, True)] + _xy(R0_FOV0) + [("move_z_to", 3.0, True)]
    )


def test_no_retract_between_fovs_inside_one_region():
    stage = RecordingStage()
    worker = _worker(stage, retract=True)

    worker.move_to_coordinate(R0_FOV0, "R0", 0)
    stage.calls.clear()
    worker.move_to_coordinate(R0_FOV1, "R0", 1)

    # Same as the flag-off path: Z alongside XY, no trip to home.
    assert stage.calls == [("move_z_to", 3.0, False)] + _xy(R0_FOV1)


def test_entering_the_next_region_is_bracketed():
    stage = RecordingStage()
    worker = _worker(stage, retract=True)

    worker.move_to_coordinate(R0_FOV0, "R0", 0)
    worker.move_to_coordinate(R0_FOV1, "R0", 1)
    stage.calls.clear()
    worker.move_to_coordinate(R1_FOV0, "R1", 0)

    assert stage.calls == (
        [("move_z_to", HOME_Z_MM, True)] + _xy(R1_FOV0) + [("move_z_to", 3.5, True)]
    )


def test_a_move_into_a_different_region_is_bracketed_even_when_fov_is_not_zero():
    stage = RecordingStage()
    worker = _worker(stage, retract=True)

    worker.move_to_coordinate(R0_FOV0, "R0", 0)
    stage.calls.clear()
    worker.move_to_coordinate(R1_FOV0, "R1", 2)

    assert stage.calls[0] == ("move_z_to", HOME_Z_MM, True)
    assert stage.calls[-1] == ("move_z_to", 3.5, True)


def test_retract_keeps_the_af_cache_as_the_target_z():
    stage = RecordingStage()
    worker = _worker(stage, retract=True, do_autofocus=True, time_point=1, z_pos_proposal={("R0", 0): 4.25})

    worker.move_to_coordinate(R0_FOV0, "R0", 0)

    # The AF cache still picks the target; the retract only brackets the XY move.
    assert stage.calls == (
        [("move_z_to", HOME_Z_MM, True)] + _xy(R0_FOV0) + [("move_z_to", 4.25, True)]
    )


def test_retract_falls_back_to_the_current_z_when_no_target_is_known():
    # AF on, nothing cached for this FOV: today that leaves Z alone, so the
    # retract has to come back to where it started or the objective stays parked.
    stage = RecordingStage(z_mm=2.75)
    worker = _worker(stage, retract=True, do_autofocus=True, time_point=1, z_pos_proposal={})

    worker.move_to_coordinate((10.0, 20.0), "R0", 0)

    assert stage.calls == (
        [("move_z_to", HOME_Z_MM, True)] + _xy((10.0, 20.0)) + [("move_z_to", 2.75, True)]
    )


def test_x_dominant_travel_keeps_its_axis_order_under_the_retract():
    stage = RecordingStage(x_mm=0.0, y_mm=59.9)
    worker = _worker(stage, retract=True)

    worker.move_to_coordinate(R1_FOV0, "R1", 0)

    assert stage.calls == [
        ("move_z_to", HOME_Z_MM, True),
        ("move_y_to", 60.0, False),
        ("move_x_to", 40.0, True),
        ("move_z_to", 3.5, True),
    ]


def test_zero_xy_travel_with_retract_on_skips_the_home_retract():
    """A single-position time-lapse re-visits the same FOV every timepoint: with
    the old hand-rolled bracket, ``fov == 0`` alone triggered a full Z excursion
    every time even though the stage never left. ``move_xy_with_z_retract``'s
    XY-travel guard (F3) skips the home retract (and the no-op XY calls) and
    reduces this to a single Z move."""
    stage = RecordingStage(x_mm=10.0, y_mm=20.0, z_mm=3.0)
    worker = _worker(stage, retract=True)

    worker.move_to_coordinate(R0_FOV0, "R0", 0)

    assert stage.calls == [("move_z_to", 3.0, True)]
    assert ("move_z_to", HOME_Z_MM, True) not in stage.calls


def test_acquire_current_fov_twice_in_a_row_never_retracts():
    """"Acquire Current FOV" calls move_to_coordinate with fov == 0 every time;
    back-to-back calls to the same coordinate must not retract on the second one
    either."""
    stage = RecordingStage(x_mm=10.0, y_mm=20.0, z_mm=3.0)
    worker = _worker(stage, retract=True)

    worker.move_to_coordinate(R0_FOV0, "R0", 0)
    stage.calls.clear()
    worker.move_to_coordinate(R0_FOV0, "R0", 0)

    assert ("move_z_to", HOME_Z_MM, True) not in stage.calls


# ═══════════════════════════════════════════════════════════════════════════════
# (f) Laser-AF seed scan: brackets region entries only
# ═══════════════════════════════════════════════════════════════════════════════


def _seed_xy(coord):
    return [("move_x_to", coord[0], True), ("move_y_to", coord[1], True)]


def _seed_worker(stage, *, retract):
    from control import utils as control_utils

    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker.stage = stage
    worker._log = MagicMock()
    worker._alignment_widget = None
    worker.retract_z_between_regions = retract
    worker._sleep = lambda seconds: None
    worker._timing = control_utils.TimingManager("test seed scan")
    worker.abort_requested_fn = lambda: False
    worker.laser_auto_focus_controller = MagicMock()
    worker.laser_auto_focus_controller.move_to_target.return_value = True
    worker._resolve_region_laser_af_reference = lambda region_id: None
    worker._fov_z_map = {}
    worker._fov_z_delta_map = {}
    worker._z_pos_proposal = {}
    worker._region_anchor_z_current = {}
    return worker


def test_seed_scan_brackets_region_entries_and_not_intra_region_fovs():
    stage = RecordingStage(x_mm=0.0, y_mm=0.0, z_mm=2.0)
    worker = _seed_worker(stage, retract=True)
    worker.scan_region_fov_coords_mm = {
        "R0": [R0_FOV0[:2], R0_FOV1[:2]],
        "R1": [R1_FOV0[:2]],
    }

    worker._seed_fov_z_map()

    assert stage.calls == (
        # R0 fov 0: region entry -> bracketed. No Z target of its own is known
        # (the seed scan lets laser AF set Z after arriving), so the bracket
        # restores the Z it left from.
        [("move_z_to", HOME_Z_MM, True)] + _seed_xy(R0_FOV0[:2]) + [("move_z_to", 2.0, True)]
        # R0 fov 1: same region -> no bracket.
        + _seed_xy(R0_FOV1[:2])
        # R1 fov 0: new region -> bracketed again.
        + [("move_z_to", HOME_Z_MM, True)] + _seed_xy(R1_FOV0[:2]) + [("move_z_to", 2.0, True)]
    )


# ═══════════════════════════════════════════════════════════════════════════════
# (d) End-of-run return to the start position (lives on the controller)
# ═══════════════════════════════════════════════════════════════════════════════


class _ControllerUnderTest:
    """Borrows just the return-to-start method; MultiPointController is a QObject."""

    _move_back_to_start_position = MultiPointController._move_back_to_start_position

    def __init__(self, stage, retract):
        self.stage = stage
        self.retract_z_between_regions = retract
        self._start_position = Pos(x_mm=1.0, y_mm=2.0, z_mm=3.0, theta_rad=None)
        self._log = MagicMock()


def test_return_to_start_is_bracketed():
    stage = RecordingStage()
    controller = _ControllerUnderTest(stage, retract=True)

    controller._move_back_to_start_position()

    assert stage.calls == [
        ("move_z_to", HOME_Z_MM, True),
        ("move_x_to", 1.0, True),
        ("move_y_to", 2.0, True),
        ("move_z_to", 3.0, True),
    ]
    assert controller._start_position is None


def test_return_to_start_unchanged_with_the_flag_off():
    stage = RecordingStage()
    controller = _ControllerUnderTest(stage, retract=False)

    controller._move_back_to_start_position()

    assert stage.calls == [
        ("move_x_to", 1.0, True),
        ("move_y_to", 2.0, True),
        ("move_z_to", 3.0, True),
    ]


def test_return_to_start_is_a_no_op_without_a_start_position():
    stage = RecordingStage()
    controller = _ControllerUnderTest(stage, retract=True)
    controller._start_position = None

    controller._move_back_to_start_position()

    assert stage.calls == []


def test_return_to_start_with_zero_xy_travel_skips_the_home_retract():
    """The stage is already at the start XY (only Z drifted, e.g. from AF) --
    the same F3 guard applies here as in the worker."""
    stage = RecordingStage(x_mm=1.0, y_mm=2.0, z_mm=5.0)
    controller = _ControllerUnderTest(stage, retract=True)
    controller._start_position = Pos(x_mm=1.0, y_mm=2.0, z_mm=3.0, theta_rad=None)

    controller._move_back_to_start_position()

    assert stage.calls == [("move_z_to", 3.0, True)]


# ═══════════════════════════════════════════════════════════════════════════════
# (e) AcquisitionParameters default and acquisition.yaml round-trip
# ═══════════════════════════════════════════════════════════════════════════════


def _params(**overrides):
    kwargs = dict(
        experiment_ID="exp",
        base_path="/tmp/exp",
        acquisition_start_time=0.0,
        scan_position_information=ScanPositionInformation(
            scan_region_coords_mm=[(10.0, 20.0)],
            scan_region_names=["R0"],
            scan_region_fov_coords_mm={"R0": [R0_FOV0]},
        ),
        NX=1,
        deltaX=0.9,
        NY=1,
        deltaY=0.9,
        NZ=1,
        deltaZ=0.0,
        Nt=1,
        deltat=0.0,
        do_autofocus=False,
        do_reflection_autofocus=False,
        use_piezo=False,
        display_resolution_scaling=1.0,
        z_stacking_config="FROM CENTER",
        z_range=(0.0, 0.0),
        use_fluidics=False,
    )
    kwargs.update(overrides)
    return AcquisitionParameters(**kwargs)


def test_acquisition_parameters_defaults_to_retracting():
    assert _params().retract_z_between_regions is True


@pytest.mark.parametrize("retract", [True, False])
def test_acquisition_yaml_records_the_flag(tmp_path, monkeypatch, retract):
    # The metadata manifest needs a live microscope; the layout block under test
    # does not, so stub the augmentation out and keep the real writer.
    monkeypatch.setattr(
        "control.core.acquisition_metadata_helpers.augment_multipoint_acquisition_yaml_dict",
        lambda base_yaml, **kwargs: {"schema_version": 2, **base_yaml},
    )

    _save_unified_multipoint_acquisition_yaml(
        _params(retract_z_between_regions=retract),
        str(tmp_path),
        widget_type="flexible",
        repo=MagicMock(),
        live_controller=MagicMock(),
        camera=MagicMock(),
        objective_store=MagicMock(),
        recording_start_time=0.0,
        selected_observation_state_names=[],
        use_manual_focus_map=False,
        logger=MagicMock(),
    )

    with open(tmp_path / "acquisition.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["acquisition"]["retract_z_between_regions"] is retract
