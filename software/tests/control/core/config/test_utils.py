"""
Unit tests for config utility functions.
"""

import pytest
from pathlib import Path
import tempfile
import shutil

from control.core.config import ConfigRepository
from control.core.config.utils import (
    copy_profile_configs,
)


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
