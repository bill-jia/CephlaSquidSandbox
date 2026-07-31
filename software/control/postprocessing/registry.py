"""
Routine registry — resolves a ``PostprocessSpec`` to a live routine instance.

Built-in routines are registered by name; a spec with ``routine == "script"``
loads a user ``.py`` file that defines a module-level ``ROUTINE`` instance of
:class:`~control.postprocessing.base.PostprocessRoutine`. Used both by the
controller (pre-flight validation / output declaration) and by the postprocess
``JobRunner`` subprocess (compute).
"""

import hashlib
import importlib.util
import os
from typing import Dict, List, Tuple, Type

from control.postprocessing.base import PostprocessRoutine
from control.postprocessing.routines.dpc2d import DPC2DRoutine
from control.postprocessing.routines.phase2d import Phase2DRoutine

# The PostprocessSpec.routine value marking a user script.
SCRIPT_ROUTINE = "script"

BUILTIN_ROUTINES: Dict[str, Type[PostprocessRoutine]] = {
    Phase2DRoutine.name: Phase2DRoutine,
    DPC2DRoutine.name: DPC2DRoutine,
}


def routine_display_names() -> List[Tuple[str, str]]:
    """``(routine_name, display_name)`` pairs for the GUI dropdown."""
    return [(name, cls.display_name or name) for name, cls in BUILTIN_ROUTINES.items()]


def load_routine(spec) -> PostprocessRoutine:
    """Instantiate the routine a spec references.

    ``spec`` is any object with ``routine`` / ``script_path`` attributes
    (``PostprocessSpec`` or its dict-reconstructed equivalent). Raises
    ``ValueError`` with a user-actionable message on any failure.
    """
    routine = getattr(spec, "routine", None)
    if routine != SCRIPT_ROUTINE:
        cls = BUILTIN_ROUTINES.get(routine)
        if cls is None:
            raise ValueError(f"Unknown postprocessing routine {routine!r} (available: {sorted(BUILTIN_ROUTINES)})")
        return cls()

    path = getattr(spec, "script_path", None)
    if not path:
        raise ValueError("Postprocessing spec selects a custom script but has no script_path")
    if not os.path.isfile(path):
        raise ValueError(f"Postprocessing script not found: {path}")
    module_name = f"squid_postprocess_{hashlib.sha1(path.encode('utf-8')).hexdigest()[:12]}"
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise ValueError(f"Could not load postprocessing script: {path}")
    module = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(module)
    except Exception as e:
        raise ValueError(f"Postprocessing script {path} failed to import: {e}") from e
    routine_obj = getattr(module, "ROUTINE", None)
    if not isinstance(routine_obj, PostprocessRoutine):
        raise ValueError(
            f"Postprocessing script {path} must define a module-level ROUTINE = <PostprocessRoutine instance>"
        )
    return routine_obj
