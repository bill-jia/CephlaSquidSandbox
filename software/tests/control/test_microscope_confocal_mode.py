"""``Microscope.set_confocal_mode`` — the headless entry point for switching modes.

Exercised against a stub rather than a real Microscope: building one opens the
microcontroller, and this only needs the method's own logic.
"""

from types import SimpleNamespace

import pytest

import control._def
from control.microscope import Microscope


class FakeXLight:
    def __init__(self):
        self.disk_positions = []

    def set_disk_position(self, position):
        self.disk_positions.append(position)


class FakeDragonfly:
    def __init__(self):
        self.modalities = []

    def set_modality(self, modality):
        self.modalities.append(modality)


class FakeObsController:
    def __init__(self):
        self.toggled = []

    def toggle_confocal_widefield(self, confocal):
        self.toggled.append(confocal)


def _scope(xlight=None, dragonfly=None):
    obs = FakeObsController()
    return SimpleNamespace(
        addons=SimpleNamespace(xlight=xlight, dragonfly=dragonfly),
        obs_controller=obs,
        # set_confocal_mode must not reach for live_controller: the mode flag
        # lives on the observation state controller.
        live_controller=SimpleNamespace(obs_controller=obs),
    )


@pytest.fixture
def confocal_enabled(monkeypatch):
    monkeypatch.setattr(control._def, "ENABLE_SPINNING_DISK_CONFOCAL", True)


def test_xlight_moves_disk_and_records_mode(confocal_enabled):
    xlight = FakeXLight()
    scope = _scope(xlight=xlight)

    Microscope.set_confocal_mode(scope, True)

    assert xlight.disk_positions == [1]
    assert scope.obs_controller.toggled == [True]


def test_widefield_moves_disk_back(confocal_enabled):
    xlight = FakeXLight()
    scope = _scope(xlight=xlight)

    Microscope.set_confocal_mode(scope, False)

    assert xlight.disk_positions == [0]
    assert scope.obs_controller.toggled == [False]


def test_dragonfly_uses_modality(confocal_enabled):
    dragonfly = FakeDragonfly()
    scope = _scope(dragonfly=dragonfly)

    Microscope.set_confocal_mode(scope, True)

    assert dragonfly.modalities == ["CONFOCAL"]
    assert scope.obs_controller.toggled == [True]


def test_raises_without_hardware(confocal_enabled):
    with pytest.raises(RuntimeError, match="No spinning disk hardware"):
        Microscope.set_confocal_mode(_scope(), True)


def test_raises_when_disabled(monkeypatch):
    monkeypatch.setattr(control._def, "ENABLE_SPINNING_DISK_CONFOCAL", False)

    with pytest.raises(RuntimeError, match="not enabled"):
        Microscope.set_confocal_mode(_scope(xlight=FakeXLight()), True)
