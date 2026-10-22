"""Incremental, lossless troubleshooting records for sample alignment sessions."""

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import numpy as np
import tifffile


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _json_value(value, arrays=None, key="data"):
    if isinstance(value, np.ndarray):
        if arrays is not None:
            arrays[key] = value
            return {"npz_key": key}
        return _json_value(value.tolist())
    if isinstance(value, np.generic):
        return _json_value(value.item(), arrays, key)
    if isinstance(value, Enum):
        return _json_value(value.value, arrays, key)
    if is_dataclass(value) and not isinstance(value, type):
        value = {field.name: getattr(value, field.name) for field in fields(value)}
    elif hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    elif isinstance(value, SimpleNamespace):
        value = vars(value)
    if isinstance(value, dict):
        return {str(k): _json_value(v, arrays, f"{key}.{k}") for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(v, arrays, f"{key}.{i}") for i, v in enumerate(value)]
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def position_snapshot(hardware):
    """Read cached stage positions in canonical and raw mm; piezo is separate."""
    pos = hardware.stage.get_pos()
    config = hardware.stage.get_config()
    canonical = {axis: getattr(pos, axis + "_mm") for axis in ("x", "y", "z")}
    raw = {axis: getattr(config, axis.upper() + "_AXIS").canonical_to_raw(value) for axis, value in canonical.items()}
    return {
        "stage_canonical_mm": canonical,
        "stage_raw_mm": raw,
        "piezo_um": None if hardware.piezo is None else hardware.piezo.position,
    }


class AlignmentRecorder:
    """Write each image/event immediately; keep completed data after any stop.

    Disk writes run on the calibration worker, after capture has switched its
    illumination off. Errors propagate so the normal hardware restoration runs.
    There are no background writer threads, persistent handles or queued images.
    """

    def __init__(self, parent_folder):
        if not str(parent_folder).strip():
            raise ValueError("Choose a troubleshooting output folder before starting.")
        self.parent_folder = Path(parent_folder).expanduser().resolve()
        self.path = None
        self.image_count = 0
        self.record_count = 0
        self.manifest = None

    def _write_json(self, path, value):
        # Leave the previous manifest intact if a write is interrupted.
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(_json_value(value), indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)

    def start(self, metadata):
        if self.path is not None:
            raise RuntimeError("This troubleshooting recorder has already been used.")
        self.parent_folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("sample_alignment_%Y%m%d_%H%M%S_%f_") + uuid.uuid4().hex[:8]
        self.path = self.parent_folder / name
        self.path.mkdir()
        (self.path / "images").mkdir()
        (self.path / "analysis").mkdir()
        self.manifest = dict(format_version=1, status="running", started_utc=utc_now(), metadata=metadata)
        self._write_json(self.path / "session.json", self.manifest)
        (self.path / "README.txt").write_text(
            "Sample flatness and laser incidence troubleshooting data\n"
            "session.json: settings, starting configuration, units, final status/error and restored position.\n"
            "events.jsonl: chronological image/analysis index; each line is an independent JSON object.\n"
            "images/: full-resolution delivered camera frames as lossless TIFF, preserving dtype.\n"
            "Camera SDK reversals and configured software rotation/flip/crop are already applied.\n"
            "Stage canonical/raw positions are mm; commanded scan Z and analysis heights are um.\n"
            "Piezo positions are separate um values; timestamps ending _utc are UTC.\n"
            "analysis/: JSON results and associated NPZ arrays, loadable with numpy.load(..., allow_pickle=False).\n"
            "A JSON {npz_key: ...} refers to that key in the same-stem NPZ file.\n"
            "Images link to sweep/field/plane identifiers; focus scores and peaks are saved per field.\n"
            "Registration inputs, illumination fits, drift corrections and final results are also retained.\n"
            "Partial data remain valid after cancellation/error. A 'running' session may have been interrupted.\n",
            encoding="utf-8",
        )

    def event(self, kind, **data):
        record = dict(kind=kind, recorded_utc=utc_now(), **data)
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(_json_value(record), allow_nan=False) + "\n")

    def capture(self, image, **metadata):
        self.image_count += 1
        relative_path = f"images/{self.image_count:06d}_{metadata['phase']}.tif"
        record = dict(image=relative_path, shape=image.shape, dtype=str(image.dtype), **metadata)
        tifffile.imwrite(
            str(self.path / relative_path), image, metadata=None, description=json.dumps(_json_value(record))
        )
        self.event("capture", **record)

    def record(self, kind, **data):
        self.record_count += 1
        stem = f"analysis/{self.record_count:06d}_{kind}"
        arrays = {}
        metadata = _json_value(data, arrays)
        if arrays:
            np.savez_compressed(str(self.path / (stem + ".npz")), **arrays)
        self._write_json(self.path / (stem + ".json"), metadata)
        self.event(kind, metadata=stem + ".json", arrays=stem + ".npz" if arrays else None)

    def finish(self, status, **data):
        self.manifest.update(
            status=status,
            finished_utc=utc_now(),
            image_count=self.image_count,
            analysis_count=self.record_count,
            **data,
        )
        self._write_json(self.path / "session.json", self.manifest)
