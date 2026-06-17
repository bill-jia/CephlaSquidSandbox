"""
Per-region laser autofocus reference model.

A laser-AF *reference* is the in-focus state the laser autofocus corrects back
to: the reflected spot's x position plus a small normalized crop of the spot
image used for cross-correlation verification. The objective-level calibration
(ROI offsets, pixel_to_um, detection parameters) lives in
:class:`control.models.laser_af_config.LaserAFConfig` and is shared across the
whole acquisition; only the *reference* differs when you want a distinct focus
target per region (e.g. samples on substrates of different thickness).

The ``x_reference`` stored here is crop/ROI-relative, matching the in-memory
representation the :class:`LaserAutofocusController` consumes
(``laser_af_properties.x_reference``), not the absolute-sensor value persisted
in the per-objective YAML cache.
"""

import base64
import math
from typing import List, Optional, Tuple

import numpy as np
from pydantic import BaseModel, Field, field_validator


def encode_reference_image(image: Optional[np.ndarray]) -> Tuple[Optional[str], Optional[List[int]], Optional[str]]:
    """Encode a numpy image as ``(base64_str, shape, dtype)``.

    ``None`` in -> ``(None, None, None)`` out, so it round-trips a missing image.
    """
    if image is None:
        return None, None, None
    return (
        base64.b64encode(image.tobytes()).decode("utf-8"),
        list(image.shape),
        str(image.dtype),
    )


def decode_reference_image(
    data: Optional[str], shape: Optional[List[int]], dtype: Optional[str]
) -> Optional[np.ndarray]:
    """Inverse of :func:`encode_reference_image`. Any missing field -> ``None``."""
    if data is None or shape is None or dtype is None:
        return None
    raw = base64.b64decode(data.encode("utf-8"))
    # np.frombuffer yields a read-only view over the decoded bytes; copy so
    # callers get a normal writable array (the source buffer is also freed).
    return np.frombuffer(raw, dtype=np.dtype(dtype)).reshape(shape).copy()


class LaserAFReference(BaseModel):
    """One region's laser-AF focus target: spot x position + verification crop."""

    x_reference: float = Field(..., description="In-focus laser spot x position (crop/ROI-relative)")
    reference_image: Optional[str] = Field(None, description="Base64-encoded normalized spot crop")
    reference_image_shape: Optional[List[int]] = Field(None, description="Shape of the crop array")
    reference_image_dtype: Optional[str] = Field(None, description="Dtype of the crop array")

    model_config = {"extra": "forbid"}

    @field_validator("x_reference")
    @classmethod
    def _x_reference_must_be_finite(cls, value: float) -> float:
        # A non-finite spot position (e.g. from a malformed import) corrupts the
        # downstream AF math (int(inf)/int(nan) raise; displacement -> inf), so
        # reject it at construction. Import paths catch the ValueError and skip.
        if not math.isfinite(value):
            raise ValueError("x_reference must be a finite number")
        return value

    @property
    def reference_crop(self) -> Optional[np.ndarray]:
        """The normalized spot crop as a numpy array, or ``None`` if not stored."""
        return decode_reference_image(self.reference_image, self.reference_image_shape, self.reference_image_dtype)

    def set_reference_image(self, image: Optional[np.ndarray]) -> None:
        """Store (or clear) the normalized spot crop."""
        self.reference_image, self.reference_image_shape, self.reference_image_dtype = encode_reference_image(image)

    @classmethod
    def from_capture(cls, x_reference: float, crop: Optional[np.ndarray]) -> "LaserAFReference":
        """Build a reference from a freshly measured spot position and crop image."""
        reference = cls(x_reference=float(x_reference))
        reference.set_reference_image(crop)
        return reference
