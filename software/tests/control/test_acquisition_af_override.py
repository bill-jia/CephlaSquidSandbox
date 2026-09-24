"""Acquisition override persistence does not modify an observation preset."""

import os
import tempfile
import yaml
from types import SimpleNamespace
from unittest.mock import MagicMock

from control.core.multi_point_controller import MultiPointController
from control.models.contrast_autofocus import AcquisitionContrastAFOverride, ContrastAFSettings
from control.models.contrast_autofocus import ContrastSupervisionPolicy
from control.models.observation_state import ObservationState
from control.acquisition_yaml_loader import parse_acquisition_yaml


def test_zero_override_roundtrips_in_dedicated_cache():
    state = ObservationState(name="AF source")
    handle = tempfile.NamedTemporaryFile(dir=os.getcwd(), suffix=".yaml", delete=False)
    handle.close()
    try:
        def fake():
            controller = SimpleNamespace(
                _CONTRAST_AF_SETTINGS_CACHE_PATH=handle.name,
                _log=MagicMock(),
                contrast_af_state_name="AF source", contrast_af_override=None,
                liveController=SimpleNamespace(get_observation_state_by_name=lambda name: state),
            )
            controller._save_contrast_af_settings_to_cache = lambda: (
                MultiPointController._save_contrast_af_settings_to_cache(controller))
            return controller

        first = fake()
        override = AcquisitionContrastAFOverride(
            state_name="AF source", settings=ContrastAFSettings(energy_threshold=0,
                                                                  energy_margin=0),
            metric="GLVA", legacy_step_um=1, legacy_count=10,
            crop_width=256, crop_height=256)
        MultiPointController.set_contrast_af_acquisition_settings(first, "AF source", override)
        second = fake()
        MultiPointController._load_contrast_af_settings_from_cache(second)
        assert second.contrast_af_override.settings.energy_threshold == 0
        assert second.contrast_af_override.metric == "GLVA"
        assert state.contrast_af is None
        with open(handle.name, "w", encoding="utf-8") as output:
            yaml.safe_dump({
                "acquisition": {"widget_type": "wellplate"},
                "autofocus": {
                    "contrast_af": True,
                    "contrast_af_state_name": "AF source",
                    "contrast_af_effective": {
                        "settings": override.settings.model_dump(mode="json"),
                        "metric": override.metric,
                        "legacy_step_um": override.legacy_step_um,
                        "legacy_count": override.legacy_count,
                        "crop_width": override.crop_width,
                        "crop_height": override.crop_height,
                        "failure_policy": override.failure_policy,
                    },
                },
            }, output)
        loaded = parse_acquisition_yaml(handle.name)
        assert loaded.contrast_af_state_name == "AF source"
        assert loaded.contrast_af_effective["settings"]["energy_threshold"] == 0
        with open(handle.name, "w", encoding="utf-8") as output:
            yaml.safe_dump({"acquisition": {"widget_type": "wellplate"},
                            "autofocus": {"contrast_af": True}}, output)
        legacy = parse_acquisition_yaml(handle.name)
        assert legacy.contrast_af_state_name is None
        assert legacy.contrast_af_effective is None
        MultiPointController.restore_contrast_af_from_acquisition_yaml(
            second, legacy.contrast_af_state_name, legacy.contrast_af_effective)
        assert second.contrast_af_override is None
    finally:
        os.unlink(handle.name)


def test_supervision_cache_and_acquisition_yaml_roundtrip():
    handle = tempfile.NamedTemporaryFile(dir=os.getcwd(), suffix=".yaml", delete=False)
    handle.close()
    try:
        first = SimpleNamespace(
            _CONTRAST_AF_SETTINGS_CACHE_PATH=handle.name, _log=MagicMock(),
            contrast_af_state_name="AF source", contrast_af_override=None,
            contrast_supervision_policy=ContrastSupervisionPolicy(),
        )
        first._save_contrast_af_settings_to_cache = lambda: (
            MultiPointController._save_contrast_af_settings_to_cache(first))
        MultiPointController.set_contrast_supervision_policy(first, {
            "mode": "quality", "every_n_completed_fovs": 3,
            "operator_confirms_reference_focus": True,
        })
        second = SimpleNamespace(_CONTRAST_AF_SETTINGS_CACHE_PATH=handle.name,
                                 contrast_supervision_policy=ContrastSupervisionPolicy(), _log=MagicMock())
        MultiPointController._load_contrast_supervision_policy(second)
        assert second.contrast_supervision_policy.mode == "quality"
        assert second.contrast_supervision_policy.every_n_completed_fovs == 3
        assert not second.contrast_supervision_policy.operator_confirms_reference_focus
        with open(handle.name, "w", encoding="utf-8") as output:
            yaml.safe_dump({"acquisition": {"widget_type": "wellplate"},
                            "autofocus": {"contrast_supervision": second.contrast_supervision_policy.model_dump(mode="json")}}, output)
        assert parse_acquisition_yaml(handle.name).contrast_supervision["mode"] == "quality"
        with open(handle.name, "w", encoding="utf-8") as output:
            yaml.safe_dump({"acquisition": {"widget_type": "wellplate"}}, output)
        assert parse_acquisition_yaml(handle.name).contrast_supervision is None
    finally:
        os.unlink(handle.name)


def test_legacy_scheduled_yaml_restores_with_correction_off():
    controller = SimpleNamespace(contrast_supervision_policy=ContrastSupervisionPolicy())
    controller.set_contrast_supervision_policy = lambda value: setattr(
        controller, "contrast_supervision_policy", value)
    MultiPointController.restore_supervision_policy_from_acquisition_yaml(controller, {
        "mode": "scheduled", "full_verification_every_n_checks": 2,
        "operator_confirms_reference_focus": True,
    })
    assert controller.contrast_supervision_policy.mode == "off"
    assert not controller.contrast_supervision_policy.operator_confirms_reference_focus
