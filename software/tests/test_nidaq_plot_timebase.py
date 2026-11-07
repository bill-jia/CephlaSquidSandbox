"""Exercise the plot method without loading camera drivers or constructing Qt UI."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
from matplotlib.figure import Figure


def test_empty_plot_first_pattern_zoom_and_duration_change():
    source = Path(__file__).parents[1] / "gui/widgets/nidaq_fast.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "_update_waveform_plot")
    namespace = {"np": np}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    fig = Figure()
    ao = fig.add_subplot(211)
    do = fig.add_subplot(212, sharex=ao)
    widget = SimpleNamespace(
        _ni_daq=SimpleNamespace(samples_per_channel=600000, sample_rate_hz=10000),
        _last_plot_xspan_s=None, ax_ao=ao, ax_do=do, fig=fig,
        _ao_waveforms={}, _do_patterns={}, _endpoint_labels_ao={}, _endpoint_labels_do={},
        do_port_combo=Mock(currentText=lambda: "port0"),
        _draw_camera_exposure_windows=Mock(), canvas=Mock(),
    )
    redraw = namespace["_update_waveform_plot"]
    redraw(widget)
    assert ao.get_xlim() == (0, 60)
    widget._do_patterns[0] = np.zeros(600000, dtype=bool)
    redraw(widget)
    assert do.get_xlim() == (0, 60)
    ao.set_xlim(10, 20)
    redraw(widget)
    assert do.get_xlim() == (10, 20)
    widget._ni_daq.samples_per_channel = 300000
    widget._do_patterns[0] = np.zeros(300000, dtype=bool)
    redraw(widget)
    assert ao.get_xlim() == (0, 30)
