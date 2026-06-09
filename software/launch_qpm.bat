@echo off
REM launch_qpm.bat — double-click fallback for the QPM / SciMicroscopy dome launcher.
REM Delegates to launch_qpm.ps1 (the single source of truth).
powershell.exe -NoExit -NoProfile -ExecutionPolicy Bypass -File "C:\Code\CephlaSquidSandbox\software\launch_qpm.ps1"
