import math
import time
from typing import Callable, List, Optional, Tuple, Sequence, Dict

import numpy as np
import pydantic

import control.utils
import squid.logging
from squid.abc import (
    AbstractCamera,
    CameraAcquisitionMode,
    CameraGainRange,
    CameraFrameFormat,
    CameraPixelFormat,
    CameraFrame,
)
from squid.config import CameraConfig, CameraReadoutMode, ToupcamCameraModel
from control._def import *

import threading
import control.toupcam as toupcam
from control.toupcam_exceptions import hresult_checker
from control._sdk_watchdog import BoundedSdkCaller, CameraTimeoutError

log = squid.logging.get_logger(__name__)

# Watchdog timeout for per-capture Toupcam control/reconfigure SDK calls (set
# exposure, set gain, software trigger). These normally complete in well under a
# second; a call that blocks past this is a wedged native driver transaction (the
# failure mode that froze a timelapse mid-frame for ~2.5 days). Chosen generous
# enough never to false-trip a legitimate call, small enough to fail fast relative
# to a multi-minute timepoint. See control/_sdk_watchdog.py.
TOUPCAM_CONTROL_CALL_TIMEOUT_S = 15.0

# Budget for reopen() — recovering a wedged camera does more than one control call
# (enumerate + open the device, base-configure, restore ROI/mode, restart the pull
# stream), so it gets a larger timeout. If even this times out (e.g. the USB
# endpoint itself is dead) reopen raises CameraTimeoutError and the caller falls
# back to a clean acquisition abort.
TOUPCAM_REOPEN_TIMEOUT_S = 30.0


class ToupCamCapabilities(pydantic.BaseModel):
    binning_to_resolution: Dict[Tuple[int, int], Tuple[int, int]]
    has_fan: bool
    has_TEC: bool
    has_low_noise_mode: bool
    has_black_level: bool
    # Conversion Gain support: HCG/LCG (TOUPCAM_FLAG_CG) and the HDR superset (TOUPCAM_FLAG_CGHDR).
    has_conversion_gain: bool
    has_cghdr: bool
    has_high_fullwell: bool


class StrobeInfo(pydantic.BaseModel):
    strobe_time_us: float
    trigger_delay_us: float


def get_sn_by_model(camera_model: ToupcamCameraModel):
    try:
        device_list = toupcam.Toupcam.EnumV2()
    except:
        log.error("Problem generating Toupcam device list")
        return None
    for dev in device_list:
        if dev.displayname == camera_model.value:
            return dev.id
    return None  # return None if no device with the specified model_name is connected



class ToupcamCamera(AbstractCamera):
    TOUPCAM_OPTION_RAW_RAW_VAL = 1
    TOUPCAM_OPTION_RAW_RGB_VAL = 0
    PIXEL_SIZE_UM = 3.76

    @staticmethod
    def _event_callback(event_number, camera):
        if event_number == toupcam.TOUPCAM_EVENT_IMAGE:
            camera._on_frame_callback()

    @staticmethod
    def _tdib_width_bytes(w):
        return (w * 24 + 31) // 32 * 4

    @staticmethod
    def _calculate_strobe_info(
        camera: toupcam.Toupcam, pixel_size: int, exposure_time_ms: float, capabilities: ToupCamCapabilities
    ) -> StrobeInfo:
        log = squid.logging.get_logger("ToupcamCamera._calculate_strobe_delay")
        # use camera arguments such as resolutuon, ROI, exposure time, set max FPS, bandwidth to calculate the trigger delay time

        pixel_bits = pixel_size * 8
        line_length = 0
        low_noise = 0

        try:
            resolution_width, resolution_height = camera.get_Size()
        except toupcam.HRESULTException as ex:
            log.exception("get resolution fail, hr=0x{:x}".format(ex.hr))
            raise

        xoffset, yoffset, roi_width, roi_height = camera.get_Roi()

        try:
            bandwidth = camera.get_Option(toupcam.TOUPCAM_OPTION_BANDWIDTH)
        except toupcam.HRESULTException as ex:
            log.exception("get badwidth fail, hr=0x{:x}".format(ex.hr))
            raise

        if capabilities.has_low_noise_mode:
            try:
                low_noise = camera.get_Option(toupcam.TOUPCAM_OPTION_LOW_NOISE)
            except toupcam.HRESULTException as ex:
                log.exception("get low_noise fail, hr=0x{:x}".format(ex.hr))

        if resolution_width == 6224 and resolution_height == 4168:
            if pixel_bits == 8:
                line_length = 1200 * (roi_width / 6224)
                if line_length < 450:
                    line_length = 450
            elif pixel_bits == 16:
                if low_noise == 1:
                    line_length = 5000
                elif low_noise == 0:
                    line_length = 2500
        elif resolution_width == 3104 and resolution_height == 2084:
            if pixel_bits == 8:
                line_length = 906
            elif pixel_bits == 16:
                line_length = 1200
        elif resolution_width == 2064 and resolution_height == 1386:
            if pixel_bits == 8:
                line_length = 454
            elif pixel_bits == 16:
                line_length = 790

        line_length = int(line_length / (bandwidth / 100.0))
        row_time = line_length / 72

        try:
            max_framerate_tenths_fps = camera.get_Option(toupcam.TOUPCAM_OPTION_MAX_PRECISE_FRAMERATE)
        except toupcam.HRESULTException as ex:
            log.error(f"get max_framerate fail --> {control.toupcam_exceptions.explain(ex)}")
            raise

        # need reset value, because the default value is only 90% of setting value
        try:
            camera.put_Option(toupcam.TOUPCAM_OPTION_PRECISE_FRAMERATE, max_framerate_tenths_fps)
        except toupcam.HRESULTException as ex:
            log.exception(f"put max_framerate fail --> {control.toupcam_exceptions.explain(ex)}")
            raise

        max_framerate_fps = max_framerate_tenths_fps / 10.0

        vheight = 72000000 / (max_framerate_fps * line_length)
        if vheight < roi_height + 56:
            vheight = roi_height + 56

        """
        The trigger delay in [ms].  This is the time after the trigger but before the camera actually
        starts the exposure.  For larger exposure times, this is ~0.  But for small exposure times this
        can actually be multiples of the exposure time.  It's included in the strobe time since it looks
        like strobe delay for both hardware and software trigger purposes.  See the "TRG_DELAY&ROW_TIME&TOTAL_RESET"
        pdf from toupcam.
        """
        exposure_time_us = exposure_time_ms * 1000.0
        exposure_length = int(72 * exposure_time_us / line_length)

        if vheight >= exposure_length - 1:
            shr = vheight - exposure_length
        else:
            shr = 1

        trigger_delay_us = (shr * line_length) / 72
        strobe_time = int(vheight * row_time)

        log.debug(
            f"New strobe time calculated as {strobe_time} [us]. {resolution_width=}, {resolution_height=}, {pixel_bits=}, {line_length=}, {low_noise=}, {vheight=}, {trigger_delay_us=}"
        )

        return StrobeInfo(strobe_time_us=strobe_time, trigger_delay_us=trigger_delay_us)

    @staticmethod
    def _open(index=None, sn=None) -> Tuple[toupcam.Toupcam, ToupCamCapabilities]:
        log = squid.logging.get_logger("ToupcamCamera._open")
        log.info(f"Opening toupcam with {index=}, {sn=}")
        devices = toupcam.Toupcam.EnumV2()
        if len(devices) <= 0:
            raise ValueError("There are no Toupcam V2 devices.  Is the camera connected and powered on?")

        if index is not None and sn is not None:
            raise ValueError("You specified both a device index and a sn, this is not allowed.")

        if sn is not None:
            sn_matches = [idx for idx in range(len(devices)) if devices[idx].id == sn]
            if not len(sn_matches):
                all_sn = [d.id for d in devices]
                raise ValueError(f"Could not find camera with SN={sn}, options are: {','.join(all_sn)}")

        for idx, device in enumerate(devices):
            log.info(
                "Camera {}: {}: flag = {:#x}, preview = {}, still = {}".format(
                    idx,
                    device.displayname,
                    device.model.flag,
                    device.model.preview,
                    device.model.still,
                )
            )

        for r in devices[index].model.res:
            log.info("\t = [{} x {}]".format(r.width, r.height))

        resolution_list = []
        for r in devices[index].model.res:
            resolution_list.append((r.width, r.height))
        if len(resolution_list) == 0:
            raise ValueError("No resolutions found for camera")
        resolution_list.sort(key=lambda x: x[0] * x[1], reverse=True)

        highest_res = resolution_list[0]

        binning_res = {}
        for res in resolution_list:
            x_binning = int(highest_res[0] / res[0])
            y_binning = int(highest_res[1] / res[1])
            binning_res[(x_binning, y_binning)] = res

        camera = toupcam.Toupcam.Open(devices[index].id)
        capabilities = ToupCamCapabilities(
            binning_to_resolution=binning_res,
            has_fan=(devices[index].model.flag & toupcam.TOUPCAM_FLAG_FAN) > 0,
            has_TEC=(devices[index].model.flag & toupcam.TOUPCAM_FLAG_TEC_ONOFF) > 0,
            has_low_noise_mode=(devices[index].model.flag & toupcam.TOUPCAM_FLAG_LOW_NOISE) > 0,
            has_black_level=(devices[index].model.flag & toupcam.TOUPCAM_FLAG_BLACKLEVEL) > 0,
            has_conversion_gain=(devices[index].model.flag & (toupcam.TOUPCAM_FLAG_CG | toupcam.TOUPCAM_FLAG_CGHDR)) > 0,
            has_cghdr=(devices[index].model.flag & toupcam.TOUPCAM_FLAG_CGHDR) > 0,
            has_high_fullwell=(devices[index].model.flag & toupcam.TOUPCAM_FLAG_HIGH_FULLWELL) > 0,
        )

        return camera, capabilities

    def __init__(self, config: CameraConfig, hw_trigger_fn, hw_set_strobe_delay_ms_fn):
        super().__init__(config, hw_trigger_fn, hw_set_strobe_delay_ms_fn)

        # Watchdog for blocking per-capture control SDK calls. Created before
        # _configure_camera() below because that path calls the (now watchdogged)
        # set_analog_gain. A wedged native call raises CameraTimeoutError instead of
        # hanging the acquisition forever. See control/_sdk_watchdog.py.
        self._sdk = BoundedSdkCaller(
            default_timeout_s=TOUPCAM_CONTROL_CALL_TIMEOUT_S, log=self._log, name="toupcam"
        )

        # Logical state tracked in Python (NOT read back from the handle) so reopen()
        # can restore it after a wedge without touching the dead handle. get_acquisition_mode
        # / get_region_of_interest read the handle, so they are unusable once wedged.
        self._acquisition_mode: Optional[CameraAcquisitionMode] = None
        self._roi: Optional[Tuple[int, int, int, int]] = None

        # Drop detection: the camera's real hardware frame sequence
        # (ToupcamFrameInfoV2.seq) from the last processed frame, plus the cumulative
        # count of frames the SDK/USB dropped (detected as gaps in that sequence). The
        # CameraFrame.frame_id below is only a synthetic +1 counter and can never reveal
        # a drop, which is why the 2026-07-02 run's frame collapse (3240 -> 79 per
        # timepoint) was almost entirely silent.
        self._last_seq: Optional[int] = None
        self._dropped_frame_count: int = 0

        self._current_frame: Optional[CameraFrame] = None
        self._camera: Optional[toupcam.Toupcam] = None

        # These are used only in both software and hw trigger mode.  We use them to make sure we don't send a trigger
        # when a frame is already in progress.  The send_trigger method should be the only one setting this to True
        # (and setting the timestamp), and the raw frame callback can set the _trigger_sent to False when
        # it receives a frame.
        self._trigger_sent = False
        self._last_trigger_timestamp = 0

        # _raw_camera_stream_started keeps track of the ToupcamCamera <-> hardware stream. This should always be running,
        # because it is how we get notified by the camera that new frames are available.  Our _on_frame_callback
        # is what the camera driver calls when a new frame is available.
        self._raw_camera_stream_started = False
        self._raw_frame_callback_lock = threading.Lock()

        # Fast acquisition state (protected by _raw_frame_callback_lock)
        self._fast_acquisition_active = False
        self._fast_acquisition_callback: Optional[Callable] = None
        self._fast_acquisition_frame_index = 0
        (self._camera, self._capabilities) = ToupcamCamera._open(index=0)
        self._pixel_format = self._config.default_pixel_format
        self._binning = self._config.default_binning

        # Since we need to set the on-camera exposure time different depending on our trigger mode
        # (eg: sometimes we compensate for a strobe delay when hardware triggering), we can't back
        # out our users' exposure time easily from the camera value.  To get around this, we need
        # to store the exposure time they give to us.
        #
        # Because it is better than nothing, we initialize our stored value to whatever is on the
        # camera at startup (but then set_exposure_time will modify it when a user sets exposure time)
        self._exposure_time = self._get_raw_exposure_time()
        self._trigger_duration_us = 40
        # Default strobe info so get_strobe_time() is safe before the first
        # (streaming-gated) strobe recalc in _update_internal_settings.
        self._strobe_info = StrobeInfo(strobe_time_us=0.0, trigger_delay_us=0.0)
        # True when a settings change skipped the strobe recalc (stream stopped);
        # start_streaming refreshes it.
        self._strobe_dirty = False

        # toupcam temperature
        self.temperature_reading_callback = None
        self.terminate_read_temperature_thread = False
        self.thread_read_temperature = threading.Thread(target=self._check_temperature, daemon=True)
        self.thread_read_temperature.start()


        self._byte_decoding_fn = lambda raw, meta: self.toupcam_raw_bytes_to_np(raw, meta)
        self._configure_camera()
        self._start_raw_camera_stream()
        self._update_internal_settings()


    def toupcam_raw_bytes_to_np(self, raw: bytes, meta: dict) -> np.ndarray:
        """Decode one fast-acquisition frame; packing comes from the camera, not metadata.

        Applies the same flip as the normal frame path (AbstractCamera._process_raw_frame,
        driven by self._config.flip) so fast-acquisition output has the same real-space
        orientation as live/multipoint frames. The normal path can't be reused here because
        fast acquisition deliberately operates on raw bytes (no rotate/crop) for performance.
        """
        height = int(meta["height"])
        width = int(meta["width"])
        px_size_bytes = self._get_pixel_size_in_bytes()
        if px_size_bytes == 1:
            image = np.frombuffer(raw, dtype="uint8").reshape(height, width)
        elif px_size_bytes == 2:
            image = np.frombuffer(raw, dtype="uint16").reshape(height, width)
        else:
            raise ValueError(f"Unknown pixel size for fast-acquisition decode: {px_size_bytes!r}")
        return self._apply_config_flip(image)

    def _apply_config_flip(self, image: np.ndarray) -> np.ndarray:
        """Apply the configured camera flip (self._config.flip) to a 2D frame.

        Mirrors the flip half of utils.rotate_and_flip_image so the raw
        fast-acquisition decode matches the normal _process_raw_frame orientation.
        Returns a contiguous array (np.frombuffer gives a read-only view). Flips
        preserve frame dimensions, so the fast-acquisition writer's height/width
        metadata stays valid.
        """
        flip = self._config.flip
        if flip == control.utils.FlipVariant.VERTICAL:
            return np.ascontiguousarray(image[::-1, :])
        elif flip == control.utils.FlipVariant.HORIZONTAL:
            return np.ascontiguousarray(image[:, ::-1])
        elif flip == control.utils.FlipVariant.BOTH:
            return np.ascontiguousarray(image[::-1, ::-1])
        return image

    def _start_raw_camera_stream(self):
        """
        Make sure the camera is setup to tell us when frames are available.
        """
        try:
            self._log.debug("Starting raw stream in PullModeWithCallback.")
            self._camera.StartPullModeWithCallback(self._event_callback, self)
            self._raw_camera_stream_started = True
            # The SDK frame sequence restarts with the stream; reset our tracker so the
            # first frame after a (re)start isn't mis-counted as a drop.
            self._last_seq = None
        except toupcam.HRESULTException as ex:
            self._raw_camera_stream_started = False
            self._log.exception("failed to start camera, hr=0x{:x}".format(ex.hr))
            raise ex

    def _note_frame_seq(self, seq: int) -> None:
        """Detect SDK/USB-level frame drops from the camera's hardware frame sequence.

        ToupcamFrameInfoV2.seq increments by 1 per delivered frame; a jump means the
        driver dropped frames before we ever saw them. Those drops are otherwise
        invisible (CameraFrame.frame_id is a synthetic +1 counter), which is why the
        2026-07-02 collapse from 3240 to 79 frames/timepoint logged almost nothing.
        """
        prev = self._last_seq
        self._last_seq = seq
        if prev is None:
            return
        gap = seq - prev - 1
        if gap > 0:  # >0 only; a reset/wrap goes negative and is ignored (no false alarm)
            self._dropped_frame_count += gap
            self._log.warning(
                f"[FRAME-DROP] Toupcam dropped {gap} frame(s) at the SDK/USB level "
                f"(hardware seq {prev} -> {seq}); cumulative dropped this stream="
                f"{self._dropped_frame_count}"
            )

    def get_dropped_frame_count(self) -> int:
        """Cumulative frames the camera/SDK dropped (seq gaps) since the last stream start."""
        return self._dropped_frame_count

    def _on_frame_callback(self):
        """
        This is the callback that we have the toupcam software call when a frame is ready.  It should always be running.
        """
        fast_acq_callback = None
        fast_acq_frame_bytes = None
        fast_acq_metadata = None
        current_frame = None

        with self._raw_frame_callback_lock:
            # A reopen() may be swapping the native handle; ignore any stale callback
            # from the abandoned handle during the brief window self._camera is None.
            if self._camera is None:
                return
            # Since we are receiving a frame callback, we know things are setup properly.
            self._raw_camera_stream_started = True

            # Make sure that if this was triggered by a software trigger, or we switched to software triggering
            # while waiting for this frame, that we allow subsequent software triggers.
            self._trigger_sent = False

            # get the image from the camera; frame_info carries the camera's real
            # hardware frame sequence, used for drop detection on the normal path below.
            frame_info = toupcam.ToupcamFrameInfoV2()
            try:
                self._camera.PullImageV2(
                    self._internal_read_buffer, self._get_pixel_size_in_bytes() * 8, frame_info
                )  # the second arg is bits per pixel - ignored in RAW mode
            except toupcam.HRESULTException as ex:
                self._log.error("pull image failed, hr=0x{:x}".format(ex.hr))
                return

            # Fast acquisition path: pass raw bytes + metadata, skip normal processing
            if self._fast_acquisition_active and self._fast_acquisition_callback is not None:
                (x_offset, y_offset, width, height) = self.get_region_of_interest()
                fast_acq_metadata = {
                    "height": height,
                    "width": width,
                    "timestamp": time.time(),
                    "frame_index": self._fast_acquisition_frame_index,
                    "pixel_size_bytes": self._get_pixel_size_in_bytes(),
                }
                self._fast_acquisition_frame_index += 1
                fast_acq_callback = self._fast_acquisition_callback
                fast_acq_frame_bytes = bytes(self._internal_read_buffer)
            else:
                # Normal frame processing path
                # Drop detection runs only here (not the fast-acquisition branch, which
                # has its own accounting) so normal-mode frames are contiguous in seq.
                self._note_frame_seq(frame_info.seq)
                this_frame_id = (self._current_frame.frame_id if self._current_frame else 0) + 1
                this_timestamp = time.time()
                this_frame_format = self.get_frame_format()
                this_pixel_format = self.get_pixel_format()

                if this_frame_format != CameraFrameFormat.RAW:
                    self._log.error("Only RAW CameraFrameFormat are supported, cannot handle frame.")
                    return

                (x_offset, y_offset, width, height) = self.get_region_of_interest()
                if self._get_pixel_size_in_bytes() == 1:
                    raw_image = np.frombuffer(self._internal_read_buffer, dtype="uint8")
                elif self._get_pixel_size_in_bytes() == 2:
                    raw_image = np.frombuffer(self._internal_read_buffer, dtype="uint16")
                current_raw_image = raw_image.reshape(height, width)

                current_frame = CameraFrame(
                    frame_id=this_frame_id,
                    timestamp=this_timestamp,
                    frame=self._process_raw_frame(current_raw_image),
                    frame_format=this_frame_format,
                    frame_pixel_format=this_pixel_format,
                )

                # Before releasing the lock, set the new current frame with the incremented frame id so other methods can
                # see we have a new frame. This should be the only place we modify _current_frame outside of init, and
                # since we hold a lock this whole time, we know that the frame id is still correct.
                self._current_frame = current_frame

        # Outside the lock: invoke callbacks
        if fast_acq_callback is not None:
            try:
                fast_acq_callback(fast_acq_frame_bytes, fast_acq_metadata)
            except Exception:
                self._log.exception("Fast acquisition frame callback error")
        elif current_frame is not None:
            self._propogate_frame(current_frame)

    def _update_internal_settings(self, send_exposure=True):
        """
        This needs to be called when a camera side setting changes that needs a:
          * read buffer size update
          * strobe delay recalc

        It might be called in a performance sensitive context, so you should make sure any updates here
        are as fast as they can be.
        """
        # resize the buffer
        _, _, width, height = self._camera.get_Roi()

        # calculate buffer size
        pixel_size = self._get_pixel_size_in_bytes()
        if self.get_frame_format() == CameraFrameFormat.RGB and pixel_size != 4:
            buffer_size = ToupcamCamera._tdib_width_bytes(width * pixel_size * 8) * height
        else:
            buffer_size = width * pixel_size * height
        # create the buffer
        self._internal_read_buffer = bytes(buffer_size)

        image_exposure_time_ms = self.get_exposure_time()
        camera_exposure_time_ms = self._calculate_camera_exposure_time(image_exposure_time_ms)
        self._log.info(
            f"Updating internal settings with {width=} x {height=}, {buffer_size=}, image_exposure_time={image_exposure_time_ms} [ms], camera_exposure_time={camera_exposure_time_ms} [ms], send_exposure={send_exposure}")
        # Strobe timing reads camera options (e.g. MAX_PRECISE_FRAMERATE) that the
        # toupcam SDK only services while the pull stream is running. When the
        # stream is stopped (e.g. after stopping CONTINUOUS live) that read raises
        # E_UNEXPECTED ("Catastrophic failure"). Skip the recalc when not streaming
        # and mark strobe dirty so it is refreshed on the next start_streaming;
        # this keeps the last-known strobe info usable in the meantime. Without
        # this, changing any camera setting after live broke with a fatal error.
        if self.get_is_streaming():
            try:
                self._strobe_info = ToupcamCamera._calculate_strobe_info(
                    camera=self._camera,
                    pixel_size=self._get_pixel_size_in_bytes(),
                    exposure_time_ms=camera_exposure_time_ms,
                    capabilities=self._capabilities,
                )
                self._strobe_dirty = False
            except toupcam.HRESULTException as ex:
                # Transient failure (e.g. just after restart): keep last strobe
                # info and retry on the next update/start.
                self._strobe_dirty = True
                self._log.warning(
                    f"Strobe recalc failed (camera not ready, hr=0x{ex.hr:x}); will retry on stream start."
                )
            if self._hw_set_strobe_delay_ms_fn and self.get_acquisition_mode() == CameraAcquisitionMode.HARDWARE_TRIGGER:
                self._hw_set_strobe_delay_ms_fn(self.get_strobe_time())
        else:
            # Defer the strobe recalc until the stream is running again.
            self._strobe_dirty = True

        if send_exposure:
            self._calculate_and_set_camera_exposure_time(image_exposure_time_ms)

        self._log.info(
            f"image size: {width=} x {height=}, {buffer_size=}, strobe_time={self.get_strobe_time()} [ms], exposure_time={self.get_exposure_time()} [ms], full frame time={self.get_total_frame_time()} [ms], {send_exposure=}"
        )

    def _check_temperature(self):
        while not self.terminate_read_temperature_thread:
            time.sleep(2)
            # reopen() briefly sets self._camera = None while swapping handles, and a
            # freshly-opened handle can transiently error. Either would otherwise raise
            # out of this loop and kill the temperature/TEC monitor for the rest of the
            # run, so skip the poll instead of dying.
            if self._camera is None:
                continue
            try:
                temperature = self.get_temperature()
            except Exception as ex:
                self._log.debug(f"Temperature read skipped (camera may be reopening): {ex!r}")
                continue
            if self.temperature_reading_callback is not None:
                try:
                    self.temperature_reading_callback(temperature)
                except TypeError as ex:
                    self._log.error("Temperature read callback failed due to error: " + repr(ex))
                    pass

    def _configure_camera(self):
        """
        Run our initial configuration to get the camera into a know and safe starting state.
        """
        # Disable SDK auto-exposure/auto-gain. Touptek defaults it ON, and in
        # CONTINUOUS (live) mode it ramps ExpoTime up to 350 ms and ExpoAGain up
        # to 5x chasing a brightness target — blowing out the image and silently
        # overriding the user's exposure/gain (it also auto-drives gain to 500,
        # which the live-state cache then persists). The software owns exposure
        # and analog gain explicitly, so the camera must stay in full manual mode.
        try:
            self.set_auto_exposure(False)
        except Exception as e:
            self._log.warning(f"Could not disable auto-exposure at init: {e}")

        if self._capabilities.has_low_noise_mode:
            self._camera.put_Option(toupcam.TOUPCAM_OPTION_LOW_NOISE, 0)

        # High fullwell capacity is an init-only setting (not runtime configurable here).
        if self._config.default_high_fullwell is not None:
            if self._capabilities.has_high_fullwell:
                self._camera.put_Option(
                    toupcam.TOUPCAM_OPTION_HIGH_FULLWELL, 1 if self._config.default_high_fullwell else 0
                )
                self._log.info(f"High fullwell capacity set to {self._config.default_high_fullwell}")
            else:
                self._log.warning(
                    "high_fullwell requested but this toupcam model does not support it, ignoring"
                )

        # Reset analog gain to a deterministic baseline. The sensor powers on at a
        # non-unity raw gain (~5x = 13.979 user units); without this it leaks into the
        # cached live state (bootstrap-from-hardware) and causes recurring live blowout.
        if self._config.default_analog_gain is not None:
            try:
                self.set_analog_gain(self._config.default_analog_gain)
            except Exception as e:
                self._log.warning(f"Could not set default analog gain at init: {e}")

        self._set_fan_speed(self._config.default_fan_speed)

        # set temperature
        if self._config.default_temperature is None:
            if self._capabilities.has_TEC:
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_TEC, 0)
                self._log.info("TEC disabled (default_temperature is None)")
        else:
            self.set_temperature(self._config.default_temperature)

        self._raw_set_frame_format(CameraFrameFormat.RAW)
        self._raw_set_pixel_format(self._pixel_format)  # 'MONO8'
        try:
            self.set_black_level(self._config.default_black_level)
        except NotImplementedError:
            self._log.warning("Black level is not supported by this toupcam model, ignoring default black level value")

        # We can't trigger update_internal_settings yet, because the strobe calc will fail.  So set the res
        # using the raw helper.
        (width, height) = self._capabilities.binning_to_resolution[self._binning]
        self._raw_set_resolution(width, height)

        # TODO: Do hardware cropping here (set ROI)

    def set_temperature_reading_callback(self, func):
        self.temperature_reading_callback = func

    def _get_raw_exposure_time(self) -> float:
        return self._camera.get_ExpoTime() / 1000.0  # microseconds -> milliseconds

    def close(self):
        self.terminate_read_temperature_thread = True
        # Bounded join: the poll thread could be blocked in an unbounded native
        # get_Temperature() on a wedged handle; it is a daemon thread, so abandoning it
        # is fine and keeps app shutdown from hanging here.
        self.thread_read_temperature.join(timeout=3)
        # Skip the SDK teardown calls on a wedged handle — they could block on the same
        # stuck driver lock and hang app shutdown. The handle is unrecoverable anyway.
        if not self._sdk.is_wedged:
            self._set_fan_speed(0)
            self._camera.Close()
        self._camera = None
        self._sdk.shutdown()

    def reopen(self):
        """Recover a wedged camera so acquisition can continue.

        A watchdog timeout means the current native handle is stuck in a call that
        will never return; it cannot be closed safely. So we ABANDON it (and its
        stuck daemon watchdog thread), stand up a fresh watchdog, open a brand-new
        handle, and restore the tracked logical state (pixel format / binning via
        _configure_camera, acquisition mode, ROI, exposure) plus the pull stream.

        Frame callbacks are stored on this object (not the handle), so they carry
        over untouched — the worker does not need to re-register them.

        NOT restored here: the conversion-gain camera_mode (LCG/HCG/HDR) and the active
        analog gain — _configure_camera resets gain to the configured default. The
        caller is responsible for reasserting those (the MultiPointWorker does so by
        re-running _seed_camera_for_first_observation_state after reopen). A future
        caller must do the same or post-reopen frames may use the power-on gain mode.

        The whole sequence runs under the fresh watchdog, so if the reopen itself
        hangs (e.g. the USB endpoint is dead) it raises CameraTimeoutError instead of
        blocking, and the caller falls back to a clean abort. Raises on any failure.
        """
        self._log.warning(
            "Reopening wedged Toupcam: abandoning the stuck handle and its watchdog thread, opening a fresh device."
        )
        old_sdk = self._sdk
        # Fresh, un-wedged watchdog for the new handle, keeping the NORMAL per-call
        # budget so later control calls still fail fast; the reopen itself gets a
        # larger budget via a per-call timeout below.
        self._sdk = BoundedSdkCaller(
            default_timeout_s=TOUPCAM_CONTROL_CALL_TIMEOUT_S, log=self._log, name="toupcam"
        )
        old_sdk.shutdown()  # non-blocking; the wedged daemon thread is left abandoned

        # Snapshot logical state from tracked fields — NEVER read the dead handle.
        exposure_ms = self._exposure_time
        acq_mode = self._acquisition_mode
        roi = self._roi
        # Reopen does device enumeration + open + full reconfigure, so give this one
        # call a larger budget; subsequent control calls keep the 15s default.
        self._sdk.call(
            "reopen",
            lambda: self._reopen_impl(exposure_ms, acq_mode, roi),
            timeout_s=TOUPCAM_REOPEN_TIMEOUT_S,
        )
        self._log.info("Toupcam reopened and reconfigured successfully.")

    def _reopen_impl(self, exposure_ms, acq_mode, roi):
        # Swap the handle under the frame-callback lock so a stale callback from the
        # abandoned handle only ever observes old / None / fully-swapped-new — never a
        # half-configured handle or a torn read-buffer resize. Deadlock-safe: nothing
        # called below acquires this lock, and _on_frame_callback makes no self._sdk.call
        # while holding it.
        with self._raw_frame_callback_lock:
            # Forget the wedged handle entirely — do not Close() it (that call could
            # hang too). Setting it None first makes any stale frame callback bail out.
            self._camera = None
            self._raw_camera_stream_started = False
            self._trigger_sent = False
            self._last_trigger_timestamp = 0
            self._strobe_dirty = False
            self._last_seq = None  # fresh handle restarts the SDK frame sequence

            (self._camera, self._capabilities) = ToupcamCamera._open(index=0)
            # Base config from stored self._pixel_format / self._binning / self._config
            # (auto-exposure off, frame/pixel format, resolution, fan, temperature, gain).
            self._configure_camera()

            # Restore trigger/acquisition mode (stream not started yet, so this won't
            # re-issue exposure — we do that below).
            if acq_mode is not None:
                self._set_acquisition_mode_imp(acq_mode)

            # Restore ROI while the stream is stopped (matches the normal set-then-stream
            # ordering). Skipped if the acquisition used the full frame.
            if roi is not None:
                try:
                    self._camera.put_Roi(*roi)
                except toupcam.HRESULTException:
                    self._log.exception("Could not restore ROI on reopen; continuing with full frame.")

            self._exposure_time = exposure_ms
            self._start_raw_camera_stream()
            self._update_internal_settings()

    def start_streaming(self):
        self._log.info("start streaming requested")
        if not self._raw_camera_stream_started:
            self._start_raw_camera_stream()
            # Settings changed while the stream was stopped deferred the strobe
            # recalc (the SDK can't read MAX_PRECISE_FRAMERATE when stopped). Now
            # that the stream is running again, refresh strobe/buffer/exposure so
            # the next acquisition uses correct timing.
            if self._strobe_dirty:
                self._update_internal_settings()

    def stop_streaming(self):
        # If the camera is wedged, its native handle is stuck and Stop() could block on
        # the same driver lock — skip it so acquisition teardown (and the finalize that
        # follows) is never held up. The app must be restarted to recover the camera
        # anyway, so leaving the (dead) stream "running" is moot.
        if self._sdk.is_wedged:
            self._log.warning("Camera wedged; skipping stop_streaming() Stop() to avoid blocking teardown.")
            self._raw_camera_stream_started = False
            return
        self._camera.Stop()
        self._raw_camera_stream_started = False

    def get_is_streaming(self):
        return self._raw_camera_stream_started

    def set_exposure_time(self, exposure_time_ms: float):
        # Watchdogged: _update_internal_settings issues native SDK calls (strobe
        # recalc, put_ExpoTime) that must not be able to hang the acquisition forever.
        self._sdk.call("set_exposure_time", lambda: self._set_exposure_time_impl(exposure_time_ms))

    def _set_exposure_time_impl(self, exposure_time_ms: float):
        # Since we have to set the on-camera exposure time differently depending on the trigger mode
        # and the calculated strobe delay, it is tricky to get the exposure time from the
        # camera.  To get around this, we store it.
        self._exposure_time = exposure_time_ms

        self._update_internal_settings(send_exposure=True)

    def _calculate_camera_exposure_time(self, image_exposure_time_ms):
        exposure_for_camera_ms = image_exposure_time_ms
        # In the calls below, we need to make sure we convert to microseconds.
        if self.get_acquisition_mode() == CameraAcquisitionMode.HARDWARE_TRIGGER:
            # Only add the strobe_time_us, and not strobe_time_us + trigger_delay_us.  We'll tell the lighting
            # to come on at strobe_time_us + trigger_delay_us since that's when the common (all row) exposure time
            # starts, but if we tell that to the camera we'll get an extra trigger_delay_us of exposure.
            exposure_for_camera_ms += self._strobe_info.strobe_time_us / 1000.0

        return exposure_for_camera_ms

    def _calculate_and_set_camera_exposure_time(self, image_exposure_time_ms):
        exposure_for_camera_us = int(self._calculate_camera_exposure_time(image_exposure_time_ms) * 1000.0)
        self._log.debug(
            f"Sending exposure {exposure_for_camera_us} [us] to camera for image_exposure_time={1000 * image_exposure_time_ms} [us]"
        )
        self._camera.put_ExpoTime(exposure_for_camera_us)

    def get_exposure_time(self) -> float:
        return self._exposure_time

    def get_exposure_limits(self) -> Tuple[float, float]:
        (min_exposure, max_exposure, default_exposure) = self._camera.get_ExpTimeRange()
        return min_exposure / 1000.0, max_exposure / 1000.0  # us -> ms

    @staticmethod
    def _user_gain_to_toupcam(user_gain):
        """
        0-40 is the valid user range.  This must map to 100-10000 in toupcam
        """
        return int(100 * (10 ** (user_gain / 20)))

    @staticmethod
    def _toupcam_gain_to_user(toupcam_gain):
        return 20 * math.log10(toupcam_gain / 100)

    def set_analog_gain(self, analog_gain):
        # Watchdogged: get_ExpoAGainRange / get_Option / put_ExpoAGain are native SDK
        # calls. This is the exact path that wedged and froze a timelapse mid-frame.
        self._sdk.call("set_analog_gain", lambda: self._set_analog_gain_impl(analog_gain))

    def _set_analog_gain_impl(self, analog_gain):
        gain_range = self.get_gain_range()
        self._log.info(f"Requested {analog_gain=} with gain range {gain_range} in gain mode {self._get_gain_mode()}")

        clamped_gain = max(gain_range.min_gain, min(analog_gain, gain_range.max_gain))

        if clamped_gain != analog_gain:
            self._log.warning(
                f"Requested {analog_gain=} is outside the range {gain_range.min_gain} to {gain_range.max_gain}"
            )

        # for touptek cameras gain is 100-10000 (for 1x - 100x)
        self._log.info(f"Trying to set analog gain = {clamped_gain}")
        self._camera.put_ExpoAGain(self._user_gain_to_toupcam(clamped_gain))

    def _raw_set_pixel_format(self, pixel_format: CameraPixelFormat):
        if self.get_frame_format() == CameraFrameFormat.RAW:
            if pixel_format == CameraPixelFormat.MONO8:
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_BITDEPTH, 0)
            elif pixel_format == CameraPixelFormat.MONO12:
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_BITDEPTH, 1)
            elif pixel_format == CameraPixelFormat.MONO14:
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_BITDEPTH, 1)
            elif pixel_format == CameraPixelFormat.MONO16:
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_BITDEPTH, 1)
            else:
                raise ValueError(f"Unsupported pixel format: {pixel_format=}")
        else:
            # RGB data format
            if pixel_format == CameraPixelFormat.MONO8:
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_BITDEPTH, 0)
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_RGB, 3)  # for monochrome camera only
            elif pixel_format == CameraPixelFormat.MONO12:
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_BITDEPTH, 1)
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_RGB, 4)  # for monochrome camera only
            elif pixel_format == CameraPixelFormat.MONO14:
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_BITDEPTH, 1)
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_RGB, 4)  # for monochrome camera only
            elif pixel_format == CameraPixelFormat.MONO16:
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_BITDEPTH, 1)
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_RGB, 4)  # for monochrome camera only
            elif pixel_format == CameraPixelFormat.RGB24:
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_BITDEPTH, 0)
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_RGB, 0)
            elif pixel_format == CameraPixelFormat.RGB32:
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_BITDEPTH, 0)
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_RGB, 2)
            elif pixel_format == CameraPixelFormat.RGB48:
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_BITDEPTH, 1)
                self._camera.put_Option(toupcam.TOUPCAM_OPTION_RGB, 1)
            else:
                raise ValueError(f"Unsupported pixel format: {pixel_format=}")

        # NOTE(imo): Ideally we'd query pixel_format from the device instead of storing the state here, but it's
        # impossible to do so - the settings for a particular depth are not unique.  EG MONO12 and MONO14 both
        # have the same settings.  I'm not sure how this works?  But just store the pixel format here...
        self._pixel_format = pixel_format

    def set_pixel_format(self, pixel_format: CameraPixelFormat):
        if pixel_format == self._pixel_format:
            self._log.debug(f"set_pixel_format: already {pixel_format}, skipping")
            return

        with self._pause_streaming():
            self._raw_set_pixel_format(pixel_format)
            self.set_black_level(self._config.default_black_level)
        self._update_internal_settings()

    def get_pixel_format(self) -> CameraPixelFormat:
        return self._pixel_format

    def get_available_pixel_formats(self) -> Sequence[CameraPixelFormat]:
        raise NotImplementedError("get_available_pixel_formats is not implemented for Toupcam")

    # ------------------------------------------------------------------
    # Camera mode == Conversion Gain mode for ToupcamCamera.
    # Models with TOUPCAM_FLAG_CG expose LCG/HCG; models with
    # TOUPCAM_FLAG_CGHDR additionally expose HDR. Models with neither
    # have no selectable camera mode.
    # ------------------------------------------------------------------

    def get_available_camera_modes(self) -> List[str]:
        """Return the selectable gain (conversion gain) modes for this camera."""
        if self._capabilities.has_cghdr:
            return ["LCG", "HCG", "HDR"]
        elif self._capabilities.has_conversion_gain:
            return ["LCG", "HCG"]
        return []

    def get_camera_mode(self) -> Optional[str]:
        """Return the current gain mode, or None if the camera has no selectable mode."""
        if not self._capabilities.has_conversion_gain:
            return None
        return self._get_gain_mode()

    def set_camera_mode(self, camera_mode: str):
        """Set the gain (conversion gain) mode. No-op on cameras without conversion gain."""
        if not self._capabilities.has_conversion_gain:
            self._log.debug("set_camera_mode: camera has no conversion gain support, ignoring")
            return

        available = self.get_available_camera_modes()
        if camera_mode not in available:
            raise ValueError(f"Unknown camera mode '{camera_mode}'. Available: {available}")

        if camera_mode == self.get_camera_mode():
            self._log.debug(f"set_camera_mode: already {camera_mode}, skipping")
            return

        with self._pause_streaming():
            self._set_gain_mode(camera_mode)
        self._update_internal_settings()
        self._log.info(f"Set camera mode (conversion gain) to {camera_mode}")

    def set_readout_mode(self, readout_mode: CameraReadoutMode):
        """Set the readout mode. ToupcamCamera does not expose readout modes; this is a no-op."""
        pass

    def get_readout_mode(self) -> CameraReadoutMode:
        """Get the current readout mode. Treated as a fixed GLOBAL mode."""
        return CameraReadoutMode.GLOBAL

    def get_available_readout_modes(self) -> Sequence[CameraReadoutMode]:
        """Get the list of supported readout modes."""
        return [CameraReadoutMode.GLOBAL]

    def set_auto_exposure(self, enabled: bool):
        try:
            self._camera.put_AutoExpoEnable(enabled)
        except toupcam.HRESULTException as ex:
            self._log.exception("Unable to set auto exposure: " + repr(ex))
            raise

    def _raw_set_frame_format(self, data_format: CameraFrameFormat):
        if data_format == CameraFrameFormat.RGB:
            self._camera.put_Option(
                toupcam.TOUPCAM_OPTION_RAW, ToupcamCamera.TOUPCAM_OPTION_RAW_RGB_VAL
            )  # 0 is RGB mode, 1 is RAW mode
        elif data_format == CameraFrameFormat.RAW:
            self._camera.put_Option(
                toupcam.TOUPCAM_OPTION_RAW, ToupcamCamera.TOUPCAM_OPTION_RAW_RAW_VAL
            )  # 1 is RAW mode, 0 is RGB mode

    def set_frame_format(self, data_format: CameraFrameFormat):
        if data_format == self.get_frame_format():
            self._log.debug(f"set_frame_format: already {data_format}, skipping")
            return

        with self._pause_streaming():
            self._raw_set_frame_format(data_format)
        self._update_internal_settings()

    def get_frame_format(self) -> CameraFrameFormat:
        camera_val = self._camera.get_Option(toupcam.TOUPCAM_OPTION_RAW)

        if camera_val == ToupcamCamera.TOUPCAM_OPTION_RAW_RAW_VAL:
            return CameraFrameFormat.RAW
        elif camera_val == ToupcamCamera.TOUPCAM_OPTION_RAW_RGB_VAL:
            return CameraFrameFormat.RGB
        else:
            raise ValueError(f"Camera returned unknown frame format: value={camera_val}")

    def set_binning(self, binning_factor_x: int, binning_factor_y: int):
        if (binning_factor_x, binning_factor_y) == self._binning:
            self._log.debug(f"set_binning: already {self._binning}, skipping")
            return

        with self._pause_streaming():
            if (binning_factor_x, binning_factor_y) not in self._capabilities.binning_to_resolution:
                raise ValueError(f"Binning ({binning_factor_x},{binning_factor_y}) not supported by camera")
            width, height = self._capabilities.binning_to_resolution[(binning_factor_x, binning_factor_y)]
            self._raw_set_resolution(width, height)
            self._binning = (binning_factor_x, binning_factor_y)
            self._log.debug(f"Setting binning to {binning_factor_x},{binning_factor_y} -> {width},{height}")

            # We will disable hardware cropping until hardware trigger issue is resolved.
            # old_binning = self._binning
            # self._binning = (binning_factor_x, binning_factor_y)
            # old_roi = self.get_region_of_interest()

        # new_roi = AbstractCamera.calculate_new_roi_for_binning(old_binning, old_roi, self._binning)
        # self._log.debug(f"Changing roi from {old_roi=} to {new_roi=} to keep FOV the same after resolution change.")
        # self.set_region_of_interest(*new_roi)

        self._update_internal_settings()

    def _raw_set_resolution(self, width, height):
        try:
            self._camera.put_Size(width, height)
        except toupcam.HRESULTException as ex:
            err_type = hresult_checker(ex, "E_INVALIDARG", "E_BUSY", "E_ACCESDENIED", "E_UNEXPECTED")
            if err_type == "E_INVALIDARG":
                self._log.exception(f"Resolution ({width},{height}) not supported by camera")
            else:
                self._log.exception(f"Resolution cannot be set due to error: " + err_type)
            raise

    def get_temperature(self):
        try:
            return self._camera.get_Temperature() / 10
        except toupcam.HRESULTException as ex:
            error_type = hresult_checker(ex)
            self._log.exception("Could not get temperature, error: " + error_type)
            raise

    def set_temperature(self, temperature):
        try:
            self._camera.put_Temperature(int(temperature * 10))
        except toupcam.HRESULTException as ex:
            error_type = hresult_checker(ex)
            self._log.exception("Unable to set temperature: " + error_type)
            raise

    def _set_fan_speed(self, speed):
        try:
            self._camera.put_Option(toupcam.TOUPCAM_OPTION_FAN, speed)
        except toupcam.HRESULTException as ex:
            error_type = hresult_checker(ex)
            self._log.exception("Unable to set fan speed: " + error_type)
            raise

    def _set_trigger_width_mode(self):
        self._camera.IoControl(1, toupcam.TOUPCAM_IOCONTROLTYPE_SET_PWMSOURCE, 1)  # set PWM source to GPIO0
        self._camera.IoControl(1, toupcam.TOUPCAM_IOCONTROLTYPE_SET_TRIGGERSOURCE, 4)  # trigger source to PWM

    def _set_gain_mode(self, mode):
        if mode == "LCG":
            self._camera.put_Option(toupcam.TOUPCAM_OPTION_CG, 0)
        elif mode == "HCG":
            self._camera.put_Option(toupcam.TOUPCAM_OPTION_CG, 1)
        elif mode == "HDR":
            self._camera.put_Option(toupcam.TOUPCAM_OPTION_CG, 2)
    
    def _get_gain_mode(self):
        cg_mode = self._camera.get_Option(toupcam.TOUPCAM_OPTION_CG)
        if cg_mode == 0:
            return "LCG"
        elif cg_mode == 1:
            return "HCG"
        elif cg_mode == 2:
            return "HDR"
        else:
            raise ValueError(f"Camera returned unknown gain mode: value={cg_mode}")
    
    def set_trigger_duration_us(self, trigger_duration_us: int):
        self._trigger_duration_us = trigger_duration_us
        
    def send_trigger(self, illumination_time: Optional[float] = None):
        if self.get_acquisition_mode() == CameraAcquisitionMode.HARDWARE_TRIGGER and not self._hw_trigger_fn:
            raise RuntimeError("In HARDWARE_TRIGGER mode, but no hw trigger function given.")

        if not self.get_ready_for_trigger():
            raise RuntimeError(
                f"Requested trigger too early (last trigger was {time.time() - self._last_trigger_timestamp} [s] ago), refusing."
            )

        if self.get_acquisition_mode() == CameraAcquisitionMode.HARDWARE_TRIGGER:
            self._log.debug(f"Sending hardware trigger with {illumination_time=}")
            self._hw_trigger_fn(illumination_time)
        elif self.get_acquisition_mode() == CameraAcquisitionMode.SOFTWARE_TRIGGER:
            self._log.debug("Sending software trigger..")
            # Watchdogged: a wedged Trigger() would hang before the worker's
            # frame-wait timeout could ever fire (that only guards the wait AFTER
            # the trigger returns), so bound this native call itself.
            self._sdk.call("Trigger", lambda: self._camera.Trigger(1))

        self._last_trigger_timestamp = time.time()
        self._trigger_sent = True

    def get_ready_for_trigger(self) -> bool:
        # TODO(imo): Should we pass in the timeout?  This might be fine since it's calculated based on the exposure time.
        trigger_timeout_s = 1.5 * self._get_raw_exposure_time() / 1000 * 1.02 + 4
        trigger_age = time.time() - self._last_trigger_timestamp
        trigger_too_old = trigger_age > trigger_timeout_s
        trigger_sent = self._trigger_sent
        if trigger_sent and trigger_too_old:
            self._log.warning(
                f"Previous software trigger timed out after {trigger_timeout_s} [s]. Assuming it failed and allowing re-trigger."
            )
            self._trigger_sent = False
        elif trigger_sent:
            return False
        return True

    def _stop_exposure(self):
        if self.get_is_streaming() and self._trigger_sent == True:
            self._camera.Trigger(0)
            self._trigger_sent = False
        else:
            pass

    def get_strobe_time(self) -> float:
        # Use both strobe_time_us and trigger_delay_us here because our notion of "strobe time" is when the
        # last row first starts exposing.  For the toupcam, this happens after trigger delay + strobe time.
        #
        # For software lighting, sleeping get_strobe_time() + get_exposure_time() works.  For hardware triggering,
        # we need to ignore trigger_delay_us since the camera itself imposes that delay after it sees the trigger.
        return (self._strobe_info.strobe_time_us + self._strobe_info.trigger_delay_us) / 1000.0

    def set_region_of_interest(self, offset_x: int, offset_y: int, width: int, height: int):
        roi_offset_x = control.utils.truncate_to_interval(offset_x, 2)
        roi_offset_y = control.utils.truncate_to_interval(offset_y, 2)
        roi_width = control.utils.truncate_to_interval(width, 2)
        roi_height = control.utils.truncate_to_interval(height, 2)

        # Track for reopen() recovery (get_region_of_interest reads the handle, which
        # is unusable once wedged).
        self._roi = (roi_offset_x, roi_offset_y, roi_width, roi_height)

        if (roi_offset_x, roi_offset_y, roi_width, roi_height) == self.get_region_of_interest():
            self._log.debug(f"set_region_of_interest: already {(roi_offset_x, roi_offset_y, roi_width, roi_height)}, skipping")
            return

        with self._pause_streaming():
            try:
                self._camera.put_Roi(roi_offset_x, roi_offset_y, roi_width, roi_height)
            except toupcam.HRESULTException as ex:
                self._log.exception("ROI bounds invalid, not changing ROI.")

        self._update_internal_settings()

    def get_binning(self) -> Tuple[int, int]:
        return self._binning

    def get_binning_options(self) -> Sequence[Tuple[int, int]]:
        return self._capabilities.binning_to_resolution.keys()

    def get_resolution(self) -> Tuple[int, int]:
        return self._capabilities.binning_to_resolution[self._binning]

    def get_pixel_size_unbinned_um(self) -> float:
        return self.PIXEL_SIZE_UM

    def get_pixel_size_binned_um(self) -> float:
        return (
            self.PIXEL_SIZE_UM * self.get_binning()[0]
        )  # We will use the same binning factor in width and height for now

    def get_analog_gain(self) -> float:
        return self._toupcam_gain_to_user(self._camera.get_ExpoAGain())

    def get_gain_range(self) -> CameraGainRange:
        (min_gain, max_gain, default_gain) = self._camera.get_ExpoAGainRange()
        return CameraGainRange(
            min_gain=self._toupcam_gain_to_user(min_gain), max_gain=self._toupcam_gain_to_user(max_gain), gain_step=0.01
        )

    def read_camera_frame(self):
        # TODO(imo): Seems like the timeout should be something passed in, not hard coded.
        timeout_s = (self.get_exposure_time() / 1000) * 1.02 + 4
        timeout_end_time_s = time.time() + timeout_s
        starting_frame_id = self.get_frame_id()

        while time.time() < timeout_end_time_s:
            if self.get_frame_id() != starting_frame_id:
                return self._current_frame
            time.sleep(0.001)

        self._log.error(f"Timed out after {timeout_s} [s] waiting for a frame.")

        return None

    def get_frame_id(self) -> int:
        return self._current_frame.frame_id if self._current_frame else -1

    def get_white_balance_gains(self) -> Tuple[float, float, float]:
        return self._camera.get_WhiteBalanceGain()

    def set_white_balance_gains(self, red_gain: float, green_gain: float, blue_gain: float):
        self._camera.put_WhiteBalanceGain((red_gain, green_gain, blue_gain))

    def set_auto_white_balance_gains(self) -> Tuple[float, float, float]:
        self._camera.AwbInit()
        return self.get_white_balance_gains()

    _BLACK_LEVEL_MAPPING = {
        (CameraFrameFormat.RAW, CameraPixelFormat.MONO8): 1,
        (CameraFrameFormat.RAW, CameraPixelFormat.MONO12): 16,
        (CameraFrameFormat.RAW, CameraPixelFormat.MONO14): 64,
        (CameraFrameFormat.RAW, CameraPixelFormat.MONO16): 256,
        # TODO(imo): We didn't set a black level factor if outside of 1 of the 4 options above, but still used the factor.  Is the mapping below correct, or is black level ignored for RGB?
        (CameraFrameFormat.RGB, CameraPixelFormat.MONO8): 1,
        (CameraFrameFormat.RGB, CameraPixelFormat.MONO12): 16,
        (CameraFrameFormat.RGB, CameraPixelFormat.MONO14): 64,
        (CameraFrameFormat.RGB, CameraPixelFormat.MONO16): 256,
        (CameraFrameFormat.RGB, CameraPixelFormat.RGB24): 1,  # Bit depth of 8 -> same as MONO8
        (CameraFrameFormat.RGB, CameraPixelFormat.RGB32): 1,  # Bit depth of 8 -> same as MONO8
        (CameraFrameFormat.RGB, CameraPixelFormat.RGB48): 256,  # Bit depth of 16 -> same as MONO16
    }

    def _get_black_level_factor(self):
        frame_and_format = (self.get_frame_format(), self.get_pixel_format())
        if frame_and_format not in ToupcamCamera._BLACK_LEVEL_MAPPING:
            raise ValueError(f"Unknown combo for black level: {frame_and_format=}")

        return ToupcamCamera._BLACK_LEVEL_MAPPING[frame_and_format]

    _PIXEL_SIZE_MAPPING = {
        (CameraFrameFormat.RAW, CameraPixelFormat.MONO8): 1,
        (CameraFrameFormat.RAW, CameraPixelFormat.MONO12): 2,
        (CameraFrameFormat.RAW, CameraPixelFormat.MONO14): 2,
        (CameraFrameFormat.RAW, CameraPixelFormat.MONO16): 2,
        (CameraFrameFormat.RGB, CameraPixelFormat.MONO8): 1,
        (CameraFrameFormat.RGB, CameraPixelFormat.MONO12): 2,
        (CameraFrameFormat.RGB, CameraPixelFormat.MONO14): 2,
        (CameraFrameFormat.RGB, CameraPixelFormat.MONO16): 2,
        (CameraFrameFormat.RGB, CameraPixelFormat.RGB24): 3,
        (CameraFrameFormat.RGB, CameraPixelFormat.RGB32): 4,
        (CameraFrameFormat.RGB, CameraPixelFormat.RGB48): 6,
    }

    def _get_pixel_size_in_bytes(self):
        frame_and_format = (self.get_frame_format(), self.get_pixel_format())
        if frame_and_format not in ToupcamCamera._PIXEL_SIZE_MAPPING:
            raise ValueError(f"Unknown combo for pixel size: {frame_and_format=}")

        return ToupcamCamera._PIXEL_SIZE_MAPPING[frame_and_format]

    def get_black_level(self) -> float:
        if not self._capabilities.has_black_level:
            raise NotImplementedError("This toupcam does not have black level setting.")

        raw_black_level = self._camera.get_Option(toupcam.TOUPCAM_OPTION_BLACKLEVEL)

        return raw_black_level / self._get_black_level_factor()

    def set_black_level(self, black_level: float):
        if not self._capabilities.has_black_level:
            raise NotImplementedError("This toupcam does not have black level setting.")
        raw_black_level = black_level * self._get_black_level_factor()

        try:
            self._camera.put_Option(toupcam.TOUPCAM_OPTION_BLACKLEVEL, raw_black_level)
        except toupcam.HRESULTException as ex:
            print("put blacklevel fail, hr=0x{:x}".format(ex.hr))

    def _set_acquisition_mode_imp(self, acquisition_mode: CameraAcquisitionMode):
        if acquisition_mode == CameraAcquisitionMode.CONTINUOUS:
            trigger_option_value = 0
        elif acquisition_mode == CameraAcquisitionMode.SOFTWARE_TRIGGER:
            trigger_option_value = 1
        elif acquisition_mode == CameraAcquisitionMode.HARDWARE_TRIGGER:
            trigger_option_value = 2
        else:
            raise ValueError(f"Do not know how to handle {acquisition_mode=}")
        self._camera.put_Option(toupcam.TOUPCAM_OPTION_TRIGGER, trigger_option_value)

        if acquisition_mode == CameraAcquisitionMode.HARDWARE_TRIGGER:
            if HARDWARE_TRIGGER_MODE == HardwareTriggerMode.LEVEL:
                try:
                    self._camera.put_Option(toupcam.TOUPCAM_OPTION_TRIGGER, 2)
                except toupcam.HRESULTException as ex:
                    error_type = hresult_checker(ex)
                    # TODO(imo): Propagate error in some way and handle
                    self._log.error("Unable to set option_trigger to 2: " + error_type)

                try:
                    # set IO controltype to PWM mode
                    self._camera.IoControl(0, toupcam.TOUPCAM_IOCONTROLTYPE_SET_TRIGGERSOURCE, 4)
                    self._camera.IoControl(2, toupcam.TOUPCAM_IOCONTROLTYPE_SET_GPIODIR, 0)
                    self._camera.IoControl(2, toupcam.TOUPCAM_IOCONTROLTYPE_SET_PWMSOURCE, 1)
                except toupcam.HRESULTException as ex:
                    error_type = hresult_checker(ex)
                    # TODO(imo): Propagate error in some way and handle
                    self._log.error("Unable to select trigger source: " + error_type)
            else:
                # select trigger source to GPIO0
                try:
                    self._camera.IoControl(1, toupcam.TOUPCAM_IOCONTROLTYPE_SET_TRIGGERSOURCE, 1)
                except toupcam.HRESULTException as ex:
                    error_type = hresult_checker(ex)
                    self._log.exception("Unable to select trigger source: " + error_type)
                    raise
                # set GPIO1 to trigger wait
                try:
                    self._camera.IoControl(3, toupcam.TOUPCAM_IOCONTROLTYPE_SET_OUTPUTMODE, 0)
                    self._camera.IoControl(3, toupcam.TOUPCAM_IOCONTROLTYPE_SET_OUTPUTINVERTER, 0)
                except toupcam.HRESULTException as ex:
                    error_type = hresult_checker(ex)
                    self._log.exception("Unable to set GPIO1 for trigger ready: " + error_type)
                    raise
        # Re-set exposure time to force strobe to get set to the remote.
        if self._raw_camera_stream_started:
            self.set_exposure_time(self.get_exposure_time())

        # Track for reopen() recovery (get_acquisition_mode reads the handle, which is
        # unusable once wedged).
        self._acquisition_mode = acquisition_mode

    def get_acquisition_mode(self) -> CameraAcquisitionMode:
        trigger_option_value = self._camera.get_Option(toupcam.TOUPCAM_OPTION_TRIGGER)
        if trigger_option_value == 0:
            return CameraAcquisitionMode.CONTINUOUS
        elif trigger_option_value == 1:
            return CameraAcquisitionMode.SOFTWARE_TRIGGER
        elif trigger_option_value == 2:
            return CameraAcquisitionMode.HARDWARE_TRIGGER
        else:
            raise ValueError(f"Received unknown trigger option from toupcam: {trigger_option_value}")

    def get_region_of_interest(self) -> Tuple[int, int, int, int]:
        return self._camera.get_Roi()

    def start_fast_acquisition_frame_grabbing(
        self,
        frame_rate_hz: float,
        n_frames_expected: int = 0,
        frame_callback: Optional[Callable] = None,
        acquisition_mode: Optional[CameraAcquisitionMode] = None,
    ):
        """Start fast acquisition frame grabbing for FastAcquisitionController.

        Puts the camera into hardware trigger mode and diverts incoming frames
        to frame_callback instead of the normal CameraFrame propagation path.

        Args:
            frame_rate_hz: Expected frame rate (informational for ToupCam).
            n_frames_expected: Expected number of frames (informational).
            frame_callback: Receives (frame_bytes: bytes, metadata: dict).
            acquisition_mode: If provided and differs from current mode, switches to it.
        """
        if frame_callback is None:
            raise ValueError("frame_callback is required for fast acquisition")

        if acquisition_mode is not None and acquisition_mode != self.get_acquisition_mode():
            self._set_acquisition_mode_imp(acquisition_mode)
            self._log.info(f"Acquisition mode changed to {acquisition_mode} for fast acquisition")

        current_mode = self.get_acquisition_mode()
        if current_mode != CameraAcquisitionMode.HARDWARE_TRIGGER:
            raise ValueError(
                f"Fast acquisition requires HARDWARE_TRIGGER mode, but camera is in {current_mode}"
            )

        with self._raw_frame_callback_lock:
            self._fast_acquisition_callback = frame_callback
            self._fast_acquisition_frame_index = 0
            self._fast_acquisition_active = True

        self._log.info(
            f"Fast acquisition frame grabbing started "
            f"(frame_rate_hz={frame_rate_hz}, n_frames_expected={n_frames_expected})"
        )

    def stop_fast_acquisition_frame_grabbing(self):
        """Stop fast acquisition frame grabbing.

        Clears the fast acquisition flag so subsequent frames go through
        the normal CameraFrame propagation path again.
        """
        with self._raw_frame_callback_lock:
            was_active = self._fast_acquisition_active
            self._fast_acquisition_active = False
            self._fast_acquisition_callback = None
            self._fast_acquisition_frame_index = 0

        if was_active:
            self._log.info("Fast acquisition frame grabbing stopped")
        else:
            self._log.debug("stop_fast_acquisition_frame_grabbing called but was not active")
