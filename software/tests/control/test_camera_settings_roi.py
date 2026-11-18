"""Camera ROI controls must follow resolution changes made by observation states."""

from unittest.mock import Mock

import pytest
from qtpy.QtWidgets import QApplication

from gui.widgets.hardware_panels import CameraSettingsWidget
from squid.abc import AbstractCamera, CameraGainRange


# Resolutions advertised by the ITR3CMOS26000KMA, including alignment at 2x/3x.
RESOLUTIONS = {(1, 1): (6224, 4168), (2, 2): (3104, 2084), (3, 3): (2064, 1386)}


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def panel(qt_app):
    camera = Mock(spec=AbstractCamera)
    camera.get_exposure_limits.return_value = (0.01, 10000)
    camera.get_exposure_time.return_value = 20
    camera.get_gain_range.return_value = CameraGainRange(min_gain=0, max_gain=10, gain_step=0.1)
    camera.get_analog_gain.return_value = 0
    camera.get_available_camera_modes.return_value = ["default"]
    camera.get_camera_mode.return_value = "default"
    camera.get_binning_options.return_value = list(RESOLUTIONS)
    camera.get_binning.return_value = (2, 2)
    camera.get_resolution.side_effect = lambda: RESOLUTIONS[camera.get_binning()]
    camera.get_region_of_interest.return_value = (0, 0, 3104, 2084)
    camera.get_available_readout_modes.return_value = []
    widget = CameraSettingsWidget(camera)
    camera.reset_mock()
    yield widget
    widget.deleteLater()


@pytest.mark.parametrize(
    "binning, roi",
    [
        ((1, 1), (0, 0, 6224, 4168)),
        ((1, 1), (4000, 2500, 2000, 1000)),
        ((2, 2), (0, 0, 3104, 2084)),
        ((3, 3), (0, 0, 2064, 1386)),
    ],
)
def test_state_sync_refreshes_roi_limits_before_values_without_camera_writes(panel, binning, roi):
    camera = panel.camera
    # Start at full resolution, then load a binned state so ranges shrink too.
    if binning != (1, 1):
        camera.get_binning.return_value = (1, 1)
        camera.get_region_of_interest.return_value = (0, 0, 6224, 4168)
        panel.sync_controls_from_hardware()
        camera.reset_mock()

    camera.get_binning.return_value = binning
    camera.get_region_of_interest.return_value = roi
    panel.sync_controls_from_hardware()

    width, height = RESOLUTIONS[binning]
    assert panel.dropdown_binning.currentText() == f"{binning[0]}x{binning[1]}"
    controls = (panel.entry_ROI_offset_x, panel.entry_ROI_offset_y, panel.entry_ROI_width, panel.entry_ROI_height)
    assert tuple(control.maximum() for control in controls) == (width, height, width, height)
    assert tuple(control.value() for control in controls) == roi
    assert all(call[0].startswith("get_") for call in camera.mock_calls)
    assert all(not control.signalsBlocked() for control in controls)


def test_live_roi_can_expand_to_full_sensor_after_state_changes_binning(panel):
    camera = panel.camera
    camera.get_binning.return_value = (1, 1)
    camera.get_region_of_interest.return_value = (2612, 1584, 1000, 1000)
    panel.sync_controls_from_hardware()
    assert panel.checkbox_ROI_centered.isChecked()
    camera.reset_mock()

    panel.entry_ROI_width.setValue(6224)
    panel.entry_ROI_height.setValue(4168)

    camera.set_region_of_interest.assert_called_with(0, 0, 6224, 4168)
    assert camera.set_region_of_interest.call_count == 2
