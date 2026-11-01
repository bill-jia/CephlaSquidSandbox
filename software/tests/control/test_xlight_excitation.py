"""Excitation filter selection follows the emission wheel's state and metadata path."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from qtpy.QtWidgets import QApplication

from control.core.acquisition_metadata_helpers import (
    augment_multipoint_acquisition_yaml_dict,
    build_acquisition_metadata,
)
from control.core.observation_state_service import collect_excitation_filter_positions
from control.models.acquisition_metadata import AcquisitionMetadata
from control.models.hardware_bindings import FilterWheelReference
from control.serial_peripherals import XLight_Simulation
from gui.widgets.hardware_panels import SpinningDiskConfocalWidget
from tests.control.core.test_observation_state_confocal import _make_controller, _repo_with_profile, _state


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.parametrize("ttl", [False, True], ids=["LDI", "LDI_TTL"])
def test_panel_selection_round_trips_config_preset_and_metadata(tmp_path, qt_app, ttl):
    """Use each actual LDI config, select in the panel, save, reload and acquire."""
    repo = _repo_with_profile(tmp_path)
    library = Path(__file__).resolve().parents[2] / "machine_configs" / "library"
    filename = f"machine_config_Squid+_LDI{'_TTL' if ttl else ''}_XLight_TucsenAries6506.yaml"
    (repo.machine_configs_path / "machine_config.yaml").write_text(
        (library / filename).read_text(encoding="utf-8"), encoding="utf-8"
    )
    settings = repo.get_confocal_settings()
    assert settings.excitation_filter_positions == 4
    excitation = repo.get_excitation_wheels()["confocal"][0]
    emission = repo.get_emission_wheels()["confocal"][0]
    assert excitation.id != emission.id
    assert repo.resolve_wheel_reference(FilterWheelReference(source="confocal", id=2)) == excitation

    xlight = XLight_Simulation(excitation_filter_positions=settings.excitation_filter_positions)
    ctl = _make_controller(xlight=xlight, config_repo=repo)
    ctl._current_state = repo.get_observation_state()
    panel = SpinningDiskConfocalWidget(xlight, config_repo=repo)
    try:
        panel.signal_excitation_filter_changed.connect(ctl.set_excitation_filter_position)
        dropdown = panel.dropdown_excitation_filter
        assert dropdown.count() == 4
        assert dropdown.currentData() == xlight.excitation_wheel_pos == 1
        assert dropdown.itemText(2) == f"3: {excitation.positions[3]}"
        assert panel.dropdown_emission_filter.itemText(0) == f"1: {emission.positions[1]}"
        dropdown.setCurrentIndex(dropdown.findData(3))
        assert xlight.excitation_wheel_pos == 3
        assert ctl.current_observation_state.excitation_filter_positions == {"default": 3}

        # The periodic live cache and saved presets must both retain the choice.
        assert ctl.cache_current_state_to_disk()
        cached = yaml.safe_load(
            (repo.user_profiles_path / "p1" / "channel_configs" / "general.yaml").read_text(encoding="utf-8")
        )
        assert cached["excitation_filter_positions"] == {"default": 3}
        collected = ctl.collect_observation_state()
        repo.save_observation_preset("excitation_test", collected)
        loaded = repo.load_observation_preset("excitation_test")
        assert loaded.excitation_filter_positions == {"default": 3}

        xlight.set_excitation_filter(1)
        ctl.apply_observation_state_preset(loaded)
        panel.sync_from_observation_state(loaded)
        assert xlight.excitation_wheel_pos == dropdown.currentData() == 3

        objective_store = SimpleNamespace(current_objective="20x", objectives_dict={"20x": {}})
        live = SimpleNamespace(obs_controller=ctl, microscope=ctl.microscope, get_trigger_mode=lambda: "Software")
        metadata = build_acquisition_metadata(
            experiment_id="test",
            objective_store=objective_store,
            live_controller=live,
            camera=ctl.camera,
            scan_parameters={},
            observation_state=ctl.collect_observation_state(),
        )
        path = repo.save_acquisition_metadata(tmp_path / "acquisition", metadata)
        saved = AcquisitionMetadata.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert saved.observation_state.excitation_filter_positions == {"default": 3}

        for names in ([], ["excitation_test"]):
            multipoint = augment_multipoint_acquisition_yaml_dict(
                {},
                experiment_id="test",
                recording_start_time=123.0,
                repo=repo,
                objective_store=objective_store,
                live_controller=live,
                camera=ctl.camera,
                selected_configurations=[],
                obs_names=names,
            )
            if names:
                camera_states = multipoint["observation_states_used"][names[0]]["camera_states"]
                assert next(iter(camera_states.values()))["excitation_filter_positions"] == {"default": 3}
            else:
                assert multipoint["manifest"]["observation_state"]["excitation_filter_positions"] == {"default": 3}
    finally:
        panel.deleteLater()


def test_panel_sync_is_display_only_and_controls_disable(qt_app):
    xlight = XLight_Simulation()
    panel = SpinningDiskConfocalWidget(xlight)
    signals = []
    panel.signal_excitation_filter_changed.connect(signals.append)
    try:
        state = _state()
        state.excitation_filter_positions = {"default": 4}
        panel.sync_from_observation_state(state)
        assert panel.dropdown_excitation_filter.currentData() == 4
        assert xlight.excitation_wheel_pos == 1
        assert signals == []
        panel.enable_all_buttons(False)
        assert not panel.dropdown_excitation_filter.isEnabled()
        panel.enable_all_buttons(True)
        assert panel.dropdown_excitation_filter.isEnabled()
    finally:
        panel.deleteLater()


def test_panel_without_excitation_hardware_has_no_dropdown(qt_app):
    xlight = XLight_Simulation()
    xlight.has_excitation_filters_wheel = False
    panel = SpinningDiskConfocalWidget(xlight)
    try:
        assert panel.dropdown_excitation_filter is None
        panel.sync_from_observation_state(_state())
        panel.enable_all_buttons(False)
    finally:
        panel.deleteLater()


def test_collection_uses_cache_and_preserves_last_selection_on_read_failure(monkeypatch):
    xlight = XLight_Simulation()
    ctl = _make_controller(xlight=xlight)
    ctl._current_state = _state()
    ctl.set_excitation_filter_position(2)

    def fail():
        raise OSError("read failed")

    monkeypatch.setattr(xlight, "get_excitation_filter", fail)
    assert collect_excitation_filter_positions(xlight) == {"default": 2}
    xlight.excitation_wheel_pos = None
    assert collect_excitation_filter_positions(xlight) == {}
    assert ctl.collect_observation_state().excitation_filter_positions == {"default": 2}
    xlight.has_excitation_filters_wheel = False
    assert collect_excitation_filter_positions(xlight) == {}


def test_older_preset_leaves_excitation_wheel_unchanged(tmp_path):
    repo = _repo_with_profile(tmp_path)
    state = _state()
    repo.save_observation_preset("old", state)
    preset_path = repo.user_profiles_path / "p1" / "observation_presets" / "old.yaml"
    raw = yaml.safe_load(preset_path.read_text(encoding="utf-8"))
    for camera_state in raw["camera_states"].values():
        camera_state.pop("excitation_filter_positions", None)
    preset_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    loaded = repo.load_observation_preset("old")
    assert loaded.excitation_filter_positions == {}
    xlight = XLight_Simulation()
    xlight.set_excitation_filter(4)
    ctl = _make_controller(xlight=xlight, config_repo=repo)
    ctl.apply_observation_state_preset(loaded)
    assert xlight.excitation_wheel_pos == 4
