# Running the software

## Quick start (recommended): the Squid launcher

Double-click **`Squid.bat`** (repo root) or the **Squid** desktop shortcut. It
launches the HCS GUI from source, so editing `.py` files and relaunching picks
up changes immediately — no build step.

Pass arguments through to `main_hcs.py`:

```
Squid.bat --simulation
Squid.bat --profile my_profile
```

The window stays open only if startup fails, so a crash traceback remains
readable; a clean GUI close exits silently.

### Why it's fast

Launching the old way — a PowerShell window running
`conda activate squid; python main_hcs.py` — spent **~5.4s** in PowerShell
startup plus the conda activation hook *before Python even began*. `Squid.bat`
avoids all of that:

- It does **not** run `conda activate`. Instead it sets `PATH` to the env's
  bin/DLL directories and `SSL_CERT_FILE` — the only things activation actually
  contributes here (verified: the Windows Qt platform plugin, numpy/MKL, and
  OpenCV all load without it) — then runs the env's `python.exe` directly.
- cmd startup is ~0.2s versus PowerShell + activate at ~5.4s.

The launcher finds the environment at `%USERPROFILE%\.conda\envs\squid` by
default; set `SQUID_ENV` before running to override.

## Fast startup internals (`main_hcs.py`)

The profile picker is shown **before** the heavy GUI/hardware stack is imported:

- `import gui.gui_hcs` (napari/pyqtgraph) and `import control.microscope`
  (camera/NIDAQ drivers) cost **~13s** to import. They are deferred into
  `__main__` and run *after* the picker is dismissed.
- The picker dialog lives at `gui/profile_selection.py` — deliberately *not*
  under `gui/widgets/`, whose package `__init__` eagerly imports the entire
  widget stack (napari, NIDAQ, pyqtgraph). Its import chain is just Qt +
  `ConfigRepository`.

Net effect: the picker's dependencies import in **<1s**, and the ~13s of heavy
imports happen behind the dialog while the user reads it, instead of in front
of it. Combined with the launcher, time-to-picker drops from ~13s+ (and ~6.5s
of that was shell overhead) to roughly ~1.3s.

## Developer path (manual)

Equivalent to the launcher but pays the `conda activate` cost:

```
conda activate squid
cd software
python main_hcs.py [--simulation] [--profile NAME] [--skip-init] ...
```

## Recreating the desktop shortcut

Point a new shortcut's **Target** at `<repo>\Squid.bat`, **Start in** at
`<repo>`, and **Icon** at `software\icon\cephla_logo.ico`.
