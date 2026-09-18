"""Confocal round-trip tests for ObservationStateController.

Covers the X-Light side of an ObservationState's optical path:

* collecting the emission wheel position from the confocal unit when the rig has
  no standalone wheel (and doing so from the driver's cache, not the serial port),
* recording a user-driven wheel move onto the live state,
* and the confocal_mode flag surviving collect -> save -> load -> apply, with the
  apply actually moving the spinning disk.

Everything here runs against fakes; no hardware object is ever constructed.
"""

from pathlib import Path
from typing import Optional

import pytest
import yaml

import control.core.observation_state_controller as osc_mod
from control.core.config.repository import ConfigRepository
from control.core.observation_state_controller import ObservationStateController
from control.core.observation_state_service import (
    collect_emission_filter_positions,
    confocal_emission_filter_position,
)
from control.models.observation_state import (
    CameraSettings,
    ConfocalSettings,
    IlluminatorState,
    ObservationState,
)


# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeXLight:
    """Stand-in for ``serial_peripherals.XLight`` (never the real driver)."""

    def __init__(self, emission_wheel_pos: Optional[int] = 1, raise_on_set: bool = False):
        self.emission_wheel_pos = emission_wheel_pos
        self.raise_on_set = raise_on_set
        self.has_emission_filters_wheel = True
        self.has_illumination_iris_diaphragm = True
        self.has_emission_iris_diaphragm = True
        self.disable_emission_filter_wheel = False
        self.spinning_disk_pos = 0
        self.illumination_iris = 100
        self.emission_iris = 100
        self.get_emission_filter_calls = 0
        self.set_emission_filter_calls = []
        self.disk_position_calls = []

    def get_emission_filter(self):
        self.get_emission_filter_calls += 1
        if self.emission_wheel_pos is None:
            raise RuntimeError("no cached position and no hardware in this fake")
        return self.emission_wheel_pos

    def set_emission_filter(self, position, extraction=False, validate=None):
        self.set_emission_filter_calls.append((position, extraction, validate))
        if self.raise_on_set:
            raise OSError("wheel jammed")
        self.emission_wheel_pos = position
        return position

    def set_disk_position(self, position):
        self.disk_position_calls.append(position)
        self.spinning_disk_pos = position
        return position

    def get_disk_position(self):
        return self.spinning_disk_pos

    def set_illumination_iris(self, value):
        self.illumination_iris = value

    def set_emission_iris(self, value):
        self.emission_iris = value


class FakeStandaloneWheel:
    def __init__(self, positions=None):
        self.positions = dict(positions or {1: 2})
        self.set_calls = []

    def get_filter_wheel_position(self):
        return dict(self.positions)

    def set_filter_wheel_position(self, positions):
        self.set_calls.append(dict(positions))
        self.positions = dict(positions)

    def set_delay_offset_ms(self, ms):
        pass


class FakeGainRange:
    min_gain = 0.0
    max_gain = 0.0


class FakeCamera:
    def __init__(self):
        self.calls = []

    # reads
    def get_exposure_time(self):
        return 12.5

    def get_analog_gain(self):
        return 0.0

    def get_pixel_format(self):
        return "MONO16"

    def get_camera_mode(self):
        return None

    def get_binning(self):
        return (1, 1)

    def get_region_of_interest(self):
        return (0, 0, 512, 512)

    def get_resolution(self):
        return (512, 512)

    def get_gain_range(self):
        return FakeGainRange()

    def get_strobe_time(self):
        return 0.0

    # writes
    def set_exposure_time(self, v):
        self.calls.append(("set_exposure_time", v))

    def set_analog_gain(self, v):
        self.calls.append(("set_analog_gain", v))

    def set_binning(self, x, y):
        self.calls.append(("set_binning", x, y))

    def set_region_of_interest(self, *a):
        self.calls.append(("set_region_of_interest", *a))

    def set_pixel_format(self, pf):
        self.calls.append(("set_pixel_format", pf))

    def set_camera_mode(self, m):
        self.calls.append(("set_camera_mode", m))


class FakeIlluminationController:
    """Minimal IlluminationController: enough for collect/apply, does nothing."""

    channel_names = []

    def snapshot(self):
        return None

    def has_unified_led_matrix(self):
        return False

    def set_channel_state(self, name, on):
        pass

    def set_channel_intensity(self, name, intensity):
        pass

    def apply_observation_illumination(self, states, turn_on):
        pass


class FakeAddons:
    def __init__(self, xlight=None, emission_filter_wheel=None):
        self.xlight = xlight
        self.dragonfly = None
        self.emission_filter_wheel = emission_filter_wheel
        self.nl5 = None
        self.cellx = None


class FakeConfigRepo:
    """Just enough repository for ``collect_observation_state``."""

    current_profile = "p1"

    def __init__(self, saved: Optional[ObservationState] = None):
        self.saved = saved

    def get_observation_state(self):
        return self.saved


class FakeMicroscope:
    def __init__(self, addons, config_repo=None):
        self.addons = addons
        self.illumination_controller = FakeIlluminationController()
        self.config_repo = config_repo if config_repo is not None else FakeConfigRepo()


def _make_controller(
    xlight=None,
    wheel=None,
    config_repo=None,
) -> ObservationStateController:
    microscope = FakeMicroscope(FakeAddons(xlight=xlight, emission_filter_wheel=wheel), config_repo)
    return ObservationStateController(microscope=microscope, camera=FakeCamera())


def _state(name="live", confocal=False, emission=None) -> ObservationState:
    return ObservationState(
        name=name,
        confocal_mode=confocal,
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=0.0),
        illuminator_states=[IlluminatorState(illumination_channel="TestLaser", intensity=50.0, on=False)],
        emission_filter_positions=dict(emission or {}),
    )


@pytest.fixture
def confocal_enabled(monkeypatch):
    """Pretend the build has a spinning-disk confocal (X-Light, not Dragonfly)."""
    monkeypatch.setattr(osc_mod, "ENABLE_SPINNING_DISK_CONFOCAL", True, raising=False)
    monkeypatch.setattr(osc_mod, "USE_DRAGONFLY", False, raising=False)


def _repo_with_profile(tmp_path: Path) -> ConfigRepository:
    base = tmp_path / "sw"
    (base / "machine_configs").mkdir(parents=True)
    (base / "user_profiles" / "p1" / "channel_configs").mkdir(parents=True)
    (base / "user_profiles" / "p1" / "observation_presets").mkdir(parents=True)
    (base / "machine_configs" / "illumination_channel_config.yaml").write_text(
        "version: 1\ncontroller_port_mapping: {}\nchannels: []\n", encoding="utf-8"
    )
    general = ObservationState(
        version=3,
        name="TestLaser",
        display_color="#FF0000",
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[IlluminatorState(illumination_channel="TestLaser", intensity=50.0, on=False)],
        confocal_hardware_settings=ConfocalSettings(illumination_iris=70.0, emission_iris=40.0),
    )
    (base / "user_profiles" / "p1" / "channel_configs" / "general.yaml").write_text(
        yaml.safe_dump(general.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
    )
    repo = ConfigRepository(base_path=base)
    repo.set_profile("p1")
    return repo


# ── Collection ────────────────────────────────────────────────────────────────


def test_service_reads_xlight_cache_without_touching_serial():
    xlight = FakeXLight(emission_wheel_pos=4)
    assert confocal_emission_filter_position(xlight) == 4
    assert collect_emission_filter_positions(None, xlight=xlight) == {"default": 4}
    assert xlight.get_emission_filter_calls == 0


def test_service_signature_is_backward_compatible():
    """Other callers pass only the standalone wheel — behaviour must be unchanged."""
    assert collect_emission_filter_positions(None) == {}
    wheel = FakeStandaloneWheel({1: 3})
    assert collect_emission_filter_positions(wheel) == {"1": 3}


def test_collection_uses_xlight_when_no_standalone_wheel():
    xlight = FakeXLight(emission_wheel_pos=6)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _state()

    state = ctl._collect_live_state_with_emission_filters()

    assert state.emission_filter_positions == {"default": 6}
    assert xlight.get_emission_filter_calls == 0


def test_collection_does_not_hit_serial_on_every_tick():
    """The state cache collects periodically: no serial round-trip per tick."""
    xlight = FakeXLight(emission_wheel_pos=2)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _state()

    for _ in range(10):
        ctl._collect_live_state_with_emission_filters()

    assert xlight.get_emission_filter_calls == 0


def test_collection_queries_hardware_once_when_cache_unset():
    """Bootstrap (driver cache still unset) may query hardware — exactly once."""
    xlight = FakeXLight(emission_wheel_pos=None)
    xlight.emission_wheel_pos = None
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _state()

    # First collection: the cache is empty, so the one hardware read is allowed.
    # The fake raises (no hardware), so the previous positions must be kept, not wiped.
    ctl.current_observation_state.emission_filter_positions["default"] = 8
    state = ctl._collect_live_state_with_emission_filters()
    assert xlight.get_emission_filter_calls == 1
    assert state.emission_filter_positions == {"default": 8}

    # Once the driver has a cached position, collection never calls back out.
    xlight.emission_wheel_pos = 5
    state = ctl._collect_live_state_with_emission_filters()
    assert state.emission_filter_positions == {"default": 5}
    assert xlight.get_emission_filter_calls == 1


def test_collection_prefers_standalone_wheel_over_xlight():
    xlight = FakeXLight(emission_wheel_pos=7)
    wheel = FakeStandaloneWheel({1: 2})
    ctl = _make_controller(xlight=xlight, wheel=wheel)
    ctl.current_observation_state = _state()

    state = ctl._collect_live_state_with_emission_filters()

    assert state.emission_filter_positions == {"1": 2}
    assert xlight.get_emission_filter_calls == 0


# ── Recording setter ──────────────────────────────────────────────────────────


def test_set_emission_filter_position_records_and_applies():
    xlight = FakeXLight(emission_wheel_pos=1)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _state()

    ctl.set_emission_filter_position(3)

    assert ctl.current_observation_state.emission_filter_positions == {"default": 3}
    assert len(xlight.set_emission_filter_calls) == 1
    position, extraction, validate = xlight.set_emission_filter_calls[0]
    assert position == 3
    assert extraction is False
    # validate must be left to the driver's own validate_wheel_pos default.
    assert validate is None


def test_set_emission_filter_position_uses_standalone_wheel_when_no_xlight():
    wheel = FakeStandaloneWheel({1: 1})
    ctl = _make_controller(wheel=wheel)
    ctl.current_observation_state = _state()

    ctl.set_emission_filter_position(4)

    assert ctl.current_observation_state.emission_filter_positions == {"default": 4}
    assert wheel.set_calls == [{1: 4}]


def test_set_emission_filter_position_swallows_hardware_error(caplog):
    xlight = FakeXLight(emission_wheel_pos=1, raise_on_set=True)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _state()

    with caplog.at_level("WARNING"):
        ctl.set_emission_filter_position(2)  # must not raise

    assert ctl.current_observation_state.emission_filter_positions == {"default": 2}
    assert any("emission filter position" in rec.getMessage() for rec in caplog.records)


def test_set_emission_filter_position_without_state_still_applies():
    xlight = FakeXLight(emission_wheel_pos=1)
    ctl = _make_controller(xlight=xlight)

    ctl.set_emission_filter_position(5)

    assert xlight.set_emission_filter_calls[0][0] == 5


def test_collected_state_carries_a_recorded_wheel_move():
    """A GUI wheel move must show up in the next collected/saved state."""
    xlight = FakeXLight(emission_wheel_pos=1)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _state()

    ctl.set_emission_filter_position(6)
    state = ctl._collect_live_state_with_emission_filters()

    assert state.emission_filter_positions == {"default": 6}


# ── Confocal mode ─────────────────────────────────────────────────────────────


def test_apply_confocal_mode_moves_the_disk_on_change(confocal_enabled):
    xlight = FakeXLight()
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _state()

    ctl.apply_confocal_mode(True)
    assert xlight.disk_position_calls == [1]
    assert ctl.is_confocal_mode() is True

    ctl.apply_confocal_mode(False)
    assert xlight.disk_position_calls == [1, 0]
    assert ctl.is_confocal_mode() is False


def test_apply_confocal_mode_is_a_noop_when_unchanged(confocal_enabled):
    """A disk move costs seconds — never re-drive it for the mode we're already in."""
    xlight = FakeXLight()
    ctl = _make_controller(xlight=xlight)

    ctl.apply_confocal_mode(False)
    assert xlight.disk_position_calls == []
    assert ctl.is_confocal_mode() is False


def test_toggle_confocal_widefield_stays_state_only(confocal_enabled):
    """The GUI panel drives the disk itself, so its signal target must not."""
    xlight = FakeXLight()
    ctl = _make_controller(xlight=xlight)

    ctl.toggle_confocal_widefield(True)

    assert ctl.is_confocal_mode() is True
    assert xlight.disk_position_calls == []


def test_apply_confocal_mode_survives_a_disk_error(confocal_enabled, caplog):
    class BadXLight(FakeXLight):
        def set_disk_position(self, position):
            raise OSError("disk stuck")

    ctl = _make_controller(xlight=BadXLight())
    with caplog.at_level("WARNING"):
        ctl.apply_confocal_mode(True)
    assert any("spinning disk" in rec.getMessage() for rec in caplog.records)


def test_confocal_mode_round_trips_collect_save_load_apply(tmp_path, confocal_enabled):
    repo = _repo_with_profile(tmp_path)
    xlight = FakeXLight(emission_wheel_pos=1)
    ctl = _make_controller(xlight=xlight, config_repo=repo)
    ctl.current_observation_state = _state()

    # 1. User goes confocal and picks a filter.
    ctl.toggle_confocal_widefield(True)
    ctl.set_emission_filter_position(5)

    # 2. Collect hardware-true state and save it as a preset.
    collected = ctl._collect_live_state_with_emission_filters()
    assert collected.confocal_mode is True
    assert collected.emission_filter_positions == {"default": 5}
    assert collected.confocal_hardware_settings is not None
    repo.save_observation_preset("confocal_preset", collected)

    # 3. Load it back: every confocal field must survive the v3 format.
    loaded = repo.load_observation_preset("confocal_preset")
    assert loaded is not None
    assert loaded.confocal_mode is True
    assert loaded.emission_filter_positions == {"default": 5}
    assert loaded.confocal_hardware_settings is not None
    assert loaded.confocal_hardware_settings.illumination_iris == 70.0
    assert loaded.confocal_hardware_settings.emission_iris == 40.0

    # 4. Hardware has since been put back in widefield; applying the preset must
    #    move the disk back, not just flip the software flag.
    ctl.toggle_confocal_widefield(False)
    xlight.disk_position_calls.clear()
    xlight.set_emission_filter_calls.clear()

    ctl.apply_observation_state_preset(loaded)

    assert ctl.is_confocal_mode() is True
    assert xlight.disk_position_calls == [1]
    assert xlight.set_emission_filter_calls[0][0] == 5
    assert xlight.illumination_iris == 70
    assert xlight.emission_iris == 40


def test_widefield_preset_moves_the_disk_back(tmp_path, confocal_enabled):
    repo = _repo_with_profile(tmp_path)
    xlight = FakeXLight(emission_wheel_pos=1)
    ctl = _make_controller(xlight=xlight, config_repo=repo)
    ctl.current_observation_state = _state()
    ctl.toggle_confocal_widefield(True)

    widefield = _state(name="wf", confocal=False, emission={"default": 2})
    ctl.apply_observation_state_preset(widefield)

    assert ctl.is_confocal_mode() is False
    assert xlight.disk_position_calls == [0]
