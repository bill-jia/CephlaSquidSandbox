"""Render the multipoint panels off simulated hardware and save them as PNGs.

Usage (from software/, squid env, hardware may be off):
    python tools/screenshot_multipoint.py <out_dir> [--profile NAME] [--populate N]

``--populate N`` walks the simulated stage over N spots and adds each one to the
Flexible panel's position table (selecting the middle row) so the positions block
is rendered with content rather than empty.

Builds the whole HCS GUI with every SIMULATE_* flag on, grabs the Flexible and
Wellplate multipoint widgets at a fixed width, writes
``<out_dir>/flexible_multipoint.png`` and ``<out_dir>/wellplate_multipoint.png``,
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
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    import control._def

    control._def.apply_simulation_mode_defaults(True)

    from qtpy.QtWidgets import QApplication

    app = QApplication(["Squid"])
    app.setStyle("Fusion")

    import control.microscope
    import gui.gui_hcs as gui

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
        if args.populate and hasattr(widget, "add_location"):
            _populate(app, microscope, widget, args.populate)
        if args.enable_zt:
            _enable_z_and_time(app, widget)
        # Bring the widget's tab to the front so it is laid out at full size.
        parent = widget.parentWidget()
        while parent is not None:
            if hasattr(parent, "setCurrentWidget") and hasattr(parent, "indexOf") and parent.indexOf(widget) >= 0:
                parent.setCurrentWidget(widget)
            widget_in_parent = parent
            parent = parent.parentWidget()
        widget.resize(args.width, widget.sizeHint().height())
        for _ in range(10):
            app.processEvents()
        path = os.path.join(args.out_dir, f"{name.replace('Widget', '').replace('MultiPoint', '_multipoint')}.png")
        widget.grab().save(path)
        print("saved", path)

    win.close()
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
