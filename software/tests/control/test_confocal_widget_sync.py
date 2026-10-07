"""The spinning-disk confocal panel must show what the light path is doing.

The panel used to seed its iris sliders from nothing at all: they started at the
widget minimum (0) while the unit's irises were physically at 100, and that
displayed 0 was what the next user interaction pushed to the hardware.  Nothing
told the panel when an observation state was applied from elsewhere either, so
the emission wheel, the dichroic, the filter slider and the confocal/widefield
button all drifted away from the state that was actually loaded.

Two levels are covered here:

* the driver (``XLight``) seeding its position caches from the unit at
  construction, over a faked serial port -- the write-skipping in its setters is
  only safe if those caches are truthful;
* the widget (``SpinningDiskConfocalWidget``) seeding from the driver and
  following an applied state, against ``XLight_Simulation``.

No real hardware is ever constructed: no ``XLight``-over-a-real-port, no camera,
no microcontroller.
"""

import sys

import pytest

import control.serial_peripherals as sp
from control.models.observation_state import (
    CameraSettings,
    ConfocalSettings,
    IlluminatorState,
    ObservationState,
)
from tests.control.test_xlight_driver import FakeSerialDevice

pytest.importorskip("qtpy")
from qtpy.QtWidgets import QApplication

from gui.widgets.hardware_panels import SpinningDiskConfocalWidget


# ── Driver: seeding the caches from the unit ──────────────────────────────────


class SeedingSerial(FakeSerialDevice):
    """Fake serial that answers the read-back commands like a real unit.

    ``FakeSerialDevice`` echoes the expected prefix back, which is too short for
    the driver's parsers; this one returns realistic payloads so the seeding path
    can actually be observed.
    """

    # rJ/rV report tenths of a percent, so 1000 == 100%.
    responses = {
        "rB\r": "rB3",
        "rC\r": "rC2",
        "rP\r": "rP1",
        "rJ\r": "rJ1000",
        "rV\r": "rV1000",
        "rD\r": "rD1",
        "rN\r": "rN1",
    }

    def write_and_check(self, command, expected_response, **kwargs):
        type(self).commands.append(command)
        type(self).calls.append(("check", command))
        return type(self).responses.get(command, expected_response)


def _seeded_xlight(monkeypatch, **response_overrides):
    responses = dict(SeedingSerial.responses)
    responses.update(response_overrides)
    fake = type(
        "SeedingSerialForTest",
        (SeedingSerial,),
        {"commands": [], "calls": [], "closed": False, "responses": responses},
    )
    monkeypatch.setattr(sp, "SerialDevice", fake)
    return sp.XLight("SN", sleep_time_for_wheel=0.0), fake


def test_driver_seeds_every_cache_from_hardware_at_construction(monkeypatch):
    xlight, fake = _seeded_xlight(monkeypatch)

    assert xlight.illumination_iris == 100
    assert xlight.emission_iris == 100
    assert xlight.emission_wheel_pos == 3
    assert xlight.dichroic_wheel_pos == 2
    assert xlight.slider_position == 1
    assert xlight.spinning_disk_pos == 1
    assert xlight.disk_motor_state is True
    for command in ("rB\r", "rC\r", "rP\r", "rJ\r", "rV\r", "rD\r", "rN\r"):
        assert command in fake.commands


def test_a_truthful_cache_lets_a_closing_write_through(monkeypatch):
    """The bug: iris cached as 0 on a unit at 100 silently dropped 'close to 0'."""
    xlight, fake = _seeded_xlight(monkeypatch)
    fake.commands.clear()

    xlight.set_illumination_iris(0)

    assert fake.commands == ["J0\r"]
    assert xlight.illumination_iris == 0


def test_seeding_skips_mechanisms_the_unit_does_not_have(monkeypatch):
    """Capability-gated: a unit without irises is never asked about them."""
    xlight, fake = _seeded_xlight(monkeypatch)
    xlight.has_illumination_iris_diaphragm = False
    xlight.has_emission_iris_diaphragm = False
    xlight.has_dichroic_filter_slider = False
    xlight.illumination_iris = None
    xlight.emission_iris = None
    xlight.slider_position = None
    fake.commands.clear()

    xlight.seed_caches_from_hardware()

    assert "rJ\r" not in fake.commands
    assert "rV\r" not in fake.commands
    assert "rP\r" not in fake.commands
    assert "rC\r" in fake.commands  # the mechanisms it does have are still read
    assert xlight.illumination_iris is None


def test_an_unreadable_mechanism_leaves_its_cache_unknown(monkeypatch):
    """A unit that will not answer must not break startup."""
    xlight, _ = _seeded_xlight(monkeypatch, **{"rJ\r": "rJ", "rV\r": "rV"})

    assert xlight.illumination_iris is None
    assert xlight.emission_iris is None
    # ...and the other mechanisms were still seeded.
    assert xlight.dichroic_wheel_pos == 2


def test_first_slider_write_after_startup_is_not_skipped(monkeypatch):
    """Seeded at 1: a request for 1 is free, a request for 2 is a real move."""
    xlight, fake = _seeded_xlight(monkeypatch)
    fake.commands.clear()

    xlight.set_filter_slider(1)
    assert fake.commands == []

    xlight.set_filter_slider(2)
    assert fake.commands == ["P2\r"]


# ── Widget ────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture
def panel(qt_app):
    """A panel over a simulated X-Light whose irises are wide open (100)."""
    xlight = sp.XLight_Simulation()
    widget = SpinningDiskConfocalWidget(xlight)
    yield widget
    widget.deleteLater()


def _state(**hw) -> ObservationState:
    state = ObservationState(
        name="live",
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=0.0),
        illuminator_states=[IlluminatorState(illumination_channel="TestLaser", intensity=50.0, on=False)],
    )
    if hw:
        state.confocal_hardware_settings = ConfocalSettings(**hw)
    return state


class Spy:
    """Counts emissions of a Qt signal."""

    def __init__(self, signal):
        self.calls = []
        signal.connect(self._record)

    def _record(self, *args):
        self.calls.append(args)


def test_irises_are_seeded_from_hardware_at_construction(panel):
    assert panel.xlight.illumination_iris == 100
    assert panel.slider_illumination_iris.value() == 100
    assert panel.spinbox_illumination_iris.value() == 100
    assert panel.slider_emission_iris.value() == 100
    assert panel.spinbox_emission_iris.value() == 100


def test_a_state_without_confocal_settings_keeps_the_hardware_value(panel):
    """A channel that never saved irises must not display (or push) 0."""
    panel.sync_from_observation_state(_state())

    assert panel.slider_illumination_iris.value() == 100
    assert panel.spinbox_illumination_iris.value() == 100
    assert panel.slider_emission_iris.value() == 100


def test_a_state_with_irises_is_displayed(panel):
    panel.sync_from_observation_state(_state(illumination_iris=70.0, emission_iris=40.0))

    assert panel.slider_illumination_iris.value() == 70
    assert panel.spinbox_illumination_iris.value() == 70
    assert panel.slider_emission_iris.value() == 40
    assert panel.spinbox_emission_iris.value() == 40


def test_a_refresh_emits_no_change_signals(panel):
    """A refresh that re-emitted would drive a 2 s iris write per channel switch."""
    spies = [
        Spy(panel.signal_illumination_iris_changed),
        Spy(panel.signal_emission_iris_changed),
        Spy(panel.signal_toggle_confocal_widefield),
        Spy(panel.signal_emission_filter_changed),
        Spy(panel.signal_dichroic_changed),
        Spy(panel.signal_filter_slider_changed),
    ]

    panel.sync_from_observation_state(
        _state(
            illumination_iris=70.0,
            emission_iris=40.0,
            dichroic_position=3,
            filter_slider_position=2,
        )
    )

    assert [spy.calls for spy in spies] == [[], [], [], [], [], []]


def test_a_refresh_moves_no_hardware(panel):
    """Display only: the driver must be exactly where it was before the refresh."""
    before = (
        panel.xlight.illumination_iris,
        panel.xlight.emission_iris,
        panel.xlight.dichroic_wheel_pos,
        panel.xlight.slider_position,
        panel.xlight.spinning_disk_pos,
    )

    panel.sync_from_observation_state(
        _state(illumination_iris=70.0, emission_iris=40.0, dichroic_position=3, filter_slider_position=2)
    )

    assert (
        panel.xlight.illumination_iris,
        panel.xlight.emission_iris,
        panel.xlight.dichroic_wheel_pos,
        panel.xlight.slider_position,
        panel.xlight.spinning_disk_pos,
    ) == before


def test_emission_dropdown_follows_an_applied_state(panel):
    state = _state()
    state.emission_filter_positions = {"default": 5}

    panel.sync_from_observation_state(state)

    assert panel.dropdown_emission_filter.currentData() == 5


def test_confocal_button_follows_an_applied_state(panel):
    state = _state()
    state.confocal_mode = True

    panel.sync_from_observation_state(state)

    assert panel.disk_position_state == 1
    assert panel.btn_toggle_widefield.text() == "Switch to Widefield"

    state.confocal_mode = False
    panel.sync_from_observation_state(state)

    assert panel.disk_position_state == 0
    assert panel.btn_toggle_widefield.text() == "Switch to Confocal"


def test_dichroic_and_slider_follow_an_applied_state(panel):
    panel.sync_from_observation_state(_state(dichroic_position=3, filter_slider_position=2))

    assert panel.dropdown_dichroic.currentText() == "3"
    assert panel.filter_slider.value() == 2


def test_dichroic_and_slider_keep_the_hardware_value_when_unrecorded(panel):
    """An old preset carries neither, so neither mechanism may be repositioned."""
    panel.xlight.set_dichroic(4)
    panel.xlight.set_filter_slider(3)
    panel.sync_from_observation_state(None)
    assert (panel.dropdown_dichroic.currentText(), panel.filter_slider.value()) == ("4", 3)

    panel.sync_from_observation_state(_state(illumination_iris=70.0))

    assert panel.dropdown_dichroic.currentText() == "4"
    assert panel.filter_slider.value() == 3


def test_disk_motor_button_reflects_a_running_motor(qt_app):
    xlight = sp.XLight_Simulation()
    xlight.set_disk_motor_state(True)

    widget = SpinningDiskConfocalWidget(xlight)
    try:
        assert widget.btn_toggle_motor.isChecked() is True
        # ...and the panel did not start (or stop) the motor to find out.
        assert xlight.disk_motor_state is True
    finally:
        widget.deleteLater()


def test_disk_motor_button_is_unchecked_for_a_parked_disk(panel):
    assert panel.btn_toggle_motor.isChecked() is False


def test_user_dichroic_choice_is_emitted_not_written(panel):
    """The controller records the choice on the state, so the panel only asks."""
    spy = Spy(panel.signal_dichroic_changed)
    before = panel.xlight.dichroic_wheel_pos

    panel.dropdown_dichroic.setCurrentText("4")

    assert spy.calls == [(4,)]
    assert panel.xlight.dichroic_wheel_pos == before


def test_panel_seeds_from_a_partly_unknown_driver(qt_app):
    """Caches the driver could not seed fall back to a read, never to 0."""

    class HalfKnownXLight(sp.XLight_Simulation):
        def __init__(self):
            super().__init__()
            self.illumination_iris = None  # startup read failed
            self.iris_reads = 0

        def get_illumination_iris(self):
            self.iris_reads += 1
            return 85

    xlight = HalfKnownXLight()
    widget = SpinningDiskConfocalWidget(xlight)
    try:
        assert xlight.iris_reads == 1
        assert widget.slider_illumination_iris.value() == 85
    finally:
        widget.deleteLater()


def test_an_unreadable_mechanism_is_probed_only_once(qt_app):
    """Every channel switch calls this, acquisition included: no repeated reads."""

    class UnreadableXLight(sp.XLight_Simulation):
        def __init__(self):
            super().__init__()
            self.illumination_iris = None
            self.iris_reads = 0

        def get_illumination_iris(self):
            self.iris_reads += 1
            raise OSError("no answer")

    xlight = UnreadableXLight()
    widget = SpinningDiskConfocalWidget(xlight)
    try:
        for _ in range(5):
            widget.sync_from_observation_state(_state())
        assert xlight.iris_reads == 1
    finally:
        widget.deleteLater()
