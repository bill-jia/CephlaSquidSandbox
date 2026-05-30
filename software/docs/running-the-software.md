# Running the software

There are two ways to launch, both of which skip the slow
`conda activate` step:

| Launch | Console | Taskbar | Use it for |
|--------|---------|---------|------------|
| **Squid** desktop shortcut (`pythonw.exe`) | none | single clean "Squid" button, pinnable | everyday "just open it" |
| **`Squid.bat`** (repo root) | yes (logs visible) | cmd + python buttons | development / watching logs |

## Quick start: the desktop shortcut

Double-click the **Squid** icon on the Desktop. It runs the env's
`pythonw.exe` (no console window) directly on `main_hcs.py`. Logs still go to
the log file (`C:\Microscope_Data\logs\main_hcs.log`).

Because the app sets an explicit taskbar identity (see below), it shows as one
**Squid** button with the Cephla icon while running.

While the heavy GUI/hardware stack loads (~10-15s), a **splash screen** with the
Cephla logo and a status line ("Loading modules…" → "Initializing microscope…"
→ "Starting interface…") is shown so the launch never looks like a hang. It's
built in `main_hcs.py` (`_make_splash` / `_splash_message`) and closes the
moment the main window appears.

### Pinning to the taskbar (Windows 11)

Either:
- Right-click the running **Squid** taskbar button → *Pin to taskbar*, or
- Right-click the desktop **Squid** shortcut → *Show more options* → *Pin to
  taskbar*.

This works because the shortcut targets `pythonw.exe` (an executable). A
shortcut to `Squid.bat` cannot be pinned and shows up as separate **cmd** +
**python** buttons — which is why the desktop shortcut uses `pythonw` instead.

## Developer launcher: `Squid.bat`

Double-click `Squid.bat` (repo root), or run `Squid.bat --simulation`,
`Squid.bat --profile NAME`, etc. It keeps a console window so logs are visible,
and stays open only if startup fails (so a crash traceback is readable). Runs
from source — edit `.py` files and relaunch, no build step.

### Why both are fast

Launching the old way — a PowerShell window running
`conda activate squid; python main_hcs.py` — spent **~5.4s** in PowerShell
startup plus the conda activation hook *before Python even began*. Both launch
paths avoid that:

- Neither runs `conda activate`. `Squid.bat` sets `PATH` to the env's bin/DLL
  dirs + `SSL_CERT_FILE`; the `pythonw` shortcut relies on `main_hcs.py`'s
  Windows bootstrap (`_windows_startup_bootstrap`), which adds the env's DLL
  dirs via `os.add_dll_directory` and sets the SSL cert. Verified that the
  Windows Qt plugin, numpy/MKL, and OpenCV all load without activation.
- `main_hcs.py` also calls `SetCurrentProcessExplicitAppUserModelID("Cephla.Squid")`
  so Windows gives the GUI its own taskbar identity (single button, window
  icon) instead of grouping it under python/pythonw.
- cmd startup is ~0.2s versus PowerShell + activate at ~5.4s.

`Squid.bat` finds the environment at `%USERPROFILE%\.conda\envs\squid` by
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

The console-less **Squid** desktop shortcut points at:

- **Target:** `%USERPROFILE%\.conda\envs\squid\pythonw.exe`
- **Arguments:** `main_hcs.py`
- **Start in:** `<repo>\software`
- **Icon:** `<repo>\software\icon\cephla_logo.ico`
