"""Verify cleanup task lifecycle without loading camera drivers or DAQ hardware."""
import ast
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable
from unittest.mock import MagicMock

import numpy as np
import pytest


def zero_method():
    path = Path(__file__).parents[1] / "control/nidaq.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NIDAQ")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "zero_outputs")
    namespace = {"np": np, "Iterable": Iterable}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["zero_outputs"]


@pytest.mark.parametrize("analog", [False, True])
def test_zero_outputs_uses_only_short_output_tasks_and_preserves_config(analog):
    tasks = [MagicMock(), MagicMock()]
    for task in tasks:
        task.__enter__.return_value = task
    config = SimpleNamespace(device_name="Dev1", do_port="port0", ao_min_voltage=-10,
                             ao_max_voltage=10, sample_rate_hz=10000, samples_per_channel=6000000)
    waveforms = object()
    daq = SimpleNamespace(
        _lock=threading.Lock(), _config=config, _waveforms=waveforms,
        _stop_internal=MagicMock(), _cleanup_tasks=MagicMock(),
        _nidaqmx=SimpleNamespace(Task=MagicMock(side_effect=tasks)),
        _constants=SimpleNamespace(AcquisitionType=SimpleNamespace(FINITE="finite")),
        _live_do_values={3: True, 5: True}, _live_ao_values={"ao0": 2.0},
    )
    before = vars(config).copy()
    zero_method()(daq, ["ao0"] if analog else [], [3, 6])
    assert vars(config) == before
    assert daq._waveforms is waveforms
    assert daq._live_do_values == {3: False, 5: True, 6: False}
    tasks[0].write.assert_called_once_with([False, False], auto_start=True)
    tasks[0].timing.cfg_samp_clk_timing.assert_not_called()
    assert daq._nidaqmx.Task.call_count == (2 if analog else 1)
    for task in tasks:
        task.di_channels.add_di_chan.assert_not_called()
        task.ai_channels.add_ai_voltage_chan.assert_not_called()
        task.stop.assert_not_called()
    if analog:
        tasks[1].timing.cfg_samp_clk_timing.assert_called_once_with(
            rate=1000.0, sample_mode="finite", samps_per_chan=2)
        tasks[1].wait_until_done.assert_called_once_with(timeout=5.0)
        assert daq._live_ao_values["ao0"] == 0.0


def test_zero_write_failure_propagates_and_closes_task():
    task = MagicMock()
    task.__enter__.return_value = task
    task.__exit__.return_value = False
    task.write.side_effect = RuntimeError("write failed")
    daq = SimpleNamespace(
        _lock=threading.Lock(), _config=SimpleNamespace(device_name="Dev1", do_port="port0"),
        _stop_internal=MagicMock(), _cleanup_tasks=MagicMock(),
        _nidaqmx=SimpleNamespace(Task=MagicMock(return_value=task)), _live_do_values={3: True},
    )
    with pytest.raises(RuntimeError, match="write failed"):
        zero_method()(daq, [], [3])
    task.__exit__.assert_called_once()
    assert daq._live_do_values[3] is True
