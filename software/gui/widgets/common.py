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
        error_message = (
            f"This acquisition will capture {image_count:,} images, which will"
            f" require {megabytes_required:,} [MB], but '{save_directory}' only has {megabytes_available:,} [MB] available."
        )
        logger.error(error_message)
        error_dialog(error_message, title="Not Enough Disk Space")
        return False
    return True


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


def get_last_used_saving_path() -> str:
    """Get the last used saving path from cache file, or return the default."""
    cache_file = "cache/last_saving_path.txt"
    try:
        with open(cache_file, "r") as f:
            path = f.read().strip()
            if path and os.path.isdir(path):
                return path
    except OSError:
        pass
    return DEFAULT_SAVING_PATH


def save_last_used_saving_path(path: str) -> None:
    """Save the last used saving path to cache file."""
    if path:  # Only save non-empty paths
        cache_file = "cache/last_saving_path.txt"
        try:
            os.makedirs("cache", exist_ok=True)
            with open(cache_file, "w") as f:
                f.write(path)
        except OSError:
            pass  # Silently fail - caching is a convenience feature


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


