"""Runtime-set ``_def`` flags must not be read as bare star-imported names.

``control/_def.py`` holds the defaults; ``config_bridge.apply_machine_config``
overwrites them at runtime from the machine config. But most modules do
``from control._def import *``, which binds a **copy** at import time — and
``main_hcs`` imports the GUI and driver stack *before* building the microscope,
which is what applies the config. A module that reads such a flag as a bare name
therefore sees the stale default forever.

That is not hypothetical: the spinning-disk confocal panel was gated on a bare
``ENABLE_SPINNING_DISK_CONFOCAL``, so on a rig with an X-Light the widget was
never constructed and the emission filter was never applied, silently. The fix
is to branch on the built hardware (``microscope.addons.xlight``) or, where a
flag really is the right source, to read it through the module as
``control._def.FLAG`` so the lookup happens at call time.
"""

import re
from pathlib import Path

import pytest

_SOFTWARE = Path(__file__).resolve().parents[2]

# Flags that apply_machine_config rewrites at runtime, so a star-imported copy lies.
_RUNTIME_SET_FLAGS = [
    "ENABLE_SPINNING_DISK_CONFOCAL",
    "USE_DRAGONFLY",
]

# _def defines them; config_bridge is what assigns them.
_ALLOWED = {
    Path("control/_def.py"),
    Path("control/core/config_bridge.py"),
}

_SEARCH_DIRS = ["control", "gui"]


def _offending_lines(flag: str):
    """Source lines reading ``flag`` as a bare name (not ``control._def.flag``)."""
    # The name, not preceded by a dot or word character, and not in a string/comment.
    pattern = re.compile(rf"(?<![\w.]){flag}\b")
    hits = []
    for directory in _SEARCH_DIRS:
        for path in (_SOFTWARE / directory).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(_SOFTWARE)
            if rel in _ALLOWED:
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
            ):
                code = line.split("#", 1)[0]
                if pattern.search(code):
                    hits.append(f"{rel}:{number}: {line.strip()}")
    return hits


@pytest.mark.parametrize("flag", _RUNTIME_SET_FLAGS)
def test_runtime_flag_is_not_read_as_a_bare_name(flag):
    offenders = _offending_lines(flag)
    assert not offenders, (
        f"{flag} is rewritten by apply_machine_config at runtime, so a bare "
        f"star-imported read sees a stale default. Branch on the built hardware, "
        f"or read it as control._def.{flag}. Offenders:\n  " + "\n  ".join(offenders)
    )


def test_guard_would_catch_a_bare_read(tmp_path, monkeypatch):
    """The scan must actually flag a regression, not vacuously pass."""
    module = tmp_path / "control" / "regression.py"
    module.parent.mkdir(parents=True)
    module.write_text("if ENABLE_SPINNING_DISK_CONFOCAL:\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(
        "tests.control.test_def_flag_staleness._SOFTWARE", tmp_path, raising=False
    )

    assert _offending_lines("ENABLE_SPINNING_DISK_CONFOCAL")


def test_qualified_reads_are_accepted(tmp_path, monkeypatch):
    module = tmp_path / "control" / "ok.py"
    module.parent.mkdir(parents=True)
    module.write_text("if control._def.ENABLE_SPINNING_DISK_CONFOCAL:\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(
        "tests.control.test_def_flag_staleness._SOFTWARE", tmp_path, raising=False
    )

    assert not _offending_lines("ENABLE_SPINNING_DISK_CONFOCAL")
