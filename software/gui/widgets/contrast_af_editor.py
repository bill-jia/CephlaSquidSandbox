"""Shared Phase 1 numeric editor for manual and acquisition autofocus."""

from qtpy.QtWidgets import QDoubleSpinBox, QSpinBox

from control.models.contrast_autofocus import ContrastAFSettings


DOUBLE_FIELDS = (
    ("coarse_step_um", "Coarse step (µm)", 10000, 3),
    ("medium_step_um", "Medium step (µm)", 10000, 3),
    ("fine_step_um", "Fine step (µm)", 10000, 3),
    ("window_below_um", "Window below current Z (µm)", 100000, 3),
    ("window_above_um", "Window above current Z (µm)", 100000, 3),
    ("energy_threshold", "E threshold T", 1, 3),
    ("energy_margin", "Absolute E margin", 1000000000, 3),
    ("sensor_full_scale", "Sensor full scale (encoded counts)", 65535, 0),
    ("max_time_s", "Time budget (s)", 3600, 1),
    ("max_exposure_ms", "Exposure budget (ms)", 1000000, 1),
    ("verification_tolerance", "Verification tolerance (fraction)", 1, 3),
)
INTEGER_FIELDS = (("max_frames", "Frame budget"), ("max_moves", "Move budget"))


def add_phase1_fields(form, changed=None):
    """Create controls with identical ranges in both owners' editors."""
    boxes = {}
    for name, label, maximum, decimals in DOUBLE_FIELDS:
        box = QDoubleSpinBox()
        box.setRange(0, maximum)
        box.setDecimals(decimals)
        box.setKeyboardTracking(False)
        form.addRow(label, box)
        boxes[name] = box
        if changed is not None:
            box.valueChanged.connect(changed)
    for name, label in INTEGER_FIELDS:
        box = QSpinBox()
        box.setRange(5, 10000)
        form.addRow(label, box)
        boxes[name] = box
        if changed is not None:
            box.valueChanged.connect(changed)
    return boxes


def validated_settings(boxes, method, dense_fallback, *, preserve_legacy=False):
    """Validate the draft without touching observation state or hardware."""
    if method == "legacy" and not preserve_legacy:
        return ContrastAFSettings(method="legacy")
    return ContrastAFSettings.model_validate({
        **{name: box.value() for name, box in boxes.items()},
        "method": method,
        "dense_fallback": dense_fallback,
    })
