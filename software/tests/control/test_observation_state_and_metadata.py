"""Tests for Observation State presets and Acquisition Metadata manifests."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from control.core.config.repository import ConfigRepository
from control.core.observation_state_service import (
    observation_state_binning_mode_for_metadata,
    project_merged_channels_for_observation_preset,
    sanitize_preset_filename,
)
from control.models import AcquisitionChannel, CameraSettings, GeneralChannelConfig, IlluminationSettings
from control.models.acquisition_metadata import AcquisitionMetadata
from control.models.observation_state import CameraLiveSnapshot, ObservationState


def _minimal_channel(name: str = "Ch1") -> AcquisitionChannel:
    return AcquisitionChannel(
        name=name,
        enabled=True,
        display_color="#FF0000",
        camera=None,
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        filter_wheel=None,
        filter_position=None,
        z_offset_um=0.0,
        illumination_settings=IlluminationSettings(illumination_channel="TestLaser", intensity=50.0),
    )


def test_sanitize_preset_filename():
    assert sanitize_preset_filename("  my_preset  ") == "my_preset"
    with pytest.raises(ValueError):
        sanitize_preset_filename("bad/name")


def test_observation_state_roundtrip_yaml(tmp_path: Path):
    ch = _minimal_channel()
    live = CameraLiveSnapshot(
        exposure_time_ms=20.0,
        analog_gain=0.0,
        binning_x=2,
        binning_y=2,
        roi_offset_x=0,
        roi_offset_y=0,
        roi_width=1024,
        roi_height=1024,
        trigger_mode="Software Trigger",
        trigger_fps=10.0,
        roi_centered=True,
    )
    state = ObservationState(
        confocal_mode=True,
        active_channel_name="Ch1",
        channels=[ch],
        channel_groups=[],
        emission_filter_positions={"1": 2},
        camera_live=live,
        enable_channel_auto_filter_switching=True,
    )
    data = state.model_dump(mode="json")
    y = yaml.safe_dump(data)
    back = ObservationState.model_validate(yaml.safe_load(y))
    assert back.confocal_mode is True
    assert back.channels[0].name == "Ch1"
    assert back.emission_filter_positions["1"] == 2
    assert back.camera_live is not None
    assert back.camera_live.roi_width == 1024
    assert back.camera_live.trigger_fps == 10.0
    assert back.enable_channel_auto_filter_switching is True
    assert "objective" not in data


def test_observation_state_top_level_binning_roundtrip_yaml():
    ch = _minimal_channel()
    state = ObservationState(
        channels=[ch],
        channel_groups=[],
        binning_x=2,
        binning_y=4,
        camera_mode="TestMode",
    )
    data = state.model_dump(mode="json")
    y = yaml.safe_dump(data)
    back = ObservationState.model_validate(yaml.safe_load(y))
    assert back.binning_x == 2
    assert back.binning_y == 4
    assert back.camera_mode == "TestMode"


def test_observation_state_binning_mode_for_metadata_prefers_top_level():
    ch = _minimal_channel()
    live = CameraLiveSnapshot(
        exposure_time_ms=20.0,
        analog_gain=0.0,
        binning_x=1,
        binning_y=1,
        roi_offset_x=0,
        roi_offset_y=0,
        roi_width=100,
        roi_height=100,
    )
    state = ObservationState(
        channels=[ch],
        channel_groups=[],
        camera_live=live,
        binning_x=3,
        binning_y=3,
        camera_mode="Override",
    )
    bx, by, cm = observation_state_binning_mode_for_metadata(state, camera=None)
    assert bx == 3 and by == 3 and cm == "Override"


def test_project_merged_strips_confocal_override():
    from control.models import AcquisitionChannelOverride

    ch = _minimal_channel()

    override = AcquisitionChannelOverride(
        illumination_settings=IlluminationSettings(illumination_channel=None, intensity=10.0),
        camera_settings=None,
    )
    ch2 = ch.model_copy(update={"confocal_override": override})
    out = project_merged_channels_for_observation_preset([ch2])
    assert out[0].confocal_override is None


def test_apply_observation_state_skips_general_yaml_when_unchanged():
    """Re-loading the same channel content should not rewrite general.yaml (disk + cache)."""
    from control.core.observation_state_service import apply_observation_state

    ch = _minimal_channel()
    state = ObservationState(channels=[ch], channel_groups=[])
    matching = GeneralChannelConfig(
        version=state.version,
        channels=list(state.channels),
        channel_groups=list(state.channel_groups),
    )

    repo = MagicMock()
    repo.current_profile = "p1"
    repo.get_general_config.return_value = matching

    lc = MagicMock()
    lc.is_confocal_mode.return_value = state.confocal_mode
    lc.get_channels.return_value = [ch]
    lc.microscope = MagicMock(illumination_controller=None)
    lc.camera = MagicMock()

    objective_store = MagicMock()
    objective_store.current_objective = "20x"

    apply_observation_state(state, repo, lc, objective_store, emission_filter_wheel=None)

    repo.save_general_config.assert_not_called()
    lc.get_channels.assert_called()
    lc.set_microscope_mode.assert_called()


def test_config_repository_observation_preset_io(tmp_path: Path):
    base = tmp_path / "sw"
    (base / "machine_configs").mkdir(parents=True)
    (base / "user_profiles" / "p1" / "channel_configs").mkdir(parents=True)
    (base / "user_profiles" / "p1" / "observation_presets").mkdir(parents=True)
    (base / "machine_configs" / "illumination_channel_config.yaml").write_text(
        "version: 1\ncontroller_port_mapping: {}\nchannels: []\n", encoding="utf-8"
    )
    general = GeneralChannelConfig(version=1, channels=[_minimal_channel()], channel_groups=[])
    (base / "user_profiles" / "p1" / "channel_configs" / "general.yaml").write_text(
        yaml.safe_dump(general.model_dump(mode="json")), encoding="utf-8"
    )

    repo = ConfigRepository(base_path=base)
    repo.set_profile("p1")
    state = ObservationState(channels=[_minimal_channel("Saved")])
    path = repo.save_observation_preset("test_preset", state)
    assert path.exists()
    assert repo.list_observation_presets() == ["test_preset"]
    loaded = repo.load_observation_preset("test_preset")
    assert loaded is not None
    assert loaded.channels[0].name == "Saved"


def test_last_active_profile_persisted_across_set_profile(tmp_path: Path):
    base = tmp_path / "sw"
    (base / "machine_configs").mkdir(parents=True)
    (base / "user_profiles" / "alpha" / "channel_configs").mkdir(parents=True)
    (base / "user_profiles" / "beta" / "channel_configs").mkdir(parents=True)
    (base / "machine_configs" / "illumination_channel_config.yaml").write_text(
        "version: 1\ncontroller_port_mapping: {}\nchannels: []\n", encoding="utf-8"
    )
    for prof in ("alpha", "beta"):
        gen = GeneralChannelConfig(version=1, channels=[_minimal_channel()], channel_groups=[])
        (base / "user_profiles" / prof / "channel_configs" / "general.yaml").write_text(
            yaml.safe_dump(gen.model_dump(mode="json")), encoding="utf-8"
        )

    repo = ConfigRepository(base_path=base)
    repo.set_profile("alpha")
    assert repo.get_last_active_profile() == "alpha"
    repo.set_profile("beta")
    assert repo.get_last_active_profile() == "beta"

    repo2 = ConfigRepository(base_path=base)
    assert repo2.get_last_active_profile() == "beta"


def test_save_acquisition_metadata(tmp_path: Path):
    repo = ConfigRepository(base_path=tmp_path)
    meta = AcquisitionMetadata(
        experiment_id="exp_001",
        recording_start_time=123.0,
        objective="20x",
        objective_details={"name": "20x", "magnification": 20},
        binning_x=2,
        binning_y=2,
        camera_mode="Rolling",
        selected_channel_names=["Ch1"],
        scan_parameters={"Nx": 2},
    )
    exp_dir = tmp_path / "exp"
    exp_dir.mkdir()
    out = repo.save_acquisition_metadata(exp_dir, meta)
    p = exp_dir / "acquisition_metadata.yaml"
    assert out == p
    assert p.exists()
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["objective"] == "20x"
    assert data["experiment_id"] == "exp_001"
    assert data["binning_x"] == 2
    assert data["camera_mode"] == "Rolling"


def test_save_acquisition_metadata_custom_filename(tmp_path: Path):
    repo = ConfigRepository(base_path=tmp_path)
    meta = AcquisitionMetadata(
        experiment_id="snap_stem",
        recording_start_time=1.0,
        objective="10x",
        objective_details={"name": "10x"},
        selected_channel_names=["Ch1"],
        scan_parameters={"source": "live_snap"},
    )
    out = repo.save_acquisition_metadata(tmp_path, meta, filename="2025-01-01_12-00-00_tag_acquisition_metadata.yaml")
    assert out.name == "2025-01-01_12-00-00_tag_acquisition_metadata.yaml"
    assert out.exists()
