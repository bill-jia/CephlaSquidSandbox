"""
Main entry point for the High Content Screening (HCS) microscope control software.

This module initializes the Qt application, sets up logging, loads configuration files,
and creates the main GUI window with all microscope components. It handles command-line
arguments for simulation mode, live-only mode, and verbose logging.

The software controls a complete microscope system including:
- Stage positioning (X, Y, Z, Theta)
- Camera acquisition (main and focus cameras)
- Illumination control (LEDs, lasers, filter wheels)
- Autofocus systems
- Optional addons (piezo stages, fluidics, etc.)
"""

import argparse
import logging
import os
import sys


def _windows_startup_bootstrap():
    """Make the app launchable by a bare env Python without ``conda activate``.

    When started via a console-less ``pythonw.exe`` shortcut (the clean,
    pinnable taskbar launch), conda activation hasn't run, so:
      - add the env's native DLL dirs so Qt/numpy/OpenCV resolve their DLLs
      - point SSL at the env's cert bundle (conda's openssl activate.d script)
      - set an explicit AppUserModelID so Windows shows one "Squid" taskbar
        button (using the app's window icon) instead of grouping under
        python/pythonw — which also lets it be pinned cleanly.

    Every step is a harmless no-op off Windows or when the env is already
    activated (DLL dirs just get added twice, env vars are left as-is).
    """
    if sys.platform != "win32":
        return
    for sub in ("Library\\bin", "Library\\mingw-w64\\bin", "Library\\usr\\bin", "Scripts"):
        dll_dir = os.path.join(sys.prefix, sub)
        if os.path.isdir(dll_dir):
            try:
                os.add_dll_directory(dll_dir)
            except OSError:
                pass
    cert = os.path.join(sys.prefix, "Library", "ssl", "cacert.pem")
    if os.path.isfile(cert):
        os.environ.setdefault("SSL_CERT_FILE", cert)
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Cephla.Squid")
    except Exception:
        pass


_windows_startup_bootstrap()

# Set QT_API environment variable before importing Qt libraries
# This ensures we use PyQt5 as the Qt backend
os.environ["QT_API"] = "pyqt5"
import signal

# Qt libraries for GUI
from qtpy.QtWidgets import *
from qtpy.QtGui import *

import squid.logging

# Set up exception logging to catch and log unhandled exceptions
squid.logging.setup_uncaught_exception_logging()

# Application-specific libraries.
#
# Only lightweight config flags are imported at module load. The heavy stack —
# gui.gui_hcs (napari/pyqtgraph) and control.microscope (camera/NIDAQ drivers) —
# costs ~13s to import, so it is deferred into __main__ and imported *after* the
# profile picker is shown. This lets the picker appear in <1s instead of ~13s.
from control._def import USE_TERMINAL_CONSOLE, ENABLE_MCP_SERVER_SUPPORT, CONTROL_SERVER_HOST, CONTROL_SERVER_PORT
import control._def

# Import auto-migration function
# from tools.migrate_acquisition_configs import run_auto_migration


def _make_splash():
    """Build a branded splash (Cephla logo) shown during the slow startup load.

    With the console-less ``pythonw`` launch there is otherwise no on-screen
    feedback while the heavy GUI/hardware stack imports and initializes
    (~10-15s), which can look like a hang or an error. Rendering is
    best-effort: any failure still yields a plain splash rather than blocking
    startup.
    """
    from qtpy.QtCore import Qt, QRectF
    from qtpy.QtGui import QColor, QFont, QPainter, QPen, QPixmap
    from qtpy.QtWidgets import QSplashScreen

    side = 440
    canvas = QPixmap(side, side)
    canvas.fill(QColor("#ffffff"))
    icon_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon")

    painter = QPainter(canvas)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        logo_box = QRectF(90, 40, side - 180, side - 180)
        drawn = False
        try:
            from qtpy.QtSvg import QSvgRenderer

            renderer = QSvgRenderer(os.path.join(icon_dir, "cephla_logo.svg"))
            if renderer.isValid():
                renderer.render(painter, logo_box)
                drawn = True
        except Exception:
            drawn = False
        if not drawn:
            pm = QPixmap(os.path.join(icon_dir, "cephla_logo.ico"))
            if not pm.isNull():
                pm = pm.scaled(
                    int(logo_box.width()), int(logo_box.height()),
                    Qt.KeepAspectRatio, Qt.SmoothTransformation,
                )
                painter.drawPixmap(int(logo_box.x()), int(logo_box.y()), pm)
        title_font = QFont()
        title_font.setPointSize(26)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor("#31c4f3"))
        painter.drawText(QRectF(0, 300, side, 56), Qt.AlignHCenter | Qt.AlignVCenter, "Squid")
        painter.setPen(QPen(QColor("#dcdcdc")))
        painter.drawRect(0, 0, side - 1, side - 1)
    finally:
        painter.end()

    return QSplashScreen(canvas)


def _splash_message(splash, text):
    """Update the splash status line and repaint immediately (no-op if None)."""
    from qtpy.QtCore import Qt
    from qtpy.QtGui import QColor
    from qtpy.QtWidgets import QApplication

    if splash is None:
        return
    splash.showMessage("   " + text + "   ", Qt.AlignBottom | Qt.AlignHCenter, QColor("#146b86"))
    QApplication.processEvents()


if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulation", help="Run the GUI with simulated hardware.", action="store_true")
    parser.add_argument("--live-only", help="Run the GUI only the live viewer.", action="store_true")
    parser.add_argument("--verbose", help="Turn on verbose logging (DEBUG level)", action="store_true")
    parser.add_argument(
        "--start-server", help="Auto-start the MCP control server for programmatic control", action="store_true"
    )
    parser.add_argument(
        "--skip-init",
        help="Skip hardware initialization and homing (for restart after settings change)",
        action="store_true",
    )
    parser.add_argument(
        "--skip-homing",
        help="Initialize devices but skip mechanical motions (stage/filter wheel homing, etc.)",
        action="store_true",
    )
    parser.add_argument(
        "--profile",
        help="User profile to load (directory name under software/user_profiles/). "
             "Defaults to the last-active profile, then the first available, "
             "then a freshly created 'default'.",
        default=None,
    )
    args = parser.parse_args()

    # Set up logging
    log = squid.logging.get_logger("main_hcs")

    if args.verbose:
        log.info("Turning on debug logging.")
        squid.logging.set_stdout_log_level(logging.DEBUG)

    # Set up file logging for debugging
    if not squid.logging.add_file_logging(f"{squid.logging.get_default_log_directory()}/main_hcs.log", verbose=args.verbose):
        log.error("Couldn't setup logging to file!")
        sys.exit(1)

    # When running with --simulation, default all per-component SIMULATE_* to True
    # (config can override with simulate_camera=false etc. to use real hardware for that component)
    control._def.apply_simulation_mode_defaults(args.simulation)

    # Auto-migrate legacy acquisition configurations if present
    # run_auto_migration()

    app = QApplication(["Squid"])
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon("icon/cephla_logo.ico"))
    # This allows shutdown via ctrl+C even after the gui has popped up.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Resolve which user profile to load BEFORE importing the heavy GUI/hardware
    # stack, so the picker appears in <1s instead of after the full ~13s import.
    # --profile on the CLI wins; otherwise prompt the user with a dialog. The
    # dialog module is deliberately a lightweight import (Qt + ConfigRepository).
    profile_name = args.profile
    if profile_name is None:
        from gui.profile_selection import prompt_for_profile
        profile_name = prompt_for_profile()
        if profile_name is None:
            log.info("Profile selection cancelled — exiting")
            sys.exit(0)

    # Branded splash so the launch never looks like a hang: with the console-less
    # pythonw shortcut there is otherwise no feedback while the heavy GUI/hardware
    # stack imports and initializes (~10-15s).
    splash = _make_splash()
    splash.show()
    _splash_message(splash, "Loading modules…")

    # Heavy imports happen now, with the picker already dismissed: napari/pyqtgraph
    # (gui.gui_hcs) and the full camera/NIDAQ driver stack (control.microscope).
    import control.utils
    import control.microscope
    import gui.gui_hcs as gui

    if USE_TERMINAL_CONSOLE:
        from control.console import ConsoleThread

    if ENABLE_MCP_SERVER_SUPPORT:
        from control.microscope_control_server import MicroscopeControlServer
        from gui.widgets.claude import ClaudeApiKeyDialog, load_claude_api_key_from_cache
        import shlex
        import subprocess
        import shutil
        import tempfile

    log.info(f"Squid Repository State: {control.utils.get_squid_repo_state_description()}")

    # Build the microscope object from the global configuration. This will initialize all hardware components
    _splash_message(splash, "Initializing microscope…")
    microscope = control.microscope.Microscope.build_from_global_config(
        args.simulation,
        skip_init=args.skip_init,
        skip_homing=args.skip_homing,
        profile_name=profile_name,
    )

    _splash_message(splash, "Starting interface…")
    win = gui.HighContentScreeningGui(
        microscope=microscope,
        is_simulation=args.simulation,
        live_only_mode=args.live_only,
        skip_init=args.skip_init,
        skip_homing=args.skip_homing,
    )

    microscope_utils_menu = QMenu("Utils", win)

    # Add stage utilities (manual stage control, homing, etc.)
    stage_utils_action = QAction("Stage Utils", win)
    stage_utils_action.triggered.connect(win.stageUtils.show)
    microscope_utils_menu.addAction(stage_utils_action)

    workflow_runner_action = QAction("Workflow Runner...", win)
    workflow_runner_action.triggered.connect(win.openWorkflowRunner)
    microscope_utils_menu.addAction(workflow_runner_action)

    # Add menus to menu bar
    menu_bar = win.menuBar()
    menu_bar.addMenu(microscope_utils_menu)

    # Show startup warning if simulated disk I/O mode is enabled
    if control._def.SIMULATED_DISK_IO_ENABLED:
        splash.hide()  # don't let the splash cover the modal warning
        QMessageBox.warning(
            None,
            "Development Mode Active",
            "SIMULATED DISK I/O IS ENABLED\n\n"
            "Images are encoded to memory (exercises RAM/CPU) but NOT saved to disk.\n"
            f"Simulated write speed: {control._def.SIMULATED_DISK_IO_SPEED_MB_S} MB/s\n\n"
            "This mode is for development/testing only.\n\n"
            "To disable: Settings > Settings... > Dev tab",
            QMessageBox.Ok,
        )

    # Restore last-session window geometry if available; otherwise maximize.
    # Close the splash as soon as the main window appears.
    splash.finish(win)
    gui_state = microscope.config_repo.get_gui_state()
    if gui_state is not None and gui_state.window_geometry_b64:
        win.show()
        win.apply_persisted_window_state()
    else:
        win.showMaximized()

    # Optionally start interactive console for debugging
    if USE_TERMINAL_CONSOLE:
        console_locals = {"microscope": win.microscope}
        console_thread = ConsoleThread(console_locals)
        console_thread.start()

    if ENABLE_MCP_SERVER_SUPPORT:
        # Create control server but don't start it yet (on-demand)
        control_server = MicroscopeControlServer(
            microscope=microscope,
            host=CONTROL_SERVER_HOST,
            port=CONTROL_SERVER_PORT,
            multipoint_controller=win.multipointController,
            scan_coordinates=win.scanCoordinates,
            gui=win,
        )
        # Ensure clean shutdown of control server socket on app exit
        app.aboutToQuit.connect(control_server.stop)

        def start_control_server_if_needed():
            """Start the control server if not already running."""
            if not control_server.is_running():
                control_server.start()
                log.info(f"MCP control server started on {CONTROL_SERVER_HOST}:{CONTROL_SERVER_PORT}")
                return True
            return False

        # Load cached Anthropic API key for Claude Code
        load_claude_api_key_from_cache()

        # Auto-start server if --start-server flag is provided
        if args.start_server:
            start_control_server_if_needed()

        # Add MCP menu items to Settings menu
        settings_menu = None
        for action in menu_bar.actions():
            if action.text() == "Settings":
                settings_menu = action.menu()
                break

        if settings_menu:
            settings_menu.addSeparator()

            # Control server toggle
            control_server_action = QAction("Enable MCP Control Server", win)
            control_server_action.setCheckable(True)
            control_server_action.setChecked(False)
            control_server_action.setToolTip("Start/stop the MCP control server for Claude Code integration")

            def on_control_server_toggled(checked):
                if checked:
                    start_control_server_if_needed()
                else:
                    control_server.stop()
                    log.info("MCP control server stopped")

            control_server_action.toggled.connect(on_control_server_toggled)
            settings_menu.addAction(control_server_action)

            # Python exec toggle
            python_exec_action = QAction("Enable MCP Python Exec", win)
            python_exec_action.setCheckable(True)
            python_exec_action.setChecked(False)
            python_exec_action.setToolTip("Allow MCP clients (e.g., Claude Code) to execute arbitrary Python code")

            def on_python_exec_toggled(checked):
                if checked:
                    reply = QMessageBox.warning(
                        win,
                        "Security Warning",
                        "Enabling MCP Python Exec allows AI agents to execute arbitrary Python code "
                        "with full access to microscope hardware and system resources.\n\n"
                        "Only enable this if you trust the connected MCP client.\n\n"
                        "Warning: Even trusted clients can execute code that may cause unintended "
                        "damage to hardware or samples due to bugs or mistakes.\n\n"
                        "Do you want to continue?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No,
                    )
                    if reply == QMessageBox.Yes:
                        control_server.set_python_exec_enabled(True)
                    else:
                        python_exec_action.setChecked(False)
                else:
                    control_server.set_python_exec_enabled(False)

            python_exec_action.toggled.connect(on_python_exec_toggled)
            settings_menu.addAction(python_exec_action)

            # Add Set Anthropic API Key action
            def open_api_key_dialog():
                dialog = ClaudeApiKeyDialog(parent=win)
                dialog.exec_()

            api_key_action = QAction("Set Anthropic API Key...", win)
            api_key_action.setToolTip("Set the API key used when launching Claude Code")
            api_key_action.triggered.connect(open_api_key_dialog)
            settings_menu.addAction(api_key_action)

            # Add Launch Claude Code action
            def launch_claude_code():
                # Check for API key (optional — user may already be logged in via claude.ai)
                api_key = control._def.ANTHROPIC_API_KEY
                if not api_key:
                    reply = QMessageBox.question(
                        win,
                        "API Key Not Set",
                        "No Anthropic API key is configured.\n\n"
                        "If you are already logged into claude.ai, you can skip this.\n"
                        "Otherwise, would you like to set an API key now?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No,
                    )
                    if reply == QMessageBox.Yes:
                        open_api_key_dialog()
                        api_key = control._def.ANTHROPIC_API_KEY

                # Start control server if not running
                if start_control_server_if_needed():
                    control_server_action.setChecked(True)

                # Get the directory containing .mcp.json
                working_dir = os.path.dirname(os.path.abspath(__file__))

                # Check if claude is installed
                if not shutil.which("claude"):
                    reply = QMessageBox.question(
                        win,
                        "Claude Code Not Found",
                        "Claude Code CLI is not installed.\n\n" "Would you like to install it now?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.Yes,
                    )
                    if reply == QMessageBox.Yes:
                        try:
                            if sys.platform in ("linux", "darwin"):
                                # Use official install script for Linux/macOS
                                install_cmd = (
                                    "curl -fsSL https://claude.ai/install.sh | bash && "
                                    "echo 'Installation complete! Press Enter to continue...' && read"
                                )
                                if sys.platform == "linux":
                                    subprocess.Popen(["gnome-terminal", "--", "bash", "-c", install_cmd])
                                else:
                                    script = f'tell application "Terminal" to do script "{install_cmd}"'
                                    subprocess.Popen(["osascript", "-e", script])
                            elif sys.platform == "win32":
                                # Use official install script for Windows (CMD version)
                                install_cmd = "curl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd && del install.cmd"
                                subprocess.Popen(f'start cmd /k "{install_cmd}"', shell=True)
                            log.info("Started Claude Code installation")
                            QMessageBox.information(
                                win,
                                "Installation Started",
                                "Claude Code installation has started in a terminal window.\n\n"
                                "After installation completes, restart Squid to use Claude Code.",
                            )
                        except Exception as e:
                            log.error(f"Failed to start installation: {e}")
                            QMessageBox.warning(
                                win, "Installation Failed", f"Failed to start installation:\n\n{str(e)}"
                            )
                    return

                # Write a temporary launcher script so the API key never appears
                # in the terminal's visible command line (e.g., in `ps` output).
                # On Unix, the script deletes itself before launching claude.
                # On Windows, the batch file self-deletes after claude exits.
                try:
                    if sys.platform == "win32":
                        fd, script_path = tempfile.mkstemp(suffix=".bat", prefix="squid_claude_")
                        with os.fdopen(fd, "w") as f:
                            f.write("@echo off\n")
                            f.write("setlocal\n")
                            if api_key:
                                safe_key = api_key.replace("%", "%%").replace('"', '""')
                                f.write(f'set "ANTHROPIC_API_KEY={safe_key}"\n')
                            f.write('set "CLAUDE_MODEL=claude-opus-4-6"\n')
                            f.write(f'cd /d "{working_dir}"\n')
                            f.write("claude\n")
                            f.write("endlocal\n")
                            f.write('(goto) 2>nul & del "%~f0"\n')
                    else:
                        fd, script_path = tempfile.mkstemp(suffix=".sh", prefix="squid_claude_")
                        with os.fdopen(fd, "w") as f:
                            f.write("#!/bin/bash\n")
                            if api_key:
                                f.write(f"export ANTHROPIC_API_KEY={shlex.quote(api_key)}\n")
                            f.write("export CLAUDE_MODEL=claude-opus-4-6\n")
                            f.write(f"rm -f {shlex.quote(script_path)}\n")
                            f.write(f"cd {shlex.quote(working_dir)}\n")
                            f.write("claude\n")
                            # On Linux, keep the terminal open after claude exits.
                            # Unset API key so it doesn't linger in the interactive shell.
                            if sys.platform != "darwin":
                                f.write("unset ANTHROPIC_API_KEY\n")
                                f.write("exec bash\n")
                        os.chmod(script_path, 0o700)
                except Exception as e:
                    try:
                        os.unlink(script_path)
                    except (OSError, NameError) as cleanup_err:
                        log.debug(f"Failed to remove temporary launcher script during cleanup: {cleanup_err}")
                    log.error(f"Failed to create launcher script: {e}")
                    QMessageBox.warning(
                        win,
                        "Launch Failed",
                        f"Failed to create launcher script:\n\n{str(e)}",
                    )
                    return

                try:
                    if sys.platform == "darwin":  # macOS
                        bash_cmd = f"bash {shlex.quote(script_path)}"
                        escaped_cmd = bash_cmd.replace("\\", "\\\\").replace('"', '\\"')
                        script = (
                            'tell application "Terminal"\n'
                            "    activate\n"
                            f'    do script "{escaped_cmd}"\n'
                            "end tell"
                        )
                        subprocess.Popen(["osascript", "-e", script])

                    elif sys.platform == "win32":  # Windows
                        subprocess.Popen(f'start cmd /k "{script_path}"', shell=True)

                    else:  # Linux
                        terminals = [
                            ["gnome-terminal", "--", "bash", script_path],
                            ["konsole", "-e", "bash", script_path],
                            ["xfce4-terminal", "-e", f"bash {shlex.quote(script_path)}"],
                            ["xterm", "-e", f"bash {shlex.quote(script_path)}"],
                        ]
                        launched = False
                        for cmd in terminals:
                            try:
                                subprocess.Popen(cmd)
                                launched = True
                                break
                            except OSError:
                                continue

                        if not launched:
                            try:
                                os.unlink(script_path)
                            except OSError as cleanup_err:
                                log.warning(
                                    f"Failed to clean up launcher script at {script_path}: {cleanup_err}. "
                                    "This file contains the API key and should be manually deleted."
                                )
                            QMessageBox.warning(
                                win,
                                "Terminal Not Found",
                                "Could not find a supported terminal emulator.\n\n"
                                "Supported: gnome-terminal, konsole, xfce4-terminal, xterm",
                            )
                            return

                    log.info("Launched Claude Code")

                except Exception as e:
                    # Clean up temp script on failure
                    try:
                        os.unlink(script_path)
                    except OSError as cleanup_err:
                        log.warning(
                            f"Failed to clean up launcher script at {script_path}: {cleanup_err}. "
                            "This file contains the API key and should be manually deleted."
                        )
                    log.error(f"Failed to launch Claude Code: {e}")
                    QMessageBox.warning(
                        win,
                        "Launch Failed",
                        f"Failed to launch Claude Code:\n\n{str(e)}",
                    )

            launch_claude_action = QAction("Launch Claude Code", win)
            launch_claude_action.setToolTip("Open Claude Code CLI in a terminal with MCP connection")
            launch_claude_action.triggered.connect(launch_claude_code)
            settings_menu.addAction(launch_claude_action)

    # Use os._exit() to prevent segfault during Python's shutdown sequence.
    # PyQt5's C++ destructor order conflicts with Python's garbage collector.
    #
    # Note: This does NOT skip critical cleanup because:
    # - closeEvent() runs when the window closes (before app.exec_() returns)
    # - aboutToQuit signal fires before app.exec_() returns
    # All hardware cleanup (camera, stage, microcontroller) happens in closeEvent,
    # which completes before os._exit() is called.
    exit_code = app.exec_()
    logging.shutdown()  # Flush log handlers before os._exit() bypasses Python cleanup
    os._exit(exit_code)
