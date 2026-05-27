@echo off
setlocal enableextensions
title Squid Microscope

rem ──────────────────────────────────────────────────────────────────────────
rem  Snappy Squid launcher (runs from source).
rem
rem  Launching via PowerShell + `conda activate squid` costs ~5s of shell/hook
rem  overhead before Python even starts. This batch skips `conda activate` and
rem  runs the env's Python directly, which is all the app actually needs.
rem  Edit the .py files and relaunch — changes are live, no rebuild step.
rem
rem  Usage:   double-click, or  Squid.bat [args]   e.g.  Squid.bat --simulation
rem  Override the env location by setting SQUID_ENV before running.
rem ──────────────────────────────────────────────────────────────────────────

if not defined SQUID_ENV set "SQUID_ENV=%USERPROFILE%\.conda\envs\squid"

if not exist "%SQUID_ENV%\python.exe" (
    echo(
    echo  ERROR: could not find the 'squid' conda environment at:
    echo         %SQUID_ENV%
    echo(
    echo  Create it ^(see software\docs^), or set SQUID_ENV to your env path.
    echo(
    pause
    exit /b 1
)

rem Replicate the load-bearing parts of `conda activate squid`:
rem   - env bin/DLL dirs on PATH so native extensions (Qt, numpy/MKL, cv2) load
rem   - SSL cert bundle (from the env's openssl activate.d script)
set "CONDA_PREFIX=%SQUID_ENV%"
set "CONDA_DEFAULT_ENV=squid"
set "PATH=%SQUID_ENV%;%SQUID_ENV%\Library\mingw-w64\bin;%SQUID_ENV%\Library\usr\bin;%SQUID_ENV%\Library\bin;%SQUID_ENV%\Scripts;%SQUID_ENV%\bin;%PATH%"
if "%SSL_CERT_FILE%"=="" set "SSL_CERT_FILE=%SQUID_ENV%\Library\ssl\cacert.pem"

rem main_hcs.py uses paths relative to software\ (icon\, cache\, machine_configs\).
cd /d "%~dp0software"

"%SQUID_ENV%\python.exe" main_hcs.py %*
set "RC=%ERRORLEVEL%"

rem Keep the window open only on failure, so a startup crash stays readable.
if not "%RC%"=="0" (
    echo(
    echo  Squid exited with code %RC%.  ^(See the message above / the log file.^)
    pause
)
endlocal
