import os
import pytest
import tempfile

import squid.stage.cephla
import squid.stage.prior
import squid.stage.utils
import squid.config
import squid.abc
from tests.control.test_microcontroller import get_test_micro


def test_create_simulated_stages():
    microcontroller = get_test_micro()
    cephla_stage = squid.stage.cephla.CephlaStage(microcontroller, squid.config.get_stage_config())


def test_simulated_cephla_stage_ops():
    microcontroller = get_test_micro()
    stage: squid.stage.cephla.CephlaStage = squid.stage.cephla.CephlaStage(
        microcontroller, squid.config.get_stage_config()
    )

    assert stage.get_pos() == squid.abc.Pos(x_mm=0.0, y_mm=0.0, z_mm=0.0, theta_rad=0.0)


def test_position_caching():
    (unused_temp_fd, temp_cache_path) = tempfile.mkstemp(".cache", "squid_testing_")

    # Use 6 figures after the decimal so we test that we can capture nanometers
    p = squid.abc.Pos(x_mm=11.111111, y_mm=22.222222, z_mm=1.333333, theta_rad=None)
    cfg = squid.config.get_stage_config()
    squid.stage.utils.cache_position(pos=p, stage_config=cfg, cache_path=temp_cache_path)

    p_read = squid.stage.utils.get_cached_position(cache_path=temp_cache_path, stage_config=cfg)

    assert p_read == p


def test_get_cached_position_skips_bad_lines():
    (_, temp_cache_path) = tempfile.mkstemp(".cache", "squid_testing_bad_")
    with open(temp_cache_path, "w") as f:
        f.write("not,a,valid,extra\n")
        f.write("1.0,2.0,3.0\n")
    p_read = squid.stage.utils.get_cached_position(
        cache_path=temp_cache_path, stage_config=squid.config.get_stage_config()
    )
    assert p_read == squid.abc.Pos(x_mm=1.0, y_mm=2.0, z_mm=3.0, theta_rad=None)


def test_move_to_cached_or_default_uses_defaults_when_no_cache():
    microcontroller = get_test_micro()
    stage: squid.stage.cephla.CephlaStage = squid.stage.cephla.CephlaStage(
        microcontroller, squid.config.get_stage_config()
    )
    (fd, missing_cache_path) = tempfile.mkstemp(".cache", "squid_empty_coords_")
    os.close(fd)
    os.unlink(missing_cache_path)
    import control._def as _def

    squid.stage.utils.move_to_cached_or_default_startup_position(
        stage, stage.get_config(), cache_path=missing_cache_path
    )
    pos = stage.get_pos()
    assert pos.x_mm == pytest.approx(_def.STARTUP_DEFAULT_STAGE_X_MM)
    assert pos.y_mm == pytest.approx(_def.STARTUP_DEFAULT_STAGE_Y_MM)
    assert pos.z_mm == pytest.approx(_def.STARTUP_DEFAULT_STAGE_Z_MM)
