"""Zarr v3 saving using TensorStore.

This module provides Zarr v3 saving during acquisition with sharding,
streaming multiscale pyramid generation, and OME-NGFF v0.5 metadata.

Layout (always 5D, always per-FOV, always sharded):
- HCS:        plate.ome.zarr/{row}/{col}/{fov}/{level}        (level 0..N)
- Non-HCS:    zarr/{region}/fov_{n}.ome.zarr/{level}          (level 0..N)

Each FOV's group holds:
- Resolution levels 0..N as sibling zarr v3 arrays of shape (T, C, Z, Y, X).
- frame_times (optional): float64 array of shape (T, C, Z), unix timestamps.
- zarr.json with OME-NGFF multiscales + omero + a _squid.manifest_path pointer.

Chunks are always (1, 1, 1, Y, X); shards are always (1, C, Z, Y, X) regardless
of compression. Pyramid levels are opened at initialize() and written inline
on every write_frame() so finalize() does no heavy work.
"""

import asyncio
import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

import squid.logging
from control._def import ZarrCompression

log = squid.logging.get_logger(__name__)

# TensorStore is an optional dependency - import lazily to allow module import
# even when tensorstore is not installed
_tensorstore = None


def _get_tensorstore():
    """Lazily import tensorstore to avoid import errors when not installed."""
    global _tensorstore
    if _tensorstore is None:
        try:
            import tensorstore as ts

            _tensorstore = ts
        except ImportError:
            raise ImportError("TensorStore is required for Zarr v3 saving. " "Install it with: pip install tensorstore")
    return _tensorstore


@dataclass
class ZarrAcquisitionConfig:
    """Configuration for Zarr v3 saving during acquisition.

    Attributes:
        output_path: Path to the resolution-0 array (e.g. ``.../{fov}/0``).
        shape: Full array shape ``(T, C, Z, Y, X)``.
        dtype: NumPy dtype for the data.
        pixel_size_um: Physical pixel size in micrometers.
        z_step_um: Z step size in micrometers (optional).
        time_increment_s: Time between timepoints in seconds (optional).
        channel_names: Channel names for the omero metadata block.
        channel_colors: Hex colors per channel (e.g. ``"#FF0000"``).
        channel_wavelengths: Emission wavelengths in nm; ``None`` for brightfield.
        compression: Compression preset.
        translation_um: ``(y_um, x_um)`` stage position of the FOV's origin.
            Embedded as the OME-NGFF ``translation`` transform alongside ``scale``.
        manifest_path: Relative path from the FOV's parent group to the
            experiment's ``acquisition.yaml``. Stored in ``_squid.manifest_path``.
        max_pyramid_levels: Maximum number of additional resolution levels.
        min_pyramid_dim_px: Stop generating levels once ``min(Y, X) < this``.
        shard_per_z: When True (default), each shard is one z-slice
            ``(1, C, 1, Y, X)``, committed once that z is fully captured —
            synchronous with the z-outer/channel-inner acquisition loop, tiny
            buffer, no per-frame read-modify-write of a giant shard. When False,
            the shard is the whole FOV timepoint ``(1, C, Z, Y, X)`` (the legacy
            layout) and is committed in one burst. See :func:`_level_shard_shape`.
    """

    output_path: str
    shape: Tuple[int, int, int, int, int]  # (T, C, Z, Y, X)
    dtype: np.dtype
    pixel_size_um: float
    z_step_um: Optional[float] = None
    time_increment_s: Optional[float] = None
    channel_names: List[str] = field(default_factory=list)
    channel_colors: List[str] = field(default_factory=list)
    channel_wavelengths: List[Optional[int]] = field(default_factory=list)
    compression: ZarrCompression = ZarrCompression.BALANCED
    translation_um: Tuple[float, float] = (0.0, 0.0)
    manifest_path: Optional[str] = None
    max_pyramid_levels: int = 5
    min_pyramid_dim_px: int = 128
    shard_per_z: bool = True
    squid_extras: Dict[str, Any] = field(default_factory=dict)
    """Extra keys merged into the FOV group's ``_squid`` block (e.g. the
    originating region name and FOV index for a synthetic HCS plate)."""

    @property
    def t_size(self) -> int:
        return self.shape[0]

    @property
    def c_size(self) -> int:
        return self.shape[1]

    @property
    def z_size(self) -> int:
        return self.shape[2]

    @property
    def y_size(self) -> int:
        return self.shape[3]

    @property
    def x_size(self) -> int:
        return self.shape[4]


def _level_chunk_shape(y: int, x: int) -> Tuple[int, int, int, int, int]:
    """Inner chunk for a level of size (Y=y, X=x): one plane."""
    return (1, 1, 1, y, x)


def _level_shard_shape(c: int, z: int, y: int, x: int, per_z: bool) -> Tuple[int, int, int, int, int]:
    """Outer shard (the on-disk file unit).

    A shard is written as a whole file, so the shard shape must match the unit
    that is committed at once during acquisition:

    - ``per_z=True``  -> ``(1, C, 1, Y, X)``: one z-slice (all channels). The
      writer accumulates a z-slice's channels and commits the shard exactly once
      the moment that z is fully captured. Synchronous with the z-outer/
      channel-inner acquisition loop, tiny buffer, file count = ``T*Z`` per FOV
      per level. **No per-frame read-modify-write of a giant shard.**
    - ``per_z=False`` -> ``(1, C, Z, Y, X)``: the whole FOV timepoint in one
      shard. Fewest files (``T`` per FOV per level), but the writer must buffer
      the whole FOV and commit it in one burst.

    The inner *chunk* stays one plane ``(1,1,1,Y,X)`` either way, so read
    granularity (scrolling z / switching channels) is identical.
    """
    return (1, c, 1, y, x) if per_z else (1, c, z, y, x)


def _get_compression_codec(compression: ZarrCompression) -> Optional[Dict[str, Any]]:
    """Get blosc codec configuration for compression preset."""
    if compression == ZarrCompression.NONE:
        return None
    elif compression == ZarrCompression.FAST:
        return {
            "name": "blosc",
            "configuration": {"cname": "lz4", "clevel": 1, "shuffle": "shuffle"},
        }
    elif compression == ZarrCompression.BALANCED:
        return {
            "name": "blosc",
            "configuration": {"cname": "zstd", "clevel": 3, "shuffle": "bitshuffle"},
        }
    elif compression == ZarrCompression.BEST:
        return {
            "name": "blosc",
            "configuration": {"cname": "zstd", "clevel": 9, "shuffle": "bitshuffle"},
        }
    else:
        return {
            "name": "blosc",
            "configuration": {"cname": "lz4", "clevel": 5, "shuffle": "bitshuffle"},
        }


def _dtype_to_zarr(dtype: np.dtype) -> str:
    """Convert numpy dtype to zarr v3 dtype string."""
    dtype = np.dtype(dtype)
    dtype_map = {
        np.dtype("uint8"): "uint8",
        np.dtype("uint16"): "uint16",
        np.dtype("uint32"): "uint32",
        np.dtype("uint64"): "uint64",
        np.dtype("int8"): "int8",
        np.dtype("int16"): "int16",
        np.dtype("int32"): "int32",
        np.dtype("int64"): "int64",
        np.dtype("float32"): "float32",
        np.dtype("float64"): "float64",
    }
    if dtype in dtype_map:
        return dtype_map[dtype]
    raise ValueError(f"Unsupported dtype for zarr: {dtype}")


# HCS Plate/Well metadata helpers ---------------------------------------------


def _write_group_metadata(path: str, ome_metadata: dict, description: str) -> None:
    """Write OME-NGFF group metadata to ``zarr.json``.

    Raises:
        RuntimeError: If metadata files cannot be written.
    """
    try:
        os.makedirs(path, exist_ok=True)
        zarr_json = {"zarr_format": 3, "node_type": "group", "attributes": ome_metadata}
        zarr_json_path = os.path.join(path, "zarr.json")
        with open(zarr_json_path, "w") as f:
            json.dump(zarr_json, f, indent=2)
        log.debug(f"Wrote {description} metadata to {zarr_json_path}")
    except OSError as e:
        log.error(f"Failed to write {description} metadata to {path}: {e}")
        raise RuntimeError(f"Failed to write {description} metadata: {e}") from e


def write_plate_metadata(
    plate_path: str,
    rows: List[str],
    cols: List[int],
    wells: List[Tuple[str, int]],
    plate_name: str = "plate",
    squid_attributes: Optional[Dict[str, Any]] = None,
) -> None:
    """Write OME-NGFF HCS plate metadata at the plate root.

    ``squid_attributes`` is written as a ``_squid`` block beside ``ome``. The
    NGFF plate schema does not restrict additional properties, so this validates
    cleanly; it is where a synthetic plate records which region each well came
    from (see :mod:`control.core.hcs_region_mapping`).
    """
    well_entries = [
        {"path": f"{row}/{col}", "rowIndex": rows.index(row), "columnIndex": cols.index(col)} for row, col in wells
    ]

    plate_metadata: Dict[str, Any] = {
        "ome": {
            "version": "0.5",
            "plate": {
                "version": "0.5",
                "name": plate_name,
                "rows": [{"name": r} for r in rows],
                "columns": [{"name": str(c)} for c in cols],
                "wells": well_entries,
            },
        }
    }
    if squid_attributes:
        plate_metadata["_squid"] = dict(squid_attributes)
    _write_group_metadata(plate_path, plate_metadata, "plate")


def write_well_metadata(
    well_path: str,
    fields: List[int],
    squid_attributes: Optional[Dict[str, Any]] = None,
) -> None:
    """Write OME-NGFF HCS well metadata.

    ``squid_attributes`` carries the originating region name for a synthetic
    plate, so a well is self-describing without consulting the plate root.
    """
    well_metadata: Dict[str, Any] = {
        "ome": {
            "version": "0.5",
            "well": {
                "version": "0.5",
                "images": [{"path": str(f)} for f in fields],
            },
        }
    }
    if squid_attributes:
        well_metadata["_squid"] = dict(squid_attributes)
    _write_group_metadata(well_path, well_metadata, "well")


# Pyramid helpers -------------------------------------------------------------


def _compute_pyramid_shapes(y: int, x: int, max_levels: int, min_dim_px: int) -> List[Tuple[int, int]]:
    """Return [(y0, x0), (y1, x1), ...] for levels 0..N including level 0.

    Levels stop when ``min(y, x) < min_dim_px`` or ``max_levels`` extra levels are produced.
    cv2.pyrDown halves with ``ceil`` semantics: ``y' = (y + 1) // 2``.
    """
    shapes = [(y, x)]
    cy, cx = y, x
    for _ in range(max_levels):
        ny = (cy + 1) // 2
        nx = (cx + 1) // 2
        if min(ny, nx) < min_dim_px:
            break
        shapes.append((ny, nx))
        cy, cx = ny, nx
    return shapes


# ZarrWriter ------------------------------------------------------------------


class ZarrWriter:
    """Zarr v3 writer for OME-NGFF per-FOV output with streaming pyramid.

    Opens level 0 + N pyramid levels at ``initialize()``. Each ``write_frame()``
    cascades through ``cv2.pyrDown`` and submits async writes to every level.
    All pending writes are awaited at ``finalize()``; pyramid generation does no
    extra read/write work after acquisition.
    """

    # Maximum number of TensorStore write futures to accumulate before draining.
    MAX_PENDING_WRITES = 32

    def __init__(self, config: ZarrAcquisitionConfig):
        self._config = config
        self._level_datasets: List[Any] = []  # index = pyramid level, value = TensorStore
        self._level_shapes: List[Tuple[int, int]] = []  # (y, x) per level
        self._frame_times_dataset: Optional[Any] = None
        self._pending_futures: List[Any] = []
        # Buffered planes for z-slices not yet committed, keyed by (t, z) ->
        # {c: image}. In shard-per-z mode a z-slice's channels accumulate here
        # until all C arrive, then the whole (1, C, 1, Y, X) shard is written
        # once. Empty (unused) in legacy per-FOV mode.
        self._pending_z: Dict[Tuple[int, int], Dict[int, np.ndarray]] = {}
        # Shard grid cells written since the last upload barrier drained them,
        # as (t, z_grid) where z_grid is the z index (shard-per-z) or 0 (per-FOV).
        # A dense/ragged cycle FOV visit folds many frames into a block of cells,
        # so the barrier must stage every one — not just the scan time_point.
        self._unstaged_shards: set[Tuple[int, int]] = set()
        self._initialized = False
        self._finalized = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._owns_loop = False

    # Event loop management ---------------------------------------------------

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            try:
                self._loop = asyncio.get_event_loop()
                self._owns_loop = False
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._owns_loop = True
        return self._loop

    def _cleanup_event_loop(self) -> None:
        if self._loop is not None and self._owns_loop and not self._loop.is_closed():
            try:
                self._loop.close()
            except Exception as e:
                log.warning(f"Error closing event loop: {e}")
        self._loop = None
        self._owns_loop = False

    # Path helpers ------------------------------------------------------------

    def _group_dir(self) -> str:
        """Parent group directory (the FOV group), where zarr.json lives."""
        return os.path.dirname(self._config.output_path)

    def _level_path(self, level: int) -> str:
        """Resolution-level array path: ``<group>/<level>``."""
        return os.path.join(self._group_dir(), str(level))

    def _frame_times_path(self) -> str:
        return os.path.join(self._group_dir(), "frame_times")

    def _zarr_json_path(self) -> str:
        return os.path.join(self._group_dir(), "zarr.json")

    def _level_zarr_json_path(self, level: int) -> str:
        return os.path.join(self._level_path(level), "zarr.json")

    def _level_shard_path(self, level: int, t: int, z_grid: int) -> str:
        """On-disk path of one shard file at ``level``.

        With zarr-v3 ``default`` chunk-key encoding the shard file path is its
        grid coordinate ``c/<t>/<c_grid>/<z_grid>/<y_grid>/<x_grid>``. The shard
        spans all channels (c_grid=0) and the full Y, X (y_grid=x_grid=0). In
        shard-per-z mode the z axis is one cell per slice so ``z_grid`` is the z
        index; in legacy per-FOV mode the shard spans all z so ``z_grid`` is 0.
        """
        return os.path.join(self._level_path(level), "c", str(t), "0", str(z_grid), "0", "0")

    def _frame_times_shard_path(self) -> str:
        """Single chunk file backing the ``frame_times`` (T, C, Z) array."""
        return os.path.join(self._frame_times_path(), "c", "0", "0", "0")

    def drain_unstaged_shard_paths(self) -> List[str]:
        """Shard files for every shard cell written since the last drain.

        Returns one shard file per ``(written-cell, pyramid-level)`` and clears
        the pending set so each shard is staged for upload exactly once. A cell
        is ``(t, z_grid)``: one per z-slice in shard-per-z mode, or one per
        timepoint (z_grid=0) in legacy per-FOV mode. A single FOV visit writes
        many cells (every z of the stack), and the upload barrier must stage all
        of them, not just the scan time_point.

        Each shard is exclusive to a single cell; once that cell's write is
        flushed the writer never touches the file again, so deleting it locally
        after a verified remote copy is safe. Callers run ``wait_for_pending()``
        first, so every recorded shard is on disk.
        """
        if not self._unstaged_shards:
            return []
        paths: List[str] = []
        staged: set[Tuple[int, int]] = set()
        for t, z_grid in sorted(self._unstaged_shards):
            cell_paths = [self._level_shard_path(level, t, z_grid) for level in range(len(self._level_shapes))]
            present = [p for p in cell_paths if os.path.exists(p)]
            # Only consider a cell staged once *all* its level shards are on
            # disk; otherwise leave it pending so a later barrier re-checks
            # (guards against a shard TensorStore hasn't flushed yet — never
            # drop a written cell). An all-fill-value (e.g. all-zero) frame
            # writes no chunk, so a cell whose shards never appear simply stays
            # pending and is harmless.
            if present and len(present) == len(cell_paths):
                paths.extend(present)
                staged.add((t, z_grid))
        self._unstaged_shards -= staged
        return paths

    def metadata_paths(self) -> List[str]:
        """Shared metadata files — **uploaded every barrier, never deleted**.

        These files are either:
          - Written once at ``initialize()`` and again at ``finalize()`` (the
            group-level and per-level ``zarr.json`` files), OR
          - Rewritten in place on every frame (``frame_times/c/0/0/0`` holds
            timestamps for *all* ``(t, c, z)`` slots; ``record_frame_time``
            updates a single cell per call).

        Deleting any of these while the writer is still active would corrupt
        the running acquisition. They are re-uploaded on every barrier so
        the remote tree stays continuously readable, but the local copies
        must remain in place until the writer has finalized.

        Files that do not yet exist are filtered out.
        """
        candidates: List[str] = [self._zarr_json_path()]
        for level in range(len(self._level_shapes)):
            candidates.append(self._level_zarr_json_path(level))
        candidates.append(os.path.join(self._frame_times_path(), "zarr.json"))
        candidates.append(self._frame_times_shard_path())
        return [p for p in candidates if os.path.exists(p)]

    # Spec construction -------------------------------------------------------

    def _build_array_spec(
        self,
        path: str,
        y: int,
        x: int,
        compression: ZarrCompression,
    ) -> Dict[str, Any]:
        config = self._config
        shape = (config.t_size, config.c_size, config.z_size, y, x)
        chunk_shape = _level_chunk_shape(y, x)
        shard_shape = _level_shard_shape(config.c_size, config.z_size, y, x, config.shard_per_z)
        compression_codec = _get_compression_codec(compression)

        # 5D transpose order for C-contiguous storage of (T, C, Z, Y, X).
        transpose_order = [4, 3, 2, 1, 0]

        inner_codecs: List[Dict[str, Any]] = [
            {"name": "transpose", "configuration": {"order": transpose_order}},
            {"name": "bytes", "configuration": {"endian": "little"}},
        ]
        if compression_codec is not None:
            inner_codecs.append(compression_codec)

        # Always shard: outer chunk = shard = (1, C, Z, Y, X), inner chunk = (1, 1, 1, Y, X).
        codecs = [
            {
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": list(chunk_shape),
                    "codecs": inner_codecs,
                    "index_codecs": [
                        {"name": "bytes", "configuration": {"endian": "little"}},
                        {"name": "crc32c"},
                    ],
                },
            }
        ]

        return {
            "driver": "zarr3",
            "kvstore": {"driver": "file", "path": path},
            "metadata": {
                "shape": list(shape),
                "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(shard_shape)}},
                "chunk_key_encoding": {"name": "default"},
                "data_type": _dtype_to_zarr(config.dtype),
                "codecs": codecs,
                "fill_value": 0,
            },
        }

    def _build_frame_times_spec(self) -> Dict[str, Any]:
        """Spec for the ``frame_times`` array (T, C, Z) float64.

        Single chunk = the whole array; tiny so no compression needed.
        """
        config = self._config
        shape = (config.t_size, config.c_size, config.z_size)
        return {
            "driver": "zarr3",
            "kvstore": {"driver": "file", "path": self._frame_times_path()},
            "metadata": {
                "shape": list(shape),
                "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(shape)}},
                "chunk_key_encoding": {"name": "default"},
                "data_type": "float64",
                "codecs": [
                    {"name": "bytes", "configuration": {"endian": "little"}},
                ],
                "fill_value": 0.0,
            },
        }

    # Lifecycle ---------------------------------------------------------------

    def initialize(self) -> None:
        """Open level 0 + pyramid levels + frame_times array; write zarr.json."""
        if self._initialized:
            log.warning("Writer already initialized")
            return

        try:
            ts = _get_tensorstore()
            config = self._config

            os.makedirs(self._group_dir(), exist_ok=True)

            self._level_shapes = _compute_pyramid_shapes(
                config.y_size, config.x_size, config.max_pyramid_levels, config.min_pyramid_dim_px
            )

            log.info(
                f"Initializing Zarr v3 dataset: {self._group_dir()} "
                f"levels={len(self._level_shapes)} (sizes={self._level_shapes}) "
                f"compression={config.compression.value}"
            )

            if os.path.exists(self._config.output_path):
                log.warning("Zarr level-0 path already exists and will be overwritten: %s", self._config.output_path)

            loop = self._get_loop()

            for level, (y, x) in enumerate(self._level_shapes):
                spec = self._build_array_spec(self._level_path(level), y, x, config.compression)

                async def _open(spec=spec):
                    return await ts.open(spec, create=True, delete_existing=True)

                ds = loop.run_until_complete(_open())
                self._level_datasets.append(ds)

            # Frame timestamps array (small, uncompressed)
            ft_spec = self._build_frame_times_spec()

            async def _open_ft():
                return await ts.open(ft_spec, create=True, delete_existing=True)

            self._frame_times_dataset = loop.run_until_complete(_open_ft())

            self._write_group_metadata()

            self._initialized = True
            log.info("Zarr v3 dataset initialized successfully")
        except Exception:
            self._cleanup_event_loop()
            raise

    def _write_group_metadata(self) -> None:
        """Write OME-NGFF v0.5 multiscales + omero + _squid pointer to ``zarr.json``."""
        config = self._config

        axes = [
            {"name": "t", "type": "time", "unit": "second"},
            {"name": "c", "type": "channel"},
            {"name": "z", "type": "space", "unit": "micrometer"},
            {"name": "y", "type": "space", "unit": "micrometer"},
            {"name": "x", "type": "space", "unit": "micrometer"},
        ]

        # One dataset per pyramid level
        datasets = []
        for level, (_y, _x) in enumerate(self._level_shapes):
            scale_factor = 2 ** level
            scale = [
                config.time_increment_s or 1.0,
                1.0,
                config.z_step_um or 1.0,
                config.pixel_size_um * scale_factor,
                config.pixel_size_um * scale_factor,
            ]
            translation = [
                0.0,
                0.0,
                0.0,
                config.translation_um[0],  # y_um
                config.translation_um[1],  # x_um
            ]
            datasets.append(
                {
                    "path": str(level),
                    "coordinateTransformations": [
                        {"type": "scale", "scale": scale},
                        {"type": "translation", "translation": translation},
                    ],
                }
            )

        # omero channel metadata
        channels_meta: List[Dict[str, Any]] = []
        for i, name in enumerate(config.channel_names or []):
            channel_info: Dict[str, Any] = {"label": name, "active": True}
            if config.channel_colors and i < len(config.channel_colors):
                color = config.channel_colors[i]
                if isinstance(color, str) and color.startswith("#"):
                    channel_info["color"] = color[1:]
                elif isinstance(color, str):
                    channel_info["color"] = color
                else:
                    # Tolerate ints (e.g. 0xFFFFFF) as a fallback
                    channel_info["color"] = f"{int(color):06X}"
            if config.channel_wavelengths and i < len(config.channel_wavelengths):
                wl = config.channel_wavelengths[i]
                if wl is not None:
                    channel_info["emission_wavelength"] = {"value": wl, "unit": "nanometer"}
            dtype = np.dtype(config.dtype)
            if np.issubdtype(dtype, np.integer):
                info = np.iinfo(dtype)
                channel_info["window"] = {"start": 0, "end": info.max, "min": 0, "max": info.max}
            elif np.issubdtype(dtype, np.floating):
                channel_info["window"] = {"start": 0.0, "end": 1.0, "min": 0.0, "max": 1.0}
            channels_meta.append(channel_info)

        attrs = {
            "ome": {
                "version": "0.5",
                "multiscales": [
                    {
                        "version": "0.5",
                        "name": os.path.basename(self._group_dir()),
                        "axes": axes,
                        "datasets": datasets,
                        "coordinateTransformations": [{"type": "identity"}],
                    }
                ],
                "omero": {
                    "name": os.path.basename(self._group_dir()),
                    "version": "0.5",
                    "channels": channels_meta,
                },
            },
            "_squid": {
                "manifest_path": config.manifest_path or "",
                "acquisition_complete": False,
                **dict(config.squid_extras),
            },
        }

        zarr_json = {"zarr_format": 3, "node_type": "group", "attributes": attrs}
        try:
            with open(self._zarr_json_path(), "w") as f:
                json.dump(zarr_json, f, indent=2)
            log.debug(f"Wrote OME-NGFF group metadata to {self._zarr_json_path()}")
        except OSError as e:
            log.error(f"Failed to write zarr group metadata: {e}")
            raise RuntimeError(f"Failed to write zarr group metadata: {e}") from e

    # Frame writes ------------------------------------------------------------

    def write_frame(self, image: np.ndarray, t: int, c: int, z: int) -> None:
        """Hand one plane to the writer (level 0 + all pyramid levels).

        In shard-per-z mode (default) the plane is buffered until its z-slice
        has all channels, then the whole ``(1, C, 1, Y, X)`` shard is committed
        once — no per-frame read-modify-write of a giant shard. In legacy
        per-FOV mode each plane is written as an individual chunk and pyramids
        cascade per frame. Pending futures are drained when the in-flight pool
        exceeds MAX_PENDING_WRITES.
        """
        if not self._initialized:
            raise RuntimeError("Writer not initialized. Call initialize() first.")
        if self._finalized:
            raise RuntimeError("Writer already finalized.")

        config = self._config
        if not (0 <= t < config.t_size):
            raise ValueError(f"Time index {t} out of range [0, {config.t_size})")
        if not (0 <= c < config.c_size):
            raise ValueError(f"Channel index {c} out of range [0, {config.c_size})")
        if not (0 <= z < config.z_size):
            raise ValueError(f"Z index {z} out of range [0, {config.z_size})")

        if image.dtype != config.dtype:
            image = image.astype(config.dtype)

        if config.shard_per_z:
            # Buffer this channel's plane; commit the z-slice shard once every
            # channel for (t, z) has arrived (z-outer/channel-inner loop => the
            # current z completes just before the next z's first frame).
            self._pending_z.setdefault((t, z), {})[c] = image
            if len(self._pending_z[(t, z)]) >= config.c_size:
                self._commit_z(t, z)
        else:
            # Legacy per-FOV shard: write each plane as its own chunk into the
            # one big (1, C, Z, Y, X) shard and cascade pyramids per frame.
            self._unstaged_shards.add((t, 0))
            self._pending_futures.append(self._level_datasets[0][t, c, z, :, :].write(image))
            if len(self._level_datasets) > 1:
                try:
                    import cv2
                except ImportError:
                    log.warning("cv2 not available, skipping pyramid generation for this frame")
                else:
                    current = image
                    for level in range(1, len(self._level_datasets)):
                        expected_y, expected_x = self._level_shapes[level]
                        current = cv2.pyrDown(current)
                        if current.shape != (expected_y, expected_x):
                            current = cv2.resize(current, (expected_x, expected_y), interpolation=cv2.INTER_AREA)
                        if current.dtype != config.dtype:
                            current = current.astype(config.dtype)
                        self._pending_futures.append(self._level_datasets[level][t, c, z, :, :].write(current))

        if len(self._pending_futures) >= self.MAX_PENDING_WRITES:
            self._drain_completed_futures()

    def _commit_z(self, t: int, z: int) -> None:
        """Write the ``(1, C, 1, Y, X)`` shard for z-slice ``(t, z)`` once.

        Assembles the buffered channels into one ``(C, Y, X)`` array per level
        (cascading ``cv2.pyrDown`` per channel) and issues a single write per
        level, so each shard file is created with one pass — no read-modify-write.

        A *complete* slice takes that whole-shard fast path. An incomplete slice
        (only reachable from ``finalize()``) is written channel-by-channel
        instead: a whole-shard write stores the fill value for the absent
        channels, and because TensorStore drops chunks equal to the fill value
        that would **erase** any channel already committed for this cell rather
        than leave it untouched.
        """
        planes = self._pending_z.pop((t, z), None)
        if not planes:
            return
        config = self._config
        C = config.c_size
        complete = len(planes) == C

        y0, x0 = self._level_shapes[0]
        arr0 = np.zeros((C, y0, x0), dtype=config.dtype)
        for ch, img in planes.items():
            arr0[ch] = img
        if complete:
            self._pending_futures.append(self._level_datasets[0][t, :, z, :, :].write(arr0))
        else:
            for ch in planes:
                self._pending_futures.append(self._level_datasets[0][t, ch, z, :, :].write(arr0[ch]))

        if len(self._level_datasets) > 1:
            try:
                import cv2
            except ImportError:
                log.warning("cv2 not available, skipping pyramid generation for this z-slice")
            else:
                current = {ch: arr0[ch] for ch in planes}  # per-channel (y, x)
                for level in range(1, len(self._level_datasets)):
                    expected_y, expected_x = self._level_shapes[level]
                    arrl = np.zeros((C, expected_y, expected_x), dtype=config.dtype)
                    for ch in current:
                        d = cv2.pyrDown(current[ch])
                        if d.shape != (expected_y, expected_x):
                            d = cv2.resize(d, (expected_x, expected_y), interpolation=cv2.INTER_AREA)
                        if d.dtype != config.dtype:
                            d = d.astype(config.dtype)
                        arrl[ch] = d
                        current[ch] = d
                    if complete:
                        self._pending_futures.append(self._level_datasets[level][t, :, z, :, :].write(arrl))
                    else:
                        for ch in current:
                            self._pending_futures.append(
                                self._level_datasets[level][t, ch, z, :, :].write(arrl[ch])
                            )

        self._unstaged_shards.add((t, z))

    def _flush_pending_z(self, final: bool = False) -> None:
        """Commit buffered z-slices.

        Barriers pass ``final=False``, which commits only slices whose channels
        have *all* arrived. Committing a partial slice mid-acquisition is
        destructive: the remaining channels land later and commit the same cell
        a second time, and that write erases the channels stored by the first.
        Frame jobs are dispatched from the camera callback thread, so a slice
        can legitimately still be incomplete when a barrier runs; leaving it
        buffered costs nothing — it commits as soon as its last channel lands,
        and the next barrier stages it.

        ``final=True`` (finalize) commits whatever is left, so a short or
        aborted FOV keeps the frames it did capture.
        """
        for t, z in list(self._pending_z.keys()):
            if not final and len(self._pending_z[(t, z)]) < self._config.c_size:
                continue
            self._commit_z(t, z)

    def _drain_completed_futures(self) -> int:
        still_pending = []
        drained = 0
        for f in self._pending_futures:
            if f.done():
                f.result()
                drained += 1
            else:
                still_pending.append(f)
        self._pending_futures = still_pending
        if drained:
            log.debug(f"Drained {drained} completed writes, {len(still_pending)} still pending")
        return drained

    def record_frame_time(
        self,
        t: int,
        c: int,
        z: int,
        unix_time_s: float,
        channel_name: Optional[str] = None,
    ) -> None:
        """Write a single timestamp into the ``frame_times[t, c, z]`` slot.

        The ``channel_name`` argument is accepted for API compatibility but not
        stored — channel names live in the omero metadata block.
        """
        if not self._initialized:
            raise RuntimeError("Writer not initialized. Call initialize() first.")
        if self._frame_times_dataset is None:
            return
        config = self._config
        if not (0 <= t < config.t_size and 0 <= c < config.c_size and 0 <= z < config.z_size):
            log.warning(f"record_frame_time index out of range: t={t}, c={c}, z={z}")
            return
        try:
            value = np.asarray([[[float(unix_time_s)]]], dtype=np.float64)
            fut = self._frame_times_dataset[t : t + 1, c : c + 1, z : z + 1].write(value)
            self._pending_futures.append(fut)
        except Exception as e:
            log.warning(f"Failed to write frame timestamp at t={t} c={c} z={z}: {e}")

    def wait_for_pending(self, timeout_s: Optional[float] = None) -> int:
        """Block until all pending writes complete; re-raise the first error.

        Commits any *complete* buffered z-slice first so it is on disk before
        the barrier awaits the futures. Slices still missing channels stay
        buffered — see :meth:`_flush_pending_z`.
        """
        self._flush_pending_z()
        if not self._pending_futures:
            return 0
        count = len(self._pending_futures)
        log.debug(f"Waiting for {count} pending writes...")
        for f in self._pending_futures:
            f.result()
        self._pending_futures.clear()
        log.debug(f"Completed {count} pending writes")
        return count

    @property
    def pending_write_count(self) -> int:
        return len(self._pending_futures)

    # Finalize / abort --------------------------------------------------------

    def finalize(self) -> None:
        """Flush pending writes and mark ``acquisition_complete=True``.

        Pyramid is already populated incrementally, so this only awaits I/O
        and updates the completion flag — no read-back, no extra compute.
        """
        if self._finalized:
            log.warning("Writer already finalized")
            return

        log.info("Finalizing Zarr v3 dataset...")
        # No more frames are coming, so commit partial slices too — this is the
        # only place they may be written.
        self._flush_pending_z(final=True)
        self.wait_for_pending()
        self._set_squid_flag("acquisition_complete", True)
        self._finalized = True
        self._cleanup_event_loop()
        log.info(f"Zarr v3 dataset finalized: {self._group_dir()}")

    def abort(self) -> None:
        """Abort and clean up; mark ``aborted=True`` in metadata."""
        log.warning("Aborting Zarr writer...")
        try:
            self._pending_futures.clear()
            self._pending_z.clear()
            self._set_squid_flag("acquisition_complete", False)
            self._set_squid_flag("aborted", True)
        finally:
            self._finalized = True
            self._cleanup_event_loop()
            log.warning(f"Zarr writer aborted: {self._group_dir()}")

    def _set_squid_flag(self, key: str, value: Any) -> None:
        path = self._zarr_json_path()
        try:
            if not os.path.exists(path):
                return
            with open(path, "r") as f:
                data = json.load(f)
            attrs = data.get("attributes", {})
            squid_block = attrs.setdefault("_squid", {})
            squid_block[key] = value
            data["attributes"] = attrs
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except (OSError, json.JSONDecodeError) as e:
            log.error(f"Failed to update _squid.{key} at {path}: {e}")

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def is_finalized(self) -> bool:
        return self._finalized

    @property
    def config(self) -> ZarrAcquisitionConfig:
        return self._config
