"""
Unit tests for config utility functions.
"""

import pytest
from pathlib import Path
import tempfile
import shutil

from control.core.config import ConfigRepository
from control.core.config.utils import (
    get_effective_observation_states,
    copy_profile_configs,
)
from control.models import (
    CameraSettings,
    ConfocalSettings,
    GeneralObservationConfig,
    ObjectiveOverride,
    ObjectiveOverrideConfig,
)
from control.models.observation_state import (
    ObservationState,
    IlluminatorState,
)


@pytest.fixture
def sample_state():
    """Create a sample observation state."""
    return ObservationState(
        version=3,
        name="Test Channel",
        display_color="#00FF00",
        camera_settings=CameraSettings(
            exposure_time_ms=100.0,
            gain_mode=0.0,
        ),
        illuminator_states=[
            IlluminatorState(
                illumination_channel="488nm",
                intensity=50.0,
                on=False,
            ),
        ],
    )


class TestGetEffectiveObservationStates:
    """Tests for get_effective_observation_states function."""

    def test_merges_general_and_objective(self):
        """Test that general and objective configs are merged."""
        general = GeneralObservationConfig(
            version=3,
            observation_states=[
                ObservationState(
                    version=3,
                    name="Channel 1",
                    display_color="#00FF00",
                    camera_settings=CameraSettings(
                        exposure_time_ms=100.0,
                        gain_mode=0.0,
                    ),
                    illuminator_states=[
                        IlluminatorState(
                            illumination_channel="488nm",
                            intensity=50.0,
                            on=False,
                        ),
                    ],
                    z_offset_um=5.0,
                )
            ],
        )

        objective = ObjectiveOverrideConfig(
            version=3,
            overrides=[
                ObjectiveOverride(
                    name="Channel 1",
                    camera_settings=CameraSettings(
                        exposure_time_ms=50.0,
                        gain_mode=1.0,
                    ),
                )
            ],
        )

        result = get_effective_observation_states(general, objective)

        assert len(result) == 1
        s = result[0]
        # From general: illumination, z_offset_um, display_color
        assert s.illuminator_states[0].illumination_channel == "488nm"
        assert s.z_offset_um == 5.0
        assert s.display_color == "#00FF00"
        # From objective: camera_settings override
        assert s.camera_settings.exposure_time_ms == 50.0
        assert s.camera_settings.gain_mode == 1.0

    def test_no_objective_override_returns_general(self):
        """Test that states without override are returned as-is."""
        general = GeneralObservationConfig(
            version=3,
            observation_states=[
                ObservationState(
                    version=3,
                    name="Channel 1",
                    camera_settings=CameraSettings(
                        exposure_time_ms=100.0,
                        gain_mode=0.0,
                    ),
                    illuminator_states=[
                        IlluminatorState(
                            illumination_channel="488nm",
                            intensity=50.0,
                            on=False,
                        ),
                    ],
                ),
            ],
        )
        objective = ObjectiveOverrideConfig(version=3, overrides=[])

        result = get_effective_observation_states(general, objective)
        assert len(result) == 1
        assert result[0].camera_settings.exposure_time_ms == 100.0


class TestCopyProfileConfigs:
    """Tests for copy_profile_configs function."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for test configs."""
        d = tempfile.mkdtemp()
        yield Path(d)
        shutil.rmtree(d)

    @pytest.fixture
    def repo_with_profiles(self, temp_dir):
        """Create a ConfigRepository with source and destination profiles."""
        user_profiles = temp_dir / "user_profiles"
        (temp_dir / "machine_configs").mkdir()

        # Create source profile with configs
        source = user_profiles / "source"
        (source / "channel_configs").mkdir(parents=True)
        (source / "laser_af_configs").mkdir(parents=True)

        # Write some config files (v3 schema)
        (source / "channel_configs" / "general.yaml").write_text(
            """
version: 3
observation_states:
  - version: 3
    name: "Test"
    display_color: "#00FF00"
    camera_settings:
      exposure_time_ms: 100.0
      gain_mode: 0.0
    illuminator_states:
      - illumination_channel: "488nm"
        intensity: 50.0
        'on': false
channel_groups: []
"""
        )
        (source / "laser_af_configs" / "20x.yaml").write_text(
            """
version: 1
reference_offset_um: 5.0
"""
        )

        # Create empty destination profile
        dest = user_profiles / "dest"
        (dest / "channel_configs").mkdir(parents=True)
        (dest / "laser_af_configs").mkdir(parents=True)

        return ConfigRepository(base_path=temp_dir)

    def test_copies_channel_configs(self, repo_with_profiles, temp_dir):
        """Test that channel configs are copied."""
        copy_profile_configs(repo_with_profiles, "source", "dest")

        dest_general = temp_dir / "user_profiles" / "dest" / "channel_configs" / "general.yaml"
        assert dest_general.exists()
        assert "Test" in dest_general.read_text()

    def test_copies_laser_af_configs(self, repo_with_profiles, temp_dir):
        """Test that laser AF configs are copied."""
        copy_profile_configs(repo_with_profiles, "source", "dest")

        dest_laser_af = temp_dir / "user_profiles" / "dest" / "laser_af_configs" / "20x.yaml"
        assert dest_laser_af.exists()
        assert "reference_offset_um" in dest_laser_af.read_text()

    def test_raises_if_source_missing(self, temp_dir):
        """Test that ValueError is raised if source profile doesn't exist."""
        (temp_dir / "machine_configs").mkdir()
        (temp_dir / "user_profiles" / "dest" / "channel_configs").mkdir(parents=True)

        repo = ConfigRepository(base_path=temp_dir)

        with pytest.raises(ValueError, match="Source profile"):
            copy_profile_configs(repo, "nonexistent", "dest")

    def test_raises_if_dest_missing(self, temp_dir):
        """Test that ValueError is raised if destination profile doesn't exist."""
        (temp_dir / "machine_configs").mkdir()
        (temp_dir / "user_profiles" / "source" / "channel_configs").mkdir(parents=True)

        repo = ConfigRepository(base_path=temp_dir)

        with pytest.raises(ValueError, match="Destination profile"):
            copy_profile_configs(repo, "source", "nonexistent")
