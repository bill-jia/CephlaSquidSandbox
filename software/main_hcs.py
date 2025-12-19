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

# Set QT_API environment variable before importing Qt libraries
# This ensures we use PyQt5 as the Qt backend
print("Test")
import argparse
import glob
import logging
import os

os.environ["QT_API"] = "pyqt5"
import signal
import sys

# Qt libraries for GUI
from qtpy.QtWidgets import *
from qtpy.QtGui import *

import squid.logging

# Set up exception logging to catch and log unhandled exceptions
squid.logging.setup_uncaught_exception_logging()

# Application-specific libraries
import control.gui_hcs as gui
from configparser import ConfigParser
from control.widgets import ConfigEditorBackwardsCompatible, StageUtils
from control._def import CACHED_CONFIG_FILE_PATH
from control._def import USE_TERMINAL_CONSOLE
import control.utils
import control.microscope


if USE_TERMINAL_CONSOLE:
    from control.console import ConsoleThread


def show_config(cfp, configpath, main_gui):
    """
    Display the configuration editor dialog.
    
    Args:
        cfp: ConfigParser instance with loaded configuration
        configpath: Path to the configuration file
        main_gui: Reference to the main GUI window
    """
    config_widget = ConfigEditorBackwardsCompatible(cfp, configpath, main_gui)
    config_widget.exec_()


if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulation", help="Run the GUI with simulated hardware.", action="store_true")
    parser.add_argument("--live-only", help="Run the GUI only the live viewer.", action="store_true")
    parser.add_argument("--verbose", help="Turn on verbose logging (DEBUG level)", action="store_true")
    args = parser.parse_args()

    # Set up logging
    log = squid.logging.get_logger("main_hcs")

    if args.verbose:
        log.info("Turning on debug logging.")
        squid.logging.set_stdout_log_level(logging.DEBUG)

    # Set up file logging for debugging
    if not squid.logging.add_file_logging(f"{squid.logging.get_default_log_directory()}/main_hcs.log"):
        log.error("Couldn't setup logging to file!")
        sys.exit(1)

    log.info(f"Squid Repository State: {control.utils.get_squid_repo_state_description()}")

    # Load configuration file
    # The configuration file contains all microscope-specific settings:
    # - Stage parameters (velocities, accelerations, limits)
    # - Camera settings (exposure, binning, pixel format)
    # - Illumination settings (LED/laser channels, intensities)
    # - Autofocus parameters
    # - Well plate formats and positions
    legacy_config = False
    cf_editor_parser = ConfigParser()
    config_files = glob.glob("." + "/" + "configuration*.ini")
    if config_files:
        cf_editor_parser.read(CACHED_CONFIG_FILE_PATH)
    else:
        log.error("configuration*.ini file not found, defaulting to legacy configuration")
        legacy_config = True
    
    # Initialize Qt application
    app = QApplication([])
    app.setStyle("Fusion")
    # Allow shutdown via Ctrl+C even after GUI is shown
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Build the microscope object from configuration
    # This creates all hardware interfaces: stage, camera, illumination, addons
    microscope = control.microscope.Microscope.build_from_global_config(args.simulation)
    
    # Create the main GUI window
    # The GUI provides:
    # - Live image viewer
    # - Stage position controls
    # - Illumination controls
    # - Acquisition settings
    # - Multi-point acquisition interface
    win = gui.HighContentScreeningGui(
        microscope=microscope, is_simulation=args.simulation, live_only_mode=args.live_only
    )

    # Set up menu bar with configuration and utility options
    file_menu = QMenu("File", win)

    if not legacy_config:
        # Add menu item to open configuration editor
        config_action = QAction("Microscope Settings", win)
        config_action.triggered.connect(lambda: show_config(cf_editor_parser, config_files[0], win))
        file_menu.addAction(config_action)

    microscope_utils_menu = QMenu("Utils", win)

    # Add stage utilities (manual stage control, homing, etc.)
    stage_utils_action = QAction("Stage Utils", win)
    stage_utils_action.triggered.connect(win.stageUtils.show)
    microscope_utils_menu.addAction(stage_utils_action)

    # Add camera settings window if available (main camera)
    try:
        csw = win.cswWindow
        if csw is not None:
            csw_action = QAction("Camera Settings", win)
            csw_action.triggered.connect(csw.show)
            file_menu.addAction(csw_action)
    except AttributeError:
        pass

    # Add focus camera settings window if available
    try:
        csw_fc = win.cswfcWindow
        if csw_fc is not None:
            csw_fc_action = QAction("Camera Settings (Focus Camera)", win)
            csw_fc_action.triggered.connect(csw_fc.show)
            file_menu.addAction(csw_fc_action)
    except AttributeError:
        pass

    # Add menus to menu bar
    menu_bar = win.menuBar()
    menu_bar.addMenu(file_menu)
    menu_bar.addMenu(microscope_utils_menu)
    win.show()

    # Optionally start interactive console for debugging
    if USE_TERMINAL_CONSOLE:
        console_locals = {"microscope": win.microscope}
        console_thread = ConsoleThread(console_locals)
        console_thread.start()

    # Start the Qt event loop
    sys.exit(app.exec_())
