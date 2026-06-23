"""
Test to verify the fix for simulation mode image size calculation.
This test verifies that in simulation mode, the image size is calculated as:
crop_width_unbinned/binning_factor x crop_height_unbinned/binning_factor
instead of using hardcoded values.
"""

import squid.camera.utils
from squid.config import CameraConfig, CameraVariant


def test_simulated_camera_with_crop_dimensions():
    """Test that SimulatedCamera respects crop dimensions from config."""
    # Example: ITR3CMOS26000KMA configuration
    # Assume crop_width_unbinned = 5320, crop_height_unbinned = 4600, binning = 2
    config = CameraConfig(
        camera_type=CameraVariant.TOUPCAM,
        camera_model="ITR3CMOS26000KMA",
        crop_width=5320,
        crop_height=4600,
        default_binning=(2, 2),
        default_pixel_format="MONO12",
    )

    sim_cam = squid.camera.utils.get_camera(config, simulated=True)

    # With binning (2, 2), the expected resolution should be:
    # width = 5320 / 2 = 2660
    # height = 4600 / 2 = 2300
    expected_width = 2660
    expected_height = 2300

    width, height = sim_cam.get_resolution()
    assert width == expected_width, f"Expected width {expected_width}, got {width}"
    assert height == expected_height, f"Expected height {expected_height}, got {height}"

    # Test changing binning
    sim_cam.set_binning(1, 1)
    width, height = sim_cam.get_resolution()
    assert width == 5320, f"Expected width 5320 with binning (1,1), got {width}"
    assert height == 4600, f"Expected height 4600 with binning (1,1), got {height}"

    sim_cam.set_binning(3, 3)
    width, height = sim_cam.get_resolution()
    # 5320 / 3 = 1773.33 -> 1773
    # 4600 / 3 = 1533.33 -> 1533
    assert width == 1773, f"Expected width 1773 with binning (3,3), got {width}"
    assert height == 1533, f"Expected height 1533 with binning (3,3), got {height}"


def test_simulated_camera_fallback_to_hardcoded():
    """Test that SimulatedCamera falls back to hardcoded values when crop dimensions are not set."""
    config = CameraConfig(
        camera_type=CameraVariant.TOUPCAM,
        camera_model="ITR3CMOS26000KMA",  # Use a valid camera model
        crop_width=None,  # No crop dimensions specified
        crop_height=None,
        default_binning=(2, 2),
        default_pixel_format="MONO12",
    )

    sim_cam = squid.camera.utils.get_camera(config, simulated=True)

    # Should fall back to hardcoded BINNING_TO_RESOLUTION
    # For (2, 2) binning, the hardcoded value is (960, 540)
    width, height = sim_cam.get_resolution()
    assert width == 960, f"Expected width 960, got {width}"
    assert height == 540, f"Expected height 540, got {height}"


def test_hardware_roi_smaller_than_crop_shrinks_fov():
    """A hardware ROI smaller than the configured crop must shrink get_crop_size / get_fov_size_mm.

    Regression for the multipoint "gap" bug: a centered hardware ROI (e.g. 2200x2200 on a
    4168x4168 crop) only delivers an ROI-sized frame, but get_crop_size used to keep reporting
    the full configured crop. FOV math then spaced tiles for a 4168 px image while only 2200 px
    were saved, leaving gaps between FOVs that were meant to overlap.
    """
    config = CameraConfig(
        camera_type=CameraVariant.TOUPCAM,
        camera_model="ITR3CMOS26000KMA",
        crop_width=4168,
        crop_height=4168,
        default_binning=(1, 1),
        default_pixel_format="MONO16",
    )
    sim_cam = squid.camera.utils.get_camera(config, simulated=True)

    # No sub-ROI yet: crop == configured crop, and the clamp is a no-op.
    assert sim_cam.get_crop_size() == (4168, 4168)
    full_fov_w, full_fov_h = sim_cam.get_fov_size_mm()

    # Apply a centered hardware ROI smaller than the crop, as the camera-settings UI does.
    sim_cam.set_region_of_interest(984, 984, 2200, 2200)

    assert sim_cam.get_crop_size() == (2200, 2200), "crop must follow the hardware ROI, not the stale config crop"
    roi_fov_w, roi_fov_h = sim_cam.get_fov_size_mm()
    # FOV shrinks in proportion to the ROI (2200 / 4168), so tile stepping matches saved frames.
    assert roi_fov_w == full_fov_w * 2200 / 4168
    assert roi_fov_h == full_fov_h * 2200 / 4168


if __name__ == "__main__":
    test_simulated_camera_with_crop_dimensions()
    test_simulated_camera_fallback_to_hardcoded()
    test_hardware_roi_smaller_than_crop_shrinks_fov()
    print("All tests passed!")
