"""Presentation-only assembly for the full touch interface.

recordTabWidget remains the acquisition router: task navigation never reparents
its pages or changes its current mode except when an acquisition task is chosen.
"""
from qtpy.QtCore import Qt, QTimer
from qtpy.QtWidgets import (
    QApplication, QAbstractButton, QAbstractItemView, QAbstractSpinBox, QComboBox, QGroupBox,
    QBoxLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
    QScrollArea, QScroller, QSizePolicy, QSplitter, QStackedWidget, QTabBar,
    QSlider, QStyledItemDelegate, QTableWidget, QVBoxLayout, QWidget,
)


def acquisition_id(widget):
    return type(widget).__name__


def add_focus_controls(page, gui):
    for name in ("autofocusWidget", "laserAutofocusControlWidget", "focusMapWidget"):
        control = getattr(gui, name, None)
        if control is not None:
            page.layout().addWidget(control)
            # Inactive QTabWidget pages retain their explicit hidden state when
            # reparented. Show the shared controls in their new task page.
            control.show()
    if getattr(gui, "focusCameraDialog", None) is not None:
        button = QPushButton("Focus Camera / Calibration Settings")
        button.clicked.connect(gui.openFocusCameraDialog)
        page.layout().addWidget(button)


def saving_name_row(path, browse, name, path_label="Save path", name_label="Experiment ID"):
    """Share the available editor space 30/70, retaining the original controls."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    for editor in (path, name):
        editor.setMaximumWidth(16777215)
        editor.setMinimumWidth(80)
        editor.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    layout.addWidget(QLabel(path_label))
    layout.addWidget(path, 3)
    layout.addWidget(browse)
    layout.addWidget(QLabel(name_label))
    layout.addWidget(name, 7)
    return row


def remove_saving_rows(layout, controls):
    """Remove the old label-only rows when their editors move into the fixed bar."""
    items = [layout.itemAt(i) for i in range(layout.count())]
    editors = [item for item in items if item.widget() in controls]
    if editors:
        # Some modes put output fields in a group box; Tracking shares its
        # experiment row with an objective control, whose label must remain.
        pure = all(item.widget() is None or item.widget() in controls or isinstance(item.widget(), QLabel)
                   for item in items)
        editor_cells = ([layout.getItemPosition(layout.indexOf(item.widget()))[:2] for item in editors]
                        if isinstance(layout, QGridLayout) else [])
        for item in items:
            label = item.widget()
            if not isinstance(label, QLabel):
                continue
            cell = layout.getItemPosition(layout.indexOf(label))[:2] if isinstance(layout, QGridLayout) else None
            if pure or any(cell[0] == row and cell[1] < column for row, column in editor_cells):
                layout.removeWidget(label)
                label.hide()
                label.deleteLater()
        for item in editors:
            layout.removeWidget(item.widget())
    for index in reversed(range(layout.count())):
        item = layout.itemAt(index)
        child = item.layout()
        if child is None:
            container = item.widget()
            if container is not None and container.layout() is not None:
                remove_saving_rows(container.layout(), controls)
                if container.layout().count() == 0:
                    layout.removeWidget(container)
                    container.hide()
            continue
        remove_saving_rows(child, controls)
        if child.count() == 0:
            layout.takeAt(index)
            child.setParent(None)


def scroll_panel(widget, touch=False):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setMinimumSize(0, 0)
    scroll.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
    scroll.setWidget(widget)
    if touch:
        scroll.setStyleSheet("QScrollBar:vertical { width: 28px; } QScrollBar:horizontal { height: 28px; }")
        QScroller.grabGesture(scroll.viewport(), QScroller.TouchGesture)
    return scroll


class TouchItemDelegate(QStyledItemDelegate):
    """Qt's native combo delegate can ignore stylesheet item minimum heights."""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(56, size.height()))
        return size


def make_touch_targets(widget):
    """Size actionable controls only; don't inflate containers or image canvases."""
    font = widget.font()
    font.setPointSize(11)
    widget.setFont(font)
    for child in widget.findChildren(QWidget):
        child_font = child.font()
        child_font.setPointSize(11)
        child.setFont(child_font)
        if isinstance(child, (QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit)):
            child.setMaximumHeight(16777215)
            child.setMinimumHeight(48)
            if isinstance(child, QAbstractSpinBox):
                child.setKeyboardTracking(False)
                child.setStyleSheet("QAbstractSpinBox::up-button, QAbstractSpinBox::down-button { width: 28px; }")
                if child.maximumWidth() < 110:
                    child.setMaximumWidth(16777215)
                    child.setMinimumWidth(110)
            if isinstance(child, QComboBox):
                child.setStyleSheet("QComboBox { combobox-popup: 0; }")
                child.setMaxVisibleItems(8)
                child.view().setMinimumWidth(250)
                child.view().setFont(child_font)
                child.setItemDelegate(TouchItemDelegate(child.view()))
                child.view().setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
                child.view().setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
                child.view().setStyleSheet(
                    "QAbstractItemView::item { min-height: 56px; padding: 4px 12px; }"
                    "QScrollBar:vertical { width: 28px; }"
                )
                QScroller.grabGesture(child.view().viewport(), QScroller.TouchGesture)
        elif isinstance(child, QSlider):
            child.setMinimumHeight(56)
            child.setStyleSheet("""
                QSlider::groove:horizontal {
                    height: 18px; border: 1px solid #888; border-radius: 9px;
                    background: #3a3a3a;
                }
                QSlider::sub-page:horizontal { background: #3d8f5a; border-radius: 9px; }
                QSlider::handle:horizontal {
                    width: 48px; height: 48px; margin: -15px 0;
                    border: 1px solid #666; border-radius: 10px; background: #e8e8e8;
                }
                QSlider::handle:horizontal:hover { background: white; }
            """)
        elif isinstance(child, QTableWidget):
            child.verticalHeader().setDefaultSectionSize(48)
            child.setMaximumHeight(16777215)
            child.setMinimumHeight(240)
        elif isinstance(child, QListWidget):
            child.setStyleSheet("QListWidget::item { min-height: 48px; }")
        elif isinstance(child, QGroupBox) and child.isCheckable():
            child.setStyleSheet("QGroupBox::indicator { width: 28px; height: 28px; }")


def contain_image_tools(gui):
    """Keep every image tool accessible without imposing a toolbar-wide minimum."""
    display = gui.imageDisplayWindow
    if display is None or not hasattr(display, "stage_position_label"):
        gui.imageDisplayTabs.setMinimumSize(0, 0)
        gui.imageDisplayTabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        return
    toolbar = display.stage_position_label.parentWidget()
    layout = display.widget.layout()
    index = layout.indexOf(toolbar)
    if index >= 0:
        layout.removeWidget(toolbar)
        scroll = scroll_panel(toolbar)
        scroll.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        scroll.setFixedHeight(toolbar.sizeHint().height() + 24)
        layout.insertWidget(index, scroll)
    gui.imageDisplayTabs.setMinimumSize(0, 0)
    gui.imageDisplayTabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)


class FullTouchInterface(QWidget):
    def __init__(self, gui):
        super().__init__(gui)
        self.gui = gui
        self.running = False
        self.splits = {}
        self._current_task = "live"
        self._scrolls = []
        font = self.font()
        font.setPointSize(11)
        self.setFont(font)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        strip = QHBoxLayout()
        self.live = QPushButton("Start Live")
        self.snap = QPushButton("Snap")
        self.laser = QPushButton("Laser AF Now")
        self.focus = QPushButton("Focus Setup")
        self.abort = QPushButton("Abort")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.progress = QLabel("Idle")
        for button in (self.live, self.snap, self.laser, self.focus, self.abort):
            button.setFont(font)
            button.setMinimumHeight(52)
        self.status.setFont(font)
        self.progress.setFont(font)
        self.live.clicked.connect(lambda: self._click(gui.liveControlWidget.btn_live))
        self.snap.clicked.connect(lambda: self._click(gui.liveControlWidget.btn_snap))
        self.laser.clicked.connect(self._laser_now)
        self.focus.clicked.connect(lambda: self.select_task("focus"))
        self.abort.clicked.connect(self._abort)
        strip.addWidget(self.live)
        strip.addWidget(self.snap)
        live_control = gui.liveControlWidget
        self.snap_path = live_control.lineEdit_snapSavingDir
        self.snap_tag = live_control.lineEdit_snapTag
        self.snap_browse = live_control.btn_setSnapSavingDir
        self.snap_settings = saving_name_row(self.snap_path, self.snap_browse, self.snap_tag, "Snap folder", "Tag")
        make_touch_targets(self.snap_settings)
        live_control.snap_group.hide()
        strip.addWidget(self.snap_settings, 3)
        for widget in (self.laser, self.focus, self.status, self.progress, self.abort):
            strip.addWidget(widget, 2 if widget is self.status else 0)
        root.addLayout(strip)
        self.tabs = QTabBar()
        self.tabs.setFont(font)
        self.tabs.setExpanding(False)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setStyleSheet("QTabBar::tab { min-height: 48px; padding: 0 16px; }")
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(16)
        self.splitter.setChildrenCollapsible(False)
        contain_image_tools(gui)
        self.splitter.addWidget(gui.imageDisplayTabs)
        self.stack = QStackedWidget()
        self.stack.setMinimumSize(0, 0)
        self.stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.controls = QWidget()
        self.controls.setMinimumSize(0, 0)
        self.controls.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        controls_layout = QVBoxLayout(self.controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(self.tabs, 0, Qt.AlignRight)
        controls_layout.addWidget(self.stack, 1)
        self.splitter.addWidget(self.controls)
        self.splitter.setSizes([850, 1000])
        root.addWidget(self.splitter, 1)
        self._place_autolevel()
        self.pages = {}
        live = self._page("live", "Live / Position")
        live_control.hide()
        camera_primary = QWidget()
        self.camera_primary = camera_primary
        camera_primary_layout = QHBoxLayout(camera_primary)
        camera_primary_layout.setContentsMargins(0, 0, 0, 0)
        camera = gui.cameraSettingWidget
        for name in ("exposure_gain_row", "format_row"):
            row = getattr(camera, name, None)
            if row is not None:
                camera._camera_details_column.removeItem(row)
                row.setParent(None)
                while row.count():
                    item = row.takeAt(0)
                    control = item.widget()
                    if control is not None:
                        if isinstance(control, QLabel):
                            control.setText({"Exposure Time (ms)": "Exposure (ms)", "Analog Gain": "Gain",
                                             "Camera Mode": "Mode"}.get(control.text(), control.text()))
                        camera_primary_layout.addWidget(control)
        camera_primary_layout.addStretch()
        self._make_joypad()
        for name in ("objectivesWidget", "observationStateWidget",
                     "illuminationWidget", "stageControlsWidget",
                     "squidFilterWidget", "dacControlWidget"):
            if name == "dacControlWidget":
                import control._def
                if not control._def.SHOW_DAC_CONTROL:
                    continue
            control = getattr(gui, name, None)
            if control is not None:
                live.layout().addWidget(control)
                if name == "observationStateWidget":
                    live.layout().addWidget(camera_primary)
        camera_toggle = QPushButton("Camera settings / ROI / triggering ▸")
        camera_toggle.setCheckable(True)
        camera_toggle.toggled.connect(gui.cameraSettingWidget.setVisible)
        live.layout().addWidget(camera_toggle)
        live.layout().addWidget(gui.cameraSettingWidget)
        gui.cameraSettingWidget.hide()
        focus = self._page("focus", "Focus Setup")
        add_focus_controls(focus, gui)
        plate = self._page("plate", "Plate / Navigate")
        plate.layout().addWidget(gui.sampleSettingsWidget)
        hint = QLabel("Tap wells to select. Use Go To Selected to move the stage.")
        plate.layout().addWidget(hint)
        actions = QHBoxLayout()
        self.zoom_out = QPushButton("Wells −")
        self.zoom_in = QPushButton("Wells +")
        self.go = QPushButton("Go To Selected Well")
        self.map_move = QPushButton("Map double tap moves stage")
        self.map_move.setCheckable(True)
        self.go.clicked.connect(self._go_to_well)
        self.zoom_out.clicked.connect(lambda: self._zoom_wells(-8))
        self.zoom_in.clicked.connect(lambda: self._zoom_wells(8))
        for button in (self.zoom_out, self.zoom_in, self.go, self.map_move):
            actions.addWidget(button)
        plate.layout().addLayout(actions)
        self.selection = QLabel()
        plate.layout().addWidget(self.selection)
        self.wells = scroll_panel(gui.wellSelectionWidget, True)
        self.wells.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.wells.setFixedHeight(390)
        plate.layout().addWidget(self.wells)
        map_actions = QHBoxLayout()
        for label, factor in (("Map +", 0.8), ("Map −", 1.25)):
            button = QPushButton(label)
            button.clicked.connect(lambda _, f=factor: gui.navigationViewer.view.scaleBy((f, f)))
            map_actions.addWidget(button)
        fit = QPushButton("Fit Map")
        fit.clicked.connect(lambda: gui.navigationViewer.view.autoRange())
        map_actions.addWidget(fit)
        plate.layout().addLayout(map_actions)
        plate.layout().addWidget(gui.navigationViewer)
        gui.navigationViewer.setMinimumHeight(350)
        # A touch double tap must not move a stage accidentally.
        gui.wellSelectionWidget.cellDoubleClicked.disconnect(gui.wellSelectionWidget.onDoubleClick)
        self._zoom_wells(0)
        acquisition = QWidget()
        acquisition_layout = QVBoxLayout(acquisition)
        summary_row = QHBoxLayout()
        self.acquisition_selection = QLabel()
        self.acquisition_selection.setWordWrap(True)
        edit = QPushButton("Edit selection")
        edit.setMinimumHeight(48)
        edit.clicked.connect(lambda: self.select_task("plate"))
        summary_row.addWidget(self.acquisition_selection, 1)
        summary_row.addWidget(edit)
        acquisition_layout.addLayout(summary_row)
        gui.recordTabWidget.tabBar().hide()
        gui.recordTabWidget.setMaximumHeight(16777215)
        acquisition_layout.addWidget(gui.recordTabWidget)
        self._acquisition_page = self.stack.addWidget(acquisition)
        for i in range(gui.recordTabWidget.count()):
            widget = gui.recordTabWidget.widget(i)
            identity = acquisition_id(widget)
            title = {"WellplateMultiPointWidget": "Wellplate Acquisition",
                     "FlexibleMultiPointWidget": "Flexible Acquisition"}.get(identity, gui.recordTabWidget.tabText(i))
            tab = self.tabs.addTab(title)
            self.tabs.setTabData(tab, identity)
            self.pages[identity] = self._acquisition_page
            self._add_reorder_buttons(widget)
            body = getattr(widget, "_acquisition_body_layout", None)
            if body is not None:
                body.setDirection(QBoxLayout.TopToBottom)
            order = getattr(widget, "acquisition_order", None)
            if order is not None:
                order.enable_touch_controls()
            channels = getattr(widget, "list_configurations", None)
            if channels is not None:
                channels.setMinimumHeight(240)
                channels.setMaximumHeight(320)
            self._make_acquisition_page(widget)
        flexible = gui.flexibleMultiPointWidget
        flexible.acquisition_is_active = lambda: gui.recordTabWidget.currentWidget() is flexible
        flexible.btn_add.setText("Add Current Position")
        flexible.checkbox_moveStageOnClick.setChecked(False)
        go_position = QPushButton("Go To Selected Position")
        go_position.clicked.connect(lambda: flexible.go_to(flexible.table_location_list.currentRow()))
        flexible.positions_actions_layout.addWidget(go_position)
        make_touch_targets(self.stack)
        for button in self.joypad_buttons:
            button.setFixedSize(64, 64)
            arrow_font = button.font()
            arrow_font.setPointSize(18)
            button.setFont(arrow_font)
        for editor in (camera.entry_exposureTime, camera.entry_analogGain):
            editor.setMinimumWidth(90)
            editor.setMaximumWidth(100)
        camera.dropdown_cameraMode.setMaximumWidth(130)
        # Plate is a dense zoomable model; sizing other tables must not change its cells.
        self._zoom_wells(0)
        self.tabs.currentChanged.connect(self._task_changed)
        self.splitter.splitterMoved.connect(lambda *_: self._remember_split())
        gui.recordTabWidget.currentChanged.connect(self._acquisition_changed)
        QApplication.instance().focusChanged.connect(self._ensure_editor_visible)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(200)
        gui.multipointController.signal_acquisition_progress.connect(self._progress)
        self.refresh()

    def _scroll(self, widget):
        scroll = scroll_panel(widget, True)
        self._scrolls.append(scroll)
        return scroll

    def _place_autolevel(self):
        """Move the original contrast action above the live histogram only."""
        self.autolevel = self.gui.liveControlWidget.btn_autolevel
        self.autolevel.setFont(self.font())
        self.autolevel.setMinimumHeight(48)
        display = self.gui.imageDisplayWindow
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.autolevel)
        if display is not None and getattr(display, "show_LUT", False):
            ui = display.graphics_widget.view.ui
            self.histogram_width = round(ui.histogram.sizeHint().width() * 0.75)
            ui.histogram.item.vb.setMinimumWidth(20)
            ui.histogram.setFixedWidth(self.histogram_width)
            self.autolevel.setFixedWidth(self.histogram_width)
            panel.setFixedWidth(self.histogram_width)
            ui.gridLayout.removeWidget(ui.histogram)
            layout.addWidget(ui.histogram, 1)
            ui.gridLayout.addWidget(panel, 0, 1, 2, 2)
            view = display.graphics_widget.view.getView()
            view.setDefaultPadding(0)
            ui.graphicsView.setContentsMargins(0, 0, 0, 0)
            ui.gridLayout.setContentsMargins(0, 0, 0, 0)
            self.gui.imageDisplayTabs.setStyleSheet("QTabWidget::pane { border: 0; padding: 0; }")
        elif getattr(self.gui, "napariLiveWidget", None) is not None:
            viewer = self.gui.napariLiveWidget
            width = round(viewer.histogram_widget.width() * 0.75)
            viewer.histogram_widget.item.vb.setMinimumWidth(20)
            viewer.histogram_widget.setFixedWidth(width)
            self.autolevel.setFixedWidth(width)
            panel.setFixedWidth(width)
            layout.addWidget(viewer.histogram_widget, 1)
            viewer.histogram_dock.setWidget(panel)
        elif display is not None:
            display.image_container.layout().insertWidget(0, panel, 0, Qt.AlignRight)
        else:
            self.layout().insertWidget(1, panel)

    def _make_joypad(self):
        nav = self.gui.navigationWidget
        old = nav.grid.takeAt(1).layout()
        while old.count():
            old.takeAt(0)
        old.setParent(None)
        row = QHBoxLayout()
        pad = QGridLayout()
        pad.setSpacing(4)
        buttons = ((nav.btn_moveY_forward, "▲", 0, 1), (nav.btn_moveX_backward, "◀", 1, 0),
                   (nav.btn_moveX_forward, "▶", 1, 2), (nav.btn_moveY_backward, "▼", 2, 1))
        for button, text, r, c in buttons:
            button.setText(text)
            pad.addWidget(button, r, c)
        xy_label = QLabel("XY")
        xy_label.setAlignment(Qt.AlignCenter)
        pad.addWidget(xy_label, 1, 1)
        row.addLayout(pad)
        row.addSpacing(16)
        z_pad = QGridLayout()
        z_pad.setSpacing(4)
        z_label = QLabel("Z")
        z_label.setAlignment(Qt.AlignCenter)
        z_pad.addWidget(nav.btn_moveZ_forward, 0, 0)
        z_pad.addWidget(z_label, 1, 0)
        z_pad.addWidget(nav.btn_moveZ_backward, 2, 0)
        row.addLayout(z_pad)
        row.addSpacing(16)
        steps = QGridLayout()
        for index, (label, editor) in enumerate((("X step (mm)", nav.entry_dX),
                                               ("Y step (mm)", nav.entry_dY), ("Z step (µm)", nav.entry_dZ))):
            step_label = QLabel(label)
            steps.addWidget(step_label, index, 0)
            steps.addWidget(editor, index, 1)
            if index == 2 and editor.isHidden():
                z_label.hide()
                step_label.hide()
        row.addLayout(steps)
        row.addStretch()
        nav.grid.insertLayout(1, row)
        self.joypad_buttons = [b[0] for b in buttons] + [nav.btn_moveZ_forward, nav.btn_moveZ_backward]

    def _make_acquisition_page(self, widget):
        """Keep each acquisition's original identity and all controls under its ownership."""
        old_layout = widget.layout()
        path = getattr(widget, "lineEdit_savingDir", None)
        name = getattr(widget, "lineEdit_experimentID", None)
        browse = getattr(widget, "btn_setSavingDir", None)
        header = None
        if all(w is not None for w in (path, name, browse)):
            remove_saving_rows(old_layout, (path, name, browse))
            header = saving_name_row(path, browse, name)
        settings = QWidget()
        settings.setLayout(old_layout)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(0, 0, 0, 0)
        if header is not None:
            outer.addWidget(header)
        scroll = self._scroll(settings)
        widget._touch_settings_scroll = scroll
        outer.addWidget(scroll, 1)
        if widget in (self.gui.flexibleMultiPointWidget, self.gui.wellplateMultiPointWidget):
            footer = QHBoxLayout()
            for button in (widget.btn_snap_images, widget.btn_startAcquisition):
                footer.addWidget(button, 1)
                button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            outer.addLayout(footer)

    def _page(self, identity, title):
        page = QWidget()
        QVBoxLayout(page)
        tab = self.tabs.addTab(title)
        self.tabs.setTabData(tab, identity)
        self.pages[identity] = self.stack.addWidget(self._scroll(page))
        return page

    def _add_reorder_buttons(self, widget):
        channel_list = getattr(widget, "list_configurations", None)
        if channel_list is None:
            return
        parent_layout = channel_list.parentWidget().layout()
        row = QHBoxLayout()
        for label, delta in (("Channel ↑", -1), ("Channel ↓", 1)):
            button = QPushButton(label)
            button.clicked.connect(lambda _, d=delta, view=channel_list: self._reorder(view, d))
            row.addWidget(button)
        parent_layout.addLayout(row)

    @staticmethod
    def _reorder(view, delta):
        index = view.currentRow()
        target = index + delta
        if index >= 0 and 0 <= target < view.count():
            # Use the model's move operation, preserving item checks and subscriptions.
            view.model().moveRows(view.rootIndex(), index, 1, view.rootIndex(), target + (delta > 0))
            view.setCurrentRow(target)

    def _remember_split(self):
        self.splits[self._current_task] = self.splitter.sizes()

    def _task_changed(self, index):
        self._remember_split()
        identity = self.tabs.tabData(index)
        self._current_task = identity
        self.stack.setCurrentIndex(self.pages[identity])
        for i in range(self.gui.recordTabWidget.count()):
            if acquisition_id(self.gui.recordTabWidget.widget(i)) == identity:
                self.gui.recordTabWidget.setCurrentIndex(i)
                break
        self.splitter.setSizes(self.splits.get(identity, [850, 1000]))
        self.refresh()

    def _acquisition_changed(self, index):
        # External workflow/preset navigation must still reveal the intended acquisition.
        self.select_task(acquisition_id(self.gui.recordTabWidget.widget(index)))

    def select_task(self, identity):
        for index in range(self.tabs.count()):
            if self.tabs.tabData(index) == identity and self.tabs.isTabEnabled(index):
                self.tabs.setCurrentIndex(index)
                return

    def _ensure_editor_visible(self, _previous, current):
        if current is None or not self.isAncestorOf(current):
            return
        # Also runs after viewport resize when the platform keyboard consumes space.
        QTimer.singleShot(0, lambda: self._scroll_to(current))

    def _scroll_to(self, current):
        for scroll in self._scrolls:
            if scroll.widget().isAncestorOf(current):
                scroll.ensureWidgetVisible(current, 32, 64)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        current = QApplication.focusWidget()
        if current is not None and self.isAncestorOf(current):
            QTimer.singleShot(0, lambda: self._scroll_to(current))

    def _zoom_wells(self, delta):
        selector = self.gui.wellSelectionWidget
        cell = max(48, min(120, getattr(self, "_well_cell", 56) + delta))
        self._well_cell = cell
        selector.touch_cell_size = cell
        for header in (selector.horizontalHeader(), selector.verticalHeader()):
            header.setMaximumSectionSize(120)
            header.setMinimumSectionSize(cell)
            header.setDefaultSectionSize(cell)
        selector.setFixedSize(selector.columnCount() * cell + selector.verticalHeader().width(),
                              selector.rowCount() * cell + selector.horizontalHeader().height())

    def _go_to_well(self):
        if self.running or not self.gui.navigationWidget.isEnabled():
            return
        cells = self.gui.wellSelectionWidget.get_selected_cells()
        if cells:
            self.gui.wellSelectionWidget.onDoubleClick(*cells[0])

    def _click(self, button):
        if not self.running and button.isEnabled():
            button.click()
        self.refresh()

    def _laser_now(self):
        widget = self.gui.laserAutofocusControlWidget
        if widget is not None and not self.running and not widget.quick_action_unavailable_reason():
            widget.btn_move_to_target.click()
        self.refresh()

    def _abort(self):
        active = self.gui.recordTabWidget.currentWidget()
        if active is getattr(self.gui, "fastAcquisitionWidget", None):
            active.stop_acquisition()
        elif active is self.gui.recordingControlWidget:
            active.stop_recording()
        else:
            self.gui.multipointController.request_abort_acquisition()

    def _progress(self, current, total, timepoint):
        self.progress.setText(f"Region {current}/{total} · Time {timepoint + 1}")

    def set_running(self, running):
        if self.running == running:
            self.refresh()
            return
        self.running = running
        active = acquisition_id(self.gui.recordTabWidget.currentWidget())
        for index in range(self.tabs.count()):
            identity = self.tabs.tabData(index)
            self.tabs.setTabEnabled(index, not running or identity in ("live", "focus", "plate", active))
        for identity in ("live", "focus", "plate"):
            self.stack.widget(self.pages[identity]).widget().setEnabled(not running)
        if not running:
            self.progress.setText("Idle")
        else:
            self.progress.setText("Acquiring…")
        self.refresh()

    def refresh(self):
        gui = self.gui
        self.live.setText("Stop Live" if gui.liveController.is_live else "Start Live")
        self.live.setEnabled(not self.running and gui.liveControlWidget.btn_live.isEnabled())
        self.snap.setEnabled(not self.running and gui.liveControlWidget.btn_snap.isEnabled())
        self.snap_settings.setEnabled(not self.running and gui.liveControlWidget.isEnabled())
        self.autolevel.setEnabled(not self.running and gui.liveControlWidget.isEnabled())
        af = gui.laserAutofocusControlWidget
        reason = "Laser AF hardware unavailable" if af is None else af.quick_action_unavailable_reason()
        if self.running:
            reason = "Acquisition in progress"
        self.laser.setEnabled(not reason)
        self.laser.setToolTip(reason or "Move to the configured laser AF target")
        target = f"Target {af.entry_target.value():.2f} µm" if af else ""
        pos = gui.stage.get_pos()
        self.status.setText(f"{gui.objectiveStore.current_objective} · X {pos.x_mm:.3f} Y {pos.y_mm:.3f} Z {pos.z_mm*1000:.1f} µm\n{reason or 'Laser AF ready'} · {target}")
        self.abort.setEnabled(self.running)
        cells = gui.wellSelectionWidget.get_selected_cells()
        names = [gui.wellSelectionWidget.verticalHeaderItem(r).text() + str(c + 1) for r, c in cells]
        summary = "Selected wells: " + (", ".join(names) if names else "none")
        self.selection.setText(summary)
        flexible = gui.recordTabWidget.currentWidget() is gui.flexibleMultiPointWidget
        self.acquisition_selection.setText(
            f"Flexible positions: {gui.flexibleMultiPointWidget.table_location_list.rowCount()} (independent of wells)"
            if flexible else summary)
        self.go.setEnabled(bool(cells) and not self.running and gui.navigationWidget.isEnabled())

    def state(self):
        self._remember_split()
        return {"task": self._current_task, "splits": self.splits}

    def restore(self, state):
        self.splits = state.get("splits", {})
        self.select_task(state.get("task", "live"))
        self.splitter.setSizes(self.splits.get(self._current_task, [850, 1000]))
