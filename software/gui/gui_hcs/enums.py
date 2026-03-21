from enum import Enum, auto

class NDViewerMode(Enum):
    """NDViewer acquisition mode for tracking active viewer state."""

    INACTIVE = auto()  # No acquisition active
    TIFF = auto()  # TIFF/OME-TIFF file-based viewing
    ZARR_5D = auto()  # Zarr 5D per-FOV mode (HCS or non-HCS)
    ZARR_6D = auto()  # Zarr 6D with FOV as dimension

