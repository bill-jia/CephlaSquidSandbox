"""
Raw-SDK software-trigger diagnostic for the Tucsen Aries (GenICam) camera.

Bypasses the TucsenCamera wrapper and the whole squid stack.  Talks to the
TUCam SDK directly through `control.TUCam` (which loads TUCam.dll via a path
relative to the CWD, so this script chdirs into `software/` before importing).

Goal: isolate which software-trigger mechanism actually delivers a frame on
this camera.  The wrapper-based test returned zero frames for both the
burst and the long-gap case, so something in the GenICam path our driver
uses isn't actually firing the exposure.

This script tries several combinations in sequence.  For each, it sets the
camera up, sends ONE trigger, waits up to 1.5 s for the frame, and reports
the SDK return code + frame stats (mean, max) if a frame came back.

Run:
    conda activate squid
    python software/12_tucsen_sdk_sw_trigger.py
(Or cd into software/ first.)
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import POINTER, byref, c_char_p, c_int32, c_void_p, cast, create_string_buffer, pointer

# TUCam.py loads its DLL with a relative path — so chdir into software/ before importing.
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
    TUCAM_VALUE_INFO,
    TUELEM_TYPE,
    TUFRM_FORMATS,
    TUXML_DEVICE,
    TUCAM_IDINFO,
    TUCAM_Api_Init,
    TUCAM_Api_Uninit,
    TUCAM_Buf_Alloc,
    TUCAM_Buf_AbortWait,
    TUCAM_Buf_Release,
    TUCAM_Buf_WaitForFrame,
    TUCAM_Cap_DoSoftwareTrigger,
    TUCAM_Cap_Start,
    TUCAM_Cap_Stop,
    TUCAM_Dev_Close,
    TUCAM_Dev_GetInfo,
    TUCAM_Dev_Open,
    TUCAM_GenICam_ElementAttr,
    TUCAM_GenICam_GetElementValue,
    TUCAM_GenICam_SetElementValue,
)

EXPOSURE_US = 20_000  # 20 ms


# ---------------------------------------------------------------------------- #
# small helpers                                                                #
# ---------------------------------------------------------------------------- #


# TUCam.dll is loaded via OleDLL, so ctypes auto-raises OSError whenever a
# function returns a high-bit-set "HRESULT-style" code (e.g. TIMEOUT=0x80000208).
# Don't override restype — instead catch OSError in wait_one_frame and extract
# the raw code via .winerror.


def retname(ret) -> str:
    try:
        if isinstance(ret, TUCAMRET):
            val = ret.value
        else:
            val = int(ret) & 0xFFFFFFFF
        return f"{TUCAMRET(val).name}(0x{val:08X})"
    except Exception:
        return f"0x{val:08X}"


def device_info(handle, info_id: int) -> str:
    info = TUCAM_VALUE_INFO(info_id, 0, 0, 0)
    ret = TUCAM_Dev_GetInfo(handle, pointer(info))
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        return f"<err {retname(ret)}>"
    try:
        return info.pText.decode("utf-8") if info.pText else ""
    except Exception:
        return f"<raw {info.nValue}>"


def _attr(handle, name: str) -> tuple[TUCAM_ELEMENT, int]:
    node = TUCAM_ELEMENT()
    name_buf = ctypes.create_string_buffer(name.encode("utf-8"))
    node.pName = ctypes.cast(name_buf, c_char_p)
    ret = TUCAM_GenICam_ElementAttr(handle, pointer(node), node.pName, TUXML_DEVICE.TU_CAMERA_XML.value)
    # keep a reference so the buffer isn't GCed while the SDK holds the pointer
    node._name_buf = name_buf  # type: ignore[attr-defined]
    return node, ret


def list_enum_entries(handle, name: str) -> dict[int, str]:
    node, ret = _attr(handle, name)
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        print(f"  [enum {name}] ElementAttr failed: {retname(ret)}")
        return {}
    if node.Type != TUELEM_TYPE.TU_ElemEnumeration.value:
        print(f"  [enum {name}] not an enumeration (Type={node.Type})")
        return {}
    entries: dict[int, str] = {}
    if node.pEntries:
        strlist = ctypes.cast(node.pEntries, ctypes.POINTER(ctypes.c_char_p))
        n = node.uValue.Int64.nMax - node.uValue.Int64.nMin + 1
        for i in range(int(n)):
            if strlist[i]:
                entries[int(node.uValue.Int64.nMin) + i] = strlist[i].decode("utf-8")
    return entries


def set_enum(handle, name: str, index: int) -> int:
    node, ret = _attr(handle, name)
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        return ret
    node.uValue.Int64.nVal = int(index)
    return TUCAM_GenICam_SetElementValue(handle, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value)


def set_int(handle, name: str, value: int) -> int:
    node, ret = _attr(handle, name)
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        return ret
    node.uValue.Int64.nVal = int(value)
    return TUCAM_GenICam_SetElementValue(handle, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value)


def get_enum_value_str(handle, name: str) -> str:
    """Return the currently-selected enum entry string (Attr fills in the current index)."""
    node, ret = _attr(handle, name)
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        return f"<attr err {retname(ret)}>"
    if node.Type != TUELEM_TYPE.TU_ElemEnumeration.value:
        return f"<not enum Type={node.Type}>"
    idx = int(node.uValue.Int64.nVal)
    if node.pEntries:
        strlist = ctypes.cast(node.pEntries, ctypes.POINTER(ctypes.c_char_p))
        min_ = int(node.uValue.Int64.nMin)
        n = int(node.uValue.Int64.nMax) - min_ + 1
        if 0 <= idx - min_ < n and strlist[idx - min_]:
            return f"{strlist[idx - min_].decode('utf-8')} (idx={idx})"
    return f"<idx={idx}>"


def execute_command(handle, name: str) -> int:
    """Execute a GenICam command node (write 1 to a TU_ElemCommand element)."""
    node, ret = _attr(handle, name)
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        return ret
    if node.Type != TUELEM_TYPE.TU_ElemCommand.value:
        print(f"  [cmd {name}] exists but is Type={node.Type}, not Command")
    node.uValue.Int64.nVal = 1
    return TUCAM_GenICam_SetElementValue(handle, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value)


# ---------------------------------------------------------------------------- #
# capture plumbing                                                             #
# ---------------------------------------------------------------------------- #


def alloc_and_start(handle, cap_mode: int) -> TUCAM_FRAME:
    frame = TUCAM_FRAME()
    frame.pBuffer = 0
    frame.ucFormatGet = TUFRM_FORMATS.TUFRM_FMT_USUAl.value
    frame.uiRsdSize = 1
    ret = TUCAM_Buf_Alloc(handle, pointer(frame))
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        raise RuntimeError(f"TUCAM_Buf_Alloc failed: {retname(ret)}")
    ret = TUCAM_Cap_Start(handle, cap_mode)
    if ret != TUCAMRET.TUCAMRET_SUCCESS:
        TUCAM_Buf_Release(handle)
        raise RuntimeError(f"TUCAM_Cap_Start failed: {retname(ret)}")
    return frame


def stop_and_release(handle) -> None:
    TUCAM_Buf_AbortWait(handle)
    TUCAM_Cap_Stop(handle)
    TUCAM_Buf_Release(handle)


def wait_one_frame(handle, frame: TUCAM_FRAME, timeout_ms: int) -> tuple[int, float | None]:
    """Returns (sdk_return_code, frame_mean). frame_mean is None on non-SUCCESS.

    OleDLL auto-raises OSError for SDK error codes; we translate that back into
    an unsigned 32-bit code so Test A/B can distinguish TIMEOUT from other errors.
    """
    try:
        ret_enum = TUCAM_Buf_WaitForFrame(handle, pointer(frame), c_int32(timeout_ms))
        ret = ret_enum.value if isinstance(ret_enum, TUCAMRET) else int(ret_enum) & 0xFFFFFFFF
    except OSError as e:
        # OleDLL converts high-bit-set HRESULTs into OSError; extract the raw code.
        ret = int(e.winerror) & 0xFFFFFFFF
        return ret, None
    except ValueError:
        # Enum restype conversion failed on an unknown code — report as 0 (unknown).
        return 0, None
    if ret != TUCAMRET.TUCAMRET_SUCCESS.value:
        return ret, None
    if not frame.pBuffer or frame.uiImgSize == 0:
        return ret, None
    buf = create_string_buffer(frame.uiImgSize)
    ctypes.memmove(buf, c_void_p(frame.pBuffer + frame.usHeader), frame.uiImgSize)
    raw = np.frombuffer(bytes(buf), dtype=np.uint16)
    try:
        raw = raw.reshape((frame.usHeight, frame.usWidth))
    except Exception:
        pass
    return ret, float(raw.mean())


# ---------------------------------------------------------------------------- #
# trigger dispatch variants                                                    #
# ---------------------------------------------------------------------------- #


def trigger_classic(handle) -> int:
    return TUCAM_Cap_DoSoftwareTrigger(handle)


def trigger_cmd(cmd_name: str):
    def send(handle):
        return execute_command(handle, cmd_name)

    send.__name__ = f"trigger_cmd({cmd_name})"
    return send


# ---------------------------------------------------------------------------- #
# test harness                                                                 #
# ---------------------------------------------------------------------------- #


def try_variant(
    handle,
    *,
    label: str,
    trigger_mode_idx: int,
    cap_start_mode: int,
    dispatch_fn,
    n_triggers: int = 3,
    gap_s: float = 0.2,
    frame_timeout_ms: int = 1500,
) -> None:
    print(f"\n--- {label} ---")
    print(f"  set TriggerMode -> idx={trigger_mode_idx}")
    ret = set_enum(handle, "TriggerMode", trigger_mode_idx)
    print(f"    SetElementValue ret={retname(ret)}")
    print(f"    readback: {get_enum_value_str(handle, 'TriggerMode')}")

    print(f"  TUCAM_Cap_Start mode=0x{cap_start_mode:02X}")
    try:
        frame = alloc_and_start(handle, cap_start_mode)
    except RuntimeError as e:
        print(f"    {e}")
        return

    if dispatch_fn is None:
        for i in range(n_triggers):
            t0 = time.time()
            fret, fmean = wait_one_frame(handle, frame, frame_timeout_ms)
            dt_ms = (time.time() - t0) * 1000
            if fret == TUCAMRET.TUCAMRET_SUCCESS.value:
                print(
                    f"    trigger {i + 1}: wait={retname(fret)}"
                    f" dt={dt_ms:6.1f}ms mean={fmean:.1f} (size={frame.usWidth}x{frame.usHeight})"
                )
            else:
                print(
                    f"    trigger {i + 1}: wait={retname(fret)}"
                    f" dt={dt_ms:6.1f}ms  NO FRAME"
                )
        stop_and_release(handle)
        return

    # Test B: idle gating — after Cap_Start, before any triggers, frames should NOT arrive.
    print("  [Test B: idle gating] wait for frames without triggering (expect all timeouts):")
    for i in range(5):
        t0 = time.time()
        fret, fmean = wait_one_frame(handle, frame, 200)
        dt_ms = (time.time() - t0) * 1000
        if fret == TUCAMRET.TUCAMRET_SUCCESS.value:
            print(f"    idle-wait {i + 1}: wait={retname(fret)} dt={dt_ms:6.1f}ms mean={fmean:.1f}  !! STRAY FRAME !!")
        else:
            print(f"    idle-wait {i + 1}: wait={retname(fret)} dt={dt_ms:6.1f}ms  (expected timeout)")

    try:
        for i in range(n_triggers):
            if i > 0 and gap_s > 0:
                time.sleep(gap_s)
            t0 = time.time()
            dret = dispatch_fn(handle)
            fret, fmean = wait_one_frame(handle, frame, frame_timeout_ms)
            dt_ms = (time.time() - t0) * 1000
            if fret == TUCAMRET.TUCAMRET_SUCCESS.value:
                print(
                    f"    trigger {i + 1}: dispatch={retname(dret)} wait={retname(fret)}"
                    f" dt={dt_ms:6.1f}ms mean={fmean:.1f} (size={frame.usWidth}x{frame.usHeight})"
                )
            else:
                print(
                    f"    trigger {i + 1}: dispatch={retname(dret)} wait={retname(fret)}"
                    f" dt={dt_ms:6.1f}ms  NO FRAME"
                )

            # Test A: one-trigger-one-frame — after each received frame, wait again with no
            # new trigger. Any further SUCCESS means the camera produced >1 frame per trigger.
            extra_count = 0
            while True:
                t1 = time.time()
                fret2, fmean2 = wait_one_frame(handle, frame, 300)
                dt2_ms = (time.time() - t1) * 1000
                if fret2 == TUCAMRET.TUCAMRET_SUCCESS.value:
                    extra_count += 1
                    print(
                        f"      extra #{extra_count}: wait={retname(fret2)} dt={dt2_ms:6.1f}ms"
                        f" mean={fmean2:.1f}  !! EXTRA FRAME (no trigger sent) !!"
                    )
                    if extra_count >= 10:
                        print("      (capping extra-frame drain at 10)")
                        break
                else:
                    if extra_count == 0:
                        print(f"      post-trigger drain clean: wait={retname(fret2)} dt={dt2_ms:6.1f}ms")
                    else:
                        print(f"      drain done after {extra_count} extras: wait={retname(fret2)} dt={dt2_ms:6.1f}ms")
                    break
    finally:
        stop_and_release(handle)


# ---------------------------------------------------------------------------- #
# main                                                                         #
# ---------------------------------------------------------------------------- #


def main() -> int:
    init = TUCAM_INIT(0, "./control".encode("utf-8"))
    ret = TUCAM_Api_Init(pointer(init))
    print(f"TUCAM_Api_Init: {retname(ret)}, cameras found: {init.uiCamCount}")
    if init.uiCamCount == 0:
        print("No cameras. Abort.")
        return 2

    opn = TUCAM_OPEN(0, 0)
    ret = TUCAM_Dev_Open(pointer(opn))
    if ret != TUCAMRET.TUCAMRET_SUCCESS or opn.hIdxTUCam == 0:
        print(f"TUCAM_Dev_Open failed: {retname(ret)}")
        TUCAM_Api_Uninit()
        return 3
    handle = opn.hIdxTUCam
    print("Opened camera at index 0")

    try:
        model = device_info(handle, TUCAM_IDINFO.TUIDI_CAMERA_MODEL.value)
        print(f"Model: {model}")

        # Configure exposure via GenICam (the driver writes an Integer in microseconds).
        ret = set_int(handle, "ExposureTime", EXPOSURE_US)
        print(f"ExposureTime <- {EXPOSURE_US}us: {retname(ret)}")

        tm_entries = list_enum_entries(handle, "TriggerMode")
        print(f"TriggerMode enum: {tm_entries}")

        # Locate the 'Software' index. If it's not present, nothing here will work.
        sw_idx = next(
            (idx for idx, name in tm_entries.items() if name.lower() == "software"),
            None,
        )
        if sw_idx is None:
            print("No 'Software' entry in TriggerMode — cannot proceed with software trigger")
            return 4
        print(f"  -> 'Software' is at index {sw_idx}")

        # Matrix of combinations to try.  TUCCM_SEQUENCE=0x00, TUCCM_TRIGGER_SOFTWARE=0x04.
        combos = [
            # Classic TUCAM_Cap_DoSoftwareTrigger, cap_start=TRIGGER_SOFTWARE (driver's current choice)
            ("classic DoSoftwareTrigger + TUCCM_TRIGGER_SOFTWARE",
             sw_idx, TUCAM_CAPTURE_MODES.TUCCM_SEQUENCE.value, trigger_cmd("TriggerSoftwarePulse")),
            # ("classic DoSoftwareTrigger + TUCCM_TRIGGER_SOFTWARE",
            #  sw_idx, TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_SOFTWARE.value, trigger_classic),
            # # Classic, cap_start=SEQUENCE (what GenICam typically expects)
            # ("classic DoSoftwareTrigger + TUCCM_SEQUENCE",
            #  sw_idx, TUCAM_CAPTURE_MODES.TUCCM_SEQUENCE.value, trigger_classic),
            # # GenICam command 'TriggerSoftwarePulse' (what the driver uses)
            # ("GenICam TriggerSoftwarePulse + TUCCM_TRIGGER_SOFTWARE",
            #  sw_idx, TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_SOFTWARE.value, trigger_cmd("TriggerSoftwarePulse")),
            # ("GenICam TriggerSoftwarePulse + TUCCM_SEQUENCE",
            #  sw_idx, TUCAM_CAPTURE_MODES.TUCCM_SEQUENCE.value, trigger_cmd("TriggerSoftwarePulse")),
            # # Standard SFNC name
            # ("GenICam TriggerSoftware + TUCCM_TRIGGER_SOFTWARE",
            #  sw_idx, TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_SOFTWARE.value, trigger_cmd("TriggerSoftware")),
            # ("GenICam TriggerSoftware + TUCCM_SEQUENCE",
            #  sw_idx, TUCAM_CAPTURE_MODES.TUCCM_SEQUENCE.value, trigger_cmd("TriggerSoftware")),
            # # Occasional alternate name
            # ("GenICam SoftwareTrigger + TUCCM_SEQUENCE",
            #  sw_idx, TUCAM_CAPTURE_MODES.TUCCM_SEQUENCE.value, trigger_cmd("SoftwareTrigger")),
        ]

        for label, tm_idx, cap_mode, dispatch in combos:
            try_variant(
                handle,
                label=label,
                trigger_mode_idx=tm_idx,
                cap_start_mode=cap_mode,
                dispatch_fn=dispatch,
                n_triggers=3,
                gap_s=0.2,
                frame_timeout_ms=1500,
            )
    finally:
        TUCAM_Dev_Close(handle)
        TUCAM_Api_Uninit()
        print("\nClosed camera and uninited API.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
