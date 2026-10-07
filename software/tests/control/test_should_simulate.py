"""``_should_simulate`` decides whether a component opens real hardware.

Getting this wrong is not a test failure, it is a rig incident: a build asking
for simulation that returns False opens serial ports and cameras on whatever
microscope the developer happens to be sitting at.
"""

import pytest

from control.microscope import _should_simulate


@pytest.mark.parametrize(
    "global_simulated, component_override, expected",
    [
        # A caller asking for simulation gets it, whatever the config says.
        (True, False, True),
        (True, True, True),
        # One component simulated against otherwise-real hardware.
        (False, True, True),
        # The only combination that may touch hardware.
        (False, False, False),
    ],
)
def test_simulation_gating(global_simulated, component_override, expected):
    assert _should_simulate(global_simulated, component_override) is expected


def test_global_flag_alone_is_enough():
    # Regression: this returned False, so Microscope.build_from_global_config(
    # simulated=True) opened the real microcontroller.
    assert _should_simulate(True, False) is True
