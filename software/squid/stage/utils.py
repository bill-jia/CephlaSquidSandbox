from typing import Optional, Callable
import os

import squid.logging
from squid.abc import Pos, AbstractStage
from squid.config import StageConfig
import control._def as _def
import control.utils

_log = squid.logging.get_logger(__package__)
_DEFAULT_CACHE_PATH = "cache/last_coords.txt"
# After blocking XY moves, wait for the controller to report idle before changing Z (startup restore).
_STARTUP_XY_IDLE_TIMEOUT_S = 120.0

"""
Attempts to load a cached stage position and return it.
"""


def get_cached_position(
    cache_path: str = _DEFAULT_CACHE_PATH,
    stage_config: Optional[StageConfig] = None,
) -> Optional[Pos]:
    """Load cached stage position and return it as a canonical-frame ``Pos``.

    The cache file holds **raw** mm values (frame-stable across canonical
    config changes).  When ``stage_config`` is provided we convert raw → canonical
    via ``AxisConfig.raw_to_canonical``; with the default identity canonical
    config this is a no-op and matches pre-refactor behavior.
    """
    if not os.path.isfile(cache_path):
        _log.debug(f"Cache file '{cache_path}' not found, no cached pos found.")
        return None
    with open(cache_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                parts = line.split(",")
                if len(parts) != 3:
                    raise ValueError(f"expected 3 comma-separated fields, got {len(parts)}")
                x_raw, y_raw, z_raw = (float(p) for p in parts)
                if stage_config is None:
                    return Pos(x_mm=x_raw, y_mm=y_raw, z_mm=z_raw, theta_rad=None)
                return Pos(
                    x_mm=stage_config.X_AXIS.raw_to_canonical(x_raw),
                    y_mm=stage_config.Y_AXIS.raw_to_canonical(y_raw),
                    z_mm=stage_config.Z_AXIS.raw_to_canonical(z_raw),
                    theta_rad=None,
                )
            except ValueError as e:
                _log.warning(f"Skipping invalid cached position line {line!r}: {e}")
    return None


def clamp_pos_to_stage_limits(pos: Pos, stage_config: Optional[StageConfig]) -> Pos:
    """Clamp a canonical-frame ``Pos`` against the canonical-projected stage limits.

    ``AxisConfig.MIN_POSITION`` / ``MAX_POSITION`` are raw-frame (they gate the
    MCU).  We project them through ``raw_to_canonical`` and sort, because a
    ``CANONICAL_SIGN=-1`` flips which raw bound is the canonical lower/upper
    bound.
    """
    if stage_config is None:
        return pos

    def _clamp(axis_cfg, val: float) -> float:
        lo, hi = sorted([
            axis_cfg.raw_to_canonical(axis_cfg.MIN_POSITION),
            axis_cfg.raw_to_canonical(axis_cfg.MAX_POSITION),
        ])
        return min(max(val, lo), hi)

    return Pos(
        x_mm=_clamp(stage_config.X_AXIS, pos.x_mm),
        y_mm=_clamp(stage_config.Y_AXIS, pos.y_mm),
        z_mm=_clamp(stage_config.Z_AXIS, pos.z_mm),
        theta_rad=pos.theta_rad,
    )


def move_to_cached_or_default_startup_position(
    stage: AbstractStage, stage_config: Optional[StageConfig] = None, cache_path: Optional[str] = None
) -> None:
    """After XY homing, restore last cached XYZ if present; otherwise move to configured defaults.

    Applies target X and Y first; Z (cached, safety, or default) only after both axes finish.

    ``STARTUP_DEFAULT_STAGE_{X,Y,Z}_MM`` and ``Z_HOME_SAFETY_POINT`` are raw-frame
    constants (physical, motor-direction) — we project them into canonical via
    ``AxisConfig.raw_to_canonical`` before handing them to the Stage API so the
    physical position is unchanged whether the rig runs with the default identity
    canonical frame or a flipped one.
    """
    cfg = stage_config if stage_config is not None else stage.get_config()
    cached = get_cached_position(
        cache_path if cache_path is not None else _DEFAULT_CACHE_PATH,
        stage_config=cfg,
    )
    safety_z_canonical_mm = cfg.Z_AXIS.raw_to_canonical(int(_def.Z_HOME_SAFETY_POINT) / 1000.0)
    if cached is not None:
        cached = clamp_pos_to_stage_limits(cached, cfg)
        _log.info(f"Restoring cached position (canonical): ({cached.x_mm},{cached.y_mm},{cached.z_mm}) [mm]")
        stage.move_x_to(cached.x_mm)
        stage.wait_for_idle(_STARTUP_XY_IDLE_TIMEOUT_S)
        stage.move_y_to(cached.y_mm)
        stage.wait_for_idle(_STARTUP_XY_IDLE_TIMEOUT_S)
        # Compare along the raw Z direction so "at or below" stays meaningful
        # regardless of Z canonical sign.
        cached_z_raw = cfg.Z_AXIS.canonical_to_raw(cached.z_mm)
        if int(_def.Z_HOME_SAFETY_POINT) / 1000.0 < cached_z_raw:
            _log.info("XY at cached targets; moving Z to cached z.")
            stage.move_z_to(cached.z_mm)
        else:
            _log.info("Cached z is at or below Z_HOME_SAFETY_POINT; moving Z to Z_HOME_SAFETY_POINT after XY.")
            stage.move_z_to(safety_z_canonical_mm)
    else:
        default = Pos(
            x_mm=cfg.X_AXIS.raw_to_canonical(_def.STARTUP_DEFAULT_STAGE_X_MM),
            y_mm=cfg.Y_AXIS.raw_to_canonical(_def.STARTUP_DEFAULT_STAGE_Y_MM),
            z_mm=cfg.Z_AXIS.raw_to_canonical(_def.STARTUP_DEFAULT_STAGE_Z_MM),
            theta_rad=None,
        )
        default = clamp_pos_to_stage_limits(default, cfg)
        _log.info(
            f"No valid cached position; moving to default startup position (canonical) "
            f"({default.x_mm},{default.y_mm},{default.z_mm}) [mm]"
        )
        stage.move_x_to(default.x_mm)
        stage.wait_for_idle(_STARTUP_XY_IDLE_TIMEOUT_S)
        stage.move_y_to(default.y_mm)
        stage.wait_for_idle(_STARTUP_XY_IDLE_TIMEOUT_S)
        _log.info("XY at default targets; moving Z to default z.")
        stage.move_z_to(default.z_mm)


"""
Write out the current x, y, z position, in mm, so we can use it later as a cached position.
"""


def cache_position(pos: Pos, stage_config: StageConfig, cache_path=_DEFAULT_CACHE_PATH):
    """Persist ``pos`` (canonical-frame) as raw-mm coordinates on disk.

    Serializing raw mm keeps the on-disk cache stable across changes to a rig's
    canonical frame — an old cache from a pre-refactor rig is still interpretable
    after canonical yaml fields land, and toggling the flip doesn't strand the
    cache either.  Bounds are checked in raw space against the raw
    ``MIN_POSITION`` / ``MAX_POSITION`` limits.
    """
    if stage_config is None:
        # StageConfig is not implemented for the Prior stage — fall back to
        # persisting pos.x_mm directly (it's effectively raw in that path too).
        with open(cache_path, "w") as f:
            _log.debug(f"Writing position={pos} (no stage_config) to cache path='{cache_path}'")
            f.write(",".join([str(pos.x_mm), str(pos.y_mm), str(pos.z_mm)]))
        return

    x_raw = stage_config.X_AXIS.canonical_to_raw(pos.x_mm)
    y_raw = stage_config.Y_AXIS.canonical_to_raw(pos.y_mm)
    z_raw = stage_config.Z_AXIS.canonical_to_raw(pos.z_mm)

    x_min = stage_config.X_AXIS.MIN_POSITION
    x_max = stage_config.X_AXIS.MAX_POSITION
    y_min = stage_config.Y_AXIS.MIN_POSITION
    y_max = stage_config.Y_AXIS.MAX_POSITION
    z_min = stage_config.Z_AXIS.MIN_POSITION
    z_max = stage_config.Z_AXIS.MAX_POSITION
    if not (x_min <= x_raw <= x_max and y_min <= y_raw <= y_max and z_min <= z_raw <= z_max):
        raise ValueError(
            f"Position {pos} (raw {(x_raw, y_raw, z_raw)}) is not cacheable because it is outside of the "
            f"min/max of at least one axis. raw x_range=({x_min}, {x_max}), y_range=({y_min}, {y_max}), "
            f"z_range=({z_min}, {z_max})"
        )
    with open(cache_path, "w") as f:
        _log.debug(f"Writing raw position=({x_raw},{y_raw},{z_raw}) to cache path='{cache_path}'")
        f.write(",".join([str(x_raw), str(y_raw), str(z_raw)]))


def _move_to_loading_position_impl(stage: AbstractStage, is_wellplate: bool):
    # Set our limits to something large.  Then later reset them back to the safe values.
    if is_wellplate:
        a_large_limit_mm = 125
        stage.set_limits(
            x_pos_mm=a_large_limit_mm,
            x_neg_mm=-a_large_limit_mm,
            y_pos_mm=a_large_limit_mm,
            y_neg_mm=-a_large_limit_mm,
        )

        stage._scanning_position_z_mm = stage.get_pos().z_mm
        stage.move_z_to(_def.OBJECTIVE_RETRACTED_POS_MM)
        stage.wait_for_idle(_def.SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)

        # TODO: These values should not be hardcoded as we have stages with different blocks
        # for opening the clamp. I'm not sure why exactly this piece is designed this way and
        # how to name the variable properly. Right now they should work for all our stages.
        stage.move_y_to(15)
        stage.move_x_to(35)
        stage.move_y_to(_def.SLIDE_POSITION.LOADING_Y_MM)
        stage.move_x_to(_def.SLIDE_POSITION.LOADING_X_MM)

        stage.set_limits(
            x_pos_mm=stage.get_config().X_AXIS.MAX_POSITION,
            x_neg_mm=stage.get_config().X_AXIS.MIN_POSITION,
            y_pos_mm=stage.get_config().Y_AXIS.MAX_POSITION,
            y_neg_mm=stage.get_config().Y_AXIS.MIN_POSITION,
        )
    else:
        stage.move_y_to(_def.SLIDE_POSITION.LOADING_Y_MM)
        stage.move_x_to(_def.SLIDE_POSITION.LOADING_X_MM)


def _move_to_scanning_position_impl(stage: AbstractStage, is_wellplate: bool):
    if is_wellplate:
        stage.move_x_to(_def.SLIDE_POSITION.SCANNING_X_MM)
        stage.move_y_to(_def.SLIDE_POSITION.SCANNING_Y_MM)
        if stage._scanning_position_z_mm is not None:
            stage.move_z_to(stage._scanning_position_z_mm)
        stage._scanning_position_z_mm = None
    else:
        stage.move_y_to(_def.SLIDE_POSITION.SCANNING_Y_MM)
        stage.move_x_to(_def.SLIDE_POSITION.SCANNING_X_MM)


def move_to_loading_position(
    stage: AbstractStage,
    blocking: bool = True,
    callback: Optional[Callable[[bool, Optional[str]], None]] = None,
    is_wellplate: bool = True,
):
    """Move the stage to loading position so it is clear for loading a sample.
    Args:
        blocking: If True, wait for the move to complete before returning.
                    If False, return immediately and run the operation in a separate thread. callback will be called when done.
        callback: Optional callback function called when movement completes.
                    Receives (success: bool, error_message: Optional[str])
        **kwargs: Additional arguments to pass to the operation.
    Returns:
        threading.Thread: The thread handling the movement. None if blocking is True.
    """
    if blocking and callback:
        raise ValueError("Callback is not supported when blocking is True")
    if blocking:
        _log.info(f"Moving to loading position. Blocking is True.")
        _move_to_loading_position_impl(stage, is_wellplate)
        _log.info("Successfully moved to loading position")
    else:
        return control.utils.threaded_operation_helper(
            _move_to_loading_position_impl, callback, stage=stage, is_wellplate=is_wellplate
        )


def move_to_scanning_position(
    stage: AbstractStage,
    blocking: bool = True,
    callback: Optional[Callable[[bool, Optional[str]], None]] = None,
    is_wellplate: bool = True,
):
    """Move the stage back to scanning position from loading position.
    Args:
        blocking: If True, wait for the move to complete before returning.
                    If False, return immediately and run the operation in a separate thread. callback will be called when done.
        callback: Optional callback function called when movement completes.
                    Receives (success: bool, error_message: Optional[str])
        **kwargs: Additional arguments to pass to the operation.
    Returns:
        threading.Thread: The thread handling the movement. None if blocking is True.
    """
    if blocking and callback:
        raise ValueError("Callback is not supported when blocking is True")
    if blocking:
        _log.info(f"Moving to scanning position. Blocking is True.")
        _move_to_scanning_position_impl(stage, is_wellplate)
        _log.info("Successfully moved to scanning position")
    else:
        return control.utils.threaded_operation_helper(
            _move_to_scanning_position_impl, callback, stage=stage, is_wellplate=is_wellplate
        )


def move_z_axis_to_safety_position(stage: AbstractStage):
    safety_z_raw_mm = int(_def.Z_HOME_SAFETY_POINT) / 1000.0
    stage.move_z_to(stage.get_config().Z_AXIS.raw_to_canonical(safety_z_raw_mm))
