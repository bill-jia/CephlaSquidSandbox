from enum import Enum, auto

class NDViewerMode(Enum):
    """NDViewer acquisition mode for tracking active viewer state."""

    INACTIVE = auto()  # No acquisition active
    TIFF = auto()  # One file per frame on disk, registered by path
    OME_TIFF = auto()  # One multi-series OME-TIFF per region, series == FOV
    ZARR_5D = auto()  # Zarr 5D per-FOV mode (HCS or non-HCS)

