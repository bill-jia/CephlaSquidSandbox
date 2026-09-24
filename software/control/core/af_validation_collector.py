"""AF evidence collection, with optional retention of every operation's artifacts."""

import csv
import hashlib
import json
import time
from pathlib import Path

import cv2
import yaml


EVENT_COLUMNS = (
    "event_id", "timestamp", "phase", "time_point", "region_id", "fov", "x_mm", "y_mm",
    "kind", "af_attempted", "af_success", "af_status", "failure_reason", "warning",
    "z_before_mm", "z_after_mm", "piezo_before_um", "piezo_after_um", "frame_z_mm", "snapshot_z_mm",
    "displacement_um", "correlation", "reference_id", "snapshot_captured", "snapshot_error",
    "rejected_count", "native_frame_count", "artifacts_path", "imaging_enabled", "image_file_prefix",
)


class AFValidationCollector:
    def __init__(self, output_dir, save_all=False):
        self.output_dir = Path(output_dir)
        self.save_all = save_all
        self._last_success = None
        self._baseline_written = False
        self._references = set()
        self._next_event = time.time_ns()

    @staticmethod
    def _image(path, frame):
        if frame is not None and not cv2.imwrite(str(path), frame):
            raise OSError(f"Could not write AF evidence image: {path}")

    @staticmethod
    def _yaml(path, metadata):
        path.write_text(yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8")

    def _reference(self, record):
        config = record["config"]
        crop = record["reference_crop"]
        digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode())
        if crop is not None:
            digest.update(str((crop.shape, str(crop.dtype))).encode())
            digest.update(crop.tobytes())
        key = digest.hexdigest()[:16]
        if key not in self._references:
            folder = self.output_dir / "references" / key
            folder.mkdir(parents=True, exist_ok=True)
            self._yaml(folder / "config.yaml", config)
            self._image(folder / "reference_crop.tiff", crop)
            self._references.add(key)
        return key

    def _pair(self, folder, prefix, frame, snapshot, metadata):
        folder.mkdir(parents=True, exist_ok=True)
        self._image(folder / f"{prefix}_native.tiff", frame)
        self._image(folder / f"{prefix}_full_sensor.tiff", snapshot)
        self._yaml(folder / f"{prefix}.yaml", metadata)

    def record(self, metadata, operation=None, snapshot=None):
        """One row per AF operation, or an unmeasured table visit.

        Native frames belong to the operation itself. Full-sensor snapshots are
        taken afterwards, potentially after rollback/fallback; snapshot_z_mm
        explicitly distinguishes their position from the measurement position.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        meta = dict(metadata)
        frame, rejects = None, []
        if operation is not None:
            frame = operation["frame"]
            rejects = operation["rejected_frames"]
            meta.update({k: operation.get(k) for k in (
                "timestamp", "kind", "failure_reason", "z_before_mm", "z_after_mm",
                "piezo_before_um", "piezo_after_um", "displacement_um", "correlation",
                "frame_z_mm", "frame_piezo_um", "measurements",
            )})
            meta.update(af_attempted=True, af_success=operation["success"], reference_id=self._reference(operation))
        else:
            meta.update(kind="table", af_attempted=False, af_success=None)
        meta.update(event_id=self._next_event, snapshot_captured=snapshot is not None, rejected_count=len(rejects))
        self._next_event += 1
        frames = operation.get("frames", []) if operation is not None else []
        noteworthy = operation is not None and (
            not meta["af_success"] or bool(meta.get("warning")) or bool(rejects) or bool(meta.get("snapshot_error"))
        )
        retain = self.save_all or noteworthy
        meta.update(
            native_frame_count=len(frames),
            artifacts_path=f"events/{meta['event_id']}" if retain else None,
        )
        path = self.output_dir / "events.csv"
        header = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=EVENT_COLUMNS, extrasaction="ignore")
            if header:
                writer.writeheader()
            writer.writerow(meta)
        if operation is None:
            if self.save_all:
                folder = self.output_dir / "events" / str(meta["event_id"])
                folder.mkdir(parents=True, exist_ok=True)
                self._yaml(folder / "current.yaml", meta)
            return

        # Retain only one previous success, and never compare different regions,
        # reference targets or kinds of operation as if they were equivalent.
        key = (meta["region_id"], meta["reference_id"], meta["kind"])
        if retain:
            folder = self.output_dir / "events" / str(meta["event_id"])
            self._pair(folder, "current", frame, snapshot, meta)
            for index, (native_frame, details) in enumerate(frames):
                self._image(folder / f"native_{index:03d}.tiff", native_frame)
                self._yaml(folder / f"native_{index:03d}.yaml", details)
            if self._last_success is not None and self._last_success[0] == key:
                _, prior_frame, prior_snapshot, prior_meta = self._last_success
                self._pair(folder, "previous_success", prior_frame, prior_snapshot, prior_meta)
            for index, (rejected_frame, details) in enumerate(rejects):
                self._image(folder / f"rejected_{index:03d}.tiff", rejected_frame)
                self._yaml(folder / f"rejected_{index:03d}.yaml", details)
        if meta["af_success"] and frame is not None:
            if not self._baseline_written:
                self._pair(self.output_dir / "baseline", "baseline", frame, snapshot, meta)
                self._baseline_written = True
            self._last_success = (key, frame, snapshot, meta)
