"""Seeding/round-trip behaviour of the DPC 2D params dialog.

The dialog is how dpc2d gets its role bindings and optics, so what it emits has
to be a valid params dict for the routine: roles bound to the group's member
states, and the live objective / preset values filled in where the user hasn't
overridden them.
"""

import pytest

from control.postprocessing.base import InputStateSpec
from control.postprocessing.routines.dpc2d import DPC2DRoutine
from gui.widgets.multipoint import DPC2DParamsDialog

STATES = ["BF_top_half", "BF_bottom_half", "BF_left_half", "BF_right_half"]


def _dialog(qtbot, params=None, states=STATES, **kwargs):
    dlg = DPC2DParamsDialog(params or {}, input_states=states, **kwargs)
    qtbot.addWidget(dlg)
    return dlg


def test_roles_bound_to_member_states(qtbot):
    params = _dialog(qtbot).get_params()
    assert params["state_top"] == "BF_top_half"
    assert params["state_bottom"] == "BF_bottom_half"
    assert params["state_left"] == "BF_left_half"
    assert params["state_right"] == "BF_right_half"


def test_roles_left_on_auto_when_names_are_untaggable(qtbot):
    params = _dialog(qtbot, states=["a", "b", "c", "d"]).get_params()
    assert all(params[f"state_{r}"] is None for r in ("top", "bottom", "left", "right"))


def test_optics_seeded_from_live_objective_and_preset(qtbot):
    params = _dialog(
        qtbot, objective_na=0.5, default_wavelength_nm=488, default_illumination_na=(0.33, 0.0)
    ).get_params()
    assert params["na_detection"] == 0.5  # objective NA, not the routine's fallback
    assert params["wavelength_nm"] == 488
    assert params["na_illumination"] == 0.33  # LED-matrix DPC NA, not the objective NA
    assert params["na_illumination_inner"] == 0.0


def test_half_annulus_preset_seeds_both_ring_nas(qtbot):
    params = _dialog(qtbot, objective_na=0.4, default_illumination_na=(0.8, 0.3)).get_params()
    assert (params["na_illumination"], params["na_illumination_inner"]) == (0.8, 0.3)


def test_saved_params_win_over_live_seeds(qtbot):
    saved = _dialog(
        qtbot, objective_na=0.5, default_wavelength_nm=488, default_illumination_na=(0.33, 0.0)
    ).get_params()
    reopened = _dialog(
        qtbot, saved, objective_na=0.9, default_wavelength_nm=999, default_illumination_na=(0.7, 0.4)
    ).get_params()
    assert reopened == saved


def test_explicit_full_half_disk_survives_an_annulus_preset(qtbot):
    """A deliberately-saved inner NA of 0 must not fall back to the ring NA."""
    params = _dialog(qtbot, {"na_illumination_inner": 0.0}, default_illumination_na=(0.8, 0.3)).get_params()
    assert params["na_illumination_inner"] == 0.0


def test_stale_role_binding_from_another_group_is_dropped(qtbot):
    params = _dialog(qtbot, {"state_top": "SomeOtherGroupState"}).get_params()
    assert params["state_top"] == "BF_top_half"


@pytest.mark.parametrize("output_absorption", [False, True])
def test_emitted_params_satisfy_the_routine(qtbot, output_absorption):
    params = _dialog(qtbot, {"output_absorption": output_absorption}, objective_na=0.4).get_params()
    input_states = {s: InputStateSpec(s, False, 1) for s in STATES}
    names = [o.name for o in DPC2DRoutine().describe_outputs(input_states, params)]
    assert names == (["phase", "absorption", "brightfield"] if output_absorption else ["phase", "brightfield"])
