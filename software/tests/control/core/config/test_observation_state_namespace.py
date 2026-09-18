"""Resolving a channel name to an Observation State.

"Channel" and "Observation State" name the same thing, and the namespace is the
saved preset set — exactly what the Observation State save/load dropdown lists.
A profile that has saved no presets falls back to the single working state in
``general.yaml``.

Before this existed, several call sites reached for
``liveController.get_channel_by_name`` / ``get_observation_states`` /
``obs_controller.get_observation_state_by_name``, none of which were defined, so
each raised ``AttributeError`` — including the contrast-autofocus lookup in the
middle of a multipoint run.
"""

from pathlib import Path

import pytest
import yaml

from control.core.config.repository import ConfigRepository
from control.models.observation_state import (
    CameraSettings,
    IlluminatorState,
    ObservationState,
)


def _state(name: str, exposure: float = 10.0) -> ObservationState:
    return ObservationState(
        version=3,
        name=name,
        camera_settings=CameraSettings(exposure_time_ms=exposure, gain_mode=0.0),
        illuminator_states=[
            IlluminatorState(illumination_channel="Fluorescence 488 nm Ex", intensity=5.0, on=False)
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


def _write_general(repo: ConfigRepository, state: ObservationState) -> None:
    path = repo.user_profiles_path / "p1" / "channel_configs" / "general.yaml"
    path.write_text(yaml.safe_dump(state.model_dump(mode="json")), encoding="utf-8")


# ── presets are the namespace ─────────────────────────────────────────────────


def test_named_preset_resolves(repo):
    repo.save_observation_preset("BF", _state("BF", exposure=7.0))
    repo.save_observation_preset("GFP", _state("GFP", exposure=25.0))

    got = repo.get_observation_state_by_name("GFP")

    assert got is not None
    assert got.camera_settings.exposure_time_ms == 25.0


def test_unknown_name_is_none_when_presets_exist(repo):
    repo.save_observation_preset("BF", _state("BF"))
    _write_general(repo, _state("live"))

    # A profile that HAS presets does not silently substitute general.yaml:
    # selecting a channel that does not exist must fail visibly.
    assert repo.get_observation_state_by_name("does-not-exist") is None


def test_enumeration_lists_presets_in_dropdown_order(repo):
    repo.save_observation_preset("GFP", _state("GFP"))
    repo.save_observation_preset("BF", _state("BF"))

    names = [s.name for s in repo.get_observation_states()]

    # list_observation_presets sorts, and the dropdown is built from it.
    assert names == repo.list_observation_presets()


# ── general.yaml is the fallback only when nothing is saved ───────────────────


def test_general_answers_any_name_when_no_presets(repo, caplog):
    _write_general(repo, _state("live", exposure=33.0))

    got = repo.get_observation_state_by_name("BF LED matrix full")

    # This is the contrast-autofocus case: the worker asks for a channel by name
    # on a profile that has saved none. Returning the working state keeps the
    # acquisition alive instead of raising mid-run.
    assert got is not None
    assert got.camera_settings.exposure_time_ms == 33.0
    assert any("no observation state presets" in r.message.lower() for r in caplog.records)


def test_general_is_the_sole_enumeration_when_no_presets(repo):
    _write_general(repo, _state("live"))

    states = repo.get_observation_states()

    assert [s.name for s in states] == ["live"]


def test_saving_a_preset_takes_over_from_general(repo):
    _write_general(repo, _state("live", exposure=33.0))
    assert repo.get_observation_state_by_name("anything") is not None

    repo.save_observation_preset("BF", _state("BF", exposure=7.0))

    # Once a preset exists the fallback stops: the namespace is now explicit.
    assert repo.get_observation_state_by_name("anything") is None
    assert repo.get_observation_state_by_name("BF").camera_settings.exposure_time_ms == 7.0


def test_empty_profile_resolves_to_nothing(repo):
    assert repo.get_observation_states() == []
    assert repo.get_observation_state_by_name("BF") is None
