"""Capture the real Desktop/Touch GUIs in disposable simulation profiles.

python tools/screenshot_touch_interface.py ../artifacts/touch-interface-2026-10-07
"""
import argparse
import datetime
import html
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

SOFTWARE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOFTWARE))


def fixture(root):
    """Known library wiring, never the operator's selected hardware/presets."""
    import yaml
    source = SOFTWARE / "machine_configs/library/machine_config_Squid+_CoolLED_ToupCam_noNIDAQ.yaml"
    config = yaml.safe_load(source.read_text(encoding="utf-8"))
    config["software"]["default_saving_path"] = str(root / "outputs")
    config["software"]["acquisition"].update(wellplate_multipoint=True, flexible_multipoint=True,
                                               fast_acquisition=False, recording=True)
    config["software"]["display"].update(use_napari_for_live_view=False, use_napari_for_mosaic=False)
    config["devices"]["main_camera"]["config"].update(
        roi={"offset_x": 0, "offset_y": 0, "width": 512, "height": 512},
        crop={"width": 512, "height": 512}, binning=1)
    for axis in ("x", "y", "z"):
        config["devices"]["stage"]["config"][axis]["homing_enabled"] = False
    path = root / "machine_configs/machine_config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    shutil.copy2(SOFTWARE / "machine_configs/illumination_channel_config.yaml",
                 root / "machine_configs/illumination_channel_config.yaml")
    for name in ("objective_and_sample_formats", "icon"):
        shutil.copytree(SOFTWARE / name, root / name)
    (root / "cache").mkdir()
    return source.name


def settle(app, seconds=0.25):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def worker(args):
    if sys.platform == "win32":
        for sub in ("Library/bin", "Library/mingw-w64/bin", "Library/usr/bin", "Scripts"):
            path = Path(sys.prefix) / sub
            if path.exists():
                os.add_dll_directory(str(path))
    # These are set before any Qt, configuration or GUI import.
    os.environ["QT_API"] = "pyqt5"
    os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen")
    root = Path(args.fixture_root)
    os.environ["SQUID_CONFIG_ROOT"] = str(root)
    os.environ["LOCALAPPDATA"] = str(root / "local")
    os.environ["APPDATA"] = str(root / "roaming")
    os.environ["PYTHONPYCACHEPREFIX"] = str(root / "pycache")
    os.environ["MPLCONFIGDIR"] = str(root / "matplotlib")
    os.environ["NUMBA_CACHE_DIR"] = str(root / "numba")
    os.environ["SQUID_LOG_DIR"] = str(root / "logs")
    # Windows appdirs resolves folders through shell APIs, ignoring APPDATA.
    # Override those helpers inside this disposable capture process only.
    import appdirs
    appdirs.user_cache_dir = lambda *a, **k: str(root / "app-cache")
    appdirs.user_config_dir = lambda *a, **k: str(root / "app-config")
    os.chdir(root)
    import control._def
    control._def.apply_simulation_mode_defaults(True)
    control._def.ENABLE_MEMORY_PROFILING = False
    from qtpy.QtWidgets import QApplication, QMessageBox, QMainWindow
    from qtpy.QtCore import Qt
    app = QApplication(["Squid capture"])
    app.setStyle("Fusion")
    # Fail promptly on unexpected pre-flight errors rather than unattended dialogs.
    def unexpected_dialog(*values, **kwargs):
        raise RuntimeError("Unexpected modal dialog: " + " ".join(str(v) for v in values[1:3]))
    QMessageBox.warning = unexpected_dialog
    QMessageBox.question = unexpected_dialog
    QMessageBox.critical = unexpected_dialog
    QMessageBox.information = unexpected_dialog
    from gui.profile_selection import ProfileSelectionDialog
    from control.core.config.repository import ConfigRepository
    from control.models.observation_state import CameraSettings, IlluminatorState, ObservationState
    repo = ConfigRepository()
    repo.create_profile("review")
    repo.set_profile("review")
    from control.models.gui_state import GuiState
    old_window = QMainWindow()
    old_window.resize(3302, 1296)
    legacy_geometry = bytes(old_window.saveGeometry().toBase64()).decode("ascii")
    repo.save_gui_state(GuiState(window_geometry_b64=legacy_geometry,
                                layouts={"desktop": {"geometry": legacy_geometry}}))
    old_window.deleteLater()
    print("Constructing", args.worker, "fixture GUI", flush=True)
    import control.microscope
    from screenshot_multipoint import _populate, _enable_z_and_time, _select_wells
    microscope = None
    win = None
    captures = []
    checks = []
    acquisition_outputs = []
    out = Path(args.out_dir)
    screen = app.primaryScreen()
    available = screen.availableGeometry()
    requested = (min(args.width, available.width()) - 16, min(args.height - 40, available.height()) - 48)

    def save(widget, name, caption, state="normal", viewport=None):
        settle(app)
        pixmap = widget.grab()
        pixmap.save(str(out / (name + ".png")))
        wanted = viewport or requested
        frame = widget.frameGeometry()
        capture = dict(file=name + ".png", layout=args.worker, caption=caption, state=state,
                       requested_viewport=list(wanted), actual_logical_size=[widget.width(), widget.height()],
                       image_size=[pixmap.width(), pixmap.height()], pixel_ratio=pixmap.devicePixelRatio(),
                       frame_bounds=list(frame.getRect()), available_screen=list(available.getRect()),
                       fits_requested=widget.width() <= wanted[0] and widget.height() <= wanted[1],
                       fits_screen=available.contains(frame))
        captures.append(capture)
        print("Captured", name, capture["actual_logical_size"], "fits:", capture["fits_requested"], flush=True)

    try:
        if args.worker == "touch":
            dialog = ProfileSelectionDialog(repo)
            dialog.combo_interface.setCurrentIndex(1)
            dialog.resize(760, 650)
            dialog.show()
            save(dialog, "launcher", "Startup profile, machine and Desktop / Touch selection", viewport=(760, 650))
            dialog.reject()
        microscope = control.microscope.Microscope.build_from_global_config(
            True, skip_homing=True, profile_name="review")
        from gui.gui_hcs.main_window import HighContentScreeningGui
        microscope.stage.move_x_to(20)
        microscope.stage.move_y_to(20)
        microscope.stage.move_z_to(1)
        # Disposable, reproducible observation presets; camera ROI stays small.
        for name, channel, mode in (("Brightfield", "LED matrix", "bf_full"),
                                    ("Darkfield", "LED matrix", "df"),
                                    ("Fluorescence", "Fluorescence 488 nm Ex", None)):
            state = ObservationState(name=name, camera_settings=CameraSettings(
                exposure_time_ms=40 if name == "Darkfield" else 20, gain_mode=2 if name == "Darkfield" else 0),
                illuminator_states=[IlluminatorState(illumination_channel=channel,
                                                    intensity=25, on=True, led_matrix_mode=mode)])
            repo.save_observation_preset(name, state)
        win = HighContentScreeningGui(microscope=microscope, is_simulation=True,
                                     skip_homing=True, interface_mode=args.worker,
                                     touch_console=args.bench, touch_console_screen=-1)
        win.resize(*requested)
        win.move(available.x() + 8, available.y() + 8)
        win.show()
        settle(app, 1)
        win.apply_persisted_window_state()
        if args.worker == "touch":
            assert (win.width(), win.height()) == requested
            assert win._collect_gui_state().layouts["desktop"]["geometry"] == legacy_geometry
            checks.append("Touch ignores legacy oversized Desktop geometry and preserves its saved layout")
        else:
            assert available.contains(win.frameGeometry())
        win.resize(*requested)
        win.move(available.x() + 8, available.y() + 8)
        settle(app)
        presets = win.observationStateWidget
        presets._combo_presets.setCurrentText("Darkfield")
        presets._on_load()
        settle(app)
        assert win.cameraSettingWidget.entry_exposureTime.value() == 40
        assert win.cameraSettingWidget.entry_analogGain.value() == 2
        assert microscope.illumination_controller.get_led_matrix_mode() == "df"
        assert set(win.illuminationWidget._channel_rows) == set(microscope.illumination_controller.channel_names)
        assert "LED matrix" in win.illuminationWidget._channel_rows
        if win.touchConsole:
            win.touchConsole._refresh_state()
            assert set(win.touchConsole._canonical_illumination._channel_rows) == set(win.illuminationWidget._channel_rows)
        checks.append("Preset load synchronizes camera, matrix mode and canonical illumination rows")
        presets._combo_presets.setCurrentText("Brightfield")
        presets._on_load()
        settle(app)
        win.liveControlWidget.btn_live.click()
        settle(app, 1)
        tasks = win.touchInterface
        if tasks:
            tasks.restore({"task": "focus", "splits": {"focus": [650, 1100]}})
            focus_split = tasks.splitter.sizes()
            tasks.select_task("live")
            tasks.select_task("focus")
            assert tasks.splitter.sizes() == focus_split
            saved = win._collect_gui_state()
            repo.save_gui_state(saved)
            tasks.select_task("live")
            win._restore_gui_state()
            assert tasks.state()["task"] == "focus"
            assert tasks.splitter.sizes() == focus_split
            checks.append("Saved task and independent splitter sizes restore through the real GUI state path")
            tasks.select_task("live")
            settle(app)
            assert not win.liveControlWidget.snap_group.isVisible()
            assert not win.liveControlWidget.btn_live.isVisible()
            assert tasks.snap_path is win.liveControlWidget.lineEdit_snapSavingDir
            assert tasks.snap_tag is win.liveControlWidget.lineEdit_snapTag
            assert abs(tasks.snap_path.width() / (tasks.snap_path.width() + tasks.snap_tag.width()) - 0.3) < 0.02
            camera = win.cameraSettingWidget
            camera_editors = (camera.entry_exposureTime, camera.entry_analogGain, camera.dropdown_cameraMode)
            assert len({editor.mapTo(tasks.camera_primary, editor.rect().topLeft()).y() for editor in camera_editors}) == 1
            nav = win.navigationWidget
            # Drive the original motion slots through each relocated arrow and
            # verify both directions, including the Z micrometre conversion.
            for axis, editor, positive, negative, delta in (
                ("x_mm", nav.entry_dX, nav.btn_moveX_forward, nav.btn_moveX_backward, 0.1),
                ("y_mm", nav.entry_dY, nav.btn_moveY_forward, nav.btn_moveY_backward, 0.1),
                ("z_mm", nav.entry_dZ, nav.btn_moveZ_forward, nav.btn_moveZ_backward, 10),
            ):
                previous_step = editor.value()
                editor.setValue(delta)
                start = getattr(win.stage.get_pos(), axis)
                positive.click()
                settle(app)
                actual_delta = editor.value() / (1000 if axis == "z_mm" else 1)
                assert abs(getattr(win.stage.get_pos(), axis) - start - actual_delta) < 0.001
                negative.click()
                settle(app)
                assert abs(getattr(win.stage.get_pos(), axis) - start) < 0.001
                editor.setValue(previous_step)
            checks.append("Single camera row, 30/70 snap fields and original XY/Z motion slots verified")
            tasks.snap_tag.setText("ribbon-review")
            tasks.snap.click()
            snap_files = list(Path(win.liveControlWidget.snap_saving_path).glob("*_ribbon-review.tif"))
            assert snap_files
            checks.append("Ribbon Snap writes an image using the original saving folder and tag")
            display = win.imageDisplayWindow
            hist = display.graphics_widget.view.ui.histogram
            assert hist.width() == tasks.autolevel.width() == tasks.histogram_width
            assert tasks.histogram_width == round(hist.sizeHint().width() * 0.75)
            assert tasks.autolevel.mapTo(display.widget, tasks.autolevel.rect().bottomLeft()).y() < hist.mapTo(display.widget, hist.rect().topLeft()).y()
            tasks.autolevel.click()
            assert display.autoLevels == tasks.autolevel.isChecked()
            tasks.autolevel.click()
            checks.append("Relocated Autolevel still controls the live histogram")
            for row in win.illuminationWidget._channel_rows.values():
                assert row["slider"].height() >= 56
            for identity in ("live", "focus", "plate"):
                tasks.select_task(identity)
                settle(app)
                if identity == "plate":
                    assert win.navigationViewer.y() > tasks.wells.geometry().bottom()
                if identity == "focus":
                    for name in ("autofocusWidget", "laserAutofocusControlWidget", "focusMapWidget"):
                        panel = getattr(win, name, None)
                        if panel is not None:
                            assert panel.isVisible(), name + " is hidden in Focus Setup"
                    af = win.laserAutofocusControlWidget
                    if af is not None:
                        assert af.btn_initialize.isVisible() and af.btn_initialize.isEnabled()
                        assert af.btn_initialize.height() >= 48
                    checks.append("Focus Setup restores inactive desktop panels and exposes touch-sized Initialize")
                save(win, "touch_" + identity, identity + " task; laser AF unavailable", "empty")
                if identity == "live":
                    combo = win.illuminationWidget._channel_rows["LED matrix"]["mode_combo"]
                    combo.showPopup()
                    settle(app)
                    assert combo.view().visualRect(combo.model().index(0, 0)).height() >= 56
                    save(combo.view().window(), "touch_led_matrix_options", "Open LED matrix options with tall touch rows")
                    combo.hidePopup()
            if win.focusCameraDialog is not None:
                win.openFocusCameraDialog()
                settle(app)
                assert win.laserAutofocusSettingWidget.parentWidget().width() >= 480
                save(win.focusCameraDialog, "touch_focus_camera_setup", "Focus camera and laser calibration settings; scrollable controls", viewport=(1000, 700))
                win.focusCameraDialog.hide()
            for index in range(win.recordTabWidget.count()):
                widget = win.recordTabWidget.widget(index)
                tasks.select_task(type(widget).__name__)
                settle(app)
                if hasattr(widget, "lineEdit_savingDir") and hasattr(widget, "lineEdit_experimentID"):
                    path, name = widget.lineEdit_savingDir, widget.lineEdit_experimentID
                    assert abs(path.width() / (path.width() + name.width()) - 0.3) < 0.02
                save(win, "touch_empty_" + type(widget).__name__, win.recordTabWidget.tabText(index), "empty")
        else:
            assert win.centralWidget.parentWidget().width() >= 580
            save(win, "desktop_empty", "Desktop layout with existing controls", "empty")
            af = win.laserAutofocusControlWidget
            if af:
                af.parentWidget().setCurrentWidget(af)
                save(af, "desktop_laser_af", "Desktop Laser AF Now, measurement and target controls", viewport=(af.width(), af.height()))
        # Populate each acquisition through its real widget and shared selector.
        for attr in ("flexibleMultiPointWidget", "wellplateMultiPointWidget"):
            widget = getattr(win, attr)
            win.recordTabWidget.setCurrentWidget(widget)
            if tasks:
                tasks.select_task(type(widget).__name__)
            if attr.startswith("flexible"):
                _populate(app, microscope, widget, 3)
            else:
                _select_wells(app, win, widget, ["B2", "C3"])
            _enable_z_and_time(app, widget)
            for i in range(widget.list_configurations.count()):
                widget.list_configurations.item(i).setCheckState(Qt.Checked if i < 2 else Qt.Unchecked)
            save(win, args.worker + "_populated_" + attr, "Selected wells / positions, Z and time settings", "populated")
            if tasks:
                settle(app)
                fixed = (widget.lineEdit_savingDir, widget.lineEdit_experimentID,
                         widget.btn_snap_images, widget.btn_startAcquisition)
                locations = [control.mapTo(widget, control.rect().topLeft()) for control in fixed]
                assert abs(fixed[0].width() / (fixed[0].width() + fixed[1].width()) - 0.3) < 0.02
                scroll = widget._touch_settings_scroll
                scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                settle(app)
                assert locations == [control.mapTo(widget, control.rect().topLeft()) for control in fixed]
                assert all(control.isVisible() and widget.rect().contains(control.mapTo(widget, control.rect().center())) for control in fixed)
                save(win, "touch_run_settings_" + attr, "Scrolled settings with fixed saving and run controls", "populated; settings scrolled to bottom")
                checks.append("Fixed 30/70 saving bar and acquisition actions remain visible while scrolling " + attr)
                scroll.verticalScrollBar().setValue(0)
        # Prove neutral task navigation doesn't change acquisition or generated regions.
        if tasks:
            selected = win.recordTabWidget.currentWidget()
            regions = dict(win.scanCoordinates.region_centers)
            wells = list(win.wellSelectionWidget.get_selected_cells())
            positions = list(win.flexibleMultiPointWidget.location_ids)
            for identity in ("live", "focus", "plate"):
                tasks.select_task(identity)
                assert win.recordTabWidget.currentWidget() is selected
                assert win.scanCoordinates.region_centers == regions
                assert win.wellSelectionWidget.get_selected_cells() == wells
                assert list(win.flexibleMultiPointWidget.location_ids) == positions
            checks.append("Neutral task navigation preserves acquisition mode and regions")
            save(win, "touch_plate_populated", "Shared selection: B2 and C3; explicit Go To Selected", "populated")
            tasks.select_task("live")
            af = win.laserAutofocusControlWidget
            if af:
                af._session_initialized = True
                af.laserAutofocusController.laser_af_properties.x_reference = 256
                af.laserAutofocusController.laser_af_properties.has_reference = True
                af._apply_button_state()
                tasks.refresh()
                save(win, "touch_laser_ready_fixture", "Laser AF ready appearance (injected fixture, no hardware validation)", "injected readiness fixture")
                af._session_initialized = False
                af._apply_button_state()
            win.resize(requested[0], 650)
            win.liveControlWidget.lineEdit_snapTag.setFocus()
            save(win, "touch_reduced_viewport", "Focused snap tag with reduced viewport; native keyboard NOT exercised",
                 "keyboard occlusion approximation", viewport=(requested[0], 650))
            win.resize(*requested)
        # Short real acquisitions, through the same Start controls as the operator.
        if win.liveController.is_live:
            win.liveControlWidget.btn_live.click()
        for attr in ("flexibleMultiPointWidget", "wellplateMultiPointWidget"):
            widget = getattr(win, attr)
            win.recordTabWidget.setCurrentWidget(widget)
            if tasks:
                tasks.select_task(type(widget).__name__)
            widget._zstack_checkbox.setChecked(False)
            widget._timelapse_checkbox.setChecked(False)
            widget.checkbox_withAutofocus.setChecked(False)
            widget.checkbox_withReflectionAutofocus.setChecked(False)
            widget.lineEdit_experimentID.setText("review_" + attr)
            widget.lineEdit_savingDir.setText(str(root / "outputs"))
            if hasattr(widget, "entry_NX"):
                widget.entry_NX.setValue(1)
                widget.entry_NY.setValue(1)
            if hasattr(widget, "radio_tiling_grid"):
                widget.radio_tiling_grid.setChecked(True)
            for i in range(widget.list_configurations.count()):
                widget.list_configurations.item(i).setCheckState(Qt.Checked if i == 0 else Qt.Unchecked)
            completed = {"finished": False}
            def mark_finished():
                completed["finished"] = True
            win.multipointController.acquisition_finished.connect(mark_finished)
            print("Starting real simulated acquisition", attr, flush=True)
            widget.btn_startAcquisition.click()
            end = time.monotonic() + 75
            seen = False
            captured = False
            while time.monotonic() < end:
                app.processEvents()
                running = win.multipointController.acquisition_in_progress()
                seen |= running
                if running and not captured:
                    if tasks:
                        mode_widget = win.recordTabWidget.currentWidget()
                        assert mode_widget.btn_startAcquisition.isEnabled()
                        for identity in ("live", "focus", "plate", type(mode_widget).__name__):
                            tasks.select_task(identity)
                            assert win.recordTabWidget.currentWidget() is mode_widget
                            assert tasks.abort.isEnabled() and not tasks.live.isEnabled() and not tasks.snap.isEnabled()
                            assert not tasks.laser.isEnabled()
                            assert not tasks.snap_settings.isEnabled() and not tasks.autolevel.isEnabled()
                        tasks.select_task("live")
                        tasks.refresh()
                        assert tasks.abort.isEnabled() and not tasks.laser.isEnabled()
                    save(win, args.worker + "_progress_" + attr, "Real simulated acquisition: persistent progress / Abort", "acquisition running")
                    captured = True
                if seen and not running and completed["finished"]:
                    break
                time.sleep(0.01)
            if win.multipointController.acquisition_in_progress():
                win.multipointController.request_abort_acquisition()
                raise RuntimeError("Acquisition did not complete before timeout: " + attr)
            if not seen:
                raise RuntimeError("Acquisition did not start: " + attr)
            if not completed["finished"]:
                raise RuntimeError("Acquisition UI did not finish: " + attr)
            win.multipointController.acquisition_finished.disconnect(mark_finished)
            output = Path(win.multipointController.base_path) / win.multipointController.experiment_ID
            image_count = sum(1 for p in output.rglob("*") if p.suffix.lower() in (".tif", ".tiff", ".bmp", ".png"))
            if image_count == 0:
                raise RuntimeError("Acquisition produced no images: " + attr)
            acquisition_outputs.append(dict(mode=attr, image_count=image_count, output=str(output),
                                            disposable=True))
            checks.append("Completed real simulated " + attr + " acquisition")
        recording = win.recordingControlWidget
        if win.recordTabWidget.indexOf(recording) >= 0:
            win.recordTabWidget.setCurrentWidget(recording)
            if tasks:
                tasks.select_task(type(recording).__name__)
            win.liveControlWidget.btn_live.click()
            recording.lineEdit_experimentID.setText("review_recording")
            recording.btn_record.click()
            settle(app, 1)
            save(win, args.worker + "_progress_recording", "Real simulated recording; persistent Abort stops recording", "recording")
            if tasks:
                assert tasks.abort.isEnabled()
                tasks._abort()
            else:
                recording.stop_recording()
            settle(app)
            win.liveControlWidget.btn_live.click()
            checks.append("Completed real simulated Simple Recording start/stop")
        if args.bench:
            console = win.touchConsole
            console.resize(1280, 800)
            save(console, args.worker + "_bench", "Optional bench: single LED matrix control and Laser AF Now", viewport=(1280, 800))
            scroll = console.findChild(__import__("qtpy.QtWidgets", fromlist=["QScrollArea"]).QScrollArea)
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum() // 2)
            save(console, args.worker + "_bench_focus", "Bench focus action and stage controls", viewport=(1280, 800))
        if not all(c["fits_requested"] for c in captures):
            checks.append("SIZE FAILURE: one or more captures expanded beyond the requested viewport")
        print("Checks:", checks, flush=True)
    finally:
        (out / (args.worker + "_manifest.json")).write_text(json.dumps(dict(
            simulation=True, profile="review", fixture="disposable library configuration + generated presets",
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            qt_backend=app.platformName(), screen_geometry=list(screen.geometry().getRect()),
            target_display=[args.width, args.height], acquisition_outputs=acquisition_outputs,
            available_screen=list(available.getRect()), screen_pixel_ratio=screen.devicePixelRatio(),
            logical_dpi=screen.logicalDotsPerInch(), captures=captures, checks=checks), indent=2), encoding="utf-8")
        if win:
            win._observation_state_cache_timer.stop()
            if win.touchConsole:
                win.touchConsole.prepare_for_shutdown()
                win.touchConsole.hide()
            win.hide()
        if microscope:
            try:
                microscope.close()
            except Exception as error:
                print("Cleanup error:", error, flush=True)
        import psutil
        children = psutil.Process().children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(children, timeout=5)


def gallery(out, manifests, command, repo_state):
    captures = [c for m in manifests for c in m["captures"]]
    data = dict(repository=repo_state, reproduction_command=command, runs=manifests)
    (out / "capture_manifest.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    limitations = "Physical touch gestures, monitor placement and laser AF require hardware review. Native Windows keyboard occlusion needs a manual check. Ready-state image is an injected fixture. Fast acquisition, tracking, fluidics and template workflows require their own configured hardware coverage."
    parts = ["<!doctype html><meta charset='utf-8'><title>Squid full touch interface review</title>",
             "<style>body{font:18px system-ui;margin:32px;background:#eee}img{width:100%;max-width:1000px}section{background:white;padding:20px;margin:24px 0}.fail{color:#a00}code{white-space:pre-wrap}</style>",
             "<h1>Full Desktop / Touch interface review</h1>",
             "<p>Task pages with tabs above the right controls; persistent Live, Snap folder/tag, Laser AF Now and Abort; one camera row; XY joypad and Z arrows; narrower histogram with Autolevel above; reduced image padding; tall LED options and larger sliders; fixed acquisition saving/run bars; 30/70 path/name editors; shared plate selection and separate layout persistence.</p>",
             "<p>" + limitations + "</p><p><code>" + html.escape(command) + "</code></p>",
             "<p><a href='../touch-console-2026-10-07/index.html'>October 7 baseline</a> · <a href='capture_manifest.json'>Manifest</a> · <a href='capture.log'>Capture log</a></p>"]
    for capture in captures:
        warning = "" if capture["fits_requested"] and capture["fits_screen"] else "<p class='fail'>Geometry exceeds requested viewport or available screen — review manifest.</p>"
        parts.append(f"<section><h2>{html.escape(capture['file'])}</h2><p>{html.escape(capture['caption'])}</p><p>{capture['layout']} / {capture['state']} · requested {capture['requested_viewport']} · actual {capture['actual_logical_size']} · image {capture['image_size']}</p>{warning}<a href='{capture['file']}'><img src='{capture['file']}'></a></section>")
    (out / "index.html").write_text("\n".join(parts), encoding="utf-8")
    summary = "\n".join(check for m in manifests for check in m["checks"])
    (out / "validation.txt").write_text(command + "\n\n" + summary + "\n\n" + limitations, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--bench", action="store_true")
    parser.add_argument("--worker", choices=("desktop", "touch"))
    parser.add_argument("--fixture-root")
    args = parser.parse_args()
    if args.worker:
        code = 0
        try:
            worker(args)
        except BaseException:
            import traceback
            traceback.print_exc()
            code = 1
        # GUI workers can hold interpreter shutdown open. Children were cleaned above.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    out = Path(args.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=False)
    command = "python tools/screenshot_touch_interface.py " + args.out_dir + (" --bench" if args.bench else "")
    def git(*arguments):
        return subprocess.check_output(["git", *arguments], cwd=SOFTWARE, text=True).strip()
    state = dict(branch=git("branch", "--show-current"), commit=git("rev-parse", "HEAD"), dirty=git("status", "--short"))
    state["source_sha256"] = {str(p.relative_to(SOFTWARE)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for folder in ("gui", "control", "tools", "squid")
                              for p in (SOFTWARE / folder).rglob("*.py")}
    manifests = []
    failed = False
    with (out / "capture.log").open("w", encoding="utf-8") as log:
        for mode in ("touch", "desktop"):
            with tempfile.TemporaryDirectory(prefix="squid-review-", dir=out) as temp:
                root = Path(temp)
                fixture(root)
                argv = [sys.executable, str(Path(__file__).resolve()), str(out), "--worker", mode,
                        "--fixture-root", str(root), "--width", str(args.width), "--height", str(args.height)]
                if args.bench:
                    argv.append("--bench")
                process = subprocess.Popen(argv, stdout=log, stderr=log)
                try:
                    code = process.wait(timeout=240)
                except subprocess.TimeoutExpired:
                    import psutil
                    descendants = psutil.Process(process.pid).children(recursive=True)
                    for child in descendants:
                        try:
                            child.kill()
                        except psutil.Error:
                            pass
                    process.kill()
                    process.wait()
                    psutil.wait_procs(descendants, timeout=5)
                    log.write(f"\n{mode} capture timed out; terminated its process tree.\n")
                    code = 1
                failed |= code != 0
                manifest_path = out / (mode + "_manifest.json")
                if manifest_path.exists():
                    manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))
    failed |= any(not c["fits_requested"] or not c["fits_screen"] for m in manifests for c in m["captures"])
    gallery(out, manifests, command, state)
    print("Gallery:", out / "index.html")
    if failed:
        raise SystemExit("Capture failed; see capture.log and incomplete gallery")


if __name__ == "__main__":
    main()
