"""Metadata-only consolidation for TensorStore-written Zarr v3 acquisitions.

Write the inline consolidated_metadata representation used by Zarr-Python 3,
without requiring a Zarr-Python upgrade in the microscope environment. Image
arrays are never opened and chunk directories are never traversed.
"""

import json
import os
from pathlib import Path
import tempfile
from typing import List

import squid.logging


log = squid.logging.get_logger(__name__)


def _read_metadata(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        metadata = json.load(stream)
    if metadata.get("zarr_format") != 3 or metadata.get("node_type") not in ("group", "array"):
        raise ValueError(f"Not Zarr v3 node metadata: {path}")
    return metadata


def _write_metadata(path: Path, metadata: dict) -> None:
    """Replace metadata atomically so readers cannot see a partial index."""
    fd, temporary = tempfile.mkstemp(prefix=".zarr-metadata-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def consolidate_store_metadata(store_path: str) -> None:
    """Rebuild a v3 group's complete metadata snapshot from live zarr.json files.

    Call only after writers have stopped modifying metadata. Preserve every
    node's attributes, array shape, codecs, and channel information separately.
    Old nested snapshots are discarded rather than copied into the new index.
    Legacy implicit groups (notably plate rows) are materialized on disk too.
    """
    store = Path(store_path)
    root_path = store / "zarr.json"
    root = _read_metadata(root_path)
    if root["node_type"] != "group":
        raise ValueError(f"Consolidation requires a group: {store}")
    root.pop("consolidated_metadata", None)
    nodes = {}

    def visit(directory: Path) -> None:
        for child in sorted(directory.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            metadata_path = child / "zarr.json"
            if metadata_path.is_file():
                metadata = _read_metadata(metadata_path)
                metadata.pop("consolidated_metadata", None)
                nodes[child.relative_to(store).as_posix()] = metadata
                if metadata["node_type"] == "array":
                    continue
            visit(child)

    visit(store)
    # All ancestors must be represented for consolidated child lookups. Older
    # HCS acquisitions have well metadata but no row-group zarr.json files.
    for key in list(nodes):
        for parent in (store / key).parents:
            if parent == store:
                break
            relative = parent.relative_to(store).as_posix()
            if relative not in nodes:
                nodes[relative] = {"zarr_format": 3, "node_type": "group", "attributes": {}}
                _write_metadata(parent / "zarr.json", nodes[relative])
    root["consolidated_metadata"] = {"kind": "inline", "must_understand": False, "metadata": nodes}
    _write_metadata(root_path, root)


def consolidate_acquisition_metadata(experiment_path: str) -> List[str]:
    """Consolidate each independent .ome.zarr store, continuing after failures.

    Accept either an experiment directory or a single store. Return failed
    store paths so callers can report an incomplete metadata optimization.
    """
    experiment = Path(experiment_path)
    if not experiment.is_dir():
        raise FileNotFoundError(experiment)
    failures = []
    for directory, dirs, files in os.walk(experiment):
        path = Path(directory)
        if path.name.endswith(".ome.zarr"):
            dirs[:] = []
            try:
                consolidate_store_metadata(str(path))
                log.info("Consolidated Zarr metadata: %s", path)
            except Exception:
                log.exception("Could not consolidate Zarr metadata: %s", path)
                failures.append(str(path))
        elif "zarr.json" in files:
            # A non-OME array may be present alongside the acquisition stores.
            if _read_metadata(path / "zarr.json")["node_type"] == "array":
                dirs[:] = []
    return failures
