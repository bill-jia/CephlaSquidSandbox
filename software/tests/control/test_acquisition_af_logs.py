"""Compatibility CSV and detail sidecar agree on the FOV status."""

import csv
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

from control.core.multi_point_worker import MultiPointWorker
from control.models.contrast_autofocus import ContrastAFSettings
from control.models.observation_state import ObservationState


def test_status_and_effective_settings_are_written_together():
    directory = os.path.dirname(__file__)
    for filename in ("autofocus_log.csv", "autofocus_detail.jsonl"):
        assert not os.path.exists(os.path.join(directory, filename))
    try:
        worker = MultiPointWorker.__new__(MultiPointWorker)
        worker.experiment_path = directory
        worker.experiment_ID = "synthetic-run"
        worker.time_point = 2
        worker.use_piezo = False
        worker._log = MagicMock()
        worker._contrast_af_state = ObservationState(
            name="AF state", contrast_af=ContrastAFSettings(method="legacy"))
        worker._last_af_result = SimpleNamespace(
            status="success", error=None, frames=3, moves=6,
            elapsed_s=0.25, fallback_used=False,
            trigger_route="logical software / physical nidaq", cleanup_errors=[])
        worker._record_autofocus_event(
            position_index=1, region_id="A1", x_mm=1, y_mm=2,
            z_expected_mm=0.5, z_actual_mm=0.501, status="ok")
        with open(os.path.join(directory, "autofocus_log.csv"), newline="") as f:
            row = next(csv.DictReader(f))
        with open(os.path.join(directory, "autofocus_detail.jsonl")) as f:
            detail = json.loads(f.readline())
        assert row["af_status"] == detail["status"] == "ok"
        assert detail["af_state"] == "AF state"
        assert detail["frames"] == 3
        assert detail["region_id"] == "A1"
    finally:
        for filename in ("autofocus_log.csv", "autofocus_detail.jsonl"):
            path = os.path.join(directory, filename)
            if os.path.exists(path):
                os.unlink(path)
