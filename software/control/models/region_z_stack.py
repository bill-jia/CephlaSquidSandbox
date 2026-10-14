"""Physical Z ranges with one shared acquisition step (all values in mm)."""

import math


def z_stack_count(z_min_mm, z_max_mm, step_mm):
    """Count inclusive planes inside the requested limits, without overshooting."""
    low, high, step = float(z_min_mm), float(z_max_mm), abs(float(step_mm))
    if not all(math.isfinite(v) for v in (low, high, step)) or high < low:
        raise ValueError("Z limits must be finite and Z max must be at least Z min.")
    if high == low:
        return 1
    if step <= 0:
        raise ValueError("A Z stack requires a positive shared Z step.")
    return int(math.floor((high - low) / step + 1e-9)) + 1


def default_z_range(reference_mm, nz, step_mm, mode):
    """Bounds around a region's nominal reference plane, matching stack traversal."""
    step = abs(float(step_mm))
    span = step * (int(nz) - 1)
    if mode == "FROM TOP":
        return reference_mm - span, reference_mm
    if mode == "FROM CENTER":
        low = reference_mm - step * round((int(nz) - 1) / 2)
        return low, low + span
    return reference_mm, reference_mm + span
