"""Tests for Observation State presets and Acquisition Metadata manifests."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from control.core.config.repository import ConfigRepository
from control.core.observation_state_service import (
    observation_state_binning_mode_for_metadata,
    observation_state_to_yaml,
    sanitize_preset_filename,
)
from control.core.acquisition_metadata_helpers import legacy_flat_multipoint_from_acquisition_yaml_dict
from control.models.acquisition_metadata import AcquisitionMetadata
from control.models.observation_state import (
    CameraLiveSnapshot,
    CameraSettings,
    IlluminatorState,
    IlluminatorTiming,
    ObservationState,
)


def _minimal_state(
    name: str = "test",
    illumination_channel: str = "TestLaser",
    on: bool = False,
) -> ObservationState:
    return ObservationState(
        name=name,
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[
            IlluminatorState(illumination_channel=illumination_channel, intensity=50.0, on=on),
        ],
        display_color="#FF0000",
    )


def test_sanitize_preset_filename():
    assert sanitize_preset_filename("  my_preset  ") == "my_preset"
    with pytest.raises(ValueError):
        sanitize_preset_filename("bad/name")


def test_observation_state_roundtrip_yaml(tmp_path: Path):
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
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[
            IlluminatorState(illumination_channel="TestLaser", intensity=50.0, on=True),
        ],
        channel_groups=[],
        emission_filter_positions={"1": 2},
        camera_live=live,
        enable_channel_auto_filter_switching=True,
        display_color="#FF0000",
    )
    data = state.model_dump(mode="json")
    y = yaml.safe_dump(data)
    back = ObservationState.model_validate(yaml.safe_load(y))
    assert back.confocal_mode is True
    assert back.illuminator_states[0].illumination_channel == "TestLaser"
    assert back.illuminator_states[0].on is True
    assert back.camera_settings is not None
    assert back.camera_settings.exposure_time_ms == 10.0
    assert back.camera_settings.gain_mode == 1.0
    assert back.emission_filter_positions["1"] == 2
    assert back.camera_live is not None
    assert back.camera_live.roi_width == 1024
    assert back.camera_live.trigger_fps == 10.0
    assert back.enable_channel_auto_filter_switching is True
    assert back.display_color == "#FF0000"
    assert "objective" not in data


def test_observation_state_yaml_v3_format():
    """YAML v3 view should have illuminator_states, camera_states, and top-level display_color."""
    state = ObservationState(
        confocal_mode=False,
        camera_settings=CameraSettings(exposure_time_ms=20.0, gain_mode=7.0),
        illuminator_states=[
            IlluminatorState(illumination_channel="LaserA", intensity=80.0, on=True),
            IlluminatorState(illumination_channel="LaserB", intensity=10.0, on=False),
        ],
        channel_groups=[],
        emission_filter_positions={"1": 5},
        camera_live=CameraLiveSnapshot(
            exposure_time_ms=20.0,
            analog_gain=7.0,
            pixel_format="MONO16",
            camera_mode="default",
            binning_x=1,
            binning_y=1,
            roi_offset_x=0,
            roi_offset_y=0,
            roi_width=100,
            roi_height=80,
        ),
        enable_channel_auto_filter_switching=True,
        display_color="#00FF00",
    )
    view = observation_state_to_yaml(state, camera_label="Cam0")

    assert view["name"] == state.name
    assert "illuminator_states" in view
    assert "channels" not in view
    assert len(view["illuminator_states"]) == 2
    assert view["illuminator_states"][0]["illumination_channel"] == "LaserA"
    assert view["illuminator_states"][0]["on"] is True

    assert "camera_states" in view
    assert view["camera_states"]["Cam0"]["camera_settings"]["exposure_time_ms"] == 20.0
    assert view["camera_states"]["Cam0"]["camera_settings"]["gain_mode"] == 7.0
    assert view["camera_states"]["Cam0"]["emission_filter_positions"]["1"] == 5

    assert view["display_color"] == "#00FF00"

    # No per-illuminator camera or filter fields
    for ist_view in view["illuminator_states"]:
        assert "camera_settings" not in ist_view
        assert "filter_wheel" not in ist_view
        assert "filter_position" not in ist_view


def test_illuminator_timing_single_pulse_round_trip_yaml():
    """An ObservationState with a single-pulse IlluminatorTiming round-trips through
    observation_state_to_yaml + load."""
    state = ObservationState(
        name="pulsed_561",
        camera_settings=CameraSettings(exposure_time_ms=50.0, gain_mode=1.0),
        illuminator_states=[
            IlluminatorState(
                illumination_channel="Fluorescence 561 nm Ex",
                intensity=50.0,
                on=True,
                timing=IlluminatorTiming(start_offset_ms=24.5, pulse_width_ms=1.0),
            ),
            IlluminatorState(
                illumination_channel="Fluorescence 488 nm Ex",
                intensity=30.0,
                on=True,
            ),  # untimed — should round-trip without a timing block
        ],
        display_color="#FF00FF",
    )

    view = observation_state_to_yaml(state, camera_label="camera")
    pulsed = view["illuminator_states"][0]
    assert pulsed["timing"] == {
        "start_offset_ms": 24.5,
        "pulse_width_ms": 1.0,
        "period_ms": 0.0,
        "num_pulses": 1,
    }
    untimed = view["illuminator_states"][1]
    assert "timing" not in untimed

    # Re-load via the model and confirm fields survived.
    serialized = yaml.safe_dump(state.model_dump(mode="json"))
    back = ObservationState.model_validate(yaml.safe_load(serialized))
    assert back.illuminator_states[0].timing is not None
    assert back.illuminator_states[0].timing.start_offset_ms == 24.5
    assert back.illuminator_states[0].timing.pulse_width_ms == 1.0
    assert back.illuminator_states[0].timing.num_pulses == 1
    assert back.illuminator_states[1].timing is None
    assert back.is_waveform_driven is True


def test_stimulus_only_state_round_trip_yaml():
    """A stimulus-only ObservationState with a 10-pulse comb round-trips through YAML."""
    state = ObservationState(
        name="opto_561_comb",
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[
            IlluminatorState(
                illumination_channel="Fluorescence 561 nm Ex",
                intensity=30.0,
                on=True,
                timing=IlluminatorTiming(
                    start_offset_ms=0.0,
                    pulse_width_ms=5.0,
                    period_ms=50.0,
                    num_pulses=10,
                ),
            ),
        ],
        is_stimulus_only=True,
        stimulus_duration_ms=500.0,
    )

    view = observation_state_to_yaml(state, camera_label="camera")
    assert view["is_stimulus_only"] is True
    assert view["stimulus_duration_ms"] == 500.0
    assert view["illuminator_states"][0]["timing"]["num_pulses"] == 10
    assert view["illuminator_states"][0]["timing"]["period_ms"] == 50.0

    serialized = yaml.safe_dump(state.model_dump(mode="json"))
    back = ObservationState.model_validate(yaml.safe_load(serialized))
    assert back.is_stimulus_only is True
    assert back.stimulus_duration_ms == 500.0
    assert back.is_waveform_driven is True
    timing = back.illuminator_states[0].timing
    assert timing is not None
    assert timing.num_pulses == 10
    assert timing.period_ms == 50.0
    assert timing.pulse_width_ms == 5.0
    # Last edge = start + (N-1) × period + width = 0 + 9×50 + 5 = 455
    assert abs(timing.end_ms - 455.0) < 1e-9


def test_is_waveform_driven_only_for_active_timed_illuminators():
    """A timing block on an inactive illuminator must NOT make the state waveform-driven
    (unless is_stimulus_only is True, which always counts)."""
    state = ObservationState(
        name="inactive_pulsed",
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[
            IlluminatorState(
                illumination_channel="LaserA",
                intensity=50.0,
                on=False,
                timing=IlluminatorTiming(start_offset_ms=1.0, pulse_width_ms=1.0),
            ),
        ],
    )
    assert state.is_waveform_driven is False
    state.illuminator_states[0] = state.illuminator_states[0].model_copy(update={"on": True})
    assert state.is_waveform_driven is True


def test_comb_validator_rejects_period_at_or_below_width():
    with pytest.raises(ValueError, match="period_ms must exceed pulse_width_ms"):
        IlluminatorTiming(pulse_width_ms=5.0, period_ms=5.0, num_pulses=2)
    with pytest.raises(ValueError, match="period_ms must exceed pulse_width_ms"):
        IlluminatorTiming(pulse_width_ms=5.0, period_ms=4.0, num_pulses=3)


def test_observation_state_binning_mode_from_camera_live():
    """Binning/mode for metadata comes from camera_live snapshot."""
    live = CameraLiveSnapshot(
        exposure_time_ms=20.0,
        analog_gain=0.0,
        binning_x=3,
        binning_y=3,
        camera_mode="Override",
        roi_offset_x=0,
        roi_offset_y=0,
        roi_width=100,
        roi_height=100,
    )
    state = ObservationState(
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[
            IlluminatorState(illumination_channel="TestLaser", intensity=50.0, on=False),
        ],
        channel_groups=[],
        camera_live=live,
    )
    bx, by, cm = observation_state_binning_mode_for_metadata(state, camera=None)
    assert bx == 3 and by == 3 and cm == "Override"



def test_config_repository_observation_preset_io(tmp_path: Path):
    base = tmp_path / "sw"
    (base / "machine_configs").mkdir(parents=True)
    (base / "user_profiles" / "p1" / "channel_configs").mkdir(parents=True)
    (base / "user_profiles" / "p1" / "observation_presets").mkdir(parents=True)
    (base / "machine_configs" / "illumination_channel_config.yaml").write_text(
        "version: 1\ncontroller_port_mapping: {}\nchannels: []\n", encoding="utf-8"
    )
    # Create a minimal general.yaml as flat ObservationState
    state_for_general = ObservationState(
        version=3,
        name="TestLaser",
        display_color="#FF0000",
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[
            IlluminatorState(illumination_channel="TestLaser", intensity=50.0, on=False),
        ],
    )
    (base / "user_profiles" / "p1" / "channel_configs" / "general.yaml").write_text(
        yaml.safe_dump(state_for_general.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
    )

    repo = ConfigRepository(base_path=base)
    repo.set_profile("p1")
    state = _minimal_state(name="Saved", illumination_channel="SavedLaser", on=True)
    path = repo.save_observation_preset("test_preset", state)
    assert path.exists()
    assert repo.list_observation_presets() == ["test_preset"]
    loaded = repo.load_observation_preset("test_preset")
    assert loaded is not None
    assert loaded.illuminator_states[0].illumination_channel == "SavedLaser"
    assert loaded.name == "test_preset"


def test_config_repository_acquisition_cycle_io(tmp_path: Path):
    from control.models.acquisition_cycle import AcquisitionCycle, CycleGroup, CycleStep, CycleWait

    base = tmp_path / "sw"
    (base / "machine_configs").mkdir(parents=True)
    (base / "user_profiles" / "p1" / "channel_configs").mkdir(parents=True)
    (base / "machine_configs" / "illumination_channel_config.yaml").write_text(
        "version: 1\ncontroller_port_mapping: {}\nchannels: []\n", encoding="utf-8"
    )
    state_for_general = ObservationState(
        version=3,
        name="TestLaser",
        display_color="#FF0000",
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[IlluminatorState(illumination_channel="TestLaser", intensity=50.0, on=False)],
    )
    (base / "user_profiles" / "p1" / "channel_configs" / "general.yaml").write_text(
        yaml.safe_dump(state_for_general.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
    )

    repo = ConfigRepository(base_path=base)
    repo.set_profile("p1")

    assert repo.list_acquisition_cycles() == []
    cycle = AcquisitionCycle(
        name="ignored - normalized on save",
        repeat=4,
        items=[
            CycleGroup(
                repeat=3,
                steps=[
                    CycleStep(observation_state="GFP", n_frames=10),
                    CycleWait(duration_ms=250.0),
                    CycleStep(observation_state="stim"),
                ],
            ),
            CycleWait(duration_ms=1000.0),
            CycleStep(observation_state="RFP", n_frames=5),
        ],
    )
    path = repo.save_acquisition_cycle("opto v1", cycle)
    assert path.exists()
    # Sanitized name becomes the file stem and the in-file name.
    assert repo.list_acquisition_cycles() == ["opto_v1"]

    loaded = repo.load_acquisition_cycle("opto_v1")
    assert loaded is not None
    assert loaded.name == "opto_v1"
    assert loaded.repeat == 4
    assert isinstance(loaded.items[0], CycleGroup)
    assert loaded.items[0].repeat == 3
    assert loaded.items[0].steps[0].observation_state == "GFP"
    assert loaded.items[0].steps[0].n_frames == 10
    # Wait round-trips at both nesting levels (pydantic union discrimination).
    assert isinstance(loaded.items[0].steps[1], CycleWait)
    assert loaded.items[0].steps[1].duration_ms == 250.0
    assert isinstance(loaded.items[1], CycleWait)
    assert loaded.items[1].duration_ms == 1000.0
    assert isinstance(loaded.items[2], CycleStep)
    assert loaded.items[2].n_frames == 5

    assert repo.delete_acquisition_cycle("opto_v1") is True
    assert repo.list_acquisition_cycles() == []
    assert repo.load_acquisition_cycle("opto_v1") is None


def test_last_active_profile_persisted_across_set_profile(tmp_path: Path):
    base = tmp_path / "sw"
    (base / "machine_configs").mkdir(parents=True)
    (base / "user_profiles" / "alpha" / "channel_configs").mkdir(parents=True)
    (base / "user_profiles" / "beta" / "channel_configs").mkdir(parents=True)
    (base / "machine_configs" / "illumination_channel_config.yaml").write_text(
        "version: 1\ncontroller_port_mapping: {}\nchannels: []\n", encoding="utf-8"
    )
    state_for_general = ObservationState(
        version=3,
        name="TestLaser",
        display_color="#FF0000",
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[
            IlluminatorState(illumination_channel="TestLaser", intensity=50.0, on=False),
        ],
    )
    for prof in ("alpha", "beta"):
        (base / "user_profiles" / prof / "channel_configs" / "general.yaml").write_text(
            yaml.safe_dump(state_for_general.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
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


def test_append_frame_acquisition_times_csv(tmp_path: Path):
    from control.core.job_processing import CaptureInfo, append_frame_acquisition_time_csv

    cfg = MagicMock()
    cfg.name = "BF"
    pos = MagicMock()
    pos.x_mm = 1.0
    pos.y_mm = 2.0
    pos.z_mm = 3.0
    info = CaptureInfo(
        position=pos,
        z_index=0,
        capture_time=1_700_000_000.25,
        observation_state=cfg,
        save_directory=str(tmp_path),
        file_id="0_0_0",
        region_id=0,
        fov=0,
        configuration_idx=0,
        time_point=0,
    )
    append_frame_acquisition_time_csv(info, "0_0_0_BF.tiff")
    out = tmp_path / "frame_acquisition_times.csv"
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "unix_time_s" in text
    assert "1700000000.250000" in text
    assert "utc_iso" in text


def test_legacy_flat_from_unified_yaml_matches_grid_and_objective():
    data = {
        "schema_version": 2,
        "acquisition": {"use_manual_focus_map": True},
        "objective": {
            "name": "20x",
            "magnification": 20.0,
            "sensor_pixel_size_um": 6.5,
            "tube_lens_f_mm": 180.0,
        },
        "z_stack": {"nz": 3, "delta_z_mm": 0.002},
        "time_series": {"nt": 2, "delta_t_s": 30.0},
        "autofocus": {"contrast_af": True, "laser_af": False},
        "flexible_scan": {"nx": 2, "ny": 3, "delta_x_mm": 0.5, "delta_y_mm": 0.6},
        "channels": {"observation_state_names": ["p1"]},
        "manifest": {"confocal_mode": False, "tube_lens_mm": 50.0},
    }
    flat = legacy_flat_multipoint_from_acquisition_yaml_dict(data)
    assert flat["dx(mm)"] == 0.5
    assert flat["dy(mm)"] == 0.6
    assert flat["Nx"] == 2
    assert flat["Ny"] == 3
    assert flat["dz(um)"] == 2.0
    assert flat["objective"]["magnification"] == 20.0
    assert flat["tube_lens_mm"] == 50.0
    assert flat["observation_state_names"] == ["p1"]


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
