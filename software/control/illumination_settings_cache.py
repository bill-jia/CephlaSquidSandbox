"""Illumination logical state persistence across sessions.

Caches per-channel intensity and on/off, plus optional unified LED matrix mode.
Hardware is not asserted on load; use :meth:`IlluminationController.restore` with
``force_hardware=False`` and live streaming gate until the user starts live view.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

import squid.logging
from control.lighting import ChannelState, IlluminationSnapshot

_log = squid.logging.get_logger(__name__)

_DEFAULT_CACHE_PATH = Path("cache/illumination_settings.yaml")


@dataclass(frozen=True)
class CachedIlluminationSettings:
    """Data loaded from the illumination YAML cache."""

    snapshot: IlluminationSnapshot
    led_matrix_mode: Optional[str] = None
    led_matrix_na: Optional[float] = None


def save_illumination_settings(controller: Any, cache_path: Path = _DEFAULT_CACHE_PATH) -> None:
    """Write illumination snapshot and optional LED matrix mode to YAML."""
    try:
        snap = controller.snapshot()
    except Exception as e:
        _log.error("Cannot snapshot illumination settings: %s", e)
        return

    channels: Dict[str, Dict[str, Any]] = {}
    for name, st in snap.channel_states.items():
        channels[name] = {"intensity": float(st.intensity), "is_on": bool(st.is_on)}

    data: Dict[str, Any] = {"channels": channels}
    try:
        if hasattr(controller, "get_led_matrix_mode") and callable(controller.get_led_matrix_mode):
            mk = controller.get_led_matrix_mode()
            if mk is not None:
                data["led_matrix_mode"] = mk
        if hasattr(controller, "get_led_matrix_array_na") and callable(controller.get_led_matrix_array_na):
            na = controller.get_led_matrix_array_na()
            if na is not None:
                data["led_matrix_na"] = float(na)
    except Exception:
        pass

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True)
        _log.info("Illumination settings saved to %s", cache_path)
    except (OSError, PermissionError) as e:
        _log.error("Cannot save illumination settings cache: %s", e)


def load_illumination_settings(cache_path: Path = _DEFAULT_CACHE_PATH) -> Optional[CachedIlluminationSettings]:
    """Load cached illumination state. Returns None if missing or invalid."""
    if not cache_path.exists():
        _log.debug("No illumination settings cache at %s", cache_path)
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        _log.error("Illumination cache corrupted at %s: %s", cache_path, e)
        return None
    except OSError as e:
        _log.error("Cannot read illumination cache: %s", e)
        return None

    if not raw or not isinstance(raw, dict):
        return None

    ch_data = raw.get("channels")
    if not isinstance(ch_data, dict):
        return None

    states: Dict[str, ChannelState] = {}
    for name, v in ch_data.items():
        if not isinstance(v, dict):
            continue
        try:
            states[name] = ChannelState(
                intensity=float(v.get("intensity", 0)),
                is_on=bool(v.get("is_on", False)),
            )
        except (TypeError, ValueError):
            continue

    if not states:
        return None

    lm = raw.get("led_matrix_mode")
    led_matrix_mode = str(lm) if lm is not None else None

    na_raw = raw.get("led_matrix_na")
    try:
        led_matrix_na = float(na_raw) if na_raw is not None else None
    except (TypeError, ValueError):
        led_matrix_na = None

    return CachedIlluminationSettings(
        snapshot=IlluminationSnapshot(states),
        led_matrix_mode=led_matrix_mode,
        led_matrix_na=led_matrix_na,
    )
