from ._bootstrap import *



def error_dialog(message: str, title: str = "Error"):
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Warning)
    msg.setText(message)
    msg.setWindowTitle(title)
    msg.setStandardButtons(QMessageBox.Ok)
    msg.setDefaultButton(QMessageBox.Ok)
    retval = msg.exec_()
    return


def check_observation_state_roi_consistency_with_dialog(
    multi_point_controller: MultiPointController, logger: logging.Logger
) -> bool:
    """Warn and require approval when the run's observation states have mismatched ROIs.

    Tiles are spaced for the largest ROI in the group (so the most complete channel keeps
    its overlap); states with smaller ROIs then under-sample, which may or may not be the
    intent. When proceeding, the regions are re-tiled for that FOV up front (via
    apply_observation_state_tiling) so the disk/RAM estimates and the preview reflect the
    tile count that will actually be acquired. Returns True to proceed (no mismatch, or the
    user approved), False to cancel.
    """
    try:
        report = multi_point_controller.build_roi_consistency_report()
    except Exception:
        logger.exception("ROI consistency check failed; proceeding without it.")
        return True

    if not report.get("mismatch"):
        multi_point_controller.apply_observation_state_tiling()
        return True

    tiling = report.get("tiling_fov_mm")
    largest = report.get("largest_name")
    mismatched = report.get("mismatch_names", [])

    def _fmt(entry):
        fov = entry.get("fov_mm")
        if fov is None:
            return f"  • {entry['name']}: FOV unknown"
        return f"  • {entry['name']}: {fov[0]:.3f} × {fov[1]:.3f} mm"

    lines = "\n".join(_fmt(e) for e in report.get("entries", []))
    tiling_str = f"{tiling[0]:.3f} × {tiling[1]:.3f} mm" if tiling else "unknown"
    message = (
        "The selected observation states do not all use the same camera ROI.\n\n"
        f"Tiling overlap will be computed for the largest ROI ('{largest}', {tiling_str}).\n"
        f"These states have a smaller ROI and will under-sample (intentional subsampling, "
        f"or possibly a mistake):\n  {', '.join(mismatched)}\n\n"
        f"Per-state FOV:\n{lines}\n\n"
        "Proceed with the acquisition?"
    )
    logger.warning("Observation-state ROI mismatch; requesting user approval. %s", mismatched)

    msg = QMessageBox()
    msg.setIcon(QMessageBox.Warning)
    msg.setWindowTitle("Mismatched Observation-State ROIs")
    msg.setText(message)
    msg.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
    msg.setDefaultButton(QMessageBox.Cancel)
    if msg.exec_() != QMessageBox.Yes:
        return False
    multi_point_controller.apply_observation_state_tiling()
    return True


def check_space_available_with_error_dialog(
    multi_point_controller: MultiPointController, logger: logging.Logger, factor_of_safecty: float = 1.03
) -> bool:
    # To check how much disk space is required, we need to have the MultiPointController all configured.  That is
    # a precondition of this function.
    save_directory = multi_point_controller.base_path
    available_disk_space = utils.get_available_disk_space(save_directory)
    space_required = factor_of_safecty * multi_point_controller.get_estimated_acquisition_disk_storage()
    image_count = multi_point_controller.get_acquisition_image_count()

    logger.info(
        f"Checking space available: {space_required=}, {available_disk_space=}, {image_count=}, {save_directory=}"
    )
    if space_required > available_disk_space:
        megabytes_required = int(space_required / 1024 / 1024)
        megabytes_available = int(available_disk_space / 1024 / 1024)
        # ZARR_V3 acquisitions can stream to a network share so the local disk
        # only ever holds about one timepoint at a time. Offer that route
        # before failing the acquisition.
        zarr_v3 = getattr(multi_point_controller, "file_saving_option", None)
        zarr_v3_selected = (
            zarr_v3 is not None
            and getattr(zarr_v3, "name", "") == "ZARR_V3"
        )
        if zarr_v3_selected and prompt_enable_network_streaming(
            multi_point_controller,
            megabytes_required=megabytes_required,
            megabytes_available=megabytes_available,
            logger=logger,
        ):
            return True
        error_message = (
            f"This acquisition will capture {image_count:,} images, which will"
            f" require {megabytes_required:,} [MB], but '{save_directory}' only has {megabytes_available:,} [MB] available."
        )
        logger.error(error_message)
        error_dialog(error_message, title="Not Enough Disk Space")
        return False
    return True


def prompt_enable_network_streaming(
    multi_point_controller: MultiPointController,
    megabytes_required: int,
    megabytes_available: int,
    logger: logging.Logger,
) -> bool:
    """Offer to stream the ZARR_V3 acquisition to a network share.

    Shown when the local disk-space check fails and ``file_saving_option`` is
    ``ZARR_V3``. The user picks a writable path on a mounted SMB share
    (Windows ``\\\\server\\share\\dir`` UNC, mac/Linux ``/Volumes/.../`` or
    ``//server/share/dir`` after mount); on accept we configure the
    controller's upload target and return True so the caller bypasses the
    local-disk failure.

    Returns True iff the user enabled streaming with a valid path.
    """
    dlg = QDialog()
    dlg.setWindowTitle("Stream to Network Drive")
    layout = QVBoxLayout(dlg)

    layout.addWidget(QLabel(
        f"This acquisition needs ~{megabytes_required:,} MB but only "
        f"{megabytes_available:,} MB is available locally."
    ))
    layout.addWidget(QLabel(
        "Stream OME-Zarr output to a mounted network drive. Each timepoint "
        "is verified on the remote and then deleted locally, so peak local "
        "usage stays small."
    ))

    path_row = QHBoxLayout()
    path_row.addWidget(QLabel("Network path:"))
    path_edit = QLineEdit()
    path_edit.setPlaceholderText(r"\\server\share\my_acquisitions  (or /Volumes/share/...)")
    last_path = _load_last_remote_streaming_path()
    if last_path:
        path_edit.setText(last_path)
    path_row.addWidget(path_edit, 1)
    browse_btn = QPushButton("Browse...")
    def _browse():
        chosen = QFileDialog.getExistingDirectory(dlg, "Select network folder")
        if chosen:
            path_edit.setText(chosen)
    browse_btn.clicked.connect(_browse)
    path_row.addWidget(browse_btn)
    layout.addLayout(path_row)

    delete_local_cb = QCheckBox("Delete each timepoint locally after verified upload")
    delete_local_cb.setChecked(True)
    layout.addWidget(delete_local_cb)

    btns = QHBoxLayout()
    btns.addStretch(1)
    cancel_btn = QPushButton("Cancel")
    cancel_btn.clicked.connect(dlg.reject)
    btns.addWidget(cancel_btn)
    accept_btn = QPushButton("Enable streaming and start")
    accept_btn.setDefault(True)
    btns.addWidget(accept_btn)
    layout.addLayout(btns)

    def _on_accept():
        remote = path_edit.text().strip()
        if not remote:
            QMessageBox.warning(dlg, "Network path required", "Please enter a network path.")
            return
        if not os.path.isdir(remote):
            QMessageBox.warning(
                dlg,
                "Path not reachable",
                f"'{remote}' is not a directory the OS can see. "
                f"Mount the share first, then try again.",
            )
            return
        # Quick write probe so we fail fast on read-only mounts.
        probe = os.path.join(remote, ".squid_upload_probe")
        try:
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except OSError as e:
            QMessageBox.warning(
                dlg,
                "Path not writable",
                f"Cannot write to '{remote}': {e}",
            )
            return
        dlg.accept()

    accept_btn.clicked.connect(_on_accept)

    if dlg.exec_() != QDialog.Accepted:
        logger.info("User declined network streaming; aborting acquisition.")
        return False

    remote_root = path_edit.text().strip()
    delete_after_verify = delete_local_cb.isChecked()
    multi_point_controller.set_zarr_upload_target(
        enabled=True,
        remote_root=remote_root,
        delete_after_verify=delete_after_verify,
    )
    _save_last_remote_streaming_path(remote_root)
    logger.info(
        f"Network streaming enabled: remote_root={remote_root!r}, "
        f"delete_after_verify={delete_after_verify}"
    )
    return True


def _load_last_remote_streaming_path() -> str:
    cache_file = "cache/last_streaming_path.txt"
    try:
        with open(cache_file, "r") as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_last_remote_streaming_path(path: str) -> None:
    if not path:
        return
    try:
        os.makedirs("cache", exist_ok=True)
        with open("cache/last_streaming_path.txt", "w") as f:
            f.write(path)
    except OSError:
        pass


def check_ram_available_with_error_dialog(
    multi_point_controller: MultiPointController,
    logger: logging.Logger,
    factor_of_safety: float = 1.15,
    performance_mode: bool = False,
) -> bool:
    """Check if enough RAM is available for mosaic view."""
    import psutil

    # Skip check if performance mode is enabled (mosaic view is disabled)
    if performance_mode:
        logger.info("Performance mode enabled, skipping RAM check for mosaic view")
        return True

    ram_required = factor_of_safety * multi_point_controller.get_estimated_mosaic_ram_bytes()
    available_ram = psutil.virtual_memory().available

    logger.info(f"Checking RAM available: {ram_required=}, {available_ram=}")

    if ram_required > available_ram:
        mb_required = int(ram_required / 1024 / 1024)
        mb_available = int(available_ram / 1024 / 1024)
        error_message = (
            f"This acquisition's mosaic view will require approximately {mb_required:,} MB RAM, "
            f"but only {mb_available:,} MB is currently available.\n\n"
            f"Consider enabling Performance Mode to disable mosaic view during acquisition."
        )
        logger.error(error_message)
        error_dialog(error_message, title="Not Enough RAM")
        return False
    return True


def check_system_load_and_pending_uploads_with_dialog(
    multi_point_controller: MultiPointController,
    logger: logging.Logger,
    cpu_pct_threshold: float = 85.0,
    ram_pct_threshold: float = 85.0,
    disk_headroom_factor: float = 1.5,
) -> bool:
    """Warn before starting when the system is loaded, disk headroom is tight,
    or a previous run's background upload is still draining.

    None of these is a hard block — concurrent upload drainers are isolated by
    design, and a busy system can still acquire — but each can degrade the run:
    a prior upload shares the network share and local-disk I/O, and its
    verified-pending shards still occupy disk, so there is less free space than
    "between runs". Returns True to proceed; False if the user cancels. Default
    button is Cancel so a stray Enter does not start the run.
    """
    import psutil

    reasons = []

    # Previous acquisitions still uploading in the background.
    try:
        from control.core.multi_point_worker import active_upload_drainer_summary

        summary = active_upload_drainer_summary()
    except Exception:
        logger.exception("Could not read active upload drainers; skipping that check.")
        summary = []
    if summary:
        total = sum(int(d.get("outstanding", 0)) for d in summary)
        reasons.append(
            f"• {len(summary)} previous acquisition(s) still uploading in the "
            f"background ({total} task(s) pending) — they share the network "
            f"share and local disk with this run."
        )

    # CPU load (interval>0 so the first sample is meaningful).
    try:
        cpu = psutil.cpu_percent(interval=0.3)
        if cpu >= cpu_pct_threshold:
            reasons.append(f"• CPU load is high ({cpu:.0f}%).")
    except Exception:
        logger.exception("CPU load check failed; skipping.")

    # Memory.
    try:
        vm = psutil.virtual_memory()
        if vm.percent >= ram_pct_threshold:
            reasons.append(
                f"• Memory is {vm.percent:.0f}% used "
                f"({vm.available / 1024 / 1024 / 1024:.1f} GB free)."
            )
    except Exception:
        logger.exception("RAM load check failed; skipping.")

    # Soft disk-headroom warning (the hard fail is handled by
    # check_space_available_with_error_dialog upstream).
    try:
        available = utils.get_available_disk_space(multi_point_controller.base_path)
        required = multi_point_controller.get_estimated_acquisition_disk_storage()
        if required and available < disk_headroom_factor * required:
            reasons.append(
                f"• Local free disk is tight: {int(available / 1024 / 1024):,} MB "
                f"free vs {int(required / 1024 / 1024):,} MB needed for this run."
            )
    except Exception:
        logger.exception("Disk headroom check failed; skipping.")

    if not reasons:
        return True

    message = (
        "The system may be under load or low on resources, which can slow "
        "acquisition and background data writing:\n\n"
        + "\n".join(reasons)
        + "\n\nStart the acquisition anyway?"
    )
    logger.warning("Pre-start load/upload warning: %s", " ".join(reasons))

    msg = QMessageBox()
    msg.setIcon(QMessageBox.Warning)
    msg.setWindowTitle("System Under Load")
    msg.setText(message)
    msg.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
    msg.setDefaultButton(QMessageBox.Cancel)
    return msg.exec_() == QMessageBox.Yes


class WrapperWindow(QMainWindow):
    def __init__(self, content_widget, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setCentralWidget(content_widget)
        self.hide()

    def closeEvent(self, event):
        self.hide()
        event.ignore()

    def closeForReal(self, event):
        super().closeEvent(event)


class NDViewerTab(QWidget):
    """
    Embedded NDViewer (ndviewer_light) for showing the latest acquisition.

    This is designed to live inside an existing QTabWidget (no separate QApplication / process).
    """

    _PLACEHOLDER_WAITING = "NDViewer: waiting for an acquisition to start..."

    def __init__(self, parent=None):
        super().__init__(parent)
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self._viewer = None
        self._dataset_path: Optional[str] = None

        self._layout = QVBoxLayout()
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self._layout)

        self._placeholder = QLabel(self._PLACEHOLDER_WAITING)
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._layout.addWidget(self._placeholder, 1)

    def _show_placeholder(self, message: str) -> None:
        """Show placeholder with message and hide viewer."""
        self._placeholder.setText(message)
        self._placeholder.setVisible(True)
        if self._viewer is not None:
            self._viewer.setVisible(False)

    def set_dataset_path(self, dataset_path: Optional[str]) -> None:
        """
        Point the embedded NDViewer at a dataset folder and refresh.

        Pass None to clear the view.
        """
        self._log.debug(f"set_dataset_path called with: {dataset_path}")

        if dataset_path == self._dataset_path:
            self._log.debug("Dataset path unchanged, skipping")
            return
        self._dataset_path = dataset_path

        if not dataset_path:
            self._show_placeholder(self._PLACEHOLDER_WAITING)
            return

        if not os.path.isdir(dataset_path):
            self._log.warning(f"Dataset folder not found: {dataset_path}")
            self._show_placeholder(f"NDViewer: dataset folder not found:\n{dataset_path}")
            return

        try:
            # Lazy import so the main UI doesn't pay NDV import costs until needed
            from control import ndviewer_light
        except ImportError as e:
            self._log.error(f"Failed to import ndviewer_light: {e}")
            self._show_placeholder(f"NDViewer: failed to import ndviewer_light:\n{e}")
            return

        # ndviewer_light handles gracefully degraded rendering if NDV is partially unavailable.
        # Complete failures to load or create the viewer fall through to the exception handler below.
        try:
            if self._viewer is None:
                self._log.debug(f"Creating new LightweightViewer for: {dataset_path}")
                self._viewer = ndviewer_light.LightweightViewer(dataset_path)
                self._layout.addWidget(self._viewer, 1)
                self._log.debug(f"LightweightViewer created, ndv_viewer={self._viewer.ndv_viewer is not None}")
            else:
                self._log.debug(f"Reloading dataset: {dataset_path}")
                self._viewer.load_dataset(dataset_path)
                self._viewer.refresh()

            self._viewer.setVisible(True)
            self._placeholder.setVisible(False)
        except Exception as e:
            self._log.exception("NDViewerTab failed to load dataset")
            error_msg = str(e) if str(e) else type(e).__name__
            self._show_placeholder(f"NDViewer: failed to load dataset:\n{dataset_path}\n\nError: {error_msg}")

    def go_to_fov(self, well_id: str, fov_index: int) -> bool:
        """
        Navigate the NDViewer to a specific well and FOV.

        Called when user double-clicks a location in the plate view.
        Maps (well_id, fov_index) to the flat xarray FOV dimension index.

        Returns:
            True if navigation succeeded, False otherwise.
        """
        if self._viewer is None:
            self._log.debug("go_to_fov: no viewer loaded")
            return False

        try:
            # Try push-based mode first (active during/after acquisition)
            if self._viewer.is_push_mode_active():
                if self._viewer.go_to_well_fov(well_id, fov_index):
                    self._log.info(f"go_to_fov: navigated to well={well_id}, fov={fov_index} (push mode)")
                    return True
                self._log.warning(
                    f"go_to_fov: push mode navigation failed for well={well_id}, fov={fov_index}. "
                    f"FOV may not be registered yet or well ID format may not match."
                )
                return False

            # Fall back to legacy mode (viewing existing datasets)
            if not self._viewer.has_fov_dimension():
                self._log.debug("go_to_fov: no fov dimension available")
                return False

            target_flat_idx = self._find_flat_fov_index(well_id, fov_index)
            if target_flat_idx is None:
                self._log.debug(f"go_to_fov: could not find FOV for well={well_id}, fov={fov_index}")
                return False

            if self._viewer.set_current_index("fov", target_flat_idx):
                self._log.info(f"go_to_fov: navigated to well={well_id}, fov={fov_index} (flat_idx={target_flat_idx})")
                return True

            self._log.debug(f"go_to_fov: set_current_index failed for fov={target_flat_idx}")
            return False
        except Exception:
            self._log.exception(f"go_to_fov: unexpected error for well={well_id}, fov={fov_index}")
            return False

    def _find_flat_fov_index(self, well_id: str, fov_index: int) -> Optional[int]:
        """
        Find the flat xarray FOV index for a given (well_id, fov_index).

        The xarray FOV dimension is a flat list of all FOVs across all wells.
        Uses the viewer's public get_fov_list() API to get the FOV mapping.

        The FOV list contains dictionaries with keys:
            - "region": str - The well ID (e.g., "A1", "B2")
            - "fov": int - The FOV index within that well

        Returns:
            The flat index if found, None otherwise. Returns None if the FOV list
            is empty (e.g., when get_fov_list() catches an internal error).
        """
        fovs = self._viewer.get_fov_list()
        return next(
            (i for i, fov in enumerate(fovs) if fov["region"] == well_id and fov["fov"] == fov_index),
            None,
        )

    # -------------------------------------------------------------------------
    # Push-based API for live acquisition (no polling)
    # -------------------------------------------------------------------------

    def _ensure_viewer_ready(self, context: str = "acquisition") -> bool:
        """Ensure ndviewer_light is imported and viewer widget is created.

        Args:
            context: Description for logging (e.g., "acquisition", "zarr acquisition")

        Returns:
            True if viewer is ready, False if import or creation failed.
        """
        try:
            from control import ndviewer_light
        except ImportError as e:
            self._log.error(f"Failed to import ndviewer_light: {e}")
            self._show_placeholder(f"NDViewer: failed to import ndviewer_light:\n{e}")
            return False

        if self._viewer is None:
            self._log.debug(f"Creating new LightweightViewer for {context}")
            self._viewer = ndviewer_light.LightweightViewer()
            self._layout.addWidget(self._viewer, 1)

        return True

    def start_acquisition(
        self,
        channels: List[str],
        num_z: int,
        height: int,
        width: int,
        fov_labels: List[str],
    ) -> bool:
        """Configure viewer for a new acquisition.

        Args:
            channels: List of channel names (e.g., ["BF LED matrix full", "Fluorescence 488 nm Ex"])
            num_z: Number of z-levels
            height: Image height in pixels
            width: Image width in pixels
            fov_labels: List of FOV labels (e.g., ["A1:0", "A1:1", "A2:0"])

        Returns:
            True if successful, False otherwise.
        """
        if not self._ensure_viewer_ready("TIFF acquisition"):
            return False

        try:
            self._viewer.start_acquisition(channels, num_z, height, width, fov_labels)
            self._viewer.setVisible(True)
            self._placeholder.setVisible(False)
            self._log.info(
                f"NDViewer configured for acquisition: {len(channels)} channels, "
                f"{num_z} z-levels, {len(fov_labels)} FOVs"
            )
            return True
        except Exception as e:
            self._log.exception("Failed to start acquisition in NDViewer")
            error_msg = str(e) if str(e) else type(e).__name__
            self._show_placeholder(f"NDViewer: failed to start acquisition:\n{error_msg}")
            return False

    def register_image(self, t: int, fov_idx: int, z: int, channel: str, filepath: str) -> None:
        """Register a newly saved image file.

        Called on main thread via Qt signal from worker thread.

        Args:
            t: Timepoint index
            fov_idx: FOV index
            z: Z-level index
            channel: Channel name
            filepath: Path to the saved image file
        """
        if self._viewer is None:
            return
        try:
            self._viewer.register_image(t, fov_idx, z, channel, filepath)
        except Exception:
            self._log.exception(
                f"Failed to register image: t={t}, fov={fov_idx}, z={z}, " f"channel={channel}, filepath={filepath}"
            )

    def load_fov(self, fov: int, t: Optional[int] = None, z: Optional[int] = None) -> bool:
        """Load and display a specific FOV.

        Args:
            fov: FOV index to display
            t: Timepoint index (None = use current)
            z: Z-level index (None = use current)

        Returns:
            True if successful, False otherwise.
        """
        if self._viewer is None:
            self._log.debug("load_fov: no viewer loaded")
            return False
        try:
            self._viewer.load_fov(fov, t, z)
            return True
        except Exception:
            self._log.exception(f"load_fov: failed for fov={fov}, t={t}, z={z}")
            return False

    def end_acquisition(self) -> None:
        """Mark acquisition as ended.

        Call this when acquisition completes. The viewer remains usable
        for navigating the acquired data.
        """
        if self._viewer is None:
            return
        try:
            self._viewer.end_acquisition()
            self._log.debug("NDViewer acquisition ended")
        except Exception:
            self._log.exception("Failed to end NDViewer acquisition")

    # -------------------------------------------------------------------------
    # Zarr Push-based API for live acquisition (requires ndviewer_light zarr support)
    # -------------------------------------------------------------------------

    def start_zarr_acquisition(
        self,
        fov_paths: List[str],
        channels: List[str],
        num_z: int,
        fov_labels: List[str],
        height: int,
        width: int,
    ) -> bool:
        """Configure viewer for zarr-based live acquisition (5D per-FOV mode).

        Args:
            fov_paths: List of zarr paths per FOV
            channels: List of channel names
            num_z: Number of z-levels
            fov_labels: List of FOV labels (e.g., ["A1:0", "A1:1"])
            height: Image height in pixels
            width: Image width in pixels

        Returns:
            True if successful, False otherwise.
        """
        if not self._ensure_viewer_ready("Zarr 5D acquisition"):
            return False

        try:
            # Check if ndviewer_light has zarr support
            if not hasattr(self._viewer, "start_zarr_acquisition"):
                self._log.warning(
                    "ndviewer_light doesn't support zarr push API. "
                    "Live viewing not available for Zarr format. "
                    "Update ndviewer_light submodule to enable this feature."
                )
                self._show_placeholder(
                    "NDViewer: zarr live view requires ndviewer_light with zarr support.\n"
                    "Update the ndviewer_light submodule."
                )
                return False

            self._viewer.start_zarr_acquisition(fov_paths, channels, num_z, fov_labels, height, width)
            self._viewer.setVisible(True)
            self._placeholder.setVisible(False)
            self._log.info(
                f"NDViewer configured for zarr acquisition: {len(channels)} channels, "
                f"{num_z} z-levels, {len(fov_labels)} FOVs"
            )
            return True
        except Exception as e:
            self._log.exception("Failed to start zarr acquisition in NDViewer")
            error_msg = str(e) if str(e) else type(e).__name__
            self._show_placeholder(f"NDViewer: failed to start zarr acquisition:\n{error_msg}")
            return False

    def start_zarr_acquisition_6d(
        self,
        region_paths: List[str],
        channels: List[str],
        num_z: int,
        fovs_per_region: List[int],
        height: int,
        width: int,
        region_labels: List[str],
    ) -> bool:
        """Configure viewer for 6D multi-region zarr acquisition.

        Args:
            region_paths: List of zarr paths (one per region)
            channels: List of channel names
            num_z: Number of z-levels
            fovs_per_region: List of FOV counts per region
            height: Image height in pixels
            width: Image width in pixels
            region_labels: List of region labels (e.g., ["region_1", "region_2"])

        Returns:
            True if successful, False otherwise.
        """
        if not self._ensure_viewer_ready("Zarr 6D acquisition"):
            return False

        try:
            # Check if ndviewer_light has 6D regions support
            if not hasattr(self._viewer, "start_zarr_acquisition_6d"):
                self._log.warning(
                    "ndviewer_light doesn't support 6D multi-region mode. "
                    "Update ndviewer_light submodule to enable this feature."
                )
                self._show_placeholder(
                    "NDViewer: 6D multi-region mode requires updated ndviewer_light.\n"
                    "Update the ndviewer_light submodule."
                )
                return False

            self._viewer.start_zarr_acquisition_6d(
                region_paths, channels, num_z, fovs_per_region, height, width, region_labels
            )
            self._viewer.setVisible(True)
            self._placeholder.setVisible(False)

            total_fovs = sum(fovs_per_region)
            self._log.info(
                f"NDViewer configured for 6D multi-region: {len(region_paths)} regions, "
                f"{total_fovs} total FOVs, {len(channels)} channels, {num_z} z-levels"
            )
            return True
        except Exception as e:
            self._log.exception("Failed to start 6D multi-region zarr acquisition in NDViewer")
            error_msg = str(e) if str(e) else type(e).__name__
            self._show_placeholder(f"NDViewer: failed to start 6D regions acquisition:\n{error_msg}")
            return False

    def notify_zarr_frame(self, t: int, fov_idx: int, z: int, channel: str, region_idx: int = 0) -> None:
        """Notify viewer that a zarr frame was written.

        Called on main thread via Qt signal from worker thread.

        Args:
            t: Timepoint index
            fov_idx: FOV index (local to region in 6D mode, flat index otherwise)
            z: Z-level index
            channel: Channel name
            region_idx: Region index (only used in 6D multi-region mode)
        """
        self._log.debug(f"notify_zarr_frame called: t={t}, fov={fov_idx}, z={z}, ch={channel}")
        if self._viewer is None:
            self._log.warning("notify_zarr_frame: viewer is None")
            return
        try:
            if hasattr(self._viewer, "notify_zarr_frame"):
                self._viewer.notify_zarr_frame(t, fov_idx, z, channel, region_idx)
            else:
                self._log.warning("viewer doesn't have notify_zarr_frame method")
        except Exception:
            self._log.exception(
                f"Failed to notify zarr frame: t={t}, fov={fov_idx}, z={z}, "
                f"channel={channel}, region_idx={region_idx}"
            )

    def end_zarr_acquisition(self) -> None:
        """Mark zarr acquisition as ended.

        Call this when zarr acquisition completes. The viewer remains usable
        for navigating the acquired data.
        """
        if self._viewer is None:
            return
        try:
            if hasattr(self._viewer, "end_zarr_acquisition"):
                self._viewer.end_zarr_acquisition()
                self._log.debug("NDViewer zarr acquisition ended")
        except Exception:
            self._log.exception("Failed to end zarr acquisition in NDViewer")

    def close(self) -> None:
        """Clean up viewer resources."""
        if self._viewer is not None:
            try:
                # Calling close() triggers LightweightViewer.closeEvent(),
                # which stops refresh timers and closes open file handles
                self._viewer.close()
            except Exception:
                self._log.exception("Error closing LightweightViewer")
            self._viewer = None
        self._dataset_path = None


