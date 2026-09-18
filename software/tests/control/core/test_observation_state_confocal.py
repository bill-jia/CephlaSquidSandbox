"""Confocal round-trip tests for ObservationStateController.

Covers the X-Light side of an ObservationState's optical path:

* collecting the emission wheel position from the confocal unit when the rig has
  no standalone wheel (and doing so from the driver's cache, not the serial port),
* recording a user-driven wheel move onto the live state,
* the confocal_mode flag surviving collect -> save -> load -> apply, with the
  apply actually moving the spinning disk,
* and the driver skipping optical-path writes the hardware is already making.

Everything here runs against fakes: either a stand-in X-Light, or the real
driver over a faked serial port. No COM port is opened and no hardware object is
ever constructed.
"""

from pathlib import Path
from typing import Optional

import pytest
import yaml

import control.serial_peripherals as sp
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
from tests.control.test_xlight_driver import make_xlight


# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeXLight:
    """Stand-in for ``serial_peripherals.XLight`` (never the real driver)."""

    def __init__(self, emission_wheel_pos: Optional[int] = 1, raise_on_set: bool = False):
        self.emission_wheel_pos = emission_wheel_pos
        self.raise_on_set = raise_on_set
        self.has_emission_filters_wheel = True
        self.has_illumination_iris_diaphragm = True
        self.has_emission_iris_diaphragm = True
        self.has_dichroic_filters_wheel = True
        self.has_dichroic_filter_slider = True
        self.has_spinning_disk_motor = True
        self.has_spinning_disk_slider = True
        self.disable_emission_filter_wheel = False
        self.spinning_disk_pos = 0
        self.illumination_iris = 100
        self.emission_iris = 100
        # None == "unknown", exactly like the real driver before it has driven or
        # read back a mechanism, so the first write is never skipped.
        self.dichroic_wheel_pos = None
        self.slider_position = None
        self.disk_motor_state = False
        self.get_emission_filter_calls = 0
        self.set_emission_filter_calls = []
        self.disk_position_calls = []
        self.set_dichroic_calls = []
        self.set_filter_slider_calls = []

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

    # The dedup lives in the real driver, so the fake mirrors it: a request for
    # the position the mechanism is already in records nothing.
    def set_dichroic(self, position, extraction=False):
        if not extraction and self.dichroic_wheel_pos == int(position):
            return self.dichroic_wheel_pos
        self.set_dichroic_calls.append(int(position))
        self.dichroic_wheel_pos = int(position)
        return self.dichroic_wheel_pos

    def get_dichroic(self):
        return self.dichroic_wheel_pos

    def set_filter_slider(self, position):
        if self.slider_position == int(position):
            return self.slider_position
        self.set_filter_slider_calls.append(int(position))
        self.slider_position = int(position)
        return self.slider_position

    def get_filter_slider(self):
        return self.slider_position


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
        self.channel_settings = []

    def get_observation_state(self):
        return self.saved

    def update_channel_setting(self, setting, value, profile=None):
        self.channel_settings.append((setting, value))
        return True


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
def confocal_enabled():
    """A build with a spinning-disk confocal is signalled by the addon existing.

    This deliberately patches no ``_def`` flag. It used to set
    ``ENABLE_SPINNING_DISK_CONFOCAL`` True on the controller module, which made
    these tests pass while production was broken: the real value is a stale
    ``False`` copied by ``from control._def import *`` before the machine config
    is applied, so every confocal branch was skipped on a live rig. The code now
    branches on ``addons.xlight`` / ``addons.dragonfly`` instead, and the fakes
    supply those.
    """
    return None


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


# ── Redundant optical-path writes ─────────────────────────────────────────────
#
# These run the *real* X-Light driver over a fake serial port (no COM port is
# opened) because the skip lives in the driver, not in the controller: the
# confocal panel writes through the same setters, so the cache cannot go stale.


def _optical_state(slot, illumination_iris=None, emission_iris=None) -> ObservationState:
    state = _state(emission={"default": slot})
    state.confocal_hardware_settings = ConfocalSettings(
        illumination_iris=illumination_iris, emission_iris=emission_iris
    )
    return state


def test_repeated_apply_writes_the_optical_path_once(monkeypatch):
    """Every channel switch applies the optical path; only changes reach serial."""
    xlight, fake = make_xlight(monkeypatch)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _optical_state(3, illumination_iris=80.0, emission_iris=60.0)

    ctl.apply_optical_path()
    ctl.apply_optical_path()
    ctl.apply_optical_path()

    assert fake.commands == ["B3\r", "J800\r", "V600\r"]


def test_apply_writes_again_when_the_state_changes(monkeypatch):
    xlight, fake = make_xlight(monkeypatch)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _optical_state(3, illumination_iris=80.0, emission_iris=60.0)
    ctl.apply_optical_path()
    fake.commands.clear()

    ctl.current_observation_state = _optical_state(4, illumination_iris=80.0, emission_iris=20.0)
    ctl.apply_optical_path()

    assert fake.commands == ["B4\r", "V200\r"]


def test_failed_wheel_write_is_retried_on_the_next_apply(monkeypatch, caplog):
    """A jammed wheel must not be remembered as 'already there'."""
    xlight, fake = make_xlight(monkeypatch)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _optical_state(3)

    fail = {"on": True}
    record = xlight.serial_connection.write

    def maybe_boom(command):
        if fail["on"]:
            raise sp.SerialDeviceError("no response")
        record(command)

    monkeypatch.setattr(xlight.serial_connection, "write", maybe_boom)
    with caplog.at_level("WARNING"):
        ctl.apply_optical_path()  # swallowed, logged
    assert any("emission filter position" in rec.getMessage() for rec in caplog.records)
    assert fake.commands == []

    fail["on"] = False
    ctl.apply_optical_path()
    assert fake.commands == ["B3\r"]
    assert xlight.emission_wheel_pos == 3


def test_apply_overrides_a_panel_driven_wheel_move(monkeypatch):
    """The confocal panel drives the driver too, so the state still wins."""
    xlight, fake = make_xlight(monkeypatch)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _optical_state(3, illumination_iris=80.0)
    ctl.apply_optical_path()
    fake.commands.clear()

    # The user picks another filter and closes the iris in the spinning-disk panel.
    ctl.set_emission_filter_position(5)
    xlight.set_illumination_iris(30)
    assert fake.commands == ["B5\r", "J300\r"]

    # Re-applying the state puts the hardware back where the state says.
    ctl.current_observation_state = _optical_state(3, illumination_iris=80.0)
    ctl.apply_optical_path()

    assert fake.commands == ["B5\r", "J300\r", "B3\r", "J800\r"]
    assert xlight.emission_wheel_pos == 3
    assert xlight.illumination_iris == 80


# ── Dichroic wheel & filter slider ────────────────────────────────────────────
#
# Both are part of the light path, so an observation state carries them. Both
# are slow (the slider is 5 s), so a state that does not change them must cost
# nothing, and a state that never recorded them must not move them at all.


def _confocal_state(**hw) -> ObservationState:
    state = _state()
    state.confocal_hardware_settings = ConfocalSettings(**hw)
    return state


def test_apply_optical_path_drives_dichroic_and_slider_once():
    xlight = FakeXLight(emission_wheel_pos=1)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _confocal_state(dichroic_position=3, filter_slider_position=2)

    ctl.apply_optical_path()

    assert xlight.set_dichroic_calls == [3]
    assert xlight.set_filter_slider_calls == [2]
    assert xlight.dichroic_wheel_pos == 3
    assert xlight.slider_position == 2


def test_reapplying_the_same_state_issues_no_further_writes():
    """The parsimony requirement: an unchanged slider is 5 s of dead time."""
    xlight = FakeXLight(emission_wheel_pos=1)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _confocal_state(dichroic_position=3, filter_slider_position=2)

    ctl.apply_optical_path()
    xlight.set_dichroic_calls.clear()
    xlight.set_filter_slider_calls.clear()

    ctl.apply_optical_path()
    ctl.apply_optical_path()

    assert xlight.set_dichroic_calls == []
    assert xlight.set_filter_slider_calls == []


def test_state_without_dichroic_or_slider_touches_neither():
    """Presets saved before these fields existed must leave the optics alone."""
    xlight = FakeXLight(emission_wheel_pos=1)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _confocal_state(illumination_iris=70.0, emission_iris=40.0)

    ctl.apply_optical_path()

    assert xlight.set_dichroic_calls == []
    assert xlight.set_filter_slider_calls == []
    assert xlight.dichroic_wheel_pos is None
    assert xlight.slider_position is None


def test_dichroic_and_slider_are_skipped_when_the_unit_lacks_them():
    xlight = FakeXLight(emission_wheel_pos=1)
    xlight.has_dichroic_filters_wheel = False
    xlight.has_dichroic_filter_slider = False
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _confocal_state(dichroic_position=3, filter_slider_position=2)

    ctl.apply_optical_path()

    assert xlight.set_dichroic_calls == []
    assert xlight.set_filter_slider_calls == []


def test_set_dichroic_position_records_and_applies():
    xlight = FakeXLight(emission_wheel_pos=1)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _state()

    ctl.set_dichroic_position(4)

    assert ctl.current_observation_state.confocal_hardware_settings.dichroic_position == 4
    assert xlight.set_dichroic_calls == [4]


def test_set_filter_slider_position_records_and_applies():
    xlight = FakeXLight(emission_wheel_pos=1)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _state()

    ctl.set_filter_slider_position(1)

    assert ctl.current_observation_state.confocal_hardware_settings.filter_slider_position == 1
    assert xlight.set_filter_slider_calls == [1]


def test_recording_setters_swallow_hardware_errors(caplog):
    class BadXLight(FakeXLight):
        def set_dichroic(self, position, extraction=False):
            raise OSError("wheel jammed")

        def set_filter_slider(self, position):
            raise OSError("slider jammed")

    ctl = _make_controller(xlight=BadXLight(emission_wheel_pos=1))
    ctl.current_observation_state = _state()

    with caplog.at_level("WARNING"):
        ctl.set_dichroic_position(2)
        ctl.set_filter_slider_position(3)

    # The user's choice is still recorded, so a retry / preset save keeps it.
    hw = ctl.current_observation_state.confocal_hardware_settings
    assert (hw.dichroic_position, hw.filter_slider_position) == (2, 3)
    assert any("dichroic position" in rec.getMessage() for rec in caplog.records)
    assert any("filter slider position" in rec.getMessage() for rec in caplog.records)


def test_persist_iris_config_records_on_the_live_state():
    """The panel's iris writes have to reach the state a preset is saved from."""
    ctl = _make_controller(xlight=FakeXLight(emission_wheel_pos=1))
    ctl.current_observation_state = _state()

    ctl.persist_iris_config("IlluminationIris", 65.0)
    ctl.persist_iris_config("EmissionIris", 35.0)

    hw = ctl.current_observation_state.confocal_hardware_settings
    assert (hw.illumination_iris, hw.emission_iris) == (65.0, 35.0)


def test_dichroic_and_slider_round_trip_through_a_preset(tmp_path):
    repo = _repo_with_profile(tmp_path)
    xlight = FakeXLight(emission_wheel_pos=1)
    ctl = _make_controller(xlight=xlight, config_repo=repo)
    ctl.current_observation_state = _state()

    ctl.set_dichroic_position(5)
    ctl.set_filter_slider_position(3)
    collected = ctl.current_observation_state
    repo.save_observation_preset("optics_preset", collected)

    loaded = repo.load_observation_preset("optics_preset")
    assert loaded.confocal_hardware_settings.dichroic_position == 5
    assert loaded.confocal_hardware_settings.filter_slider_position == 3

    # Hardware moved elsewhere in the meantime; applying the preset brings it back.
    xlight.dichroic_wheel_pos = 1
    xlight.slider_position = 0
    xlight.set_dichroic_calls.clear()
    xlight.set_filter_slider_calls.clear()
    ctl.apply_observation_state_preset(loaded)
    assert xlight.set_dichroic_calls == [5]
    assert xlight.set_filter_slider_calls == [3]


def test_older_presets_without_the_new_keys_still_load(tmp_path):
    """ConfocalSettings forbids extras, but the new keys are optional."""
    repo = _repo_with_profile(tmp_path)
    old = _state()
    old.confocal_hardware_settings = ConfocalSettings(illumination_iris=70.0, emission_iris=40.0)
    payload = old.model_dump(mode="json", exclude_none=True)
    assert "dichroic_position" not in payload["confocal_hardware_settings"]
    assert "filter_slider_position" not in payload["confocal_hardware_settings"]

    path = tmp_path / "sw" / "user_profiles" / "p1" / "observation_presets" / "legacy.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    loaded = repo.load_observation_preset("legacy")
    assert loaded is not None
    assert loaded.confocal_hardware_settings.illumination_iris == 70.0
    assert loaded.confocal_hardware_settings.dichroic_position is None
    assert loaded.confocal_hardware_settings.filter_slider_position is None


def test_real_driver_writes_dichroic_and_slider_once_across_channel_switches(monkeypatch):
    """The 5 s slider and the dichroic reach serial once, not once per switch."""
    xlight, fake = make_xlight(monkeypatch)
    state = _optical_state(3, illumination_iris=80.0, emission_iris=60.0)
    state.confocal_hardware_settings.dichroic_position = 2
    state.confocal_hardware_settings.filter_slider_position = 1
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = state

    ctl.apply_optical_path()
    ctl.apply_optical_path()
    ctl.apply_optical_path()

    assert fake.commands == ["B3\r", "J800\r", "V600\r", "C2\r", "P1\r"]


def test_real_driver_leaves_dichroic_and_slider_alone_when_unrecorded(monkeypatch):
    xlight, fake = make_xlight(monkeypatch)
    ctl = _make_controller(xlight=xlight)
    ctl.current_observation_state = _optical_state(3, illumination_iris=80.0, emission_iris=60.0)

    ctl.apply_optical_path()

    assert "C2\r" not in fake.commands
    assert not [c for c in fake.commands if c.startswith("C") or c.startswith("P")]
