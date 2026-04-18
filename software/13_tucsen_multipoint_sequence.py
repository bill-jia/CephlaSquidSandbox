"""
Bare-SDK reproduction of the TUCAM call sequence a multipoint acquisition runs
after the user presses Start. Bypasses the entire squid stack (LiveController,
MultiPointWorker, ObservationStateController, etc.) so any stray frames we see
here are attributable to the SDK/camera alone.

Sequence (mirrors control.camera_tucsen.TucsenCamera + control.core.multi_point_worker):
  setup, once:
    TUCAM_Api_Init
    TUCAM_Dev_Open
    TUCAM_GenICam_SetElementValue("TriggerMode", 2, Enumeration)   # "Software"
    TUCAM_GenICam_SetElementValue("ExposureTime", <us>, Integer)
    TUCAM_Buf_Alloc
    TUCAM_Cap_Start(TUCCM_SEQUENCE)
    spawn read thread looping on TUCAM_Buf_WaitForFrame

  per capture, inside the loop (what acquire_camera_image drives):
    TUCAM_GenICam_SetElementValue("ExposureTime", <us>, Integer)   # channel switch
    (illumination settle sleep — no SDK call)
    TUCAM_GenICam_SetElementValue("TriggerSoftwarePulse", 1, Command)
    wait for the read thread to signal a frame landed

  teardown:
    TUCAM_Buf_AbortWait
    TUCAM_Cap_Stop
    TUCAM_Buf_Release
    TUCAM_Dev_Close
    TUCAM_Api_Uninit

The script counts triggers sent vs frames received per trigger; any frame that
arrives when triggers_sent <= frames_received is flagged STRAY.

Run:
    conda activate squid
    python software/13_tucsen_multipoint_sequence.py
(Or cd into software/ first.)
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import c_char_p, c_int32, c_void_p, create_string_buffer, pointer

_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_HERE) == "software":
    os.chdir(_HERE)
else:
    sw = os.path.join(_HERE, "software")
    if os.path.isdir(sw):
        os.chdir(sw)
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())

import numpy as np
from control.TUCam import (  # noqa: E402
    TUCAMRET,
    TUCAM_CAPTURE_MODES,
    TUCAM_ELEMENT,
    TUCAM_FRAME,
    TUCAM_INIT,
    TUCAM_OPEN,
    TUELEM_TYPE,
    TUFRM_FORMATS,
    TUXML_DEVICE,
    TUCAM_Api_Init,
    TUCAM_Api_Uninit,
    TUCAM_Buf_Alloc,
    TUCAM_Buf_AbortWait,
    TUCAM_Buf_Release,
    TUCAM_Buf_WaitForFrame,
    TUCAM_Cap_Start,
    TUCAM_Cap_Stop,
    TUCAM_Dev_Close,
    TUCAM_Dev_Open,
    TUCAM_GenICam_ElementAttr,
    TUCAM_GenICam_SetElementValue,
)

# ---------- run shape ----------

N_POSITIONS = 3
CHANNELS = [
    {"name": "BF",    "exposure_us":  10_000},
    {"name": "FL488", "exposure_us":  50_000},
    {"name": "FL647", "exposure_us": 100_000},
]
ILLUMINATION_SETTLE_S = 0.018  # machine_config: illumination_settle_ms=18

# Toggle via env var: WRITE_EXPOSURE_INLOOP=0 skips mid-stream ExposureTime writes.
# Default is 1 (matches production multipoint path).
WRITE_EXPOSURE_INLOOP = os.environ.get("WRITE_EXPOSURE_INLOOP", "1") == "1"

# PAUSE_STYLE_EXPOSURE=1 wraps each mid-loop ExposureTime write in a Cap_Stop /
# Cap_Start cycle (mirrors what TucsenCamera._pause_streaming does for other
# GenICam writes). This is the proposed fix pattern.
PAUSE_STYLE_EXPOSURE = os.environ.get("PAUSE_STYLE_EXPOSURE", "0") == "1"


# ---------- GenICam helpers (same shape as camera_tucsen) ----------

def _attr(handle, name: str):
    node = TUCAM_ELEMENT()
    name_buf = ctypes.create_string_buffer(name.encode("utf-8"))
    node.pName = ctypes.cast(name_buf, c_char_p)
    ret = TUCAM_GenICam_ElementAttr(
        handle, pointer(node), node.pName, TUXML_DEVICE.TU_CAMERA_XML.value
    )
    node._name_buf = name_buf  # keep alive
    return node, ret


def set_int(handle, name: str, value: int):
    node, ret = _attr(handle, name)
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        return ret
    node.uValue.Int64.nVal = int(value)
    return TUCAM_GenICam_SetElementValue(
        handle, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value
    )


def set_enum(handle, name: str, idx: int):
    node, ret = _attr(handle, name)
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        return ret
    node.uValue.Int64.nVal = int(idx)
    return TUCAM_GenICam_SetElementValue(
        handle, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value
    )


def send_command(handle, name: str):
    """Write 1 to a TU_ElemCommand (e.g. TriggerSoftwarePulse)."""
    node, ret = _attr(handle, name)
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        return ret
    node.uValue.Int64.nVal = 1
    return TUCAM_GenICam_SetElementValue(
        handle, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value
    )


# ---------- safe wait (OleDLL auto-raises OSError on HRESULT-style codes) ----

def safe_wait_for_frame(handle, frame: TUCAM_FRAME, timeout_ms: int) -> int:
    """Returns raw uint32 SDK code. SUCCESS=0x1, TIMEOUT=0x80000208, etc."""
    try:
        ret = TUCAM_Buf_WaitForFrame(handle, pointer(frame), c_int32(timeout_ms))
        return ret.value if isinstance(ret, TUCAMRET) else int(ret) & 0xFFFFFFFF
    except OSError as e:
        return int(e.winerror) & 0xFFFFFFFF
    except ValueError:
        return 0  # unknown code; treat as non-SUCCESS


# ---------- read thread (mirrors TucsenCamera._wait_for_frame) ------------

class ReadThread(threading.Thread):
    def __init__(self, handle, frame, on_frame_fn):
        super().__init__(daemon=True)
        self._handle = handle
        self._frame = frame
        self._on_frame_fn = on_frame_fn
        self._keep = threading.Event()
        self._keep.set()
        self.frames_received = 0

    def stop(self):
        self._keep.clear()
        try:
            TUCAM_Buf_AbortWait(self._handle)
        except Exception:
            pass

    def run(self):
        while self._keep.is_set():
            ret = safe_wait_for_frame(self._handle, self._frame, 1000)
            if ret != TUCAMRET.TUCAMRET_SUCCESS.value:
                continue
            if not self._frame.pBuffer or self._frame.uiImgSize == 0:
                continue
            self.frames_received += 1
            try:
                self._on_frame_fn(self._frame, self.frames_received)
            except Exception as e:
                print(f"    on_frame callback error: {e}")


# ---------- main sequence ----------

def main() -> int:
    # ---- setup ----
    print("[setup] TUCAM_Api_Init")
    init = TUCAM_INIT(0, b"./control")
    ret = TUCAM_Api_Init(pointer(init))
    print(f"         ret={ret}  cameras found={init.uiCamCount}")
    if init.uiCamCount == 0:
        print("No cameras. Abort.")
        return 2

    print("[setup] TUCAM_Dev_Open")
    opn = TUCAM_OPEN(0, 0)
    ret = TUCAM_Dev_Open(pointer(opn))
    if ret != TUCAMRET.TUCAMRET_SUCCESS or opn.hIdxTUCam == 0:
        print(f"         FAILED ret={ret}")
        TUCAM_Api_Uninit()
        return 3
    handle = opn.hIdxTUCam
    print(f"         handle=0x{handle:X}")

    print("[setup] set TriggerMode <- Software (idx=2)")
    ret = set_enum(handle, "TriggerMode", 2)
    print(f"         ret={ret}")

    initial_exp_us = CHANNELS[0]["exposure_us"]
    print(f"[setup] set ExposureTime <- {initial_exp_us}us")
    ret = set_int(handle, "ExposureTime", initial_exp_us)
    print(f"         ret={ret}")

    print("[setup] TUCAM_Buf_Alloc")
    frame = TUCAM_FRAME()
    frame.pBuffer = 0
    frame.ucFormatGet = TUFRM_FORMATS.TUFRM_FMT_USUAl.value
    frame.uiRsdSize = 1
    ret = TUCAM_Buf_Alloc(handle, pointer(frame))
    print(f"         ret={ret}")
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        TUCAM_Dev_Close(handle)
        TUCAM_Api_Uninit()
        return 4

    print("[setup] TUCAM_Cap_Start(TUCCM_SEQUENCE)")
    ret = TUCAM_Cap_Start(handle, TUCAM_CAPTURE_MODES.TUCCM_SEQUENCE.value)
    print(f"         ret={ret}")
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        TUCAM_Buf_Release(handle)
        TUCAM_Dev_Close(handle)
        TUCAM_Api_Uninit()
        return 5

    # counters + per-frame signalling (tracked in main, not the reader, so pause
    # / restart cycles don't lose count)
    triggers_sent = 0
    frames_received = 0
    stray_count = 0
    last_frame_event = threading.Event()

    def on_frame(frm, _frame_no_ignored):
        nonlocal frames_received, stray_count
        buf = create_string_buffer(frm.uiImgSize)
        ctypes.memmove(buf, c_void_p(frm.pBuffer + frm.usHeader), frm.uiImgSize)
        arr = np.frombuffer(bytes(buf), dtype=np.uint16)
        frames_received += 1
        stray = frames_received > triggers_sent
        if stray:
            stray_count += 1
            print(
                f"    !! STRAY frame #{frames_received}  mean={arr.mean():.1f}  "
                f"triggers_sent={triggers_sent}"
            )
        else:
            print(
                f"       frame #{frames_received}  mean={arr.mean():.1f}  "
                f"triggers_sent={triggers_sent}"
            )
        last_frame_event.set()

    print("[setup] start read thread (TUCAM_Buf_WaitForFrame loop)\n")
    reader = ReadThread(handle, frame, on_frame)
    reader.start()

    def pause_set_exposure_resume(new_exposure_us):
        """Mirror TucsenCamera._pause_streaming: stop reader+Cap_Stop, write,
        Cap_Start, new reader."""
        nonlocal reader
        reader.stop()
        reader.join(timeout=2.0)
        TUCAM_Cap_Stop(handle)
        set_int(handle, "ExposureTime", new_exposure_us)
        TUCAM_Cap_Start(handle, TUCAM_CAPTURE_MODES.TUCCM_SEQUENCE.value)
        reader = ReadThread(handle, frame, on_frame)
        reader.start()

    # Quick quiet-check: before any trigger, we should see NO frames arrive.
    print("[check] 500ms quiet period (no triggers) — expect 0 frames")
    quiet_start = reader.frames_received
    time.sleep(0.5)
    if reader.frames_received != quiet_start:
        print(
            f"         !! {reader.frames_received - quiet_start} frame(s) arrived unprompted"
        )
    else:
        print("         ok (no frames)")
    print()

    # ---- multipoint loop ----
    for pos in range(N_POSITIONS):
        print(f"-- Position {pos + 1}/{N_POSITIONS} --")
        for ch in CHANNELS:
            # channel switch: write ExposureTime (matches production)
            if WRITE_EXPOSURE_INLOOP:
                if PAUSE_STYLE_EXPOSURE:
                    pause_set_exposure_resume(ch["exposure_us"])
                else:
                    set_int(handle, "ExposureTime", ch["exposure_us"])
            # illumination settle (no SDK call; production path sleeps here)
            time.sleep(ILLUMINATION_SETTLE_S)
            # fire software trigger
            triggers_sent += 1
            last_frame_event.clear()
            t0 = time.time()
            dret = send_command(handle, "TriggerSoftwarePulse")
            # wait for the read thread to signal a frame
            timeout_s = max(0.5, ch["exposure_us"] * 5 / 1e6 + 0.5)
            got = last_frame_event.wait(timeout=timeout_s)
            dt_ms = (time.time() - t0) * 1000
            status = "OK" if got else "TIMEOUT"
            print(
                f"    trigger #{triggers_sent} ({ch['name']}, exp={ch['exposure_us']}us): "
                f"dispatch={dret}  frame {status}  dt={dt_ms:6.1f}ms"
            )
        print()

    # Settle: give the reader a moment to report any trailing frames.
    time.sleep(0.3)

    print(
        f"[summary] triggers_sent={triggers_sent}  "
        f"frames_received={frames_received}  "
        f"stray={stray_count}"
    )
    if stray_count or frames_received != triggers_sent:
        print("          !! DIVERGED — lost or extra frames")
    else:
        print("          ok — exactly one frame per trigger, as expected")

    # ---- teardown ----
    print("\n[teardown] stop reader + Cap_Stop + Buf_Release + Dev_Close + Api_Uninit")
    reader.stop()
    reader.join(timeout=2.0)
    TUCAM_Cap_Stop(handle)
    TUCAM_Buf_Release(handle)
    TUCAM_Dev_Close(handle)
    TUCAM_Api_Uninit()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
