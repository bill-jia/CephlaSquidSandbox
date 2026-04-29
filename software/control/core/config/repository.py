"""
Centralized configuration repository.

Single source of truth for all config I/O and caching.
Pure Python - NO Qt dependencies.

Organization:
- Generic I/O: save_to_path() for saving any Pydantic model
- Profile Management: profile CRUD operations
- Machine Configs: global hardware configs (illumination, confocal, camera mappings)
- Channel Configs: per-profile acquisition channel settings
- Channel Config Convenience: higher-level helpers (merge, update settings)
- Laser AF Configs: per-profile laser autofocus settings
- Acquisition Output: saving settings to experiment directories
- Observation State: objective-free presets under each profile
- Acquisition Metadata: per-run manifest next to experiment outputs
- Cache Management: cache control
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TypeVar, Union

import yaml

# LibYAML C implementations (when installed) noticeably speed up large configs.
try:
    from yaml import CSafeLoader as _YamlSafeLoader
except ImportError:
    from yaml import SafeLoader as _YamlSafeLoader

try:
    from yaml import CDumper as _YamlDumper
except ImportError:
    from yaml import Dumper as _YamlDumper
from pydantic import BaseModel, ValidationError

from control.models import (
    AcquisitionOutputConfig,
    CameraMappingsConfig,
    CameraRegistryConfig,
    ConfocalConfig,
    FilterWheelDefinition,
    FilterWheelRegistryConfig,
    FilterWheelType,
    IlluminationChannelConfig,
    CameraSettings,
    LaserAFConfig,
    IOEndpointConfig,
    build_default_io_endpoint_config,
    MachineConfig,
    build_default_machine_config,
)
from control.models.hardware_bindings import (
    FilterWheelReference,
    HardwareBindingsConfig,
    FILTER_WHEEL_SOURCE_CONFOCAL,
    FILTER_WHEEL_SOURCE_STANDALONE,
)
from control.models.acquisition_metadata import AcquisitionMetadata
from control.models.observation_state import ObservationState
from control.models.observation_state import CameraLiveSnapshot
from control.models import ConfocalSettings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ConfigRepository:
    """
    Centralized configuration repository.

    Handles loading, saving, and caching for all Pydantic config models.
    Supports machine configs (global) and profile configs (per-user).

    Directory structure:
        software/
        ├── machine_configs/
        │   ├── illumination_channel_config.yaml
        │   ├── confocal_config.yaml (optional)
        │   ├── camera_mappings.yaml (legacy)
        │   ├── cameras.yaml (v1.1 - camera registry)
        │   └── filter_wheels.yaml (v1.1 - filter wheel registry)
        └── user_profiles/
            └── {profile}/
                ├── channel_configs/
                │   ├── general.yaml (includes channel_groups in v1.1)
                │   └── {objective}.yaml
                ├── observation_presets/
                │   └── *.yaml
                └── laser_af_configs/
                    └── {objective}.yaml
    """

    def __init__(self, base_path: Optional[Path] = None):
        """
        Initialize the config repository.

        Args:
            base_path: Base path for configuration files. Defaults to the
                      'software' directory containing this module.
        """
        if base_path is None:
            # Default to software/ directory (4 levels up from this file)
            base_path = Path(__file__).parent.parent.parent.parent
        self.base_path = Path(base_path)
        self.machine_configs_path = self.base_path / "machine_configs"
        self.user_profiles_path = self.base_path / "user_profiles"

        self._current_profile: Optional[str] = None
        self._machine_cache: Dict[str, Any] = {}
        self._profile_cache: Dict[str, Any] = {}

    # ═══════════════════════════════════════════════════════════════════════════
    # GENERIC I/O
    # Methods that work with any Pydantic model
    # ═══════════════════════════════════════════════════════════════════════════

    def save_to_path(self, path: Path, model: BaseModel) -> None:
        """
        Save any Pydantic model to an arbitrary path.

        This is the generic save method - use it when you need to save a model
        to a location outside the standard config directories.

        Args:
            path: Target file path (parent directories created if needed)
            model: Pydantic model to save
        """
        self._save_yaml(path, model)

    # ═══════════════════════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ═══════════════════════════════════════════════════════════════════════════

    def _load_yaml(self, path: Path, model_class: Type[T]) -> Optional[T]:
        """
        Load a YAML file and parse it into a Pydantic model.

        Error handling:
        - File not found: return None
        - YAML parse error: log warning, return None
        - Pydantic validation error: log warning, return None
        - Permission error: raise (real problem)
        """
        if not path.exists():
            logger.debug(f"Config file not found: {path}")
            return None

        try:
            with open(path, "r") as f:
                data = yaml.safe_load(f)
            if data is None:
                data = {}
            return model_class(**data)
        except PermissionError:
            logger.error(f"Permission denied reading {path}")
            raise
        except yaml.YAMLError as e:
            logger.warning(f"Failed to parse YAML file {path}: {e}")
            return None
        except ValidationError as e:
            logger.warning(f"Config validation failed for {path}: {e}")
            return None

    def _save_yaml(self, path: Path, model: BaseModel) -> None:
        """
        Save a Pydantic model to a YAML file.

        Creates parent directories if needed.
        Raises on permission or disk errors (after logging).
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            # Convert model to dict, using mode="json" to ensure Enums are serialized as strings
            # exclude_none=True omits optional fields when None (cleaner YAML files)
            data = model.model_dump(exclude_none=True, mode="json")

            with open(path, "w") as f:
                yaml.dump(
                    data,
                    f,
                    Dumper=_YamlDumper,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            logger.debug(f"Saved config to {path}")
        except PermissionError:
            logger.error(f"Permission denied writing {path}")
            raise
        except (OSError, yaml.YAMLError) as e:
            logger.error(f"Failed to save config to {path}: {e}")
            raise

    def _get_profile_path(self, profile: Optional[str] = None) -> Path:
        """Get path for a profile, defaulting to current profile."""
        profile = profile or self._current_profile
        if profile is None:
            raise ValueError("No profile set. Call set_profile() first.")
        return self.user_profiles_path / profile

    # ═══════════════════════════════════════════════════════════════════════════
    # PROFILE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════════

    @property
    def current_profile(self) -> Optional[str]:
        """Get the current profile name."""
        return self._current_profile

    def _last_active_profile_path(self) -> Path:
        """Cache file so the same profile is restored on the next application start."""
        return self.base_path / "cache" / "last_active_profile.txt"

    def get_last_active_profile(self) -> Optional[str]:
        """
        Read the last successfully loaded profile name from disk, if valid.

        Returns None if missing, unreadable, or the profile directory no longer exists.
        """
        path = self._last_active_profile_path()
        if not path.is_file():
            return None
        try:
            name = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not name:
            return None
        if not (self.user_profiles_path / name).is_dir():
            return None
        return name

    def _persist_last_active_profile(self, profile: str) -> None:
        """Write last active profile for restore on next startup (see Microscope init)."""
        try:
            path = self._last_active_profile_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(profile + "\n", encoding="utf-8")
        except OSError as e:
            logger.warning("Could not persist last active profile: %s", e)

    def set_profile(self, profile: str) -> None:
        """
        Set the current profile. Clears profile cache.

        Args:
            profile: Profile name (directory name under user_profiles/)

        Raises:
            ValueError: If profile doesn't exist
        """
        profile_path = self.user_profiles_path / profile
        if not profile_path.exists():
            raise ValueError(f"Profile '{profile}' does not exist at {profile_path}")

        self._current_profile = profile
        self._profile_cache.clear()
        self._persist_last_active_profile(profile)
        logger.debug(f"Switched to profile: {profile}")

    def load_profile(self, profile: str) -> None:
        """
        Load a profile, ensuring default configs exist.

        This is the high-level method for switching profiles that:
        1. Ensures the profile has default configs if needed
        2. Sets the profile as current

        Args:
            profile: Profile name

        Raises:
            ValueError: If profile doesn't exist
        """
        profile_path = self.user_profiles_path / profile
        if not profile_path.exists():
            raise ValueError(f"Profile '{profile}' does not exist")

        # Ensure default configs exist (lazy import to avoid circular dependency)
        try:
            from control.default_config_generator import ensure_default_configs
            import control._def

            include_confocal = getattr(control._def, "ENABLE_SPINNING_DISK_CONFOCAL", False)
            if ensure_default_configs(self, profile, include_confocal=include_confocal):
                logger.info(f"Generated default configs for profile '{profile}'")
        except ImportError as e:
            # Expected if running without full dependencies or in test environment
            logger.debug(f"Could not generate default configs (module not available): {e}")
        except FileNotFoundError as e:
            # Expected if illumination config doesn't exist yet
            logger.warning(f"Could not generate default configs (missing required config): {e}")
        except (PermissionError, OSError) as e:
            logger.error(f"Failed to generate default configs (filesystem error): {e}")
        except Exception as e:
            logger.error(f"Unexpected error generating default configs: {e}")

        self.set_profile(profile)

    def get_available_profiles(self) -> List[str]:
        """Get list of available user profiles."""
        if not self.user_profiles_path.exists():
            return []
        return sorted([d.name for d in self.user_profiles_path.iterdir() if d.is_dir() and not d.name.startswith(".")])

    def get_available_objectives(self, profile: Optional[str] = None) -> List[str]:
        """
        Get list of available objectives for a profile.

        Args:
            profile: Profile name. Defaults to current profile.
        """
        profile_path = self._get_profile_path(profile)
        channel_configs_path = profile_path / "channel_configs"
        if not channel_configs_path.exists():
            return []
        objectives = []
        for f in channel_configs_path.iterdir():
            if f.suffix == ".yaml" and f.stem != "general":
                objectives.append(f.stem)
        return sorted(objectives)

    def create_profile(self, name: str) -> None:
        """
        Create a new empty profile with directory structure.

        Args:
            name: Profile name

        Raises:
            ValueError: If profile already exists
        """
        profile_path = self.user_profiles_path / name
        if profile_path.exists():
            raise ValueError(f"Profile '{name}' already exists")

        (profile_path / "channel_configs").mkdir(parents=True)
        (profile_path / "observation_presets").mkdir(parents=True, exist_ok=True)
        (profile_path / "laser_af_configs").mkdir(parents=True)
        logger.info(f"Created profile: {name}")

    def copy_profile(self, source: str, dest: str) -> None:
        """
        Create a new profile by copying all configs from an existing profile.

        Args:
            source: Source profile name to copy from
            dest: Destination profile name to create

        Raises:
            ValueError: If dest profile already exists or source doesn't exist
        """
        import shutil

        source_path = self.user_profiles_path / source
        dest_path = self.user_profiles_path / dest

        if not source_path.exists():
            raise ValueError(f"Source profile '{source}' does not exist")
        if dest_path.exists():
            raise ValueError(f"Profile '{dest}' already exists")

        # Create directory structure
        (dest_path / "channel_configs").mkdir(parents=True)
        (dest_path / "observation_presets").mkdir(parents=True, exist_ok=True)
        (dest_path / "laser_af_configs").mkdir(parents=True)

        # Copy all YAML files from source to dest
        for subdir in ["channel_configs", "laser_af_configs"]:
            source_dir = source_path / subdir
            dest_dir = dest_path / subdir
            if source_dir.exists():
                for yaml_file in source_dir.glob("*.yaml"):
                    shutil.copy2(yaml_file, dest_dir / yaml_file.name)

        # Observation State presets (objective-free YAML files)
        obs_src = source_path / "observation_presets"
        obs_dst = dest_path / "observation_presets"
        if obs_src.exists():
            obs_dst.mkdir(parents=True, exist_ok=True)
            for yaml_file in obs_src.glob("*.yaml"):
                shutil.copy2(yaml_file, obs_dst / yaml_file.name)

        logger.info(f"Created profile '{dest}' by copying from '{source}'")

    def profile_exists(self, name: str) -> bool:
        """Check if a profile exists."""
        return (self.user_profiles_path / name).exists()

    def profile_has_configs(self, profile: Optional[str] = None) -> bool:
        """Check if a profile has any configuration files (general.yaml exists)."""
        profile_path = self._get_profile_path(profile)
        general_path = profile_path / "channel_configs" / "general.yaml"
        return general_path.exists()

    def ensure_profile_directories(self, profile: Optional[str] = None) -> None:
        """Create profile directories if they don't exist."""
        profile_path = self._get_profile_path(profile)
        (profile_path / "channel_configs").mkdir(parents=True, exist_ok=True)
        (profile_path / "observation_presets").mkdir(parents=True, exist_ok=True)
        (profile_path / "laser_af_configs").mkdir(parents=True, exist_ok=True)

    def get_profile_path(self, profile: Optional[str] = None) -> Path:
        """Get the path for a user profile (public API)."""
        return self._get_profile_path(profile)

    # ═══════════════════════════════════════════════════════════════════════════
    # MACHINE CONFIGS
    # Global hardware configuration (cached indefinitely)
    # ═══════════════════════════════════════════════════════════════════════════

    def get_illumination_config(self) -> Optional[IlluminationChannelConfig]:
        """Load illumination channel configuration (cached)."""
        cache_key = "illumination"
        if cache_key not in self._machine_cache:
            path = self.machine_configs_path / "illumination_channel_config.yaml"
            self._machine_cache[cache_key] = self._load_yaml(path, IlluminationChannelConfig)
        return self._machine_cache[cache_key]

    def get_io_endpoint_config(self) -> IOEndpointConfig:
        """Load IO endpoint configuration (cached).

        Falls back to built-in defaults that mirror legacy MCU-only wiring
        when io_endpoints.yaml is absent.
        """
        cache_key = "io_endpoints"
        if cache_key not in self._machine_cache:
            path = self.machine_configs_path / "io_endpoints.yaml"
            loaded = self._load_yaml(path, IOEndpointConfig)
            if loaded is None:
                logger.info("io_endpoints.yaml not found — using default MCU-only IO endpoints")
                loaded = build_default_io_endpoint_config()
            self._machine_cache[cache_key] = loaded
        return self._machine_cache[cache_key]

    def get_machine_config(self) -> MachineConfig:
        """Load the unified machine configuration (cached).

        Resolution order:
        1. Explicit ``machine_config.yaml`` (active config for this install)
        2. If missing, a single matching ``machine_config_*.yaml`` file
           (for setups that keep multiple named configs side-by-side)
        3. Built-in default via ``build_default_machine_config()``
        """
        cache_key = "machine_config"
        if cache_key not in self._machine_cache:
            primary = self.machine_configs_path / "machine_config.yaml"
            loaded: Optional[MachineConfig] = None

            # 1) Primary explicit config file
            if primary.exists():
                loaded = self._load_yaml(primary, MachineConfig)
            else:
                # 2) Fallback: single named machine_config_*.yaml
                candidates = sorted(self.machine_configs_path.glob("machine_config_*.yaml"))
                if len(candidates) == 1:
                    logger.info(
                        f"machine_config.yaml not found — using named config {candidates[0].name}"
                    )
                    loaded = self._load_yaml(candidates[0], MachineConfig)

            if loaded is None:
                logger.info(
                    "No machine_config.yaml (or unique machine_config_*.yaml) found — "
                    "using built-in default machine config"
                )
                loaded = build_default_machine_config()
            else:
                issues = loaded.validate_io_lines()
                for issue in issues:
                    logger.warning(f"machine_config IO validation: {issue}")
            self._machine_cache[cache_key] = loaded
        return self._machine_cache[cache_key]

    def get_confocal_config(self) -> Optional[ConfocalConfig]:
        """
        Load confocal configuration (cached).

        Returns None if confocal_config.yaml doesn't exist (system has no confocal).
        """
        cache_key = "confocal"
        if cache_key not in self._machine_cache:
            path = self.machine_configs_path / "confocal_config.yaml"
            self._machine_cache[cache_key] = self._load_yaml(path, ConfocalConfig)
        return self._machine_cache[cache_key]

    def get_camera_mappings(self) -> Optional[CameraMappingsConfig]:
        """Load camera mappings configuration (cached)."""
        cache_key = "camera_mappings"
        if cache_key not in self._machine_cache:
            path = self.machine_configs_path / "camera_mappings.yaml"
            self._machine_cache[cache_key] = self._load_yaml(path, CameraMappingsConfig)
        return self._machine_cache[cache_key]

    def has_confocal(self) -> bool:
        """Check if system has confocal hardware."""
        return self.get_confocal_config() is not None

    def save_illumination_config(self, config: IlluminationChannelConfig) -> None:
        """Save illumination channel configuration and update cache."""
        path = self.machine_configs_path / "illumination_channel_config.yaml"
        self._save_yaml(path, config)
        self._machine_cache["illumination"] = config

    def save_confocal_config(self, config: ConfocalConfig) -> None:
        """Save confocal configuration and update cache."""
        path = self.machine_configs_path / "confocal_config.yaml"
        self._save_yaml(path, config)
        self._machine_cache["confocal"] = config

    def save_camera_mappings(self, config: CameraMappingsConfig) -> None:
        """Save camera mappings configuration and update cache."""
        path = self.machine_configs_path / "camera_mappings.yaml"
        self._save_yaml(path, config)
        self._machine_cache["camera_mappings"] = config

    # ───────────────────────────────────────────────────────────────────────────
    # v1.1 Machine Configs: Camera Registry and Filter Wheels
    # ───────────────────────────────────────────────────────────────────────────

    def get_camera_registry(self) -> Optional[CameraRegistryConfig]:
        """
        Load camera registry configuration (cached).

        Returns None if cameras.yaml doesn't exist (single-camera system or legacy config).
        """
        cache_key = "camera_registry"
        if cache_key not in self._machine_cache:
            path = self.machine_configs_path / "cameras.yaml"
            self._machine_cache[cache_key] = self._load_yaml(path, CameraRegistryConfig)
        return self._machine_cache[cache_key]

    def get_filter_wheel_registry(self) -> Optional[FilterWheelRegistryConfig]:
        """
        Load filter wheel registry configuration (cached).

        When ``machine_config.yaml`` embeds a non-empty ``filter_wheel_registry``,
        that takes precedence over ``filter_wheels.yaml``.

        Returns None if neither embedded registry nor filter_wheels.yaml provides data.
        """
        cache_key = "filter_wheel_registry"
        if cache_key not in self._machine_cache:
            mc = self.get_machine_config()
            embedded = mc.filter_wheel_registry
            if embedded is not None and embedded.filter_wheels:
                self._machine_cache[cache_key] = embedded
            else:
                path = self.machine_configs_path / "filter_wheels.yaml"
                self._machine_cache[cache_key] = self._load_yaml(path, FilterWheelRegistryConfig)
        return self._machine_cache[cache_key]

    def save_camera_registry(self, config: CameraRegistryConfig) -> None:
        """Save camera registry configuration and update cache."""
        path = self.machine_configs_path / "cameras.yaml"
        self._save_yaml(path, config)
        self._machine_cache["camera_registry"] = config

    def save_filter_wheel_registry(self, config: FilterWheelRegistryConfig) -> None:
        """Save filter wheel registry configuration and update cache."""
        path = self.machine_configs_path / "filter_wheels.yaml"
        self._save_yaml(path, config)
        self._machine_cache["filter_wheel_registry"] = config

    def get_camera_names(self) -> List[str]:
        """Get list of available camera names from registry."""
        registry = self.get_camera_registry()
        if registry:
            return registry.get_camera_names()
        return []

    def get_filter_wheel_names(self) -> List[str]:
        """Get list of available filter wheel names from registry."""
        registry = self.get_filter_wheel_registry()
        if registry:
            return registry.get_wheel_names()
        return []

    # ───────────────────────────────────────────────────────────────────────────
    # v1.1 Hardware Bindings and Filter Wheel Aggregation
    # ───────────────────────────────────────────────────────────────────────────

    def get_hardware_bindings(self) -> Optional[HardwareBindingsConfig]:
        """
        Load hardware bindings configuration (cached).

        When ``machine_config.yaml`` embeds ``hardware_bindings``, that takes
        precedence over ``hardware_bindings.yaml``.

        Returns None if neither embedded bindings nor hardware_bindings.yaml exists.
        """
        cache_key = "hardware_bindings"
        if cache_key not in self._machine_cache:
            mc = self.get_machine_config()
            if mc.hardware_bindings is not None:
                self._machine_cache[cache_key] = mc.hardware_bindings
            else:
                path = self.machine_configs_path / "hardware_bindings.yaml"
                self._machine_cache[cache_key] = self._load_yaml(path, HardwareBindingsConfig)
        return self._machine_cache[cache_key]

    def save_hardware_bindings(self, config: HardwareBindingsConfig) -> None:
        """Save hardware bindings configuration and update cache."""
        path = self.machine_configs_path / "hardware_bindings.yaml"
        self._save_yaml(path, config)
        self._machine_cache["hardware_bindings"] = config

    def get_all_filter_wheels(self) -> Dict[str, List[FilterWheelDefinition]]:
        """
        Aggregate filter wheels from all sources.

        Returns a dict mapping source name to list of wheels:
        - "standalone": wheels from embedded ``machine_config.filter_wheel_registry`` or ``filter_wheels.yaml``
        - "confocal": wheels from confocal_config.yaml

        Each source has its own ID namespace (no global conflicts).
        """
        result: Dict[str, List[FilterWheelDefinition]] = {}

        # Standalone wheels from filter_wheels.yaml
        registry = self.get_filter_wheel_registry()
        if registry and registry.filter_wheels:
            result[FILTER_WHEEL_SOURCE_STANDALONE] = list(registry.filter_wheels)

        # Confocal wheels from confocal_config.yaml
        confocal = self.get_confocal_config()
        if confocal and confocal.filter_wheels:
            result[FILTER_WHEEL_SOURCE_CONFOCAL] = list(confocal.filter_wheels)

        return result

    def get_emission_wheels(self) -> Dict[str, List[FilterWheelDefinition]]:
        """
        Get all emission filter wheels, grouped by source.

        Returns dict: source -> list of emission wheels
        """
        all_wheels = self.get_all_filter_wheels()
        return {
            source: [w for w in wheels if w.type == FilterWheelType.EMISSION]
            for source, wheels in all_wheels.items()
            if any(w.type == FilterWheelType.EMISSION for w in wheels)
        }

    def get_excitation_wheels(self) -> Dict[str, List[FilterWheelDefinition]]:
        """
        Get all excitation filter wheels, grouped by source.

        Returns dict: source -> list of excitation wheels
        """
        all_wheels = self.get_all_filter_wheels()
        return {
            source: [w for w in wheels if w.type == FilterWheelType.EXCITATION]
            for source, wheels in all_wheels.items()
            if any(w.type == FilterWheelType.EXCITATION for w in wheels)
        }

    def resolve_wheel_reference(self, ref: FilterWheelReference) -> Optional[FilterWheelDefinition]:
        """
        Resolve a source-qualified reference to a wheel definition.

        Args:
            ref: FilterWheelReference with source and id/name

        Returns:
            FilterWheelDefinition if found, None otherwise
        """
        all_wheels = self.get_all_filter_wheels()
        source_wheels = all_wheels.get(ref.source.value, [])

        for wheel in source_wheels:
            if ref.id is not None and wheel.id == ref.id:
                return wheel
            if ref.name is not None and wheel.name == ref.name:
                return wheel

        available_info = (
            f"Available in '{ref.source.value}': {[w.name for w in source_wheels]}"
            if source_wheels
            else f"No wheels found in source '{ref.source.value}'"
        )
        logger.warning(
            f"Filter wheel reference not found: {ref}. {available_info}. "
            f"Check that hardware_bindings.yaml references match your "
            f"filter_wheels.yaml or confocal.yaml."
        )
        return None

    def get_effective_emission_wheel(self, camera_id: int) -> Optional[FilterWheelDefinition]:
        """
        Get emission wheel for a camera, using explicit or implicit binding.

        Resolution order:
        1. Explicit binding from hardware_bindings.yaml
        2. Implicit binding: if exactly 1 camera and 1 emission wheel

        For implicit binding, a missing cameras.yaml is treated as a single-camera
        system (legacy/default mode).

        Args:
            camera_id: Camera ID

        Returns:
            FilterWheelDefinition if binding exists, None otherwise
        """
        # Try explicit binding first
        bindings = self.get_hardware_bindings()
        if bindings:
            ref = bindings.get_emission_wheel_ref(camera_id)
            if ref:
                return self.resolve_wheel_reference(ref)
            # Explicit file exists but no binding for this camera
            return None

        # No explicit bindings file - try implicit binding
        emission_wheels = self.get_emission_wheels()
        all_emission = [w for wheels in emission_wheels.values() for w in wheels]

        cameras = self.get_camera_registry()
        # Treat missing cameras.yaml as single-camera system (legacy/default mode)
        camera_count = len(cameras.cameras) if cameras else 1

        # Implicit binding only for single camera + single emission wheel
        if camera_count == 1 and len(all_emission) == 1:
            return all_emission[0]

        return None

    def ensure_machine_configs_directory(self) -> None:
        """Create machine_configs directory if it doesn't exist."""
        self.machine_configs_path.mkdir(parents=True, exist_ok=True)
        (self.machine_configs_path / "intensity_calibrations").mkdir(exist_ok=True)

    # ═══════════════════════════════════════════════════════════════════════════
    # CHANNEL CONFIGS (per-profile)
    # Core CRUD operations for acquisition channel settings
    # ═══════════════════════════════════════════════════════════════════════════

    def get_observation_state(self, profile: Optional[str] = None) -> Optional[ObservationState]:
        """Load the observation state from general.yaml (cached when using current profile)."""
        if profile is None or profile == self._current_profile:
            cache_key = "general"
            if cache_key not in self._profile_cache:
                profile_path = self._get_profile_path()
                path = profile_path / "channel_configs" / "general.yaml"
                self._profile_cache[cache_key] = self._load_yaml(path, ObservationState)
            return self._profile_cache[cache_key]
        else:
            path = self.user_profiles_path / profile / "channel_configs" / "general.yaml"
            return self._load_yaml(path, ObservationState)

    def save_observation_state(self, profile: str, state: ObservationState) -> None:
        """Save the observation state to general.yaml and update cache."""
        if profile == self._current_profile:
            profile_path = self._get_profile_path()
            path = profile_path / "channel_configs" / "general.yaml"
            self._save_yaml(path, state)
            self._profile_cache["general"] = state
        else:
            path = self.user_profiles_path / profile / "channel_configs" / "general.yaml"
            self._save_yaml(path, state)

    def get_general_config(self, profile: Optional[str] = None) -> Optional[ObservationState]:
        """Alias for get_observation_state(). Returns the single ObservationState from general.yaml."""
        return self.get_observation_state(profile)

    def save_general_config(self, profile: str, state: ObservationState) -> None:
        """Alias for save_observation_state()."""
        self.save_observation_state(profile, state)

    def update_channel_setting(
        self,
        setting: str,
        value: Any,
        profile: Optional[str] = None,
    ) -> bool:
        """
        Update a specific setting of the observation state in-memory.

        Changes are held in the cached ObservationState; flush to general.yaml
        is driven by the GUI's periodic cache timer and shutdown hook (see
        ``ObservationStateController.cache_current_state_to_disk``).

        Supported settings:
        - "ExposureTime" -> camera_settings.exposure_time_ms
        - "AnalogGain" -> camera_settings.gain_mode
        - "IlluminationIris" -> confocal_hardware_settings.illumination_iris
        - "EmissionIris" -> confocal_hardware_settings.emission_iris

        Args:
            setting: Setting name (see supported settings above)
            value: New value for the setting
            profile: Profile name (defaults to current profile)

        Returns:
            True if update was successful, False otherwise
        """
        profile = profile or self._current_profile
        if not profile:
            logger.warning("Cannot update: no profile set")
            return False

        setting_mapping = {
            "ExposureTime": ("camera", "exposure_time_ms"),
            "AnalogGain": ("camera", "gain_mode"),
            "IlluminationIris": ("confocal_hw", "illumination_iris"),
            "EmissionIris": ("confocal_hw", "emission_iris"),
        }

        if setting not in setting_mapping:
            logger.warning(f"Unknown setting: {setting}")
            return False

        location, field = setting_mapping[setting]

        state = self.get_observation_state(profile)
        if state is None:
            logger.warning("No observation state found in general.yaml")
            return False

        if location == "confocal_hw":
            if state.confocal_hardware_settings is None:
                from control.default_config_generator import build_confocal_settings_from_config

                state.confocal_hardware_settings = build_confocal_settings_from_config(self.get_confocal_config())
            setattr(state.confocal_hardware_settings, field, value)
        elif location == "camera":
            if state.camera_settings is None:
                state.camera_settings = CameraSettings(
                    exposure_time_ms=10.0,
                    gain_mode=0.0,
                )
            setattr(state.camera_settings, field, value)

        return True

    def get_last_active_channel_name(self) -> Optional[str]:
        """Read the channel name that was active when the app last shut down."""
        try:
            path = self._get_profile_path() / "channel_configs" / "last_active_channel.txt"
            if path.exists():
                name = path.read_text().strip()
                return name if name else None
        except Exception:
            pass
        return None

    # ═══════════════════════════════════════════════════════════════════════════
    # LASER AF CONFIGS (per-profile)
    # Laser autofocus settings per objective
    # ═══════════════════════════════════════════════════════════════════════════

    def get_laser_af_config(self, objective: str, profile: Optional[str] = None) -> Optional[LaserAFConfig]:
        """Load laser AF configuration for an objective (cached when using current profile)."""
        if profile is None or profile == self._current_profile:
            cache_key = f"laser_af:{objective}"
            if cache_key not in self._profile_cache:
                profile_path = self._get_profile_path()
                path = profile_path / "laser_af_configs" / f"{objective}.yaml"
                self._profile_cache[cache_key] = self._load_yaml(path, LaserAFConfig)
            return self._profile_cache[cache_key]
        else:
            # Explicit profile - load directly without caching
            path = self.user_profiles_path / profile / "laser_af_configs" / f"{objective}.yaml"
            return self._load_yaml(path, LaserAFConfig)

    def save_laser_af_config(self, profile: str, objective: str, config: LaserAFConfig) -> None:
        """Save laser AF configuration and update cache if current profile."""
        if profile == self._current_profile:
            profile_path = self._get_profile_path()
            path = profile_path / "laser_af_configs" / f"{objective}.yaml"
            self._save_yaml(path, config)
            self._profile_cache[f"laser_af:{objective}"] = config
        else:
            # Different profile - save without caching
            path = self.user_profiles_path / profile / "laser_af_configs" / f"{objective}.yaml"
            self._save_yaml(path, config)

    # ═══════════════════════════════════════════════════════════════════════════
    # ACQUISITION OUTPUT
    # Saving acquisition settings to experiment directories
    # ═══════════════════════════════════════════════════════════════════════════

    def save_acquisition_output(
        self,
        output_dir: Union[Path, str],
        objective: str,
        observation_state_names: Optional[List[str]] = None,
        confocal_mode: bool = False,
    ) -> None:
        """
        Save acquisition settings to an experiment output directory.

        Creates acquisition_channels.yaml in the output directory to record
        what settings were used during acquisition. This is separate from
        profile configs - it's a snapshot of settings used for a specific run.

        Args:
            output_dir: Experiment output directory
            objective: Objective used for acquisition
            observation_state_names: Names of observation states used
            confocal_mode: Whether confocal mode was active
        """
        output_config = AcquisitionOutputConfig(
            objective=objective,
            confocal_mode=confocal_mode,
            observation_state_names=observation_state_names or [],
        )
        output_path = Path(output_dir) / "acquisition_channels.yaml"
        self._save_yaml(output_path, output_config)

    def save_acquisition_metadata(
        self,
        output_dir: Union[Path, str],
        metadata: AcquisitionMetadata,
        *,
        filename: Optional[str] = None,
    ) -> Path:
        """
        Write Acquisition Metadata manifest to an experiment directory.

        Creates ``acquisition_metadata.yaml`` alongside legacy sidecars unless
        ``filename`` is given (e.g. per-snap sidecar next to a TIFF).
        """
        name = filename if filename else "acquisition_metadata.yaml"
        output_path = Path(output_dir) / name
        self._save_yaml(output_path, metadata)
        return output_path

    # ═══════════════════════════════════════════════════════════════════════════
    # OBSERVATION STATE (objective-free presets)
    # ═══════════════════════════════════════════════════════════════════════════

    def list_observation_presets(self, profile: Optional[str] = None) -> List[str]:
        """
        List saved Observation State preset names (without ``.yaml``) for a profile.

        Args:
            profile: Profile name (defaults to current profile)
        """
        profile = profile or self._current_profile
        if profile is None:
            return []
        presets_dir = self.user_profiles_path / profile / "observation_presets"
        if not presets_dir.is_dir():
            return []
        names: List[str] = []
        for p in sorted(presets_dir.glob("*.yaml")):
            names.append(p.stem)
        return names

    def save_observation_preset(self, name: str, state: ObservationState, profile: Optional[str] = None) -> Path:
        """
        Save an Observation State preset under ``user_profiles/{profile}/observation_presets/``.

        Args:
            name: Display name (sanitized to a file stem)
            state: Objective-free Observation State
            profile: Profile name (defaults to current profile)

        Returns:
            Path to the written YAML file
        """
        from control.core.observation_state_service import observation_preset_path, sanitize_preset_filename

        profile = profile or self._current_profile
        if profile is None:
            raise ValueError("No profile set. Call set_profile() or pass profile= explicitly.")
        safe_name = sanitize_preset_filename(name)
        state_to_save = state.model_copy(update={"name": safe_name})
        path = observation_preset_path(self, name, profile=profile)
        from control.core.observation_state_service import observation_state_to_yaml

        # Save the cleaned v3 "view" YAML (not the internal model dump).
        view = observation_state_to_yaml(state_to_save, camera_label="camera")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(
                    view,
                    f,
                    Dumper=_YamlDumper,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
        except (OSError, yaml.YAMLError) as e:
            logger.error("Failed to save Observation State preset YAML '%s': %s", path, e)
            raise
        return path

    def load_observation_preset(self, name: str, profile: Optional[str] = None) -> Optional[ObservationState]:
        """
        Load a named Observation State preset from the profile.

        Only supports v3 format (illuminator_states). Returns None if missing or invalid.
        """
        from control.core.observation_state_service import observation_preset_path
        from control.models.observation_state import IlluminatorState

        profile = profile or self._current_profile
        if profile is None:
            return None
        path = observation_preset_path(self, name, profile=profile)
        if not path.exists():
            logger.debug("observation state not found: %s", path)
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.load(f, Loader=_YamlSafeLoader)
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise ValueError("observation state YAML did not parse into a dict")

            version = data.get("version", 2)
            if version != 3:
                raise ValueError(f"Unsupported Observation State preset version: {version!r} (only v3 supported)")

            loaded_name = data.get("name", "live")
            if not isinstance(loaded_name, str) or not loaded_name:
                loaded_name = "live"

            confocal_mode = bool(data.get("confocal_mode", False))

            # Parse camera_states block (shared between v2 and v3)
            camera_states = data.get("camera_states") or {}
            if isinstance(camera_states, dict) and camera_states:
                _, cam_state = next(iter(camera_states.items()))
                if not isinstance(cam_state, dict):
                    cam_state = {}
            else:
                cam_state = {}

            camera_live_dict = cam_state.get("camera_live")
            camera_live = (
                CameraLiveSnapshot.model_validate(camera_live_dict) if isinstance(camera_live_dict, dict) else None
            )

            camera_settings_dict = cam_state.get("camera_settings")
            camera_settings = (
                CameraSettings.model_validate(camera_settings_dict) if isinstance(camera_settings_dict, dict) else None
            )

            z_offset_um = float(cam_state.get("z_offset_um", 0.0))
            emission_filter_positions = cam_state.get("emission_filter_positions") or {}
            if not isinstance(emission_filter_positions, dict):
                emission_filter_positions = {}

            enable_auto_filter = data.get("enable_channel_auto_filter_switching")
            enable_auto_filter = bool(enable_auto_filter) if enable_auto_filter is not None else None

            display_color = data.get("display_color", "#FFFFFF")

            confocal_hw_dict = data.get("confocal_hardware_settings")
            confocal_hw = (
                ConfocalSettings.model_validate(confocal_hw_dict) if isinstance(confocal_hw_dict, dict) else None
            )

            general = self.get_general_config(profile)
            channel_groups = list(general.channel_groups) if general else []

            # ── v3 format: illuminator_states directly ──
            illuminator_states: List[IlluminatorState] = []
            for ist_dict in data.get("illuminator_states") or []:
                if isinstance(ist_dict, dict) and "illumination_channel" in ist_dict:
                    illuminator_states.append(IlluminatorState.model_validate(ist_dict))
            if not illuminator_states:
                raise ValueError("v3 observation state had no valid illuminator_states")
            return ObservationState(
                version=3,
                name=loaded_name,
                confocal_mode=confocal_mode,
                camera_settings=camera_settings,
                camera_live=camera_live,
                illuminator_states=illuminator_states,
                emission_filter_positions=dict(emission_filter_positions),
                z_offset_um=z_offset_um,
                confocal_hardware_settings=confocal_hw,
                display_color=display_color,
                channel_groups=channel_groups,
                enable_channel_auto_filter_switching=enable_auto_filter,
            )
        except (yaml.YAMLError, ValidationError, OSError, ValueError, TypeError) as e:
            logger.warning("Failed to load Observation State preset %s: %s", path, e)
            return None

    # ═══════════════════════════════════════════════════════════════════════════
    # CACHE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════════

    def clear_profile_cache(self) -> None:
        """Clear profile cache (called on profile switch)."""
        self._profile_cache.clear()

    def clear_all_cache(self) -> None:
        """Clear all caches (rarely needed)."""
        self._machine_cache.clear()
        self._profile_cache.clear()
