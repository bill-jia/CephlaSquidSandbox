"""Utilities for writing per-region multi-series OME-TIFF files via tifffile memmaps.

One OME-TIFF holds a whole region: series *i* (one OME ``Image`` / TIFF series)
is FOV *i* of that region, axes ``TZCYX``. All series are pre-allocated when the
region's first frame arrives, so a plane write is a ``tifffile.memmap`` write
into the right series.

Bookkeeping lives next to the data (``{ome_tiff}/.{stem}.meta.json``) so a
crashed run leaves a discoverable sidecar instead of an orphan in the system
temp dir.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import numpy as np
import tifffile

from control import utils

if TYPE_CHECKING:  # pragma: no cover - type-checking only
    from .job_processing import CaptureInfo, AcquisitionInfo

# Constants for metadata keys
SERIES_KEY = "series"
N_SERIES_KEY = "n_series"
PLANES_KEY = "planes"
SAVED_COUNT_KEY = "saved_count"
EXPECTED_COUNT_KEY = "expected_count"
COMPLETED_KEY = "completed"
START_TIME_KEY = "start_time"
DTYPE_KEY = "dtype"
SHAPE_KEY = "shape"
AXES_KEY = "axes"
CHANNEL_NAMES_KEY = "channel_names"
REGION_ID_KEY = "region_id"
ARRAY_KEY_KEY = "array_key"
TIME_INCREMENT_KEY = "time_increment"
TIME_INCREMENT_UNIT_KEY = "time_increment_unit"
PHYSICAL_SIZE_Z_KEY = "physical_size_z"
PHYSICAL_SIZE_Z_UNIT_KEY = "physical_size_z_unit"
PHYSICAL_SIZE_X_KEY = "physical_size_x"
PHYSICAL_SIZE_X_UNIT_KEY = "physical_size_x_unit"
PHYSICAL_SIZE_Y_KEY = "physical_size_y"
PHYSICAL_SIZE_Y_UNIT_KEY = "physical_size_y_unit"

OME_NS = "http://www.openmicroscopy.org/Schemas/OME/2016-06"

# Classic (non-BigTIFF) files address everything with 32-bit offsets, so every
# byte — pixels, IFDs, and the OME-XML that finalisation appends at EOF — has to
# fit below 4 GiB. TiffWriter picks the flavour when the file is opened and
# cannot upgrade later, so the decision is made up front from the full size.
CLASSIC_TIFF_MAX_BYTES = 2**32

# Per-plane overhead reserved on top of the pixel bytes when deciding BigTIFF:
# ~1 KiB for the plane's IFD + tags, plus ~0.5 KiB for the ``<Plane>`` element
# that finalisation writes into the (appended, hence size-counting) OME-XML.
_BYTES_PER_PLANE_OVERHEAD = 1024 + 512
_BASE_METADATA_OVERHEAD = 65536


def ome_region_folder(experiment_path: str) -> str:
    """The ``ome_tiff`` folder of an experiment root."""
    return os.path.join(experiment_path, "ome_tiff")


def ome_region_base_name(region_id: Any, array_key: Optional[str] = None) -> str:
    """Stem of a region's OME-TIFF (no extension).

    ``{region_id}`` for the dense layout, ``{region_id}__{array_key}`` for the
    ragged cycle layout (one single-channel stack per state). The separator is a
    DOUBLE underscore because region ids are user-editable names that may
    themselves contain single underscores.
    """
    base = f"{region_id}"
    if array_key is not None:
        base = f"{base}__{array_key}"
    return base


def ome_region_file_path(experiment_path: str, region_id: Any, array_key: Optional[str] = None) -> str:
    """``{experiment_path}/ome_tiff/{region_id}[__{array_key}].ome.tiff``.

    Pure: no CaptureInfo/AcquisitionInfo needed, so the GUI can predict where a
    region's frames will land before the writer has created anything. The
    writer's own :func:`ome_output_path` is this function with the experiment
    root dug out of the job's info objects — keep them in sync by construction,
    not by copying the naming rules.
    """
    return os.path.join(ome_region_folder(experiment_path), ome_region_base_name(region_id, array_key) + ".ome.tiff")


def ome_experiment_path(acq_info: "AcquisitionInfo", info: "CaptureInfo") -> str:
    # The experiment root, however the job knows it. ``save_directory`` is only
    # a last resort: OME_TIFF no longer gets a per-timepoint folder, so it is
    # normally the experiment root itself — the dirname fallback is for jobs
    # built with neither root set (tests) whose save_directory is {exp}/{t}.
    return acq_info.experiment_path or info.acquisition_root or os.path.dirname(info.save_directory)


def ome_output_folder(acq_info: "AcquisitionInfo", info: "CaptureInfo") -> str:
    return ome_region_folder(ome_experiment_path(acq_info, info))


def ome_base_name(info: "CaptureInfo") -> str:
    return ome_region_base_name(info.region_id, info.array_key)


def ome_output_path(acq_info: "AcquisitionInfo", info: "CaptureInfo") -> str:
    return ome_region_file_path(ome_experiment_path(acq_info, info), info.region_id, info.array_key)


def sidecar_paths(output_path: str) -> Tuple[str, str]:
    """(metadata, lock) sidecar paths for ``output_path``, beside the data."""
    folder = os.path.dirname(output_path)
    stem = os.path.basename(output_path)
    for suffix in (".ome.tiff", ".ome.tif"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    metadata_path = os.path.join(folder, f".{stem}.meta.json")
    return metadata_path, metadata_path + ".lock"


def load_metadata(metadata_path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(metadata_path):
        return None
    try:
        with open(metadata_path, "r", encoding="utf-8") as metadata_file:
            return json.load(metadata_file)
    except (json.JSONDecodeError, OSError):
        return None


def write_metadata(metadata_path: str, metadata: Dict[str, Any]) -> None:
    with open(metadata_path, "w", encoding="utf-8") as metadata_file:
        json.dump(metadata, metadata_file)


def _ome_t_size(acq_info: "AcquisitionInfo", info: "CaptureInfo") -> int:
    return int(info.save_t_size) if info.save_t_size is not None else int(acq_info.total_time_points)


def _ome_c_size(acq_info: "AcquisitionInfo", info: "CaptureInfo") -> int:
    return int(info.save_c_size) if info.save_c_size is not None else int(acq_info.total_channels)


def _ome_z_size(acq_info: "AcquisitionInfo", info: "CaptureInfo") -> int:
    """Z extent of this frame's array.

    Like T and C, the self-describing field wins: a ragged cycle's reference-z
    state and a postprocess output (which can collapse an N-plane input to a
    single derived plane) both have their own Z, unrelated to the acquisition's
    ``total_z_levels``.
    """
    return int(info.save_z_size) if info.save_z_size is not None else int(acq_info.total_z_levels)


def region_fov_count(acq_info: "AcquisitionInfo", info: "CaptureInfo") -> int:
    """Number of FOVs (series) in this frame's region.

    Falls back to ``fov + 1`` when the acquisition did not declare the region —
    enough to keep a stray frame writable, though a later FOV would then not
    fit and is rejected by :func:`validate_series_index`.
    """
    counts = acq_info.fovs_per_region or {}
    declared = counts.get(str(info.region_id))
    if declared is None:
        return max(1, int(info.fov) + 1)
    return max(1, int(declared))


def needs_bigtiff(n_series: int, planes_per_series: int, plane_bytes: int) -> bool:
    """True when the region file cannot fit in a classic (32-bit offset) TIFF."""
    total_planes = max(0, int(n_series)) * max(0, int(planes_per_series))
    total = total_planes * int(plane_bytes)
    total += _BASE_METADATA_OVERHEAD + total_planes * _BYTES_PER_PLANE_OVERHEAD
    return total > CLASSIC_TIFF_MAX_BYTES


def validate_capture_info(info: "CaptureInfo", acq_info: "AcquisitionInfo", image: np.ndarray) -> None:
    """Validate that capture info and acquisition info have required fields for OME-TIFF saving.

    Note: The caller (SaveOMETiffJob.run) is responsible for checking that acq_info is not None.
    The acq_info fields total_time_points, total_z_levels, and total_channels are required (non-Optional)
    per the AcquisitionInfo dataclass definition.
    """
    if info.time_point is None:
        raise ValueError("CaptureInfo.time_point is required for OME-TIFF saving")
    if image.ndim != 2:
        raise NotImplementedError("OME-TIFF saving currently supports 2D grayscale images only")


def initialize_metadata(
    acq_info: "AcquisitionInfo", info: "CaptureInfo", image: np.ndarray, n_series: int
) -> Dict[str, Any]:
    # Cycle layout self-describes its array dims; fall back to the global totals.
    t_size = _ome_t_size(acq_info, info)
    c_size = _ome_c_size(acq_info, info)
    z_size = _ome_z_size(acq_info, info)
    channel_names = (info.array_channel_names if info.array_channel_names is not None else acq_info.channel_names) or []
    time_increment = float(acq_info.time_increment_s) if acq_info.time_increment_s is not None else None
    time_increment_unit = "s" if time_increment is not None else None
    physical_size_z = float(acq_info.physical_size_z_um) if acq_info.physical_size_z_um is not None else None
    physical_size_z_unit = "µm" if physical_size_z is not None else None
    physical_size_x = float(acq_info.physical_size_x_um) if acq_info.physical_size_x_um is not None else None
    physical_size_x_unit = "µm" if physical_size_x is not None else None
    physical_size_y = float(acq_info.physical_size_y_um) if acq_info.physical_size_y_um is not None else None
    physical_size_y_unit = "µm" if physical_size_y is not None else None
    return {
        DTYPE_KEY: np.dtype(image.dtype).str,
        AXES_KEY: "TZCYX",
        SHAPE_KEY: [
            t_size,
            z_size,
            c_size,
            int(image.shape[-2]),
            int(image.shape[-1]),
        ],
        N_SERIES_KEY: int(n_series),
        REGION_ID_KEY: str(info.region_id),
        ARRAY_KEY_KEY: info.array_key,
        CHANNEL_NAMES_KEY: channel_names,
        SAVED_COUNT_KEY: 0,
        EXPECTED_COUNT_KEY: int(n_series) * t_size * z_size * c_size,
        # fov index (as str, for JSON) -> {start_time, planes}
        SERIES_KEY: {},
        COMPLETED_KEY: False,
        TIME_INCREMENT_KEY: time_increment,
        TIME_INCREMENT_UNIT_KEY: time_increment_unit,
        PHYSICAL_SIZE_Z_KEY: physical_size_z,
        PHYSICAL_SIZE_Z_UNIT_KEY: physical_size_z_unit,
        PHYSICAL_SIZE_X_KEY: physical_size_x,
        PHYSICAL_SIZE_X_UNIT_KEY: physical_size_x_unit,
        PHYSICAL_SIZE_Y_KEY: physical_size_y,
        PHYSICAL_SIZE_Y_UNIT_KEY: physical_size_y_unit,
    }


def ome_plane_indices(info: "CaptureInfo") -> "tuple[int, int]":
    """(t, c) plane coordinates, preferring the self-describing cycle fields."""
    t = int(info.save_t_index) if info.save_t_index is not None else int(info.time_point)
    c = int(info.save_c_index) if info.save_c_index is not None else int(info.configuration_idx)
    return t, c


def validate_series_index(metadata: Dict[str, Any], fov: int) -> None:
    n_series = int(metadata[N_SERIES_KEY])
    if not (0 <= int(fov) < n_series):
        raise ValueError(
            f"FOV index {fov} out of range for region OME-TIFF with {n_series} series "
            f"(region {metadata.get(REGION_ID_KEY)})"
        )


def series_entry(metadata: Dict[str, Any], fov: int, start_time: Optional[float] = None) -> Dict[str, Any]:
    series = metadata.setdefault(SERIES_KEY, {})
    key = str(int(fov))
    entry = series.get(key)
    if entry is None:
        entry = {START_TIME_KEY: start_time, PLANES_KEY: {}}
        series[key] = entry
    elif entry.get(START_TIME_KEY) is None and start_time is not None:
        entry[START_TIME_KEY] = start_time
    return entry


def update_plane_metadata(metadata: Dict[str, Any], info: "CaptureInfo") -> Dict[str, Any]:
    """Record the plane written by ``info`` under its FOV's series entry."""
    t, c = ome_plane_indices(info)
    entry = series_entry(metadata, info.fov, start_time=info.capture_time)
    plane_key = f"{t}-{c}-{info.z_index}"
    plane_data: Dict[str, Any] = {
        "TheT": t,
        "TheZ": int(info.z_index),
        "TheC": c,
    }
    if info.position is not None:
        if getattr(info.position, "x_mm", None) is not None:
            plane_data["PositionX"] = float(info.position.x_mm)
            plane_data["PositionXUnit"] = "mm"
        if getattr(info.position, "y_mm", None) is not None:
            plane_data["PositionY"] = float(info.position.y_mm)
            plane_data["PositionYUnit"] = "mm"

    stepper_z_um: Optional[float] = None
    if info.position is not None and getattr(info.position, "z_mm", None) is not None:
        stepper_z_um = float(info.position.z_mm) * 1000.0

    piezo_z_um: Optional[float] = float(info.z_piezo_um) if info.z_piezo_um is not None else None
    if entry.get(START_TIME_KEY) is not None and info.capture_time is not None:
        plane_data["DeltaT"] = float(info.capture_time - entry[START_TIME_KEY])

    if stepper_z_um is not None or piezo_z_um is not None:
        total_z_um = (stepper_z_um or 0.0) + (piezo_z_um or 0.0)
        plane_data["PositionZ"] = total_z_um
        plane_data["PositionZUnit"] = "µm"

    planes = entry.setdefault(PLANES_KEY, {})
    is_new = plane_key not in planes
    planes[plane_key] = plane_data
    if is_new:
        metadata[SAVED_COUNT_KEY] = int(metadata.get(SAVED_COUNT_KEY, 0)) + 1
    return metadata


def metadata_for_imwrite(metadata: Dict[str, Any], series_index: int) -> Dict[str, Any]:
    """tifffile ``metadata=`` payload for one series (OME ``Image``)."""
    channel_names = metadata.get(CHANNEL_NAMES_KEY) or []
    meta: Dict[str, Any] = {
        AXES_KEY: "TZCYX",
        "Name": f"{metadata.get(REGION_ID_KEY)}:{int(series_index)}",
    }
    if channel_names:
        meta["Channel"] = {"Name": list(channel_names)}
    if metadata.get(TIME_INCREMENT_KEY) is not None:
        meta["TimeIncrement"] = float(metadata[TIME_INCREMENT_KEY])
        meta["TimeIncrementUnit"] = metadata.get(TIME_INCREMENT_UNIT_KEY, "s")
    if metadata.get(PHYSICAL_SIZE_Z_KEY) is not None:
        meta["PhysicalSizeZ"] = float(metadata[PHYSICAL_SIZE_Z_KEY])
        meta["PhysicalSizeZUnit"] = metadata.get(PHYSICAL_SIZE_Z_UNIT_KEY, "µm")
    if metadata.get(PHYSICAL_SIZE_X_KEY) is not None:
        meta["PhysicalSizeX"] = float(metadata[PHYSICAL_SIZE_X_KEY])
        meta["PhysicalSizeXUnit"] = metadata.get(PHYSICAL_SIZE_X_UNIT_KEY, "µm")
    if metadata.get(PHYSICAL_SIZE_Y_KEY) is not None:
        meta["PhysicalSizeY"] = float(metadata[PHYSICAL_SIZE_Y_KEY])
        meta["PhysicalSizeYUnit"] = metadata.get(PHYSICAL_SIZE_Y_UNIT_KEY, "µm")
    return meta


def create_multiseries_file(output_path: str, metadata: Dict[str, Any]) -> None:
    """Pre-allocate every series of a region file.

    ``TiffWriter.write(data=None, shape=...)`` reserves contiguous, memmappable
    pixel space per series and defers the OME-XML (covering all series) to
    close, so ``tifffile.memmap(path, series=i)`` afterwards addresses FOV *i*.
    """
    shape = tuple(int(v) for v in metadata[SHAPE_KEY])
    dtype = np.dtype(metadata[DTYPE_KEY])
    n_series = int(metadata[N_SERIES_KEY])
    planes_per_series = shape[0] * shape[1] * shape[2]
    plane_bytes = shape[3] * shape[4] * dtype.itemsize
    use_bigtiff = needs_bigtiff(n_series, planes_per_series, plane_bytes)

    if os.path.exists(output_path):
        os.remove(output_path)
    with tifffile.TiffWriter(output_path, bigtiff=use_bigtiff, ome=True) as writer:
        for series_index in range(n_series):
            writer.write(
                shape=shape,
                dtype=dtype,
                metadata=metadata_for_imwrite(metadata, series_index),
            )


def planes_from_ome_xml(ome_xml: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Read back the per-series ``Plane`` bookkeeping of a finalized file."""
    if not ome_xml:
        return {}
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(ome_xml)
    except ET.ParseError:
        return {}

    ns = {"ome": OME_NS}
    series: Dict[str, Dict[str, Any]] = {}
    for index, image in enumerate(root.findall("ome:Image", ns)):
        planes: Dict[str, Any] = {}
        for plane in image.findall("ome:Pixels/ome:Plane", ns):
            attrs: Dict[str, Any] = {}
            for key, value in plane.attrib.items():
                if key.endswith("Unit") or key in ("TheT", "TheZ", "TheC"):
                    attrs[key] = int(value) if key.startswith("The") else value
                else:
                    try:
                        attrs[key] = float(value)
                    except ValueError:
                        attrs[key] = value
            planes[f"{attrs.get('TheT', 0)}-{attrs.get('TheC', 0)}-{attrs.get('TheZ', 0)}"] = attrs
        series[str(index)] = {START_TIME_KEY: None, PLANES_KEY: planes}
    return series


def adopt_existing_file(
    output_path: str, acq_info: "AcquisitionInfo", info: "CaptureInfo", image: np.ndarray
) -> Dict[str, Any]:
    """Bookkeeping for a region file that is on disk without a sidecar.

    Happens when a finalized region is written to again. Pre-allocating over it
    would destroy the frames already saved, so take the geometry (and the plane
    metadata already in its OME-XML) from the file itself.
    """
    with tifffile.TiffFile(output_path) as tif:
        n_series = len(tif.series)
        dtype = tif.series[0].dtype
        ome_xml = tif.ome_metadata

    metadata = initialize_metadata(acq_info, info, image, max(1, n_series))
    metadata[DTYPE_KEY] = np.dtype(dtype).str

    import xml.etree.ElementTree as ET

    if ome_xml:
        try:
            pixels = ET.fromstring(ome_xml).find("ome:Image/ome:Pixels", {"ome": OME_NS})
        except ET.ParseError:
            pixels = None
        if pixels is not None:
            metadata[SHAPE_KEY] = [
                int(pixels.get("SizeT", metadata[SHAPE_KEY][0])),
                int(pixels.get("SizeZ", metadata[SHAPE_KEY][1])),
                int(pixels.get("SizeC", metadata[SHAPE_KEY][2])),
                int(pixels.get("SizeY", metadata[SHAPE_KEY][3])),
                int(pixels.get("SizeX", metadata[SHAPE_KEY][4])),
            ]

    shape = metadata[SHAPE_KEY]
    metadata[EXPECTED_COUNT_KEY] = int(metadata[N_SERIES_KEY]) * shape[0] * shape[1] * shape[2]
    metadata[SERIES_KEY] = planes_from_ome_xml(ome_xml)
    metadata[SAVED_COUNT_KEY] = sum(len(entry.get(PLANES_KEY, {})) for entry in metadata[SERIES_KEY].values())
    return metadata


def build_base_ome_xml(metadata: Dict[str, Any]) -> str:
    """Minimal multi-``Image`` OME-XML, used when a file carries none."""
    # Lazy import: xml.etree.ElementTree only needed for XML generation functions
    import xml.etree.ElementTree as ET

    ET.register_namespace("", OME_NS)

    dtype_map = {
        "uint8": "uint8",
        "uint16": "uint16",
        "uint32": "uint32",
        "int8": "int8",
        "int16": "int16",
        "int32": "int32",
        "float32": "float",
        "float64": "double",
    }

    dtype_str = np.dtype(metadata[DTYPE_KEY]).name
    ome_type = dtype_map.get(dtype_str, dtype_str)
    size_t, size_z, size_c, size_y, size_x = metadata[SHAPE_KEY]

    root = ET.Element("{" + OME_NS + "}OME", attrib={"Creator": "Squid"})

    channel_names = metadata.get(CHANNEL_NAMES_KEY) or []
    if not channel_names:
        channel_names = [f"Channel {idx}" for idx in range(size_c)]

    for series_index in range(int(metadata[N_SERIES_KEY])):
        image = ET.SubElement(
            root,
            "{" + OME_NS + "}Image",
            attrib={
                "ID": f"Image:{series_index}",
                "Name": f"{metadata.get(REGION_ID_KEY)}:{series_index}",
            },
        )
        pixels = ET.SubElement(
            image,
            "{" + OME_NS + "}Pixels",
            attrib={
                "ID": f"Pixels:{series_index}",
                "DimensionOrder": "XYCZT",
                "Type": ome_type,
                "SizeT": str(size_t),
                "SizeC": str(size_c),
                "SizeZ": str(size_z),
                "SizeY": str(size_y),
                "SizeX": str(size_x),
            },
        )
        for idx, name in enumerate(channel_names):
            ET.SubElement(
                pixels,
                "{" + OME_NS + "}Channel",
                attrib={
                    "ID": f"Channel:{series_index}:{idx}",
                    "Name": name,
                    "SamplesPerPixel": "1",
                },
            )

    xml_body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>' + xml_body


def augment_ome_xml(existing_xml: Optional[str], metadata: Dict[str, Any]) -> str:
    """Write per-``Image`` acquisition dates, sizes and ``Plane`` elements.

    ``Image`` elements are matched to series (FOV) by document order, which is
    the order :func:`create_multiseries_file` wrote them in.
    """
    # Lazy import: xml.etree.ElementTree only needed for XML generation functions
    import xml.etree.ElementTree as ET

    ET.register_namespace("", OME_NS)
    ns = {"ome": OME_NS}

    if not existing_xml:
        existing_xml = build_base_ome_xml(metadata)
    root = ET.fromstring(existing_xml)

    images = root.findall("ome:Image", ns)
    if len(images) != int(metadata[N_SERIES_KEY]):
        # The file's own XML disagrees with our bookkeeping; rebuild from scratch
        # so the plane metadata still lands somewhere consistent.
        root = ET.fromstring(build_base_ome_xml(metadata))
        images = root.findall("ome:Image", ns)

    series = metadata.get(SERIES_KEY, {})
    channel_names = metadata.get(CHANNEL_NAMES_KEY) or []

    for series_index, image in enumerate(images):
        entry = series.get(str(series_index), {})
        image.set("Name", f"{metadata.get(REGION_ID_KEY)}:{series_index}")
        start_time = entry.get(START_TIME_KEY)
        if start_time is not None:
            try:
                image.set("AcquisitionDate", datetime.fromtimestamp(start_time).isoformat())
            except Exception:
                pass

        pixels = image.find("ome:Pixels", ns)
        if pixels is None:
            continue

        if metadata.get(TIME_INCREMENT_KEY) is not None:
            pixels.set("TimeIncrement", str(metadata[TIME_INCREMENT_KEY]))
            pixels.set("TimeIncrementUnit", metadata.get(TIME_INCREMENT_UNIT_KEY, "s"))
        if metadata.get(PHYSICAL_SIZE_Z_KEY) is not None:
            pixels.set("PhysicalSizeZ", str(metadata[PHYSICAL_SIZE_Z_KEY]))
            pixels.set("PhysicalSizeZUnit", metadata.get(PHYSICAL_SIZE_Z_UNIT_KEY, "µm"))
        if metadata.get(PHYSICAL_SIZE_X_KEY) is not None:
            pixels.set("PhysicalSizeX", str(metadata[PHYSICAL_SIZE_X_KEY]))
            pixels.set("PhysicalSizeXUnit", metadata.get(PHYSICAL_SIZE_X_UNIT_KEY, "µm"))
        if metadata.get(PHYSICAL_SIZE_Y_KEY) is not None:
            pixels.set("PhysicalSizeY", str(metadata[PHYSICAL_SIZE_Y_KEY]))
            pixels.set("PhysicalSizeYUnit", metadata.get(PHYSICAL_SIZE_Y_UNIT_KEY, "µm"))

        if channel_names:
            existing_channels = list(pixels.findall("ome:Channel", ns))
            if len(existing_channels) == len(channel_names):
                for elem, name in zip(existing_channels, channel_names):
                    elem.set("Name", name)
            else:
                for elem in existing_channels:
                    pixels.remove(elem)
                for idx, name in enumerate(channel_names):
                    ET.SubElement(
                        pixels,
                        "{" + OME_NS + "}Channel",
                        attrib={
                            "ID": f"Channel:{series_index}:{idx}",
                            "Name": name,
                            "SamplesPerPixel": "1",
                        },
                    )

        for elem in list(pixels.findall("ome:Plane", ns)):
            pixels.remove(elem)

        ordered_planes = sorted(
            entry.get(PLANES_KEY, {}).values(),
            key=lambda p: (p.get("TheT", 0), p.get("TheC", 0), p.get("TheZ", 0)),
        )
        for plane in ordered_planes:
            ET.SubElement(
                pixels,
                "{" + OME_NS + "}Plane",
                attrib={key: str(value) for key, value in plane.items()},
            )

    xml_body = ET.tostring(root, encoding="unicode")
    if not xml_body.startswith("<?xml"):
        xml_body = '<?xml version="1.0" encoding="UTF-8"?>' + xml_body
    return xml_body


def finalize_ome_xml(output_path: str, metadata: Dict[str, Any]) -> None:
    """Merge the bookkeeping metadata into the file's OME-XML comment."""
    with tifffile.TiffFile(output_path) as tif:
        current_xml = tif.ome_metadata
    ome_xml = augment_ome_xml(current_xml, metadata)
    tifffile.tiffcomment(output_path, ome_xml.encode("utf-8"))


def ensure_output_directory(path: str) -> None:
    utils.ensure_directory_exists(path)
