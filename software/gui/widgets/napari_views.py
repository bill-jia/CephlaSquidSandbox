from ._bootstrap import *

class FocusMapWidget(QFrame):
    """Widget for managing focus map points and surface fitting"""

    def __init__(self, stage: AbstractStage, navigationViewer, scanCoordinates, focusMap):
        super().__init__()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)
        self._allow_updating_focus_points_on_signal = True

        # Store controllers
        self.stage = stage
        self.navigationViewer = navigationViewer
        self.scanCoordinates = scanCoordinates
        self.focusMap = focusMap

        # Store focus points in widget
        self.focus_points = []  # list of (region_id, x, y, z) tuples
        self.enabled = False  # toggled when focus map enabled for next acquisition

        self.setup_ui()
        self.make_connections()
        self.setEnabled(False)
        self.add_margin = True  # margin for focus grid makes it smaller, but will avoid points at the borders

    def setup_ui(self):
        """Create and arrange UI components"""
        self.layout = QVBoxLayout(self)

        # Point combo and Z control
        controls_layout = QHBoxLayout()
        controls_layout.addWidget(QLabel("Focus Point:"))
        self.point_combo = QComboBox()
        controls_layout.addWidget(self.point_combo, stretch=1)
        self.update_z_btn = QPushButton("Update Z")
        controls_layout.addWidget(self.update_z_btn)
        self.layout.addLayout(controls_layout)

        # Point control buttons - line 1
        point_controls = QHBoxLayout()
        self.add_point_btn = QPushButton("Add")
        self.remove_point_btn = QPushButton("Remove")
        self.next_point_btn = QPushButton("Next")
        self.edit_point_btn = QPushButton("Edit")
        point_controls.addWidget(self.add_point_btn)
        point_controls.addWidget(self.remove_point_btn)
        point_controls.addWidget(self.next_point_btn)
        point_controls.addWidget(self.edit_point_btn)
        self.layout.addLayout(point_controls)

        # Point control buttons - line 2
        point_controls_2 = QHBoxLayout()
        point_controls_2.addWidget(QLabel("Focus Grid:"))
        self.rows_spin = QSpinBox()
        self.rows_spin.setKeyboardTracking(False)
        self.rows_spin.setRange(1, 10)
        self.rows_spin.setValue(4)
        point_controls_2.addWidget(self.rows_spin)
        x_label = QLabel("×")
        x_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        point_controls_2.addWidget(x_label)
        self.cols_spin = QSpinBox()
        self.cols_spin.setKeyboardTracking(False)
        self.cols_spin.setRange(1, 10)
        self.cols_spin.setValue(4)
        point_controls_2.addWidget(self.cols_spin)
        self.export_btn = QPushButton("Export")
        self.export_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.import_btn = QPushButton("Import")
        self.import_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        point_controls_2.addWidget(self.export_btn)
        point_controls_2.addWidget(self.import_btn)
        self.layout.addLayout(point_controls_2)

        # Surface fitting controls
        settings_layout = QHBoxLayout()
        settings_layout.addWidget(QLabel("Fitting Method:"))
        self.fit_method_combo = QComboBox()
        self.fit_method_combo.addItems(["spline", "rbf", "constant"])
        settings_layout.addWidget(self.fit_method_combo)
        settings_layout.addWidget(QLabel("Smoothing:"))
        self.smoothing_spin = QDoubleSpinBox()
        self.smoothing_spin.setKeyboardTracking(False)
        self.smoothing_spin.setRange(0.01, 1.0)
        self.smoothing_spin.setValue(0.1)
        self.smoothing_spin.setSingleStep(0.05)
        settings_layout.addWidget(self.smoothing_spin)
        self.by_region_checkbox = QCheckBox("Fit by Region")
        self.by_region_checkbox.setChecked(False)
        settings_layout.addWidget(self.by_region_checkbox)
        self.layout.addLayout(settings_layout)

        # Status label - reserve space even when hidden
        self.status_label = QLabel()
        self.status_label.setText(" ")  # Empty text to keep space
        self.layout.addWidget(self.status_label)

    def make_connections(self):
        # Auto-navigate when point selection changes
        self.point_combo.currentIndexChanged.connect(self.goto_selected_point)

        # Update Z for current point
        self.update_z_btn.clicked.connect(self.update_current_z)

        # Connect grid size changes
        self.rows_spin.valueChanged.connect(self.regenerate_grid)
        self.cols_spin.valueChanged.connect(self.regenerate_grid)

        # Connect point control buttons
        self.add_point_btn.clicked.connect(self.add_current_point)
        self.remove_point_btn.clicked.connect(self.remove_current_point)
        self.next_point_btn.clicked.connect(self.goto_next_point)
        self.edit_point_btn.clicked.connect(self.edit_current_point)
        self.export_btn.clicked.connect(self.export_focus_points)
        self.import_btn.clicked.connect(self.import_focus_points)

        # Connect fitting method change
        self.fit_method_combo.currentTextChanged.connect(self._match_by_region_box)

    def update_point_list(self):
        """Update point selection combo showing grid coordinates for points"""
        self.point_combo.blockSignals(True)
        curr_focus_point = self.point_combo.currentIndex()
        self.point_combo.clear()
        for idx, (region_id, x, y, z) in enumerate(self.focus_points):
            point_text = (
                f"{region_id}: "
                + "x:"
                + str(round(x, 3))
                + "mm  y:"
                + str(round(y, 3))
                + "mm  z:"
                + str(round(1000 * z, 2))
                + "μm"
            )
            self.point_combo.addItem(point_text)
        self.point_combo.setCurrentIndex(max(0, min(curr_focus_point, len(self.focus_points) - 1)))
        self.point_combo.blockSignals(False)

    def edit_current_point(self):
        """Edit coordinates of current point in a popup dialog"""
        index = self.point_combo.currentIndex()
        if 0 <= index < len(self.focus_points):
            region_id, x, y, z = self.focus_points[index]

            # Create dialog
            dialog = QDialog(self)
            dialog.setWindowTitle("Edit Focus Point")
            layout = QFormLayout()

            # Add coordinate spinboxes with good precision
            x_spin = QDoubleSpinBox()
            x_spin.setKeyboardTracking(False)
            x_spin.setRange(SOFTWARE_POS_LIMIT.X_NEGATIVE, SOFTWARE_POS_LIMIT.X_POSITIVE)
            x_spin.setDecimals(3)
            x_spin.setValue(x)
            x_spin.setSuffix(" mm")

            y_spin = QDoubleSpinBox()
            y_spin.setKeyboardTracking(False)
            y_spin.setRange(SOFTWARE_POS_LIMIT.Y_NEGATIVE, SOFTWARE_POS_LIMIT.Y_POSITIVE)
            y_spin.setDecimals(3)
            y_spin.setValue(y)
            y_spin.setSuffix(" mm")

            z_spin = QDoubleSpinBox()
            z_spin.setKeyboardTracking(False)
            z_spin.setRange(
                SOFTWARE_POS_LIMIT.Z_NEGATIVE * 1000, SOFTWARE_POS_LIMIT.Z_POSITIVE * 1000
            )  # Convert mm limits to μm
            z_spin.setDecimals(2)
            z_spin.setValue(z * 1000)  # Convert mm to μm
            z_spin.setSuffix(" μm")

            layout.addRow("X:", x_spin)
            layout.addRow("Y:", y_spin)
            layout.addRow("Z:", z_spin)

            # Add OK/Cancel buttons
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addRow(buttons)
            dialog.setLayout(layout)

            # Show dialog and handle result
            if dialog.exec_() == QDialog.Accepted:
                new_x = x_spin.value()
                new_y = y_spin.value()
                new_z = z_spin.value() / 1000  # Convert μm back to mm for storage
                self.focus_points[index] = (region_id, new_x, new_y, new_z)
                self.update_point_list()
                self.update_focus_point_display()

    def update_focus_point_display(self):
        """Update all focus points on navigation viewer"""
        self.navigationViewer.clear_focus_points()
        for _, x, y, _ in self.focus_points:
            self.navigationViewer.register_focus_point(x, y)

    def generate_grid(self, rows=4, cols=4):
        """Generate focus point grid that spans scan bounds"""
        if self.enabled:
            self.point_combo.blockSignals(True)
            self.focus_points.clear()
            self.navigationViewer.clear_focus_points()
            self.status_label.setText(" ")
            current_z = self.stage.get_pos().z_mm

            # Use FocusMap to generate coordinates
            coordinates = self.focusMap.generate_grid_coordinates(
                self.scanCoordinates, rows=rows, cols=cols, add_margin=self.add_margin
            )

            # Add points with current z coordinate
            for region_id, coords_list in coordinates.items():
                for coords in coords_list:
                    self.focus_points.append((region_id, coords[0], coords[1], current_z))
                    self.navigationViewer.register_focus_point(coords[0], coords[1])

            self.update_point_list()
            self.point_combo.blockSignals(False)

    def regenerate_grid(self):
        """Generate focus point grid given updated dims"""
        self.generate_grid(self.rows_spin.value(), self.cols_spin.value())

    def add_current_point(self):
        # Check if any scan regions exist
        if not self.scanCoordinates.has_regions():
            QMessageBox.warning(self, "No Regions Defined", "Please define scan regions before adding focus points.")
            return

        pos = self.stage.get_pos()
        region_id = None

        # If by_region checkbox is checked, ask for region ID
        if self.by_region_checkbox.isChecked():
            region_ids = list(self.scanCoordinates.region_centers.keys())
            if not region_ids:
                QMessageBox.warning(
                    self, "No Regions Defined", "Please define scan regions before adding focus points."
                )
                return

            region_id, ok = QInputDialog.getItem(
                self, "Select Region", "Choose a region:", [str(r) for r in region_ids], 0, False
            )
            if not ok or not region_id:
                return
            region_id = str(region_id)  # Ensure string format
        else:
            # Find the closest region to current position
            closest_region = None
            min_distance = float("inf")
            for rid, center in self.scanCoordinates.region_centers.items():
                dx = center[0] - pos.x_mm
                dy = center[1] - pos.y_mm
                distance = dx * dx + dy * dy
                if distance < min_distance:
                    min_distance = distance
                    closest_region = rid
            region_id = closest_region

        if region_id is not None:
            self.focus_points.append((region_id, pos.x_mm, pos.y_mm, pos.z_mm))
            self.update_point_list()
            self.navigationViewer.register_focus_point(pos.x_mm, pos.y_mm)
        else:
            QMessageBox.warning(self, "Region Error", "Could not determine a valid region for this focus point.")

    def remove_current_point(self):
        index = self.point_combo.currentIndex()
        if 0 <= index < len(self.focus_points):
            self.focus_points.pop(index)
            self.update_point_list()
            self.update_focus_point_display()

    def goto_next_point(self):
        if not self.focus_points:
            return
        current = self.point_combo.currentIndex()
        next_index = (current + 1) % len(self.focus_points)
        self.point_combo.setCurrentIndex(next_index)
        self.goto_selected_point()

    def goto_selected_point(self):
        if self.enabled:
            index = self.point_combo.currentIndex()
            if 0 <= index < len(self.focus_points):
                _, x, y, z = self.focus_points[index]
                self.stage.move_x_to(x)
                self.stage.move_y_to(y)
                self.stage.move_z_to(z)

    def update_current_z(self):
        index = self.point_combo.currentIndex()
        if 0 <= index < len(self.focus_points):
            new_z = self.stage.get_pos().z_mm
            region_id, x, y, _ = self.focus_points[index]
            self.focus_points[index] = (region_id, x, y, new_z)
            self.update_point_list()

    def get_region_points_dict(self):
        points_dict = {}
        for region_id, x, y, z in self.focus_points:
            if region_id not in points_dict:
                points_dict[region_id] = []
            points_dict[region_id].append((x, y, z))
        return points_dict

    def fit_surface(self):
        try:
            method = self.fit_method_combo.currentText()
            rows = self.rows_spin.value()
            cols = self.cols_spin.value()
            by_region = self.by_region_checkbox.isChecked()

            # Validate settings
            if by_region:
                scan_regions = set(self.scanCoordinates.region_centers.keys())
                focus_regions = set(region_id for region_id, _, _, _ in self.focus_points)
                if focus_regions != scan_regions:
                    QMessageBox.warning(
                        self,
                        "Region Mismatch",
                        "The focus points region IDs do not match the scan regions. Please uncheck 'By Region' or select the correct regions.",
                    )
                    return False

            if method == "constant" and (rows != 1 or cols != 1):
                QMessageBox.warning(
                    self,
                    "Confirm Your Configuration",
                    "For 'constant' method, grid size should be 1×1.\nUse 'constant' with 'By Region' checked to define a Z value for each region.",
                )
                return False

            if method != "constant" and (rows < 2 or cols < 2):
                QMessageBox.warning(
                    self,
                    "Confirm Your Configuration",
                    "For surface fitting methods ('spline' or 'rbf'), a grid size of at least 2×2 is recommended.\nAlternatively, use 1x1 grid and 'constant' with 'By Region' checked to define a Z value for each region.",
                )
                return False

            self.focusMap.set_method(method)
            self.focusMap.set_fit_by_region(by_region)
            self.focusMap.smoothing_factor = self.smoothing_spin.value()

            mean_error, std_error = self.focusMap.fit(self.get_region_points_dict())

            self.status_label.setText(f"Surface fit: {mean_error:.3f} mm mean error")
            return True

        except Exception as e:
            self.status_label.setText(f"Fitting failed: {str(e)}")
            return False

    def _match_by_region_box(self):
        if self.fit_method_combo.currentText() == "constant":
            self.by_region_checkbox.setChecked(True)

    def export_focus_points(self):
        """Export focus points to a CSV file"""
        if not self.focus_points:
            QMessageBox.warning(self, "No Focus Points", "There are no focus points to export.")
            return

        file_path, _ = QFileDialog.getSaveFileName(self, "Export Focus Points", "", "CSV Files (*.csv);;All Files (*)")
        if not file_path:
            return
        if not file_path.lower().endswith(".csv"):
            file_path += ".csv"

        try:
            with open(file_path, "w", newline="") as csvfile:
                writer = csv.writer(csvfile)
                # Write header
                writer.writerow(["Region_ID", "X_mm", "Y_mm", "Z_um"])

                # Write data
                for region_id, x, y, z in self.focus_points:
                    writer.writerow([region_id, x, y, z])

            self.status_label.setText(f"Exported {len(self.focus_points)} points to {file_path}")

        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export focus points: {str(e)}")

    def import_focus_points(self):
        """Import focus points from a CSV file"""
        file_path, _ = QFileDialog.getOpenFileName(self, "Import Focus Points", "", "CSV Files (*.csv);;All Files (*)")

        if not file_path:
            return

        try:
            # Read the CSV file
            imported_points = []
            with open(file_path, "r", newline="") as csvfile:
                reader = csv.reader(csvfile)
                header = next(reader)  # Skip header row

                # Validate header
                required_columns = ["Region_ID", "X_mm", "Y_mm", "Z_um"]
                if not all(col in header for col in required_columns):
                    QMessageBox.warning(
                        self, "Invalid Format", f"CSV file must contain columns: {', '.join(required_columns)}"
                    )
                    return

                # Get column indices
                region_idx = header.index("Region_ID")
                x_idx = header.index("X_mm")
                y_idx = header.index("Y_mm")
                z_idx = header.index("Z_um")

                # Read data
                for row in reader:
                    if len(row) >= 4:
                        try:
                            region_id = str(row[region_idx])
                            x = float(row[x_idx])
                            y = float(row[y_idx])
                            z = float(row[z_idx])
                            imported_points.append((region_id, x, y, z))
                        except (ValueError, IndexError):
                            continue

            # If by_region is checked, validate regions
            if self.by_region_checkbox.isChecked():
                scan_regions = set(self.scanCoordinates.region_centers.keys())
                focus_regions = set(region_id for region_id, _, _, _ in imported_points)

                if not focus_regions == scan_regions:
                    response = QMessageBox.warning(
                        self,
                        "Region Mismatch",
                        f"The imported focus points have regions: {', '.join(sorted(focus_regions))}\n\n"
                        f"Current scan has regions: {', '.join(sorted(scan_regions))}\n\n"
                        "Import anyway (disable 'By Region') or cancel?",
                        QMessageBox.Ok | QMessageBox.Cancel,
                        QMessageBox.Cancel,
                    )

                    if response == QMessageBox.Cancel:
                        return
                    else:
                        # User chose to continue, uncheck by_region
                        self.by_region_checkbox.setChecked(False)

            # Clear existing points and add imported ones
            self.focus_points = imported_points
            self.update_point_list()
            self.update_focus_point_display()

            self.status_label.setText(f"Imported {len(imported_points)} focus points")

        except Exception as e:
            QMessageBox.critical(self, "Import Error", f"Failed to import focus points: {str(e)}")

    def on_regions_updated(self):
        if not self._allow_updating_focus_points_on_signal:
            return
        if self.scanCoordinates.has_regions():
            self.generate_grid(self.rows_spin.value(), self.cols_spin.value())

    def disable_updating_focus_points_on_signal(self):
        self._allow_updating_focus_points_on_signal = False

    def enable_updating_focus_points_on_signal(self):
        self._allow_updating_focus_points_on_signal = True

    def setEnabled(self, enabled):
        self.enabled = enabled
        super().setEnabled(enabled)
        self.navigationViewer.focus_point_overlay_item.setVisible(enabled)
        self.on_regions_updated()

    def resizeEvent(self, event):
        """Handle resize events to maintain button sizing"""
        super().resizeEvent(event)
        self.update_z_btn.setFixedWidth(self.edit_point_btn.width())


class AlignmentWidget(QWidget):
    """
    Self-contained widget for alignment workflow.

    Allows users to align current sample position with a previous acquisition by:
    1. Loading a past acquisition folder
    2. Moving stage to a reference FOV position
    3. Displaying reference image as translucent overlay
    4. Calculating X/Y offset after manual alignment
    5. Applying offset to future scan coordinates

    The widget manages its own state and napari layers, communicating with
    external components (stage, live controller) via signals.
    """

    signal_move_to_position = Signal(float, float)  # x_mm, y_mm
    signal_offset_set = Signal(float, float)  # offset_x_mm, offset_y_mm
    signal_offset_cleared = Signal()
    signal_request_current_position = Signal()  # Response via set_current_position()

    # Button states
    STATE_ALIGN = "align"
    STATE_CONFIRM = "confirm"
    STATE_CLEAR = "clear"

    # Napari layer name
    REFERENCE_LAYER_NAME = "Alignment Reference"

    def __init__(self, napari_viewer, parent=None):
        """
        Initialize alignment widget.

        Args:
            napari_viewer: The napari viewer instance for layer management
            parent: Parent widget
        """
        super().__init__(parent)
        self._log = squid.logging.get_logger(self.__class__.__name__)

        self.viewer = napari_viewer
        self.state = self.STATE_ALIGN

        # Alignment state
        self._offset_x_mm = 0.0
        self._offset_y_mm = 0.0
        self._has_offset = False
        self._reference_fov_position = None  # (x_mm, y_mm)
        self._current_folder = None
        self._original_live_opacity = 1.0
        self._original_live_blending = "additive"
        self._pending_position_request = False

        self._setup_ui()

    def _setup_ui(self):
        """Setup the button UI."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.btn_align = QPushButton("Align")
        self.btn_align.setCursor(Qt.PointingHandCursor)
        self.btn_align.setMinimumWidth(100)  # Wide enough for "Confirm Offset"
        self.btn_align.setEnabled(False)  # Disabled until live view starts
        self.btn_align.clicked.connect(self._on_button_clicked)
        layout.addWidget(self.btn_align)

    def enable(self):
        """Enable the alignment button if currently disabled. Call when live view starts."""
        if not self.btn_align.isEnabled():
            self.btn_align.setEnabled(True)

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def has_offset(self) -> bool:
        """Check if an alignment offset is currently active."""
        return self._has_offset

    @property
    def offset_x_mm(self) -> float:
        """Get X offset in mm (0 if no offset)."""
        return self._offset_x_mm if self._has_offset else 0.0

    @property
    def offset_y_mm(self) -> float:
        """Get Y offset in mm (0 if no offset)."""
        return self._offset_y_mm if self._has_offset else 0.0

    def apply_offset(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        """Apply the current alignment offset to coordinates."""
        return (x_mm + self.offset_x_mm, y_mm + self.offset_y_mm)

    def set_current_position(self, x_mm: float, y_mm: float):
        """
        Receive current stage position (response to signal_request_current_position).

        Called by gui_hcs when position is requested during confirm step.
        """
        if self._pending_position_request:
            self._pending_position_request = False
            self._complete_confirmation(x_mm, y_mm)

    def reset(self):
        """Reset widget to initial state."""
        self.state = self.STATE_ALIGN
        self.btn_align.setText("Align")
        self._current_folder = None
        self._reference_fov_position = None
        self._has_offset = False
        self._offset_x_mm = 0.0
        self._offset_y_mm = 0.0
        self._remove_reference_layer()

    # ─────────────────────────────────────────────────────────────────────────
    # Button Click Handler
    # ─────────────────────────────────────────────────────────────────────────

    def _on_button_clicked(self):
        """Handle button click based on current state."""
        if self.state == self.STATE_ALIGN:
            self._handle_align_click()
        elif self.state == self.STATE_CONFIRM:
            self._handle_confirm_click()
        elif self.state == self.STATE_CLEAR:
            self._handle_clear_click()

    def _handle_align_click(self):
        """Handle click in ALIGN state - open folder dialog."""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Past Acquisition Folder",
            str(Path.home()),
        )
        if folder:
            self._start_alignment(folder)

    def _handle_confirm_click(self):
        """Handle click in CONFIRM state - request position and calculate offset."""
        self._pending_position_request = True
        self.signal_request_current_position.emit()

    def _handle_clear_click(self):
        """Handle click in CLEAR state - clear offset."""
        self._offset_x_mm = 0.0
        self._offset_y_mm = 0.0
        self._has_offset = False
        self._reference_fov_position = None
        self._current_folder = None

        self.state = self.STATE_ALIGN
        self.btn_align.setText("Align")

        self.signal_offset_cleared.emit()
        self._log.info("Alignment offset cleared")

    # ─────────────────────────────────────────────────────────────────────────
    # Alignment Workflow
    # ─────────────────────────────────────────────────────────────────────────

    def _start_alignment(self, folder_path: str):
        """Start alignment workflow with selected folder."""
        try:
            info = self._load_acquisition_info(folder_path)
            self._current_folder = folder_path
            ref_x, ref_y = info["center_fov_position"]
            self._reference_fov_position = (ref_x, ref_y)

            self.state = self.STATE_CONFIRM
            self.btn_align.setText("Confirm Offset")

            self.signal_move_to_position.emit(ref_x, ref_y)
            self._load_reference_image(info["image_path"])
            self._log.info(f"Alignment started: ref_pos=({ref_x:.4f}, {ref_y:.4f})")

        except Exception as e:
            self._log.error(f"Failed to start alignment: {e}")
            QMessageBox.warning(self, "Alignment Error", str(e))
            self.reset()

    def _complete_confirmation(self, current_x: float, current_y: float):
        """Complete the confirmation step with current position."""
        if self._reference_fov_position is None:
            self._log.error("Cannot confirm: no reference position set")
            QMessageBox.warning(self, "Alignment Error", "No reference position set. Please load an acquisition first.")
            return

        ref_x, ref_y = self._reference_fov_position
        offset_x = current_x - ref_x
        offset_y = current_y - ref_y

        self._offset_x_mm = offset_x
        self._offset_y_mm = offset_y
        self._has_offset = True

        self._remove_reference_layer()

        self.state = self.STATE_CLEAR
        self.btn_align.setText("Clear Offset")

        self.signal_offset_set.emit(offset_x, offset_y)
        self._log.info(f"Alignment confirmed: offset=({offset_x:.4f}, {offset_y:.4f})mm")

        QMessageBox.information(
            self,
            "Alignment Applied",
            f"Offset applied:\nX: {offset_x:.4f} mm\nY: {offset_y:.4f} mm",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Acquisition Folder Parsing
    # ─────────────────────────────────────────────────────────────────────────

    def _load_acquisition_info(self, folder_path: str) -> dict:
        """
        Load acquisition info from a past acquisition folder.

        Returns dict with: coordinates, first_region, center_fov_index, center_fov_position, image_path
        """
        folder = Path(folder_path)

        coords_file = folder / "coordinates.csv"
        if not coords_file.exists():
            raise FileNotFoundError(f"coordinates.csv not found in {folder_path}")

        coords_df = pd.read_csv(coords_file)
        first_region = coords_df["region"].iloc[0]
        region_coords = coords_df[coords_df["region"] == first_region]

        num_fovs = len(region_coords)
        center_idx = self._find_center_fov(region_coords)
        center_fov = region_coords.iloc[center_idx]
        center_fov_position = (float(center_fov["x (mm)"]), float(center_fov["y (mm)"]))

        image_path = self._find_reference_image(folder, first_region, center_idx)

        self._log.info(
            f"Loaded acquisition info: region={first_region}, "
            f"center_fov={center_idx}/{num_fovs}, "
            f"position=({center_fov_position[0]:.4f}, {center_fov_position[1]:.4f})"
        )

        return {
            "coordinates": coords_df,
            "first_region": first_region,
            "center_fov_index": center_idx,
            "center_fov_position": center_fov_position,
            "image_path": str(image_path),
        }

    def _find_center_fov(self, region_coords: "pd.DataFrame") -> int:
        """Find the FOV index closest to the region center. O(n) complexity."""
        x = region_coords["x (mm)"].values
        y = region_coords["y (mm)"].values
        center_x = (x.min() + x.max()) / 2
        center_y = (y.min() + y.max()) / 2
        distances_sq = (x - center_x) ** 2 + (y - center_y) ** 2
        return int(distances_sq.argmin())

    def _find_reference_image(self, folder: Path, region: str, fov_idx: int) -> Path:
        """Find reference image in OME-TIFF or traditional timepoint folders."""
        # Try OME-TIFF folder first
        ome_tiff_folder = folder / "ome_tiff"
        if ome_tiff_folder.exists():
            ome_images = list(ome_tiff_folder.glob(f"{region}_{fov_idx}.ome.tiff"))
            if ome_images:
                self._log.info(f"Found OME-TIFF image: {ome_images[0]}")
                return ome_images[0]

        # Try traditional timepoint folders
        timepoint_folders = sorted(
            [d for d in folder.iterdir() if d.is_dir() and d.name.isdigit()],
            key=lambda x: int(x.name),
        )
        if timepoint_folders:
            last_timepoint = timepoint_folders[-1]
            for ext in ("tiff", "tif", "bmp"):
                images = sorted(last_timepoint.glob(f"{region}_{fov_idx}_0_*.{ext}"))
                if images:
                    self._log.info(f"Found traditional format image: {images[0]}")
                    return images[0]

        raise FileNotFoundError(
            f"No images found for region={region}, FOV={fov_idx} in {folder}. "
            f"Checked ome_tiff folder and timepoint folders."
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Napari Layer Management
    # ─────────────────────────────────────────────────────────────────────────

    def _load_reference_image(self, image_path: str):
        """Load reference image and add to napari viewer."""
        import tifffile

        if image_path.endswith((".tiff", ".tif", ".ome.tiff", ".ome.tif")):
            ref_image = tifffile.imread(image_path)
            # Reduce multi-dimensional images (T, C, Z, Y, X) to 2D
            while ref_image.ndim > 2:
                ref_image = ref_image[0]
            self._log.info(f"Loaded TIFF reference image, shape: {ref_image.shape}")
        else:
            ref_image = cv2.imread(image_path, cv2.IMREAD_UNCHANGED)
            if ref_image is None:
                raise ValueError(f"Failed to read image: {image_path}")

        self._add_reference_layer(ref_image)

    def _add_reference_layer(self, image: np.ndarray):
        """Add reference image as a napari layer with magenta/green overlay."""
        self._modified_live_view = False
        self._contrast_connected = False
        if "Live View" in self.viewer.layers:
            live_layer = self.viewer.layers["Live View"]
            self._original_live_opacity = live_layer.opacity
            self._original_live_blending = live_layer.blending
            self._original_live_colormap = live_layer.colormap
            live_layer.opacity = 1.0
            live_layer.blending = "additive"
            live_layer.colormap = "green"
            live_layer.events.contrast_limits.connect(self._sync_contrast_limits)
            self._contrast_connected = True
            self._modified_live_view = True
        else:
            self._log.warning("Live View layer not found - reference image will be shown alone")

        if self.REFERENCE_LAYER_NAME in self.viewer.layers:
            self.viewer.layers[self.REFERENCE_LAYER_NAME].data = image
        else:
            self.viewer.add_image(
                image,
                name=self.REFERENCE_LAYER_NAME,
                visible=True,
                opacity=1.0,
                colormap="magenta",
                blending="additive",
            )
        # Sync initial contrast limits from Live View
        if self._contrast_connected and self.REFERENCE_LAYER_NAME in self.viewer.layers:
            ref_layer = self.viewer.layers[self.REFERENCE_LAYER_NAME]
            ref_layer.contrast_limits = live_layer.contrast_limits
        self._log.debug("Reference layer added to napari viewer")

    def _sync_contrast_limits(self, event):
        """Sync contrast limits from Live View to reference layer."""
        if self.REFERENCE_LAYER_NAME in self.viewer.layers:
            self.viewer.layers[self.REFERENCE_LAYER_NAME].contrast_limits = event.value

    def _remove_reference_layer(self):
        """Remove reference layer and restore live view settings."""
        if self.REFERENCE_LAYER_NAME in self.viewer.layers:
            self.viewer.layers.remove(self.REFERENCE_LAYER_NAME)
            self._log.debug("Reference layer removed from napari viewer")

        if getattr(self, "_modified_live_view", False) and "Live View" in self.viewer.layers:
            live_layer = self.viewer.layers["Live View"]
            if getattr(self, "_contrast_connected", False):
                live_layer.events.contrast_limits.disconnect(self._sync_contrast_limits)
                self._contrast_connected = False
            live_layer.opacity = self._original_live_opacity
            live_layer.blending = self._original_live_blending
            live_layer.colormap = self._original_live_colormap
            self._modified_live_view = False


class NapariLiveWidget(QWidget):
    signal_coordinates_clicked = Signal(int, int, int, int)
    signal_newExposureTime = Signal(float)
    signal_newAnalogGain = Signal(float)
    signal_autoLevelSetting = Signal(bool)

    def __init__(
        self,
        streamHandler,
        liveController,
        stage: AbstractStage,
        objectiveStore,
        contrastManager,
        wellSelectionWidget=None,
        show_trigger_options=True,
        show_display_options=True,
        show_autolevel=False,
        autolevel=False,
        parent=None,
    ):
        super().__init__(parent)
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.streamHandler = streamHandler
        self.liveController: LiveController = liveController
        self.stage = stage
        self.objectiveStore = objectiveStore
        self.wellSelectionWidget = wellSelectionWidget
        chs = self.liveController.get_channels(self.objectiveStore.current_objective)
        if self.liveController.currentConfiguration is None and chs:
            self.liveController.set_active_channel_reference(chs[0])
        self.live_configuration = self.liveController.currentConfiguration or (chs[0] if chs else None)
        self.image_width = 0
        self.image_height = 0
        self.dtype = np.uint8
        self.channels = set()
        self.init_live = False
        self.init_live_rgb = False
        self.init_scale = False
        self.previous_scale = None
        self.previous_center = None
        self.last_was_autofocus = False
        self.fps_trigger = 10
        self.fps_display = 10
        self.contrastManager = contrastManager
        self.is_switching_mode = False  # Guard to prevent duplicate MCU commands during mode switch

        self.initNapariViewer()
        self.addNapariGrayclipColormap()
        self.initControlWidgets(show_trigger_options, show_display_options, show_autolevel, autolevel)
        if self.live_configuration is not None:
            self.update_ui_for_mode(self.live_configuration)
        else:
            self._log.error("NapariLiveWidget: no acquisition channels for current objective")

    def initNapariViewer(self):
        self.viewer = napari.Viewer(show=False)
        self.viewerWidget = self.viewer.window._qt_window
        self.viewer.dims.axis_labels = ["Y-axis", "X-axis"]
        self.layout = QVBoxLayout()
        self.layout.addWidget(self.viewerWidget)
        self.setLayout(self.layout)
        self.customizeViewer()

    def customizeViewer(self):
        # # Hide the status bar (which includes the activity button)
        # if hasattr(self.viewer.window, "_status_bar"):
        #     self.viewer.window._status_bar.hide()

        # Disable napari's native menu bar so it doesn't take over macOS global menu bar
        if sys.platform == "darwin":
            self.viewer.window.main_menu.setNativeMenuBar(False)
        self.viewer.window.main_menu.hide()

        # Hide the layer buttons
        if hasattr(self.viewer.window._qt_viewer, "layerButtons"):
            self.viewer.window._qt_viewer.layerButtons.hide()

    def updateHistogram(self, layer):
        if self.histogram_widget is not None and layer.data is not None:
            self.pg_image_item.setImage(layer.data, autoLevels=False)
            self.histogram_widget.setLevels(*layer.contrast_limits)
            self.histogram_widget.setHistogramRange(layer.data.min(), layer.data.max())

            # Set the histogram widget's region to match the layer's contrast limits
            self.histogram_widget.region.setRegion(layer.contrast_limits)

            # Update colormap only if it has changed
            if hasattr(self, "last_colormap") and self.last_colormap != layer.colormap.name:
                self.histogram_widget.gradient.setColorMap(self.createColorMap(layer.colormap))
            self.last_colormap = layer.colormap.name

    def createColorMap(self, colormap):
        colors = colormap.colors
        positions = np.linspace(0, 1, len(colors))
        return pg.ColorMap(positions, colors)

    def initControlWidgets(self, show_trigger_options, show_display_options, show_autolevel, autolevel):
        # Initialize histogram widget
        self.pg_image_item = pg.ImageItem()
        self.histogram_widget = pg.HistogramLUTWidget(image=self.pg_image_item)
        self.histogram_widget.setFixedWidth(100)
        self.histogram_dock = self.viewer.window.add_dock_widget(self.histogram_widget, area="right", name="hist")
        self.histogram_dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
        self.histogram_dock.setTitleBarWidget(QWidget())
        self.histogram_widget.region.sigRegionChanged.connect(self.on_histogram_region_changed)
        self.histogram_widget.region.sigRegionChangeFinished.connect(self.on_histogram_region_changed)

        # Microscope Configuration (only enabled channels)
        self.dropdown_modeSelection = QComboBox()
        for config in self.liveController.get_channels(self.objectiveStore.current_objective):
            self.dropdown_modeSelection.addItem(config.name)
        if self.live_configuration is not None:
            self.dropdown_modeSelection.setCurrentText(self.live_configuration.name)
        self.dropdown_modeSelection.activated.connect(self.select_new_microscope_mode_by_name)

        # Live button
        self.btn_live = QPushButton("Start Live")
        self.btn_live.setCheckable(True)
        gradient_style = """
            QPushButton {
                background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1,
                                                  stop:0 #D6D6FF, stop:1 #C2C2FF);
                border-radius: 5px;
                color: black;
                border: 1px solid #A0A0A0;
            }
            QPushButton:checked {
                background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1,
                                                  stop:0 #FFD6D6, stop:1 #FFC2C2);
                border: 1px solid #A0A0A0;
            }
            QPushButton:hover {
                background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1,
                                                  stop:0 #E0E0FF, stop:1 #D0D0FF);
            }
            QPushButton:pressed {
                background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1,
                                                  stop:0 #9090C0, stop:1 #8080B0);
            }
        """
        self.btn_live.setStyleSheet(gradient_style)
        # self.btn_live.setStyleSheet("font-weight: bold; background-color: #7676F7") #6666D3
        current_height = self.btn_live.sizeHint().height()
        self.btn_live.setFixedHeight(int(current_height * 1.5))
        self.btn_live.clicked.connect(self.toggle_live)

        # Exposure Time
        self.entry_exposureTime = QDoubleSpinBox()
        self.entry_exposureTime.setRange(*self.liveController.camera.get_exposure_limits())
        self.entry_exposureTime.setValue(self.live_configuration.exposure_time)
        self.entry_exposureTime.setSuffix(" ms")
        self.entry_exposureTime.valueChanged.connect(self.update_config_exposure_time)

        # Analog Gain
        self.entry_analogGain = QDoubleSpinBox()
        self.entry_analogGain.setRange(0, 24)
        self.entry_analogGain.setSingleStep(0.1)
        self.entry_analogGain.setValue(self.live_configuration.analog_gain)
        # self.entry_analogGain.setSuffix('x')
        self.entry_analogGain.valueChanged.connect(self.update_config_analog_gain)

        # Illumination Intensity
        self.slider_illuminationIntensity = QSlider(Qt.Horizontal)
        self.slider_illuminationIntensity.setRange(0, 100)
        self.slider_illuminationIntensity.setValue(int(self.live_configuration.illumination_intensity))
        self.slider_illuminationIntensity.setTickPosition(QSlider.TicksBelow)
        self.slider_illuminationIntensity.setTickInterval(10)
        self.slider_illuminationIntensity.valueChanged.connect(self.update_config_illumination_intensity)
        self.label_illuminationIntensity = QLabel(str(self.slider_illuminationIntensity.value()) + "%")
        self.slider_illuminationIntensity.valueChanged.connect(
            lambda v: self.label_illuminationIntensity.setText(str(v) + "%")
        )

        # Trigger mode
        self.dropdown_triggerMode = QComboBox()
        trigger_modes = [
            ("Software", TriggerMode.SOFTWARE),
            ("Hardware", TriggerMode.HARDWARE),
            ("Continuous", TriggerMode.CONTINUOUS),
        ]
        for display_name, mode in trigger_modes:
            self.dropdown_triggerMode.addItem(display_name, mode)
        self.dropdown_triggerMode.currentIndexChanged.connect(self.on_trigger_mode_changed)

        # Trigger FPS
        self.entry_triggerFPS = QDoubleSpinBox()
        self.entry_triggerFPS.setRange(0.02, 1000)
        self.entry_triggerFPS.setValue(self.fps_trigger)
        # self.entry_triggerFPS.setSuffix(" fps")
        self.entry_triggerFPS.valueChanged.connect(self.liveController.set_trigger_fps)

        # Display FPS
        self.entry_displayFPS = QDoubleSpinBox()
        self.entry_displayFPS.setRange(1, 240)
        self.entry_displayFPS.setValue(self.fps_display)
        # self.entry_displayFPS.setSuffix(" fps")
        self.entry_displayFPS.valueChanged.connect(self.streamHandler.set_display_fps)

        # Resolution Scaling
        self.slider_resolutionScaling = QSlider(Qt.Horizontal)
        self.slider_resolutionScaling.setRange(10, 100)
        self.slider_resolutionScaling.setValue(100)
        self.slider_resolutionScaling.setTickPosition(QSlider.TicksBelow)
        self.slider_resolutionScaling.setTickInterval(10)
        self.slider_resolutionScaling.valueChanged.connect(self.update_resolution_scaling)
        self.label_resolutionScaling = QLabel(str(self.slider_resolutionScaling.value()) + "%")
        self.slider_resolutionScaling.valueChanged.connect(lambda v: self.label_resolutionScaling.setText(str(v) + "%"))

        # Autolevel
        self.btn_autolevel = QPushButton("Autolevel")
        self.btn_autolevel.setCheckable(True)
        self.btn_autolevel.setChecked(autolevel)
        self.btn_autolevel.clicked.connect(self.signal_autoLevelSetting.emit)

        def make_row(label_widget, entry_widget, value_label=None):
            row = QHBoxLayout()
            row.addWidget(label_widget)
            row.addWidget(entry_widget)
            if value_label:
                row.addWidget(value_label)
            return row

        control_layout = QVBoxLayout()

        # Add widgets to layout
        control_layout.addWidget(self.dropdown_modeSelection)
        control_layout.addWidget(self.btn_live)
        control_layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Minimum, QSizePolicy.Expanding))

        row1 = make_row(QLabel("Exposure Time"), self.entry_exposureTime)
        control_layout.addLayout(row1)

        row2 = make_row(QLabel("Illumination"), self.slider_illuminationIntensity, self.label_illuminationIntensity)
        control_layout.addLayout(row2)

        row3 = make_row((QLabel("Analog Gain")), self.entry_analogGain)
        control_layout.addLayout(row3)
        control_layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Minimum, QSizePolicy.Expanding))

        if show_trigger_options:
            row0 = make_row(QLabel("Trigger Mode"), self.dropdown_triggerMode)
            control_layout.addLayout(row0)
            row00 = make_row(QLabel("Trigger FPS"), self.entry_triggerFPS)
            control_layout.addLayout(row00)
            control_layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Minimum, QSizePolicy.Expanding))

        if show_display_options:
            row4 = make_row((QLabel("Display FPS")), self.entry_displayFPS)
            control_layout.addLayout(row4)
            row5 = make_row(QLabel("Display Resolution"), self.slider_resolutionScaling, self.label_resolutionScaling)
            control_layout.addLayout(row5)
            control_layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Minimum, QSizePolicy.Expanding))

        if show_autolevel:
            control_layout.addWidget(self.btn_autolevel)
            control_layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Minimum, QSizePolicy.Expanding))

        control_layout.addStretch(1)

        add_live_controls = False
        if USE_NAPARI_FOR_LIVE_CONTROL or add_live_controls:
            live_controls_widget = QWidget()
            live_controls_widget.setLayout(control_layout)
            # layer_list_widget.setFixedWidth(270)

            layer_controls_widget = self.viewer.window._qt_viewer.dockLayerControls.widget()
            layer_list_widget = self.viewer.window._qt_viewer.dockLayerList.widget()

            self.viewer.window._qt_viewer.layerButtons.hide()
            self.viewer.window.remove_dock_widget(self.viewer.window._qt_viewer.dockLayerControls)
            self.viewer.window.remove_dock_widget(self.viewer.window._qt_viewer.dockLayerList)

            # Add the actual dock widgets
            self.dock_layer_controls = self.viewer.window.add_dock_widget(
                layer_controls_widget, area="left", name="layer controls", tabify=True
            )
            self.dock_layer_list = self.viewer.window.add_dock_widget(
                layer_list_widget, area="left", name="layer list", tabify=True
            )
            self.dock_live_controls = self.viewer.window.add_dock_widget(
                live_controls_widget, area="left", name="live controls", tabify=True
            )

            self.viewer.window.window_menu.addAction(self.dock_live_controls.toggleViewAction())

        if USE_NAPARI_WELL_SELECTION:
            well_selector_layout = QVBoxLayout()
            # title_label = QLabel("Well Selector")
            # title_label.setAlignment(Qt.AlignCenter)  # Center the title
            # title_label.setStyleSheet("font-weight: bold;")  # Optional: style the title
            # well_selector_layout.addWidget(title_label)

            well_selector_row = QHBoxLayout()
            well_selector_row.addStretch(1)
            well_selector_row.addWidget(self.wellSelectionWidget)
            well_selector_row.addStretch(1)
            well_selector_layout.addLayout(well_selector_row)
            well_selector_layout.addStretch()

            well_selector_dock_widget = QWidget()
            well_selector_dock_widget.setLayout(well_selector_layout)
            self.dock_well_selector = self.viewer.window.add_dock_widget(
                well_selector_dock_widget, area="bottom", name="well selector"
            )
            self.dock_well_selector.setFixedHeight(self.dock_well_selector.minimumSizeHint().height())

        layer_controls_widget = self.viewer.window._qt_viewer.dockLayerControls.widget()
        layer_list_widget = self.viewer.window._qt_viewer.dockLayerList.widget()

        self.viewer.window._qt_viewer.layerButtons.hide()
        self.viewer.window.remove_dock_widget(self.viewer.window._qt_viewer.dockLayerControls)
        self.viewer.window.remove_dock_widget(self.viewer.window._qt_viewer.dockLayerList)
        self.print_window_menu_items()

    def print_window_menu_items(self):
        print("Items in window_menu:")
        for action in self.viewer.window.window_menu.actions():
            print(action.text())

    def on_histogram_region_changed(self):
        if self.live_configuration.name:
            min_val, max_val = self.histogram_widget.region.getRegion()
            self.updateContrastLimits(self.live_configuration.name, min_val, max_val)

    def toggle_live(self, pressed):
        if pressed:
            self.liveController.start_live()
            self.btn_live.setText("Stop Live")
        else:
            self.liveController.stop_live()
            self.btn_live.setText("Start Live")

    def toggle_live_controls(self, show):
        if show:
            self.dock_live_controls.show()
        else:
            self.dock_live_controls.hide()

    def toggle_well_selector(self, show):
        if show:
            self.dock_well_selector.show()
        else:
            self.dock_well_selector.hide()

    def replace_well_selector(self, wellSelector):
        self.viewer.window.remove_dock_widget(self.dock_well_selector)
        self.wellSelectionWidget = wellSelector
        well_selector_layout = QHBoxLayout()
        well_selector_layout.addStretch(1)  # Add stretch on the left
        well_selector_layout.addWidget(self.wellSelectionWidget)
        well_selector_layout.addStretch(1)  # Add stretch on the right
        well_selector_dock_widget = QWidget()
        well_selector_dock_widget.setLayout(well_selector_layout)
        self.dock_well_selector = self.viewer.window.add_dock_widget(
            well_selector_dock_widget, area="bottom", name="well selector", tabify=True
        )

    def select_new_microscope_mode_by_name(self, config_index):
        config_name = self.dropdown_modeSelection.itemText(config_index)
        maybe_new_config = self.liveController.get_channel_by_name(self.objectiveStore.current_objective, config_name)

        if not maybe_new_config:
            self._log.error(f"User attempted to select config named '{config_name}' but it does not exist!")
            return

        self.liveController.set_microscope_mode(maybe_new_config)
        self.update_ui_for_mode(maybe_new_config)

    def update_ui_for_mode(self, config):
        try:
            self.is_switching_mode = True
            self.live_configuration = config
            self.dropdown_modeSelection.setCurrentText(config.name if config else "Unknown")
            if self.live_configuration:
                self.entry_exposureTime.setValue(self.live_configuration.exposure_time)
                self.entry_analogGain.setValue(self.live_configuration.analog_gain)
                self.slider_illuminationIntensity.setValue(int(self.live_configuration.illumination_intensity))
        finally:
            self.is_switching_mode = False

    def update_config_exposure_time(self, new_value):
        if self.is_switching_mode:
            return
        self.live_configuration.exposure_time = new_value
        self.liveController.microscope.config_repo.update_channel_setting(
            self.objectiveStore.current_objective,
            self.live_configuration.name,
            "ExposureTime",
            new_value,
            confocal_mode=self.liveController.is_confocal_mode(),
        )
        self.signal_newExposureTime.emit(new_value)

    def update_config_analog_gain(self, new_value):
        if self.is_switching_mode:
            return
        self.live_configuration.analog_gain = new_value
        self.liveController.microscope.config_repo.update_channel_setting(
            self.objectiveStore.current_objective,
            self.live_configuration.name,
            "AnalogGain",
            new_value,
            confocal_mode=self.liveController.is_confocal_mode(),
        )
        self.signal_newAnalogGain.emit(new_value)

    def update_config_illumination_intensity(self, new_value):
        if self.is_switching_mode:
            return
        self.live_configuration.illumination_intensity = new_value
        self.liveController.microscope.config_repo.update_channel_setting(
            self.objectiveStore.current_objective,
            self.live_configuration.name,
            "IlluminationIntensity",
            new_value,
            confocal_mode=self.liveController.is_confocal_mode(),
        )
        self.liveController.update_illumination()

    def update_resolution_scaling(self, value):
        self.streamHandler.set_display_resolution_scaling(value)
        self.liveController.set_display_resolution_scaling(value)

    def refresh_mode_list(self):
        """Refresh the mode selection dropdown (only show enabled channels)"""
        self.dropdown_modeSelection.blockSignals(True)
        self.dropdown_modeSelection.clear()
        first_config = None
        for config in self.liveController.get_channels(self.objectiveStore.current_objective):
            if not first_config:
                first_config = config
            self.dropdown_modeSelection.addItem(config.name)
        self.dropdown_modeSelection.blockSignals(False)

        if self.dropdown_modeSelection.count() > 0 and first_config:
            self.update_ui_for_mode(first_config)
            self.liveController.set_microscope_mode(first_config)

    def on_trigger_mode_changed(self, index):
        # Get the actual value using user data
        actual_value = self.dropdown_triggerMode.itemData(index)
        print(f"Selected: {self.dropdown_triggerMode.currentText()} (actual value: {actual_value})")

    def addNapariGrayclipColormap(self):
        if hasattr(napari.utils.colormaps.AVAILABLE_COLORMAPS, "grayclip"):
            return
        grayclip = []
        for i in range(255):
            grayclip.append([i / 255, i / 255, i / 255])
        grayclip.append([1, 0, 0])
        napari.utils.colormaps.AVAILABLE_COLORMAPS["grayclip"] = napari.utils.Colormap(name="grayclip", colors=grayclip)

    def initLiveLayer(self, channel, image_height, image_width, image_dtype, rgb=False):
        """Initializes the full canvas for each channel based on the acquisition parameters."""
        self.viewer.layers.clear()
        self.image_width = image_width
        self.image_height = image_height
        if self.dtype != np.dtype(image_dtype):

            self.contrastManager.scale_contrast_limits(
                np.dtype(image_dtype)
            )  # Fix This to scale existing contrast limits to new dtype range
            self.dtype = image_dtype

        self.channels.add(channel)
        self.live_configuration.name = channel

        if rgb:
            canvas = np.zeros((image_height, image_width, 3), dtype=self.dtype)
        else:
            canvas = np.zeros((image_height, image_width), dtype=self.dtype)
        limits = self.getContrastLimits(self.dtype)
        layer = self.viewer.add_image(
            canvas,
            name="Live View",
            visible=True,
            rgb=rgb,
            colormap="grayclip",
            contrast_limits=limits,
            blending="additive",
        )
        layer.contrast_limits = self.contrastManager.get_limits(self.live_configuration.name, self.dtype)
        layer.mouse_double_click_callbacks.append(self.onDoubleClick)
        layer.events.contrast_limits.connect(self.signalContrastLimits)
        self.updateHistogram(layer)

        if not self.init_scale:
            self.resetView()
            self.previous_scale = self.viewer.camera.zoom
            self.previous_center = self.viewer.camera.center
        else:
            self.viewer.camera.zoom = self.previous_scale
            self.viewer.camera.center = self.previous_center

    def updateLiveLayer(self, image, from_autofocus=False):
        """Updates the canvas with the new image data."""
        if self.dtype != np.dtype(image.dtype):
            self.contrastManager.scale_contrast_limits(np.dtype(image.dtype))
            self.dtype = np.dtype(image.dtype)
            self.init_live = False
            self.init_live_rgb = False

        if not self.live_configuration.name:
            self.live_configuration.name = self.liveController.currentConfiguration.name
        rgb = len(image.shape) >= 3

        if not rgb and not self.init_live or "Live View" not in self.viewer.layers:
            self.initLiveLayer(self.live_configuration.name, image.shape[0], image.shape[1], image.dtype, rgb)
            self.init_live = True
            self.init_live_rgb = False
            print("init live")
        elif rgb and not self.init_live_rgb:
            self.initLiveLayer(self.live_configuration.name, image.shape[0], image.shape[1], image.dtype, rgb)
            self.init_live_rgb = True
            self.init_live = False
            print("init live rgb")

        layer = self.viewer.layers["Live View"]
        layer.data = image
        layer.contrast_limits = self.contrastManager.get_limits(self.live_configuration.name)
        self.updateHistogram(layer)

        if from_autofocus:
            # save viewer scale
            if not self.last_was_autofocus:
                self.previous_scale = self.viewer.camera.zoom
                self.previous_center = self.viewer.camera.center
            # resize to cropped view
            self.resetView()
            self.last_was_autofocus = True
        else:
            if not self.init_scale:
                # init viewer scale
                self.resetView()
                self.previous_scale = self.viewer.camera.zoom
                self.previous_center = self.viewer.camera.center
                self.init_scale = True
            elif self.last_was_autofocus:
                # return to to original view
                self.viewer.camera.zoom = self.previous_scale
                self.viewer.camera.center = self.previous_center
            # save viewer scale
            self.previous_scale = self.viewer.camera.zoom
            self.previous_center = self.viewer.camera.center
            self.last_was_autofocus = False
        layer.refresh()

    def onDoubleClick(self, layer, event):
        """Handle double-click events and emit centered coordinates if within the data range."""
        coords = layer.world_to_data(event.position)
        layer_shape = layer.data.shape[0:2] if len(layer.data.shape) >= 3 else layer.data.shape

        if coords is not None and (0 <= int(coords[-1]) < layer_shape[-1] and (0 <= int(coords[-2]) < layer_shape[-2])):
            x_centered = int(coords[-1] - layer_shape[-1] / 2)
            y_centered = int(coords[-2] - layer_shape[-2] / 2)
            # Emit the centered coordinates and dimensions of the layer's data array
            self.signal_coordinates_clicked.emit(x_centered, y_centered, layer_shape[-1], layer_shape[-2])

    def set_live_configuration(self, live_configuration):
        self.live_configuration = live_configuration

    def updateContrastLimits(self, channel, min_val, max_val):
        self.contrastManager.update_limits(channel, min_val, max_val)
        if "Live View" in self.viewer.layers:
            self.viewer.layers["Live View"].contrast_limits = (min_val, max_val)

    def signalContrastLimits(self, event):
        layer = event.source
        min_val, max_val = map(float, layer.contrast_limits)
        self.contrastManager.update_limits(self.live_configuration.name, min_val, max_val)

    def getContrastLimits(self, dtype):
        return self.contrastManager.get_default_limits()

    def resetView(self):
        self.viewer.reset_view()

    def activate(self):
        print("ACTIVATING NAPARI LIVE WIDGET")
        self.viewer.window.activate()


class NapariMultiChannelWidget(QWidget):

    def __init__(self, objectiveStore, camera, contrastManager, grid_enabled=False, parent=None):
        super().__init__(parent)
        # Initialize placeholders for the acquisition parameters
        self.objectiveStore = objectiveStore
        self.camera = camera
        self.contrastManager = contrastManager
        self.image_width = 0
        self.image_height = 0
        self.dtype = np.uint8
        self.channels = set()
        self.pixel_size_um = 1
        self.dz_um = 1
        self.Nz = 1
        self.layers_initialized = False
        self.acquisition_initialized = False
        self.viewer_scale_initialized = False
        self.update_layer_count = 0
        self.grid_enabled = grid_enabled

        # Initialize a napari Viewer without showing its standalone window.
        self.initNapariViewer()

    def initNapariViewer(self):
        self.viewer = napari.Viewer(show=False)
        if self.grid_enabled:
            self.viewer.grid.enabled = True
        self.viewer.dims.axis_labels = ["Z-axis", "Y-axis", "X-axis"]
        self.viewerWidget = self.viewer.window._qt_window
        self.layout = QVBoxLayout()
        self.layout.addWidget(self.viewerWidget)
        self.setLayout(self.layout)
        self.customizeViewer()

    def customizeViewer(self):
        # # Hide the status bar (which includes the activity button)
        # if hasattr(self.viewer.window, "_status_bar"):
        #     self.viewer.window._status_bar.hide()

        # Disable napari's native menu bar so it doesn't take over macOS global menu bar
        if sys.platform == "darwin":
            self.viewer.window.main_menu.setNativeMenuBar(False)
        self.viewer.window.main_menu.hide()

        # Hide the layer buttons
        if hasattr(self.viewer.window._qt_viewer, "layerButtons"):
            self.viewer.window._qt_viewer.layerButtons.hide()

    def initLayersShape(self, Nz, dz):
        pixel_size_um = self.objectiveStore.get_pixel_size_factor() * self.camera.get_pixel_size_binned_um()
        if self.Nz != Nz or self.dz_um != dz or self.pixel_size_um != pixel_size_um:
            self.acquisition_initialized = False
            self.Nz = Nz
            self.dz_um = dz if Nz > 1 and dz != 0 else 1.0
            self.pixel_size_um = pixel_size_um

    def initChannels(self, channels):
        self.channels = set(channels)

    def extractWavelength(self, name):
        # Split the string and find the wavelength number immediately after "Fluorescence"
        parts = name.split()
        if "Fluorescence" in parts:
            index = parts.index("Fluorescence") + 1
            if index < len(parts):
                return parts[index].split()[0]  # Assuming '488 nm Ex' and taking '488'
        for color in ["R", "G", "B"]:
            if color in parts or f"full_{color}" in parts:
                return color
        return None

    def generateColormap(self, channel_info):
        """Convert a HEX value to a normalized RGB tuple."""
        positions = [0, 1]
        c0 = (0, 0, 0)
        c1 = (
            ((channel_info["hex"] >> 16) & 0xFF) / 255,  # Normalize the Red component
            ((channel_info["hex"] >> 8) & 0xFF) / 255,  # Normalize the Green component
            (channel_info["hex"] & 0xFF) / 255,
        )  # Normalize the Blue component
        return Colormap(colors=[c0, c1], controls=[0, 1], name=channel_info["name"])

    def initLayers(self, image_height, image_width, image_dtype):
        """Initializes the full canvas for each channel based on the acquisition parameters."""
        if self.acquisition_initialized:
            for layer in list(self.viewer.layers):
                if layer.name not in self.channels:
                    self.viewer.layers.remove(layer)
        else:
            self.viewer.layers.clear()
            self.acquisition_initialized = True
            if self.dtype != np.dtype(image_dtype) and not USE_NAPARI_FOR_LIVE_VIEW:
                self.contrastManager.scale_contrast_limits(image_dtype)

        self.image_width = image_width
        self.image_height = image_height
        self.dtype = np.dtype(image_dtype)
        self.layers_initialized = True
        self.update_layer_count = 0

    def updateLayers(self, image, x, y, k, channel_name):
        """Updates the appropriate slice of the canvas with the new image data."""
        rgb = len(image.shape) == 3

        # Check if the layer exists and has a different dtype
        if self.dtype != np.dtype(image.dtype):  # or self.viewer.layers[channel_name].data.dtype != image.dtype:
            # Remove the existing layer
            self.layers_initialized = False
            self.acquisition_initialized = False

        if not self.layers_initialized:
            self.initLayers(image.shape[0], image.shape[1], image.dtype)

        if channel_name not in self.viewer.layers:
            self.channels.add(channel_name)
            if rgb:
                color = None  # RGB images do not need a colormap
                canvas = np.zeros((self.Nz, self.image_height, self.image_width, 3), dtype=self.dtype)
            else:
                channel_info = CHANNEL_COLORS_MAP.get(
                    self.extractWavelength(channel_name), {"hex": 0xFFFFFF, "name": "gray"}
                )
                if channel_info["name"] in AVAILABLE_COLORMAPS:
                    color = AVAILABLE_COLORMAPS[channel_info["name"]]
                else:
                    color = self.generateColormap(channel_info)
                canvas = np.zeros((self.Nz, self.image_height, self.image_width), dtype=self.dtype)

            limits = self.getContrastLimits(self.dtype)
            layer = self.viewer.add_image(
                canvas,
                name=channel_name,
                visible=True,
                rgb=rgb,
                colormap=color,
                contrast_limits=limits,
                blending="additive",
                scale=(self.dz_um, self.pixel_size_um, self.pixel_size_um),
            )

            # print(f"multi channel - dz_um:{self.dz_um}, pixel_y_um:{self.pixel_size_um}, pixel_x_um:{self.pixel_size_um}")
            layer.contrast_limits = self.contrastManager.get_limits(channel_name)
            layer.events.contrast_limits.connect(self.signalContrastLimits)

            if not self.viewer_scale_initialized:
                self.resetView()
                self.viewer_scale_initialized = True
            else:
                layer.refresh()

        layer = self.viewer.layers[channel_name]
        layer.data[k] = image
        layer.contrast_limits = self.contrastManager.get_limits(channel_name)
        self.update_layer_count += 1
        if self.update_layer_count % len(self.channels) == 0:
            if self.Nz > 1:
                self.viewer.dims.set_point(0, k * self.dz_um)
            for layer in self.viewer.layers:
                layer.refresh()

    def signalContrastLimits(self, event):
        layer = event.source
        min_val, max_val = map(float, layer.contrast_limits)
        self.contrastManager.update_limits(layer.name, min_val, max_val)

    def getContrastLimits(self, dtype):
        return self.contrastManager.get_default_limits()

    def resetView(self):
        self.viewer.reset_view()
        for layer in self.viewer.layers:
            layer.refresh()

    def activate(self):
        self.viewer.window.activate()


class NapariMosaicDisplayWidget(QWidget):

    signal_coordinates_clicked = Signal(float, float)  # x, y in mm
    signal_clear_viewer = Signal()
    signal_layers_initialized = Signal()
    signal_shape_drawn = Signal(list)

    def __init__(self, objectiveStore, camera, contrastManager, parent=None):
        super().__init__(parent)
        self.objectiveStore = objectiveStore
        self.camera = camera
        self.contrastManager = contrastManager
        self.viewer = napari.Viewer(show=False)
        self.layout = QVBoxLayout()
        self.layout.addWidget(self.viewer.window._qt_window)
        self.layers_initialized = False
        self.shape_layer = None
        self.shapes_mm = []
        self.is_drawing_shape = False

        # add clear button
        self.clear_button = QPushButton("Clear Mosaic View")
        self.clear_button.clicked.connect(self.clearAllLayers)
        self.layout.addWidget(self.clear_button)

        self.setLayout(self.layout)
        self.customizeViewer()
        self.viewer_pixel_size_mm = 1
        self.dz_um = None
        self.Nz = None
        self.channels = set()
        self.viewer_extents = []  # [min_y, max_y, min_x, max_x]
        self.top_left_coordinate = None  # [y, x] in mm
        self.mosaic_dtype = None

    def customizeViewer(self):
        # # hide status bar
        # if hasattr(self.viewer.window, "_status_bar"):
        #     self.viewer.window._status_bar.hide()

        # Disable napari's native menu bar so it doesn't take over macOS global menu bar
        if sys.platform == "darwin":
            self.viewer.window.main_menu.setNativeMenuBar(False)
        self.viewer.window.main_menu.hide()

        self.viewer.bind_key("D", self.toggle_draw_mode)

    def toggle_draw_mode(self, viewer):
        self.is_drawing_shape = not self.is_drawing_shape

        if "Manual ROI" not in self.viewer.layers:
            self.shape_layer = self.viewer.add_shapes(
                name="Manual ROI", edge_width=40, edge_color="red", face_color="transparent"
            )
            self.shape_layer.events.data.connect(self.on_shape_change)
        else:
            self.shape_layer = self.viewer.layers["Manual ROI"]

        if self.is_drawing_shape:
            # if there are existing shapes, switch to vertex select mode
            if len(self.shape_layer.data) > 0:
                self.shape_layer.mode = "select"
                self.shape_layer.select_mode = "vertex"
            else:
                # if no shapes exist, switch to add polygon mode
                # start drawing a new polygon on click, add vertices with additional clicks, finish/close polygon with double-click
                self.shape_layer.mode = "add_polygon"
        else:
            # if no shapes exist, switch to pan/zoom mode
            self.shape_layer.mode = "pan_zoom"

        self.on_shape_change()

    def enable_shape_drawing(self, enable):
        if enable:
            self.toggle_draw_mode(self.viewer)
        else:
            self.is_drawing_shape = False
            if self.shape_layer is not None:
                self.shape_layer.mode = "pan_zoom"

    def on_shape_change(self, event=None):
        if self.shape_layer is not None and len(self.shape_layer.data) > 0:
            # Only convert shapes to mm if mosaic is initialized (has valid coordinate system)
            if self.layers_initialized and self.top_left_coordinate is not None:
                self.shapes_mm = [self.convert_shape_to_mm(shape) for shape in self.shape_layer.data]
            # else: keep existing shapes_mm (they're already in mm from before clear)
        else:
            self.shapes_mm = []
        self.signal_shape_drawn.emit(self.shapes_mm)

    def convert_shape_to_mm(self, shape_data):
        shape_data_mm = []
        # Scale factor: viewer uses um (mm * 1000), so data coords = world coords / (pixel_size_mm * 1000)
        scale = self.viewer_pixel_size_mm * 1000
        for point in shape_data:
            # Convert world coordinates (um) to data coordinates (pixels)
            y_data = point[0] / scale
            x_data = point[1] / scale
            # Convert data coordinates to mm
            x_mm = self.top_left_coordinate[1] + x_data * self.viewer_pixel_size_mm
            y_mm = self.top_left_coordinate[0] + y_data * self.viewer_pixel_size_mm
            shape_data_mm.append([x_mm, y_mm])
        return np.array(shape_data_mm)

    def convert_mm_to_viewer_shapes(self, shapes_mm):
        viewer_shapes = []
        # Scale factor: viewer uses um (mm * 1000), so world coords = data coords * (pixel_size_mm * 1000)
        scale = self.viewer_pixel_size_mm * 1000
        for shape_mm in shapes_mm:
            viewer_shape = []
            for point_mm in shape_mm:
                # Convert mm to data coordinates (pixels)
                x_data = (point_mm[0] - self.top_left_coordinate[1]) / self.viewer_pixel_size_mm
                y_data = (point_mm[1] - self.top_left_coordinate[0]) / self.viewer_pixel_size_mm
                # Convert data coordinates to world coordinates (um)
                world_coords = [y_data * scale, x_data * scale]
                viewer_shape.append(world_coords)
            viewer_shapes.append(viewer_shape)
        return viewer_shapes

    def update_shape_layer_position(self, prev_top_left, new_top_left):
        if self.shape_layer is None or len(self.shapes_mm) == 0:
            return
        try:
            # update top_left_coordinate
            self.top_left_coordinate = new_top_left

            # convert mm coordinates to viewer coordinates
            new_shapes = self.convert_mm_to_viewer_shapes(self.shapes_mm)

            # update shape layer data
            self.shape_layer.data = new_shapes
        except Exception as e:
            print(f"Error updating shape layer position: {e}")
            import traceback

            traceback.print_exc()

    def initChannels(self, channels):
        self.channels = set(channels)

    def initLayersShape(self, Nz, dz):
        self.Nz = 1
        self.dz_um = dz

    def extractWavelength(self, name):
        # extract wavelength from channel name
        parts = name.split()
        if "Fluorescence" in parts:
            index = parts.index("Fluorescence") + 1
            if index < len(parts):
                return parts[index].split()[0]
        for color in ["R", "G", "B"]:
            if color in parts or f"full_{color}" in parts:
                return color
        return None

    def generateColormap(self, channel_info):
        # generate colormap from hex value
        c0 = (0, 0, 0)
        c1 = (
            ((channel_info["hex"] >> 16) & 0xFF) / 255,
            ((channel_info["hex"] >> 8) & 0xFF) / 255,
            (channel_info["hex"] & 0xFF) / 255,
        )
        return Colormap(colors=[c0, c1], controls=[0, 1], name=channel_info["name"])

    def updateMosaic(self, image, x_mm, y_mm, k, channel_name):
        # NOTE: Check runtime flag to allow MCP to disable mosaic updates for RAM debugging.
        # This enables toggling mosaic view without restarting the application.
        if not control._def.USE_NAPARI_FOR_MOSAIC_DISPLAY:
            return

        # calculate pixel size
        pixel_size_um = self.objectiveStore.get_pixel_size_factor() * self.camera.get_pixel_size_binned_um()
        downsample_factor = max(1, int(MOSAIC_VIEW_TARGET_PIXEL_SIZE_UM / pixel_size_um))
        image_pixel_size_um = pixel_size_um * downsample_factor
        image_pixel_size_mm = image_pixel_size_um / 1000
        image_dtype = image.dtype

        # downsample image
        if downsample_factor != 1:
            image = cv2.resize(
                image,
                (image.shape[1] // downsample_factor, image.shape[0] // downsample_factor),
                interpolation=cv2.INTER_AREA,
            )

        # adjust image position
        x_mm -= (image.shape[1] * image_pixel_size_mm) / 2
        y_mm -= (image.shape[0] * image_pixel_size_mm) / 2

        if not self.layers_initialized:
            # initialize mosaic state for first image (or after clearAllLayers)
            self.layers_initialized = True
            self.signal_layers_initialized.emit()
            self.viewer_pixel_size_mm = image_pixel_size_mm
            self.viewer_extents = [
                y_mm,
                y_mm + image.shape[0] * image_pixel_size_mm,
                x_mm,
                x_mm + image.shape[1] * image_pixel_size_mm,
            ]
            self.top_left_coordinate = [y_mm, x_mm]
            self.mosaic_dtype = image_dtype

            # Update Manual ROI shapes to new coordinate system if they exist
            if self.shape_layer is not None and len(self.shapes_mm) > 0:
                new_shapes = self.convert_mm_to_viewer_shapes(self.shapes_mm)
                self.shape_layer.data = new_shapes
        else:
            # convert image dtype and scale if necessary
            image = self.convertImageDtype(image, self.mosaic_dtype)
            if image_pixel_size_mm != self.viewer_pixel_size_mm:
                scale_factor = image_pixel_size_mm / self.viewer_pixel_size_mm
                image = cv2.resize(
                    image,
                    (int(image.shape[1] * scale_factor), int(image.shape[0] * scale_factor)),
                    interpolation=cv2.INTER_LINEAR,
                )

        if channel_name not in self.viewer.layers:
            # create new layer for channel
            channel_info = CHANNEL_COLORS_MAP.get(
                self.extractWavelength(channel_name), {"hex": 0xFFFFFF, "name": "gray"}
            )
            if channel_info["name"] in AVAILABLE_COLORMAPS:
                color = AVAILABLE_COLORMAPS[channel_info["name"]]
            else:
                color = self.generateColormap(channel_info)

            layer = self.viewer.add_image(
                np.zeros_like(image),
                name=channel_name,
                rgb=len(image.shape) == 3,
                colormap=color,
                visible=True,
                blending="additive",
                scale=(self.viewer_pixel_size_mm * 1000, self.viewer_pixel_size_mm * 1000),
            )
            layer.mouse_double_click_callbacks.append(self.onDoubleClick)
            layer.events.contrast_limits.connect(self.signalContrastLimits)

        # get layer for channel
        layer = self.viewer.layers[channel_name]

        # update extents
        self.viewer_extents[0] = min(self.viewer_extents[0], y_mm)
        self.viewer_extents[1] = max(self.viewer_extents[1], y_mm + image.shape[0] * self.viewer_pixel_size_mm)
        self.viewer_extents[2] = min(self.viewer_extents[2], x_mm)
        self.viewer_extents[3] = max(self.viewer_extents[3], x_mm + image.shape[1] * self.viewer_pixel_size_mm)

        # store previous top-left coordinate
        prev_top_left = self.top_left_coordinate.copy() if self.top_left_coordinate else None
        self.top_left_coordinate = [self.viewer_extents[0], self.viewer_extents[2]]

        # update layer
        self.updateLayer(layer, image, x_mm, y_mm, k, prev_top_left)

        # update contrast limits
        min_val, max_val = self.contrastManager.get_limits(channel_name)
        scaled_min = self.convertValue(min_val, self.contrastManager.acquisition_dtype, self.mosaic_dtype)
        scaled_max = self.convertValue(max_val, self.contrastManager.acquisition_dtype, self.mosaic_dtype)
        layer.contrast_limits = (scaled_min, scaled_max)
        layer.refresh()

    def updateLayer(self, layer, image, x_mm, y_mm, k, prev_top_left):
        # calculate new mosaic size and position
        mosaic_height = int(math.ceil((self.viewer_extents[1] - self.viewer_extents[0]) / self.viewer_pixel_size_mm))
        mosaic_width = int(math.ceil((self.viewer_extents[3] - self.viewer_extents[2]) / self.viewer_pixel_size_mm))

        is_rgb = len(image.shape) == 3 and image.shape[2] == 3
        if layer.data.shape[:2] != (mosaic_height, mosaic_width):
            # calculate offsets for existing data
            y_offset = int(math.floor((prev_top_left[0] - self.top_left_coordinate[0]) / self.viewer_pixel_size_mm))
            x_offset = int(math.floor((prev_top_left[1] - self.top_left_coordinate[1]) / self.viewer_pixel_size_mm))

            for mosaic in self.viewer.layers:
                if mosaic.name != "Manual ROI":
                    if len(mosaic.data.shape) == 3 and mosaic.data.shape[2] == 3:
                        new_data = np.zeros((mosaic_height, mosaic_width, 3), dtype=mosaic.data.dtype)
                    else:
                        new_data = np.zeros((mosaic_height, mosaic_width), dtype=mosaic.data.dtype)

                    # ensure offsets don't exceed bounds
                    y_end = min(y_offset + mosaic.data.shape[0], new_data.shape[0])
                    x_end = min(x_offset + mosaic.data.shape[1], new_data.shape[1])

                    # shift existing data
                    if len(mosaic.data.shape) == 3 and mosaic.data.shape[2] == 3:
                        new_data[y_offset:y_end, x_offset:x_end, :] = mosaic.data[
                            : y_end - y_offset, : x_end - x_offset, :
                        ]
                    else:
                        new_data[y_offset:y_end, x_offset:x_end] = mosaic.data[: y_end - y_offset, : x_end - x_offset]
                    mosaic.data = new_data

            if "Manual ROI" in self.viewer.layers:
                self.update_shape_layer_position(prev_top_left, self.top_left_coordinate)

            self.resetView()

        # insert new image
        y_pos = int(math.floor((y_mm - self.top_left_coordinate[0]) / self.viewer_pixel_size_mm))
        x_pos = int(math.floor((x_mm - self.top_left_coordinate[1]) / self.viewer_pixel_size_mm))

        # ensure indices are within bounds
        y_end = min(y_pos + image.shape[0], layer.data.shape[0])
        x_end = min(x_pos + image.shape[1], layer.data.shape[1])

        # insert image data
        if is_rgb:
            layer.data[y_pos:y_end, x_pos:x_end, :] = image[: y_end - y_pos, : x_end - x_pos, :]
        else:
            layer.data[y_pos:y_end, x_pos:x_end] = image[: y_end - y_pos, : x_end - x_pos]
        layer.refresh()

    def convertImageDtype(self, image, target_dtype):
        # convert image to target dtype
        if image.dtype == target_dtype:
            return image

        # get full range of values for both dtypes
        if np.issubdtype(image.dtype, np.integer):
            input_info = np.iinfo(image.dtype)
            input_min, input_max = input_info.min, input_info.max
        else:
            input_min, input_max = np.min(image), np.max(image)

        if np.issubdtype(target_dtype, np.integer):
            output_info = np.iinfo(target_dtype)
            output_min, output_max = output_info.min, output_info.max
        else:
            output_min, output_max = 0.0, 1.0

        # normalize and scale image
        image_normalized = (image.astype(np.float64) - input_min) / (input_max - input_min)
        image_scaled = image_normalized * (output_max - output_min) + output_min

        return image_scaled.astype(target_dtype)

    def convertValue(self, value, from_dtype, to_dtype):
        # Convert value from one dtype range to another
        from_info = np.iinfo(from_dtype)
        to_info = np.iinfo(to_dtype)

        # Normalize the value to [0, 1] range
        normalized = (value - from_info.min) / (from_info.max - from_info.min)

        # Scale to the target dtype range
        return normalized * (to_info.max - to_info.min) + to_info.min

    def signalContrastLimits(self, event):
        layer = event.source
        min_val, max_val = map(float, layer.contrast_limits)

        # Convert the new limits from mosaic_dtype to acquisition_dtype
        acquisition_min = self.convertValue(min_val, self.mosaic_dtype, self.contrastManager.acquisition_dtype)
        acquisition_max = self.convertValue(max_val, self.mosaic_dtype, self.contrastManager.acquisition_dtype)

        # Update the ContrastManager with the new limits
        self.contrastManager.update_limits(layer.name, acquisition_min, acquisition_max)

    def getContrastLimits(self, dtype):
        return self.contrastManager.get_default_limits()

    def onDoubleClick(self, layer, event):
        coords = layer.world_to_data(event.position)
        if coords is not None:
            x_mm = self.top_left_coordinate[1] + coords[-1] * self.viewer_pixel_size_mm
            y_mm = self.top_left_coordinate[0] + coords[-2] * self.viewer_pixel_size_mm
            print(f"move from click: ({x_mm:.6f}, {y_mm:.6f})")
            self.signal_coordinates_clicked.emit(x_mm, y_mm)

    def resetView(self):
        self.viewer.reset_view()
        for layer in self.viewer.layers:
            layer.refresh()

    def clear_shape(self):
        if self.shape_layer is not None:
            self.viewer.layers.remove(self.shape_layer)
            self.shape_layer = None
            self.is_drawing_shape = False
            self.signal_shape_drawn.emit([])

    def clearAllLayers(self):
        # Remove all layers except Manual ROI to free memory and allow proper reinitialization
        layers_to_remove = [layer for layer in self.viewer.layers if layer.name != "Manual ROI"]
        for layer in layers_to_remove:
            self.viewer.layers.remove(layer)

        # Reset mosaic-related state so reinitialization logic can run cleanly
        self.channels = set()
        self.viewer_extents = None
        self.layers_initialized = False
        self.top_left_coordinate = None
        self.mosaic_dtype = None

        # Force garbage collection to return memory to OS
        gc.collect()

        self.signal_clear_viewer.emit()

    def activate(self):
        self.viewer.window.activate()

    def get_screenshot(self) -> Optional[np.ndarray]:
        """Capture the current mosaic view as a numpy array.

        Returns:
            RGB image array of the current view, or None if no layers exist.
        """
        if not self.layers_initialized:
            return None
        try:
            # Use napari's screenshot functionality
            return self.viewer.screenshot(canvas_only=True)
        except Exception:
            return None


class NapariPlateViewWidget(QWidget):
    """Widget for displaying downsampled plate view with multi-channel support.

    Similar to NapariMosaicDisplayWidget but specifically for plate-based acquisitions.
    Displays downsampled well images in a grid layout.
    """

    signal_well_fov_clicked = Signal(str, int)  # well_id, fov_index

    def __init__(self, contrastManager, parent=None):
        super().__init__(parent)
        self.contrastManager = contrastManager
        self.viewer = napari.Viewer(show=False)
        # Disable napari's native menu bar so it doesn't take over macOS global menu bar
        if sys.platform == "darwin":
            self.viewer.window.main_menu.setNativeMenuBar(False)
        self.viewer.window.main_menu.hide()
        self.layout = QVBoxLayout()
        self.layout.addWidget(self.viewer.window._qt_window)

        # Clear button
        self.clear_button = QPushButton("Clear Plate View")
        self.clear_button.clicked.connect(self.clearAllLayers)
        self.layout.addWidget(self.clear_button)

        self.setLayout(self.layout)

        # Plate layout info (set by initPlateLayout)
        self.num_rows = 0
        self.num_cols = 0
        self.well_slot_shape = (0, 0)  # (height, width) pixels per well
        self.fov_grid_shape = (1, 1)  # (ny, nx) FOVs per well
        self.channel_names = []
        self.plate_dtype = None
        self.layers_initialized = False

        # Zoom limits (updated in initPlateLayout based on plate size)
        self.min_zoom = 0.1  # Prevent zooming out too far
        self.max_zoom = None  # No max limit until plate size is known
        # Flag to prevent recursive zoom clamping. This is safe because Qt's event
        # loop processes events sequentially on the main thread - _custom_wheel_event
        # and _on_zoom_changed cannot run concurrently, so no lock is needed.
        self._clamping_zoom = False

        # Override wheel event on vispy canvas to enforce zoom limits
        canvas_widget = self.viewer.window._qt_viewer.canvas.native
        canvas_widget.wheelEvent = self._custom_wheel_event

        # Clamp zoom for programmatic changes (e.g., reset_view)
        self.viewer.camera.events.zoom.connect(self._on_zoom_changed)

    def initPlateLayout(self, num_rows, num_cols, well_slot_shape, fov_grid_shape=None, channel_names=None):
        """Initialize plate layout for click coordinate calculations.

        Args:
            num_rows: Number of rows in the plate
            num_cols: Number of columns in the plate
            well_slot_shape: (height, width) of each well slot in pixels
            fov_grid_shape: (ny, nx) FOVs per well for click mapping
            channel_names: List of channel names
        """
        self.num_rows = num_rows
        self.num_cols = num_cols
        self.well_slot_shape = well_slot_shape
        self.fov_grid_shape = fov_grid_shape or (1, 1)
        self.channel_names = channel_names or []
        self.layers_initialized = False

        # Calculate zoom limits based on plate size
        plate_height = num_rows * well_slot_shape[0]
        plate_width = num_cols * well_slot_shape[1]
        if plate_height > 0 and plate_width > 0:
            # Max zoom: ensure at least MIN_VISIBLE_PIXELS visible, capped at MAX_ZOOM_FACTOR
            min_plate_dim = min(plate_height, plate_width)
            self.max_zoom = min(
                max(1.0, min_plate_dim / PLATE_VIEW_MIN_VISIBLE_PIXELS),
                PLATE_VIEW_MAX_ZOOM_FACTOR,
            )

        # Draw plate boundaries
        self._draw_plate_boundaries()

        # Reset view to fit plate, then capture that zoom as the min (zoom out limit)
        self.viewer.reset_view()
        self.min_zoom = self.viewer.camera.zoom

    def _custom_wheel_event(self, event):
        """Custom wheel event handler that enforces zoom limits."""
        # Block ALL wheel events from reaching vispy - we handle zoom ourselves
        event.accept()

        delta = event.angleDelta().y()
        if delta == 0:
            return

        # Calculate new zoom with our own factor
        zoom = self.viewer.camera.zoom
        zoom_factor = 1.1 ** (delta / 120.0)  # Standard wheel: 120 units per notch
        new_zoom = zoom * zoom_factor

        # Clamp to limits
        new_zoom = max(self.min_zoom, new_zoom)
        if self.max_zoom is not None:
            new_zoom = min(self.max_zoom, new_zoom)

        # Apply clamped zoom
        if new_zoom != zoom:
            self._clamping_zoom = True
            self.viewer.camera.zoom = new_zoom
            self._clamping_zoom = False

    def _on_zoom_changed(self, event):
        """Clamp zoom to limits after any zoom change (e.g., reset_view)."""
        if self._clamping_zoom:
            return
        zoom = self.viewer.camera.zoom
        target_zoom = zoom
        if zoom < self.min_zoom:
            target_zoom = self.min_zoom
        elif self.max_zoom is not None and zoom > self.max_zoom:
            target_zoom = self.max_zoom
        if target_zoom != zoom:
            self._clamping_zoom = True
            self.viewer.camera.zoom = target_zoom
            self._clamping_zoom = False

    def _draw_plate_boundaries(self):
        """Draw grid lines to show well boundaries.

        Uses O(rows + cols) lines instead of O(rows * cols) rectangles for better
        performance with large plates (e.g., 1536-well).
        """
        if self.num_rows == 0 or self.num_cols == 0:
            return
        if self.well_slot_shape[0] == 0 or self.well_slot_shape[1] == 0:
            return

        # Remove existing boundary layer
        if "_plate_boundaries" in self.viewer.layers:
            self.viewer.layers.remove("_plate_boundaries")

        lines = []
        slot_h, slot_w = self.well_slot_shape
        plate_height = self.num_rows * slot_h
        plate_width = self.num_cols * slot_w

        # Horizontal lines (num_rows + 1 lines)
        for row in range(self.num_rows + 1):
            y = row * slot_h
            lines.append([[y, 0], [y, plate_width]])

        # Vertical lines (num_cols + 1 lines)
        for col in range(self.num_cols + 1):
            x = col * slot_w
            lines.append([[0, x], [plate_height, x]])

        if lines:
            self.viewer.add_shapes(
                lines,
                shape_type="line",
                edge_color="white",
                edge_width=2,
                name="_plate_boundaries",
            )
            # Make boundaries layer non-interactive so it doesn't intercept clicks
            boundaries_layer = self.viewer.layers["_plate_boundaries"]
            boundaries_layer.mouse_pan = False
            boundaries_layer.mouse_zoom = False
            # Move boundaries layer to bottom
            self.viewer.layers.move(len(self.viewer.layers) - 1, 0)
            # Ensure an image layer is selected, not the shapes layer
            for layer in reversed(self.viewer.layers):
                if layer.name != "_plate_boundaries":
                    self.viewer.layers.selection.active = layer
                    break

    def extractWavelength(self, name):
        """Extract wavelength from channel name for colormap selection."""
        parts = name.split()
        if "Fluorescence" in parts:
            index = parts.index("Fluorescence") + 1
            if index < len(parts):
                return parts[index].split()[0]
        for color in ["R", "G", "B"]:
            if color in parts or f"full_{color}" in parts:
                return color
        return None

    def generateColormap(self, channel_info):
        """Generate colormap from hex value."""
        c0 = (0, 0, 0)
        c1 = (
            ((channel_info["hex"] >> 16) & 0xFF) / 255,
            ((channel_info["hex"] >> 8) & 0xFF) / 255,
            (channel_info["hex"] & 0xFF) / 255,
        )
        return Colormap(colors=[c0, c1], controls=[0, 1], name=channel_info["name"])

    def updatePlateView(self, channel_idx, channel_name, plate_image):
        """Update a single channel's plate view.

        Args:
            channel_idx: Channel index (0-based)
            channel_name: Name of the channel
            plate_image: 2D numpy array with the channel's plate view
        """
        if plate_image is None:
            return

        if not self.layers_initialized:
            self.layers_initialized = True
            self.plate_dtype = plate_image.dtype

        if channel_name not in self.viewer.layers:
            # Create layer with appropriate colormap
            wavelength = self.extractWavelength(channel_name)
            channel_info = (
                CHANNEL_COLORS_MAP.get(wavelength, {"hex": 0xFFFFFF, "name": "gray"})
                if wavelength is not None
                else {"hex": 0xFFFFFF, "name": "gray"}
            )
            if channel_info["name"] in AVAILABLE_COLORMAPS:
                color = AVAILABLE_COLORMAPS[channel_info["name"]]
            else:
                color = self.generateColormap(channel_info)

            layer = self.viewer.add_image(
                plate_image,
                name=channel_name,
                colormap=color,
                visible=True,
                blending="additive",
            )
            layer.mouse_double_click_callbacks.append(self.onDoubleClick)
            layer.events.contrast_limits.connect(self.signalContrastLimits)
        else:
            self.viewer.layers[channel_name].data = plate_image

        # Apply contrast from contrastManager
        layer = self.viewer.layers[channel_name]
        min_val, max_val = self.contrastManager.get_limits(channel_name)
        layer.contrast_limits = (min_val, max_val)
        layer.refresh()

    def signalContrastLimits(self, event):
        """Handle contrast limit changes and propagate to contrastManager."""
        layer = event.source
        min_val, max_val = layer.contrast_limits
        self.contrastManager.update_limits(layer.name, min_val, max_val)

    def onDoubleClick(self, layer, event):
        """Handle double-click: calculate well_id and fov_index."""
        coords = layer.world_to_data(event.position)
        if coords is None or self.well_slot_shape[0] == 0 or self.well_slot_shape[1] == 0:
            return

        y, x = int(coords[-2]), int(coords[-1])

        # Calculate well position
        well_row = y // self.well_slot_shape[0]
        well_col = x // self.well_slot_shape[1]

        # Validate well position
        if well_row < 0 or well_row >= self.num_rows or well_col < 0 or well_col >= self.num_cols:
            print(f"Clicked outside plate bounds: row={well_row}, col={well_col}")
            return

        # Generate well ID using shared utility (inverse of parse_well_id)
        well_id = format_well_id(well_row, well_col)

        # Calculate FOV within well
        y_in_well = y % self.well_slot_shape[0]
        x_in_well = x % self.well_slot_shape[1]

        fov_ny, fov_nx = self.fov_grid_shape
        if fov_ny > 0 and fov_nx > 0:
            fov_height = self.well_slot_shape[0] // fov_ny
            fov_width = self.well_slot_shape[1] // fov_nx
            if fov_height > 0 and fov_width > 0:
                # Clamp to valid range to handle clicks at edge of well slot
                fov_row = min(y_in_well // fov_height, fov_ny - 1)
                fov_col = min(x_in_well // fov_width, fov_nx - 1)
                fov_index = fov_row * fov_nx + fov_col
            else:
                fov_index = 0
        else:
            fov_index = 0

        print(f"Clicked: Well {well_id}, FOV {fov_index}")
        self.signal_well_fov_clicked.emit(well_id, fov_index)

    def resetView(self):
        """Reset the viewer to fit all data."""
        self.viewer.reset_view()
        for layer in self.viewer.layers:
            layer.refresh()

    def clearAllLayers(self):
        """Clear all layers to free memory."""
        layers_to_remove = list(self.viewer.layers)
        for layer in layers_to_remove:
            self.viewer.layers.remove(layer)

        self.layers_initialized = False
        self.plate_dtype = None
        gc.collect()

    def activate(self):
        """Activate the viewer window."""
        self.viewer.window.activate()


