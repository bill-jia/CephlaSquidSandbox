"""Render the multipoint panels off simulated hardware and save them as PNGs.

Usage (from software/, squid env, hardware may be off):
    python tools/screenshot_multipoint.py <out_dir> [--profile NAME] [--populate N]
        [--enable-zt] [--select-wells A1,B2] [--tiling fraction|grid|both]
        [--nx N --ny N] [--nav-shot]

``--populate N`` walks the simulated stage over N spots and adds each one to the
Flexible panel's position table (selecting the middle row) so the positions block
is rendered with content rather than empty.

``--select-wells`` selects those wells on the well selector the wellplate panel
listens to, so its Tiling rows are rendered against a real region set.
``--tiling`` picks the tiling method before grabbing; ``both`` grabs the wellplate
panel twice, as ``wellplate_fraction.png`` and ``wellplate_grid.png``.
``--nav-shot`` also grabs the navigation viewer (the plate map with the tile
overlay) as ``navigation_grid.png``.

Builds the whole HCS GUI with every SIMULATE_* flag on, grabs the Flexible and
Wellplate multipoint widgets at a fixed width, writes the PNGs into ``out_dir``,
then exits. Nothing is shown on screen for long: the window is shown only so
Qt lays the widgets out.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _populate(app, microscope, widget, count):
    """Add ``count`` positions by walking the simulated stage, then select one."""
    stage = microscope.stage
    pos = stage.get_pos()
    for i in range(count):
        stage.move_x_to(pos.x_mm + 1.5 * i)
        stage.move_y_to(pos.y_mm + 0.75 * i)
        widget.add_location()
        app.processEvents()
    widget._select_row(min(1, count - 1))
    app.processEvents()
    print(f"populated {widget.table_location_list.rowCount()} positions")


def _enable_z_and_time(app, widget):
    """Turn on the Z-stack and Time-lapse groups (and Set Z-range) before grabbing.

    The two groups default to off, so the shipped screenshot shows every row greyed
    out; this renders the other half of the state.
    """
    widget._zstack_checkbox.setChecked(True)
    widget._timelapse_checkbox.setChecked(True)
    widget.entry_NZ.setValue(5)
    widget.entry_deltaZ.setValue(2.0)
    widget.entry_Nt.setValue(3)
    widget.entry_dt.setValue(60)
    widget.checkbox_set_z_range.setChecked(True)
    app.processEvents()
    print(f"enabled Z-stack + Time-lapse on {widget.__class__.__name__}")


def _select_wells(app, win, widget, names):
    """Put the wellplate panel in Select Wells mode and select ``names``.

    Selection goes through the well selector's own item model, which is what the
    panel listens to (``signal_wellSelected`` -> ``update_well_coordinates``), so
    the regions are generated exactly as they are for a click.
    """
    selector = getattr(win, "wellSelectionWidget", None)
    if selector is None:
        print("no wellSelectionWidget")
        return
    widget.checkbox_xy.setChecked(True)
    widget.combobox_xy_mode.setCurrentText("Select Wells")
    app.processEvents()
    selector.clearSelection()
    for name in names:
        row, col = widget._parse_well_name(name)
        if row is None or row >= selector.rowCount() or col >= selector.columnCount():
            print(f"well {name} is not on this plate format")
            continue
        item = selector.item(row, col)
        if item is not None:
            item.setSelected(True)
    app.processEvents()
    print(f"selected wells {names}; regions: {list(widget.scanCoordinates.region_centers)}")


def _set_tiling(app, widget, method, nx, ny):
    """Pick the tiling method (and the grid size) before grabbing."""
    widget.entry_NX.setValue(nx)
    widget.entry_NY.setValue(ny)
    if method == "grid":
        widget.radio_tiling_grid.setChecked(True)
    else:
        widget.radio_tiling_fraction.setChecked(True)
    app.processEvents()
    fov_count = sum(len(v) for v in widget.scanCoordinates.region_fov_coordinates.values())
    print(f"tiling={method} nx={nx} ny={ny}: {fov_count} FOVs over "
          f"{len(widget.scanCoordinates.region_fov_coordinates)} region(s)")


def _raise_tab(widget):
    """Bring the widget's tab to the front so it is laid out at full size."""
    parent = widget.parentWidget()
    while parent is not None:
        if hasattr(parent, "setCurrentWidget") and hasattr(parent, "indexOf") and parent.indexOf(widget) >= 0:
            parent.setCurrentWidget(widget)
        parent = parent.parentWidget()


def _grab(app, widget, path, width):
    widget.resize(width, widget.sizeHint().height())
    for _ in range(10):
        app.processEvents()
    widget.grab().save(path)
    print("saved", path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--width", type=int, default=1230)
    parser.add_argument(
        "--populate",
        type=int,
        default=0,
        metavar="N",
        help="add N positions to the flexible panel's table before grabbing it",
    )
    parser.add_argument(
        "--enable-zt",
        action="store_true",
        help="check the Z-stack / Time-lapse groups (and Set Z-range) before grabbing",
    )
    parser.add_argument(
        "--select-wells",
        default="",
        metavar="A1,B2",
        help="select these wells on the well selector before grabbing the wellplate panel",
    )
    parser.add_argument(
        "--tiling",
        choices=("fraction", "grid", "both"),
        help="tiling method for the wellplate panel; 'both' grabs it once per method",
    )
    parser.add_argument("--nx", type=int, default=2, help="Nx for --tiling grid")
    parser.add_argument("--ny", type=int, default=3, help="Ny for --tiling grid")
    parser.add_argument(
        "--nav-shot",
        action="store_true",
        help="also grab the navigation viewer as navigation_grid.png",
    )
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    wells = [w.strip() for w in args.select_wells.split(",") if w.strip()]

    import control._def

    control._def.apply_simulation_mode_defaults(True)

    from qtpy.QtWidgets import QApplication

    app = QApplication(["Squid"])
    app.setStyle("Fusion")

    import control.microscope
    import gui.gui_hcs as gui
    import gui.widgets.multipoint as multipoint_widgets

    # The panels persist their Z/Time/tiling state to
    # software/cache/multipoint_widget_config.yaml on every toggle; a screenshot
    # run must not leave the user's real panel state behind.
    multipoint_widgets.WellplateMultiPointWidget.save_multipoint_widget_config_to_cache = lambda self, *a, **k: None

    microscope = control.microscope.Microscope.build_from_global_config(
        True, skip_init=False, skip_homing=True, profile_name=args.profile
    )
    win = gui.HighContentScreeningGui(microscope=microscope, is_simulation=True, skip_homing=True)
    win.resize(1600, 1000)
    win.show()
    for _ in range(20):
        app.processEvents()

    for name in ("flexibleMultiPointWidget", "wellplateMultiPointWidget"):
        widget = getattr(win, name, None)
        if widget is None:
            print(f"no {name}")
            continue
        # The panels ignore coordinate updates while their tab is in the background,
        # so raise it before touching anything.
        _raise_tab(widget)
        app.processEvents()
        if args.populate and hasattr(widget, "add_location"):
            _populate(app, microscope, widget, args.populate)
        if args.enable_zt:
            _enable_z_and_time(app, widget)

        is_wellplate = name == "wellplateMultiPointWidget"
        if is_wellplate and wells:
            _select_wells(app, win, widget, wells)

        if is_wellplate and args.tiling:
            methods = ("fraction", "grid") if args.tiling == "both" else (args.tiling,)
            for method in methods:
                _set_tiling(app, widget, method, args.nx, args.ny)
                _grab(app, widget, os.path.join(args.out_dir, f"wellplate_{method}.png"), args.width)
        else:
            stem = name.replace("Widget", "").replace("MultiPoint", "_multipoint")
            _grab(app, widget, os.path.join(args.out_dir, f"{stem}.png"), args.width)

    if args.nav_shot:
        viewer = getattr(win, "navigationViewer", None)
        if viewer is None:
            print("no navigationViewer")
        else:
            _grab(app, viewer, os.path.join(args.out_dir, "navigation_grid.png"), viewer.width())

    # Do NOT win.close(): closeEvent blocks on a modal "Confirm Exit" box with
    # nobody to click, and past it _cleanup_common caches the *simulated* stage
    # position to the file the real GUI restores the stage from at startup.
    # Hide the window and tear the simulated hardware down directly instead.
    win.hide()
    app.processEvents()
    try:
        microscope.close()
    except Exception as e:
        print("microscope.close():", e)
    # Hard exit: the simulated GUI leaves worker threads/processes that would
    # otherwise keep the interpreter alive. Flush first or the prints are lost.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    # Windows multiprocessing re-imports this module in every worker process.
    main()
