"""Tests for the magnification-scaled laser AF calibration sweep.

The AF laser goes out through the objective, reflects off the sample interface
and returns through it, so the spot's lateral travel per µm of defocus scales
with the objective: a 10x objective moves the spot roughly half as far per µm
as a 20x one. A fixed 6 µm sweep tuned at 20x therefore produced too little
signal at 10x and calibration failed. The sweep policy now lives in the machine
config (``devices.laser_af.config.calibration``) and is rescaled per objective.

Everything here is a pure fake — no camera, no stage, no microcontroller.
"""

from unittest.mock import MagicMock

import numpy as np
import pytest
import yaml

from control._def import SpotDetectionMode
from control.core.laser_auto_focus_controller import LaserAutofocusController
from control.models.machine_config import (
    DeviceEntry,
    LaserAFCalibrationSettings,
    LaserAFDeviceSettings,
    MachineConfig,
)


FULL_BLOCK = """
spot_detection_mode: dual_left
calibration:
  distance_um: 8.0
  reference_magnification: 40
  max_distance_um: 25.0
  positions: 7
  min_total_px: 3.0
  min_r2: 0.8
  simulation_px: 0.25
"""


class FakeObjectiveStore:
    """Stand-in for ObjectiveStore: just the two things the controller reads."""

    def __init__(self, current_objective, objectives):
        self.current_objective = current_objective
        self.objectives_dict = objectives

    def get_current_objective_info(self):
        # Matches ObjectiveStore: a KeyError for an objective not in the CSV.
        return self.objectives_dict[self.current_objective]


OBJECTIVES = {
    "4x": {"magnification": 4, "NA": 0.13, "tube_lens_f_mm": 180},
    "10x": {"magnification": 10, "NA": 0.4, "tube_lens_f_mm": 180},
    "20x": {"magnification": 20, "NA": 0.75, "tube_lens_f_mm": 200},
    "40x": {"magnification": 40, "NA": 0.95, "tube_lens_f_mm": 180},
    "broken": {"NA": 0.4, "tube_lens_f_mm": 180},
    "zero": {"magnification": 0, "NA": 0.4, "tube_lens_f_mm": 180},
    "none": {"magnification": None, "NA": 0.4, "tube_lens_f_mm": 180},
}


def make_controller(objective_store=None, machine_settings=None):
    """A LaserAutofocusController with every hardware collaborator mocked.

    ``machine_settings``: what ``ConfigRepository.get_laser_af_settings()``
    returns. ``None`` leaves the MagicMock's own return value in place, which is
    how a stub/absent repository looks to the controller.
    """
    live_controller = MagicMock()
    live_controller.microscope.config_repo.current_profile = None  # skip cache load/save
    if machine_settings is not None:
        live_controller.microscope.config_repo.get_laser_af_settings.return_value = machine_settings
    return LaserAutofocusController(
        microcontroller=MagicMock(),
        camera=MagicMock(),
        liveController=live_controller,
        stage=MagicMock(),
        piezo=None,
        objectiveStore=objective_store,
    )


# ── The settings model ───────────────────────────────────────────────────────


def test_settings_parse_a_full_block():
    settings = LaserAFDeviceSettings.model_validate(yaml.safe_load(FULL_BLOCK))
    assert settings.spot_detection_mode == SpotDetectionMode.DUAL_LEFT
    cal = settings.calibration
    assert (cal.distance_um, cal.reference_magnification, cal.max_distance_um) == (8.0, 40.0, 25.0)
    assert (cal.positions, cal.min_total_px, cal.min_r2, cal.simulation_px) == (7, 3.0, 0.8, 0.25)


@pytest.mark.parametrize("config", [None, {}, {"spot_detection_mode": "dual_left"}])
def test_settings_default_when_block_is_missing_or_partial(config):
    entry = None if config is None else DeviceEntry(driver="laser_af", config=config)
    settings = LaserAFDeviceSettings.from_device_entry(entry)
    cal = settings.calibration
    # Today's hardcoded values are the model defaults: no behaviour change for
    # a config that says nothing about calibration.
    assert (cal.distance_um, cal.reference_magnification) == (6.0, 20.0)
    assert (cal.positions, cal.min_total_px, cal.min_r2, cal.simulation_px) == (5, 5.0, 0.90, 0.5)


def test_settings_reject_unknown_keys():
    with pytest.raises(Exception):
        LaserAFDeviceSettings.model_validate({"calibration": {"distanceum": 6.0}})


def test_machine_config_accessor_reads_the_device_entry():
    mc = MachineConfig.model_validate(
        {"devices": {"laser_af": {"driver": "laser_af", "config": yaml.safe_load(FULL_BLOCK)}}}
    )
    assert mc.get_laser_af_settings().calibration.distance_um == 8.0
    # No laser_af device at all still yields usable defaults.
    assert MachineConfig().get_laser_af_settings().calibration.distance_um == 6.0


# ── The scaling formula ──────────────────────────────────────────────────────


def test_sweep_at_reference_magnification_is_the_configured_distance():
    cal = LaserAFCalibrationSettings()
    assert cal.effective_distance_um(20) == pytest.approx(6.0)


def test_sweep_doubles_at_half_magnification_and_halves_at_double():
    cal = LaserAFCalibrationSettings()
    assert cal.effective_distance_um(10) == pytest.approx(12.0)
    assert cal.effective_distance_um(40) == pytest.approx(3.0)


def test_sweep_scales_with_a_non_default_reference():
    cal = LaserAFCalibrationSettings(distance_um=8.0, reference_magnification=40)
    assert cal.effective_distance_um(40) == pytest.approx(8.0)
    assert cal.effective_distance_um(20) == pytest.approx(16.0)
    # 8 * 40 / 10 = 32 µm, held at the default 30 µm clamp.
    assert cal.effective_distance_um(10) == pytest.approx(30.0)


def test_sweep_is_clamped_at_max_distance():
    cal = LaserAFCalibrationSettings()
    # 4x lands exactly on the clamp; anything lower is held there.
    assert cal.effective_distance_um(4) == pytest.approx(30.0)
    assert cal.effective_distance_um(2) == pytest.approx(30.0)
    assert cal.effective_distance_um(1) == pytest.approx(30.0)
    assert LaserAFCalibrationSettings(max_distance_um=15.0).effective_distance_um(4) == pytest.approx(15.0)


@pytest.mark.parametrize("magnification", [None, 0, -5, float("nan"), float("inf"), "10x"])
def test_sweep_falls_back_to_the_unscaled_distance_for_a_bad_magnification(magnification):
    cal = LaserAFCalibrationSettings()
    assert cal.effective_distance_um(magnification) == pytest.approx(6.0)


# ── Controller wiring ────────────────────────────────────────────────────────


def test_controller_reads_settings_from_the_machine_config():
    settings = LaserAFDeviceSettings.model_validate(yaml.safe_load(FULL_BLOCK))
    controller = make_controller(FakeObjectiveStore("20x", OBJECTIVES), settings)
    assert controller.calibration_settings.distance_um == 8.0
    # 8 µm tuned at 40x -> 16 µm at 20x.
    assert controller.calibration_sweep_um() == pytest.approx(16.0)


def test_controller_falls_back_to_defaults_when_the_repository_is_unusable():
    # No machine settings configured: the MagicMock returns a MagicMock, which
    # must not be mistaken for real settings.
    controller = make_controller(FakeObjectiveStore("10x", OBJECTIVES))
    assert isinstance(controller.calibration_settings, LaserAFCalibrationSettings)
    assert controller.calibration_sweep_um() == pytest.approx(12.0)


def test_controller_sweep_per_objective():
    settings = LaserAFDeviceSettings()
    store = FakeObjectiveStore("20x", OBJECTIVES)
    controller = make_controller(store, settings)
    assert controller.calibration_sweep_um() == pytest.approx(6.0)  # unchanged at 20x
    store.current_objective = "10x"
    assert controller.calibration_sweep_um() == pytest.approx(12.0)  # the fix
    store.current_objective = "40x"
    assert controller.calibration_sweep_um() == pytest.approx(3.0)
    store.current_objective = "4x"
    assert controller.calibration_sweep_um() == pytest.approx(30.0)  # clamped


def test_controller_sweep_without_an_objective_store():
    controller = make_controller(None, LaserAFDeviceSettings())
    assert controller.current_magnification() is None
    assert controller.calibration_sweep_um() == pytest.approx(6.0)


@pytest.mark.parametrize("objective", ["unlisted-objective", "broken", "zero", "none"])
def test_controller_sweep_with_an_unusable_objective_entry(objective):
    controller = make_controller(FakeObjectiveStore(objective, OBJECTIVES), LaserAFDeviceSettings())
    assert controller.calibration_sweep_um() == pytest.approx(6.0)


def test_spot_detection_mode_defaults_from_the_machine_config():
    settings = LaserAFDeviceSettings(spot_detection_mode=SpotDetectionMode.DUAL_LEFT)
    controller = make_controller(None, settings)
    assert controller.laser_af_properties.spot_detection_mode == SpotDetectionMode.DUAL_LEFT
    # And the no-machine-config path keeps the model default.
    assert make_controller(None).laser_af_properties.spot_detection_mode == SpotDetectionMode.DUAL_RIGHT


def test_failure_message_names_the_objective_sweep_and_config_key():
    controller = make_controller(FakeObjectiveStore("10x", OBJECTIVES), LaserAFDeviceSettings())
    message = controller._calibration_signal_failure_message(
        total_px=3.3, span_um=12.0, magnification=10.0, objective_name="10x"
    )
    assert "10x" in message
    assert "3.3 px" in message
    assert "12.0 µm" in message
    assert "5.0 px" in message
    assert "devices.laser_af.config.calibration" in message
    # The old advice ("Increase the calibration distance") is not the
    # operator's lever any more.
    assert "Increase the calibration distance" not in message


# ── Acceptance gates come from config ────────────────────────────────────────


def fit_decision(settings: LaserAFCalibrationSettings, offsets_um, xs):
    """The accept/reject logic of ``_calibrate_pixel_to_um``, isolated.

    Mirrors the controller's arithmetic so the gates can be exercised without a
    camera or a stage; ``test_gate_logic_matches_the_controller`` pins the
    thresholds it reads to the ones the controller reads.
    """
    slope, intercept = np.polyfit(offsets_um, xs, 1)
    predicted = np.polyval((slope, intercept), offsets_um)
    ss_res = float(np.sum((np.asarray(xs) - predicted) ** 2))
    ss_tot = float(np.sum((np.asarray(xs) - np.mean(xs)) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    total_px = abs(slope) * (offsets_um[-1] - offsets_um[0])
    if total_px < settings.simulation_px:
        return "simulation"
    if total_px < settings.min_total_px:
        return "too-little-signal"
    if r_squared < settings.min_r2:
        return "not-linear"
    return "accepted"


def sweep_samples(settings: LaserAFCalibrationSettings, magnification, px_per_um, noise=None):
    span = settings.effective_distance_um(magnification)
    offsets = np.linspace(-span / 2, span / 2, settings.positions)
    xs = 768.0 + offsets * px_per_um
    if noise is not None:
        xs = xs + np.asarray(noise)
    return offsets, xs


def test_the_10x_failure_is_reproduced_at_the_old_fixed_sweep():
    # 0.55 px/µm measured at 10x: 3.3 px over 6 µm, below the 5.0 px floor.
    settings = LaserAFCalibrationSettings()
    offsets, xs = sweep_samples(settings, magnification=20, px_per_um=0.55)  # 20x -> 6 µm sweep
    assert offsets[-1] - offsets[0] == pytest.approx(6.0)
    assert fit_decision(settings, offsets, xs) == "too-little-signal"


def test_the_scaled_sweep_clears_the_floor_at_10x():
    settings = LaserAFCalibrationSettings()
    offsets, xs = sweep_samples(settings, magnification=10, px_per_um=0.55)
    assert offsets[-1] - offsets[0] == pytest.approx(12.0)  # 6.6 px of travel
    assert fit_decision(settings, offsets, xs) == "accepted"


def test_a_lower_min_total_px_accepts_a_sweep_the_default_rejects():
    offsets, xs = sweep_samples(LaserAFCalibrationSettings(), magnification=20, px_per_um=0.55)
    assert fit_decision(LaserAFCalibrationSettings(), offsets, xs) == "too-little-signal"
    assert fit_decision(LaserAFCalibrationSettings(min_total_px=2.0), offsets, xs) == "accepted"


def test_simulation_escape_hatch_is_config_driven():
    settings = LaserAFCalibrationSettings()
    offsets, xs = sweep_samples(settings, magnification=20, px_per_um=0.0, noise=[0, 0, 0, 0, 1e-9])
    assert fit_decision(settings, offsets, xs) == "simulation"
    # Raise the threshold and a genuinely small-but-real motion is now read as
    # a static image instead of a failure.
    offsets, xs = sweep_samples(settings, magnification=20, px_per_um=0.1)  # 0.6 px
    assert fit_decision(settings, offsets, xs) == "too-little-signal"
    assert fit_decision(LaserAFCalibrationSettings(simulation_px=1.0), offsets, xs) == "simulation"


def test_min_r2_gate_is_config_driven():
    settings = LaserAFCalibrationSettings()
    # A sweep with plenty of travel but a badly non-linear response.
    offsets, xs = sweep_samples(settings, magnification=10, px_per_um=1.0, noise=[0, 4, -4, 4, -4])
    assert fit_decision(settings, offsets, xs) == "not-linear"
    assert fit_decision(LaserAFCalibrationSettings(min_r2=0.0), offsets, xs) == "accepted"


def test_gate_logic_matches_the_controller():
    """The isolated gate helper must read the same thresholds the controller does."""
    settings = LaserAFDeviceSettings.model_validate(yaml.safe_load(FULL_BLOCK)).calibration
    controller = make_controller(FakeObjectiveStore("40x", OBJECTIVES), LaserAFDeviceSettings(calibration=settings))
    assert controller.calibration_settings.min_total_px == settings.min_total_px
    assert controller.calibration_settings.min_r2 == settings.min_r2
    assert controller.calibration_settings.simulation_px == settings.simulation_px
    assert controller.calibration_settings.positions == settings.positions
    assert controller.calibration_sweep_um() == pytest.approx(settings.distance_um)


def test_controller_does_not_touch_hardware_on_construction():
    controller = make_controller(FakeObjectiveStore("10x", OBJECTIVES), LaserAFDeviceSettings())
    assert isinstance(controller.laser_af_properties.spot_detection_mode, SpotDetectionMode)
    assert not isinstance(controller.calibration_settings, MagicMock)
    # No frames were requested and no z move was commanded just by building it.
    controller.camera.send_trigger.assert_not_called()
    controller.stage.move_z.assert_not_called()
