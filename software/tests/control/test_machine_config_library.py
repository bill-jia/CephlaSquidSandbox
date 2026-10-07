"""Every selectable machine config in ``machine_configs/library`` must load.

Guards against a library entry that validates in isolation drifting out of
step with the ``MachineConfig`` schema, and against IO lines that clash or
reference an undefined controller.
"""

from pathlib import Path

import pytest
import yaml

from control.models.machine_config import MachineConfig

_LIBRARY = Path(__file__).resolve().parents[2] / "machine_configs" / "library"
_CONFIGS = sorted(_LIBRARY.glob("machine_config*.yaml"))


def test_library_is_not_empty():
    assert _CONFIGS, f"no machine_config*.yaml under {_LIBRARY}"


@pytest.mark.parametrize("path", _CONFIGS, ids=[p.stem for p in _CONFIGS])
def test_library_config_loads_and_io_is_consistent(path: Path):
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    mc = MachineConfig.model_validate(raw)

    assert mc.validate_io_lines() == []
    # Every IO line resolves to a known controller and becomes an endpoint.
    mc.collect_io_endpoints()
    # Config keys nobody reads are a silent misconfiguration, not a feature.
    assert not mc.model_extra, f"unknown top-level keys: {sorted(mc.model_extra)}"
    # A confocal entry must parse into typed settings (rejects unknown keys).
    mc.get_confocal_settings()
    # Same for the laser AF entry: devices.laser_af.config is read, not decoration.
    mc.get_laser_af_settings()
