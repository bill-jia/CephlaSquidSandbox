"""Camera settings persistence for session continuity.

This module provides save/load functionality for camera settings (binning plus either
camera mode or pixel format) to maintain user preferences across application restarts.
Settings are stored as YAML in the cache directory.

Typical usage:
    # On application close
    save_camera_settings(camera)

    # On application startup
    settings = load_camera_settings()
    if settings:
        camera.set_binning(*settings.binning)
        # Prefer restoring camera mode when supported, fall back to pixel format.
        if settings.camera_mode is not None and hasattr(camera, "set_camera_mode"):
            camera.set_camera_mode(settings.camera_mode)
        elif settings.pixel_format is not None and hasattr(camera, "set_pixel_format"):
            from squid.config import CameraPixelFormat

            camera.set_pixel_format(CameraPixelFormat.from_string(settings.pixel_format))
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import yaml

import squid.logging
from squid.abc import AbstractCamera

_log = squid.logging.get_logger(__name__)

_DEFAULT_CACHE_PATH = Path("cache/camera_settings.yaml")
DEFAULT_BINNING: Tuple[int, int] = (1, 1)


@dataclass(frozen=True)
class CachedCameraSettings:
    """Container for cached camera settings loaded from disk.

    Attributes:
        binning: Tuple of (x, y) binning factors. Must be positive integers.
        camera_mode: String name of a camera-specific mode (if supported by the
            driver), or None if not cached.
        pixel_format: String representation of CameraPixelFormat enum value,
            or None if not cached. Retained for backwards compatibility and for
            cameras that still expose only a pixel-format API.
    """

    binning: Tuple[int, int]
    camera_mode: Optional[str]
    pixel_format: Optional[str]

    def __post_init__(self):
        if len(self.binning) != 2:
            raise ValueError(f"Binning must be a 2-tuple, got {self.binning}")
        if self.binning[0] < 1 or self.binning[1] < 1:
            raise ValueError(f"Binning values must be positive, got {self.binning}")


def save_camera_settings(camera: AbstractCamera, cache_path: Path = _DEFAULT_CACHE_PATH) -> None:
    """Save current camera settings (binning and camera mode / pixel format) to a YAML cache file.

    Creates parent directories if they do not exist. This function is fail-safe -
    errors are logged but do not raise exceptions, allowing application shutdown
    to continue.

    Args:
        camera: Camera instance to read settings from.
        cache_path: Path to the cache file. Defaults to 'cache/camera_settings.yaml'
            relative to the current working directory.
    """
    try:
        binning = camera.get_binning()
    except (AttributeError, RuntimeError) as e:
        _log.error(f"Cannot read camera binning - camera may be disconnected: {e}")
        return

    # Prefer a high-level camera mode when the driver exposes it; fall back to
    # pixel format for legacy drivers.
    camera_mode: Optional[str] = None
    try:
        if hasattr(camera, "get_camera_mode"):
            camera_mode = camera.get_camera_mode()  # type: ignore[attr-defined]
    except (AttributeError, RuntimeError, NotImplementedError) as e:
        _log.debug(f"Camera does not expose get_camera_mode or it failed: {e}")

    pixel_format_str: Optional[str] = None
    try:
        if hasattr(camera, "get_pixel_format"):
            pixel_format = camera.get_pixel_format()  # type: ignore[attr-defined]
            pixel_format_str = pixel_format.value if pixel_format else None
    except (AttributeError, RuntimeError, NotImplementedError) as e:
        _log.debug(f"Camera does not expose get_pixel_format or it failed: {e}")

    settings = {
        "binning": list(binning),
        "camera_mode": camera_mode,
        "pixel_format": pixel_format_str,
    }

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            yaml.safe_dump(settings, f, default_flow_style=False)
        _log.info(
            f"Camera settings saved: binning={binning}, "
            f"camera_mode={camera_mode}, pixel_format={pixel_format_str}"
        )
    except PermissionError as e:
        _log.error(f"Cannot save camera settings - permission denied for {cache_path}: {e}")
    except OSError as e:
        _log.error(f"Cannot save camera settings - file system error: {e}")


def load_camera_settings(cache_path: Path = _DEFAULT_CACHE_PATH) -> Optional[CachedCameraSettings]:
    """Load cached camera settings from a YAML cache file.

    This function is fail-safe - returns None on any error condition.

    Args:
        cache_path: Path to the cache file. Defaults to 'cache/camera_settings.yaml'
            relative to the current working directory.

    Returns:
        CachedCameraSettings if the file exists and contains valid data, None otherwise.
        Returns None if the file doesn't exist (expected on first run).
    """
    if not cache_path.exists():
        _log.debug("No camera settings cache file found - using defaults")
        return None

    try:
        with open(cache_path, "r") as f:
            settings = yaml.safe_load(f)
    except yaml.YAMLError as e:
        _log.error(
            f"Camera settings cache file is corrupted at {cache_path}: {e}. Delete this file to reset to defaults."
        )
        return None
    except PermissionError as e:
        _log.error(f"Cannot read camera settings cache - permission denied: {e}")
        return None
    except OSError as e:
        _log.error(f"Cannot read camera settings cache - file system error: {e}")
        return None

    try:
        binning_raw = settings.get("binning")
        if not isinstance(binning_raw, list) or len(binning_raw) != 2:
            if binning_raw is not None:
                _log.warning(f"Invalid binning format in cache: {binning_raw} - using default")
            else:
                _log.warning("Camera settings cache missing 'binning' key - using default")
            binning_raw = list(DEFAULT_BINNING)

        return CachedCameraSettings(
            binning=(int(binning_raw[0]), int(binning_raw[1])),
            camera_mode=settings.get("camera_mode"),
            pixel_format=settings.get("pixel_format"),
        )
    except (TypeError, ValueError) as e:
        _log.error(f"Camera settings cache contains invalid data: {e}")
        return None
