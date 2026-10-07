"""IlluminationController.shut_down() must reach every composed device.

Microscope.close() relies on this to leave serial light sources (Lumencor LDI,
CoolLED) dark with their ports released. A device that raises on shut_down must
not stop the remaining devices from being shut down too.
"""

from typing import List

from control.lighting import IlluminationController, IlluminationDevice


class FakeDevice(IlluminationDevice):
    """Minimal IlluminationDevice that records the calls it receives."""

    def __init__(self, name: str, raise_on_shut_down: bool = False):
        self.name = name
        self.raise_on_shut_down = raise_on_shut_down
        self.shut_down_calls = 0

    @property
    def channel_names(self) -> List[str]:
        return [self.name]

    def initialize(self) -> None:
        pass

    def shut_down(self) -> None:
        self.shut_down_calls += 1
        if self.raise_on_shut_down:
            raise RuntimeError(f"{self.name} port already gone")

    def set_intensity(self, channel: str, intensity: float) -> None:
        pass

    def turn_on(self, channel: str) -> None:
        pass

    def turn_off(self, channel: str) -> None:
        pass

    def get_intensity(self, channel: str) -> float:
        return 0.0

    def is_on(self, channel: str) -> bool:
        return False


def test_shut_down_reaches_every_device():
    devices = [FakeDevice("405"), FakeDevice("488"), FakeDevice("638")]
    IlluminationController(devices).shut_down()
    assert [d.shut_down_calls for d in devices] == [1, 1, 1]


def test_one_failing_device_does_not_skip_the_rest():
    first = FakeDevice("405")
    broken = FakeDevice("488", raise_on_shut_down=True)
    last = FakeDevice("638")
    IlluminationController([first, broken, last]).shut_down()
    assert first.shut_down_calls == 1
    assert broken.shut_down_calls == 1
    assert last.shut_down_calls == 1


def test_shut_down_on_an_empty_controller_is_a_noop():
    IlluminationController([]).shut_down()
