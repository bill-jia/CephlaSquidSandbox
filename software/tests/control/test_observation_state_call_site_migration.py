"""Call sites migrated off the removed per-objective channel API.

``LiveController.get_channels`` / ``get_channel_by_name`` were temporary
aliases carrying a vestigial ``objective`` argument. They are gone; everything
now goes through ``get_observation_states`` / ``get_observation_state_by_name``.

These tests cover the breakages that migration exposed:

* the contrast-autofocus lookup in the middle of a multipoint run, which must
  fail loudly naming the channel rather than feeding ``None`` to
  ``_select_config``;
* ``Microscope.set_exposure_time``, which used to assign to a read-only
  property and then call a method that does not exist;
* ``TrackingController.start_new_experiment``, which passed
  ``ObservationState`` objects under the wrong keyword;
* ``NapariLiveWidget``, which read an ``illumination_intensity`` field that
  ``ObservationState`` (``extra="forbid"``) does not have.

Fakes only — no hardware is constructed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from control.core.config.repository import ConfigRepository
from control.models.observation_state import (
    CameraSettings,
    IlluminatorState,
    ObservationState,
)


def _state(name: str, exposure: float = 10.0, intensity: float = 5.0, on: bool = True) -> ObservationState:
    return ObservationState(
        version=3,
        name=name,
        camera_settings=CameraSettings(exposure_time_ms=exposure, gain_mode=1.5),
        illuminator_states=[
            IlluminatorState(illumination_channel="Fluorescence 488 nm Ex", intensity=intensity, on=on)
        ],
    )


@pytest.fixture
def repo(tmp_path: Path) -> ConfigRepository:
    base = tmp_path / "sw"
    (base / "machine_configs").mkdir(parents=True)
    (base / "user_profiles" / "p1" / "channel_configs").mkdir(parents=True)
    (base / "user_profiles" / "p1" / "observation_presets").mkdir(parents=True)
    (base / "machine_configs" / "illumination_channel_config.yaml").write_text(
        "version: 1\ncontroller_port_mapping: {}\nchannels: []\n", encoding="utf-8"
    )
    r = ConfigRepository(base_path=base)
    r.set_profile("p1")
    return r


# ── the aliases are gone ──────────────────────────────────────────────────────


def test_live_controller_no_longer_carries_the_channel_aliases():
    from control.core.live_controller import LiveController

    assert not hasattr(LiveController, "get_channels")
    assert not hasattr(LiveController, "get_channel_by_name")
    assert hasattr(LiveController, "get_observation_states")
    assert hasattr(LiveController, "get_observation_state_by_name")


# ── contrast autofocus lookup (acquisition path) ──────────────────────────────


class _FakeLiveController:
    def __init__(self, states):
        self._states = list(states)

    def get_observation_states(self):
        return list(self._states)

    def get_observation_state_by_name(self, name):
        for s in self._states:
            if s.name == name:
                return s
        return None


def _af_worker(states):
    """A stand-in carrying only what ``perform_autofocus`` touches on the AF path."""
    selected = []
    autofocus_calls = []
    worker = SimpleNamespace(
        do_reflection_af=False,
        do_autofocus=True,
        af_fov_count=0,
        _last_af_status=None,
        liveController=_FakeLiveController(states),
        _wait_for_move_settled=lambda: None,
        _select_config=selected.append,
        autofocusController=SimpleNamespace(
            use_focus_map=False,
            autofocus=lambda: autofocus_calls.append("af"),
            wait_till_autofocus_has_completed=lambda: None,
        ),
    )
    return worker, selected, autofocus_calls


def test_contrast_af_resolves_the_channel_and_selects_it(monkeypatch):
    from control.core import multi_point_worker as mpw

    monkeypatch.setattr(mpw, "MULTIPOINT_AUTOFOCUS_CHANNEL", "BF", raising=False)
    state = _state("BF")
    worker, selected, autofocus_calls = _af_worker([state, _state("GFP")])

    mpw.MultiPointWorker.perform_autofocus(worker, "R0", 0)

    assert selected == [state]
    assert autofocus_calls == ["af"]
    assert worker._last_af_status == "ok"


def test_contrast_af_fails_loudly_on_an_unknown_channel(monkeypatch):
    from control.core import multi_point_worker as mpw

    monkeypatch.setattr(mpw, "MULTIPOINT_AUTOFOCUS_CHANNEL", "Nope", raising=False)
    worker, selected, _ = _af_worker([_state("BF"), _state("GFP")])

    with pytest.raises(RuntimeError) as excinfo:
        mpw.MultiPointWorker.perform_autofocus(worker, "R0", 0)

    message = str(excinfo.value)
    assert "Nope" in message  # names the channel that could not be resolved
    assert "BF" in message and "GFP" in message  # and what was available
    assert selected == []  # None never reached _select_config


# ── Microscope.set_exposure_time ──────────────────────────────────────────────


def _scope(repo, active=None):
    """A stand-in carrying only what ``Microscope.set_exposure_time`` touches."""
    applied = []
    return (
        SimpleNamespace(
            config_repo=repo,
            obs_controller=SimpleNamespace(
                current_observation_state=active,
                set_exposure_time=applied.append,
            ),
        ),
        applied,
    )


def test_set_exposure_time_writes_camera_settings_and_persists_the_preset(repo):
    from control.microscope import Microscope

    repo.save_observation_preset("BF", _state("BF", exposure=10.0))
    scope, applied = _scope(repo)

    Microscope.set_exposure_time(scope, "BF", 42.5)

    reloaded = repo.get_observation_state_by_name("BF")
    assert reloaded.camera_settings.exposure_time_ms == 42.5
    assert reloaded.exposure_time == 42.5
    # Not the active state, so live hardware was left alone.
    assert applied == []


def test_set_exposure_time_applies_to_the_camera_only_for_the_active_state(repo):
    from control.microscope import Microscope

    repo.save_observation_preset("BF", _state("BF", exposure=10.0))
    repo.save_observation_preset("GFP", _state("GFP", exposure=10.0))

    scope, applied = _scope(repo, active=_state("BF"))
    Microscope.set_exposure_time(scope, "BF", 33.0)
    assert applied == [33.0]

    scope, applied = _scope(repo, active=_state("BF"))
    Microscope.set_exposure_time(scope, "GFP", 12.0)
    assert applied == []
    assert repo.get_observation_state_by_name("GFP").exposure_time == 12.0


def test_set_exposure_time_raises_on_an_unknown_channel(repo):
    from control.microscope import Microscope

    repo.save_observation_preset("BF", _state("BF"))
    scope, _ = _scope(repo)

    with pytest.raises(ValueError) as excinfo:
        Microscope.set_exposure_time(scope, "Nope", 5.0)

    assert "Nope" in str(excinfo.value)
    assert "BF" in str(excinfo.value)


def test_set_exposure_time_names_its_subject_unambiguously():
    """The vestigial ``objective`` is gone, and the subject is named for what it is.

    ``set_exposure_time`` and ``set_illumination_intensity`` sit next to each
    other and used to take an identically-named ``channel``, meaning an
    Observation State in one and a light source line in the other.
    """
    import inspect

    from control.microscope import Microscope

    assert list(inspect.signature(Microscope.set_exposure_time).parameters) == [
        "self",
        "observation_state",
        "exposure_time",
    ]
    assert list(inspect.signature(Microscope.set_illumination_intensity).parameters) == [
        "self",
        "illumination_channel",
        "intensity",
    ]


# ── TrackingController.start_new_experiment ───────────────────────────────────


def test_tracking_acquisition_output_is_given_names_not_objects(tmp_path):
    from control.core.core import TrackingController

    recorded = {}

    class _Repo:
        def save_acquisition_output(self, **kwargs):
            recorded.update(kwargs)

        def save_acquisition_metadata(self, *args, **kwargs):
            pass

    states = [_state("BF"), _state("GFP")]
    tracker = SimpleNamespace(
        base_path=str(tmp_path),
        selected_configurations=states,
        objectiveStore=SimpleNamespace(current_objective="20x", objectives_dict={"20x": {"magnification": 20}}),
        camera=SimpleNamespace(get_pixel_size_binned_um=lambda: 1.0),
        liveController=SimpleNamespace(
            microscope=SimpleNamespace(config_repo=_Repo()),
            obs_controller=SimpleNamespace(is_confocal_mode=lambda: False),
            get_trigger_mode=lambda: "SOFTWARE",
        ),
    )

    TrackingController.start_new_experiment(tracker, "run")

    assert "channels" not in recorded
    assert recorded["observation_state_names"] == ["BF", "GFP"]
    assert all(isinstance(n, str) for n in recorded["observation_state_names"])


# ── NapariLiveWidget ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("qtpy")
    from qtpy.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


def test_napari_live_widget_constructs_and_its_controls_write_through(qt_app, monkeypatch):
    pytest.importorskip("napari")
    pytest.importorskip("pyqtgraph")
    from unittest.mock import MagicMock

    from gui.widgets import napari_views as nv

    state = _state("BF", exposure=17.0, intensity=63.0)

    writes = []
    obs_controller = SimpleNamespace(
        current_observation_state=state,
        set_active_observation_state=lambda s: None,
        set_exposure_time=lambda v: writes.append(("exposure", v)),
        set_analog_gain=lambda v: writes.append(("gain", v)),
        set_illumination_intensity=lambda ch, v: writes.append(("intensity", ch, v)),
        apply_full_observation_state=lambda s: None,
        is_confocal_mode=lambda: False,
    )
    live_controller = SimpleNamespace(
        obs_controller=obs_controller,
        camera=SimpleNamespace(get_exposure_limits=lambda: (0.1, 1000.0)),
        microscope=SimpleNamespace(config_repo=object()),
        set_trigger_fps=lambda v: None,
        set_display_resolution_scaling=lambda v: None,
        get_observation_states=lambda: [state],
        get_observation_state_by_name=lambda name: state if name == state.name else None,
    )

    def fake_init_viewer(self):
        self.viewer = MagicMock()

    monkeypatch.setattr(nv.NapariLiveWidget, "initNapariViewer", fake_init_viewer)
    monkeypatch.setattr(nv.NapariLiveWidget, "addNapariGrayclipColormap", lambda self: None)
    monkeypatch.setattr(nv.NapariLiveWidget, "print_window_menu_items", lambda self: None)

    widget = nv.NapariLiveWidget(
        streamHandler=SimpleNamespace(set_display_fps=lambda v: None, set_display_resolution_scaling=lambda v: None),
        liveController=live_controller,
        stage=object(),
        objectiveStore=SimpleNamespace(current_objective="20x"),
        contrastManager=SimpleNamespace(),
    )

    # Values came off the model, not off a field it does not have.
    assert widget.entry_exposureTime.value() == pytest.approx(17.0)
    assert widget.entry_analogGain.value() == pytest.approx(1.5)
    assert widget.slider_illuminationIntensity.value() == 63

    # Controls route writes through the observation-state controller.
    widget.update_config_exposure_time(25.0)
    widget.update_config_analog_gain(3.0)
    widget.update_config_illumination_intensity(80)

    assert ("exposure", 25.0) in writes
    assert ("gain", 3.0) in writes
    assert ("intensity", "Fluorescence 488 nm Ex", 80.0) in writes

    widget.deleteLater()


def test_observation_state_still_forbids_an_illumination_intensity_field():
    """The field the widget used to read really is not on the model."""
    with pytest.raises(Exception):
        ObservationState(version=3, name="BF", illumination_intensity=50.0)

    assert not hasattr(_state("BF"), "illumination_intensity")


# ── deleted dead code ─────────────────────────────────────────────────────────


def test_last_active_channel_plumbing_is_gone(repo):
    from control.models.gui_state import GuiState

    assert not hasattr(ConfigRepository, "get_last_active_channel_name")
    assert "last_active_observation_state_name" not in GuiState.model_fields


def test_legacy_modules_are_deleted():
    import importlib.util

    assert importlib.util.find_spec("control.core_PDAF") is None
    assert importlib.util.find_spec("control.core_volumetric_imaging") is None
