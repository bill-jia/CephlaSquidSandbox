"""Layout discovery and enumeration in ``scripts/zarr_backfill_upload.py``.

The script batches uploads per ``(t, fov)`` and deletes each bundle's local
files once it verifies, so its shard enumeration has to see *every* file that
belongs to a timepoint. It previously hardcoded ``c/<t>/0/0/0/0``, which missed
all but the first z-slice under the default per-z shard layout (and its guard
then refused to run at all). These tests pin both the guard and the enumeration
against output from the real ``ZarrWriter``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("tensorstore")

_SOFTWARE_DIR = Path(__file__).resolve().parents[2]
if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

import importlib.util

from control._def import ZarrCompression  # noqa: E402
from control.core.zarr_writer import ZarrAcquisitionConfig, ZarrWriter  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "zarr_backfill_upload", str(_SOFTWARE_DIR / "scripts" / "zarr_backfill_upload.py")
)
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


NT, NC, NZ, NY, NX = 2, 2, 3, 32, 32


def _write_fov(group_dir: Path, *, shard_per_z: bool, nz=NZ):
    cfg = ZarrAcquisitionConfig(
        output_path=str(group_dir / "0"),
        shape=(NT, NC, nz, NY, NX),
        dtype=np.dtype("uint16"),
        pixel_size_um=0.5,
        z_step_um=1.0,
        channel_names=["BF", "GFP"],
        compression=ZarrCompression.FAST,
        manifest_path="../../../acquisition.yaml",
        max_pyramid_levels=0,
        shard_per_z=shard_per_z,
    )
    writer = ZarrWriter(cfg)
    writer.initialize()
    for t in range(NT):
        for z in range(nz):
            for c in range(NC):
                # Non-zero: an all-fill-value plane writes no chunk at all.
                writer.write_frame(np.full((NY, NX), t * 100 + z * 10 + c + 1, dtype=np.uint16), t=t, c=c, z=z)
    writer.finalize()
    return group_dir


def _flexible_tree(root: Path, *, shard_per_z: bool, nz=NZ):
    (root / "acquisition.yaml").write_text("x: 1\n", encoding="utf-8")
    group = root / "zarr" / "R0" / "fov_0.ome.zarr"
    _write_fov(group, shard_per_z=shard_per_z, nz=nz)
    return group


def _keyed_tree(root: Path, key: str, *, shard_per_z: bool = True, nz=NZ):
    """A keyed non-HCS store: ``zarr/<key>/<region>/fov_0.ome.zarr``, the shape
    ``build_per_fov_zarr_path`` produces for a ragged cycle plate or an
    online-postprocessing derived output."""
    group = root / "zarr" / key / "R0" / "fov_0.ome.zarr"
    _write_fov(group, shard_per_z=shard_per_z, nz=nz)
    return group


# ---------------------------------------------------------------------------
# The layout guard
# ---------------------------------------------------------------------------


def test_accepts_default_per_z_shard_layout(tmp_path):
    """Regression: this is what the writer produces by default, and the guard
    used to reject it with a misleading 'older writer' message."""
    group = _flexible_tree(tmp_path, shard_per_z=True)
    ok, reason = bf.check_fov_layout(group)
    assert ok, reason
    assert "per z-slice" in reason


def test_accepts_legacy_per_fov_shard_layout(tmp_path):
    group = _flexible_tree(tmp_path, shard_per_z=False)
    ok, reason = bf.check_fov_layout(group)
    assert ok, reason
    assert "per FOV-timepoint" in reason


def test_rejects_shard_spanning_multiple_timepoints(tmp_path):
    """The batching/deletion unit is one timepoint; a shard covering several
    would be deleted while still being written."""
    import json

    group = _flexible_tree(tmp_path, shard_per_z=True)
    level0 = group / "0" / "zarr.json"
    doc = json.loads(level0.read_text(encoding="utf-8"))
    doc["chunk_grid"]["configuration"]["chunk_shape"][0] = 2
    level0.write_text(json.dumps(doc), encoding="utf-8")

    ok, reason = bf.check_fov_layout(group)
    assert not ok
    assert "spans 2 timepoints" in reason


def test_rejects_six_dimensional_arrays(tmp_path):
    import json

    group = _flexible_tree(tmp_path, shard_per_z=True)
    level0 = group / "0" / "zarr.json"
    doc = json.loads(level0.read_text(encoding="utf-8"))
    doc["shape"] = [1] + doc["shape"]
    level0.write_text(json.dumps(doc), encoding="utf-8")

    ok, reason = bf.check_fov_layout(group)
    assert not ok
    assert "dimensionality" in reason


# ---------------------------------------------------------------------------
# Shard enumeration
# ---------------------------------------------------------------------------


def test_per_z_layout_enumerates_every_z_shard(tmp_path):
    group = _flexible_tree(tmp_path, shard_per_z=True)
    level0 = group / "0"

    assert bf.enumerate_timepoints_for_level(level0) == [0, 1]
    for tp in (0, 1):
        files = bf.shard_files_for_timepoint(level0, tp)
        # One shard per z-slice, and every one under this timepoint's subtree.
        assert len(files) == NZ, files
        assert all(f.is_file() for f in files)
        assert all(str(f).startswith(str(level0 / "c" / str(tp))) for f in files)


def test_legacy_layout_enumerates_its_single_shard(tmp_path):
    group = _flexible_tree(tmp_path, shard_per_z=False)
    level0 = group / "0"
    assert bf.enumerate_timepoints_for_level(level0) == [0, 1]
    assert len(bf.shard_files_for_timepoint(level0, 0)) == 1


def test_enumeration_covers_all_shards_on_disk(tmp_path):
    """No file under a level's c/ tree may be missed — anything not enumerated
    is silently never uploaded."""
    group = _flexible_tree(tmp_path, shard_per_z=True)
    level0 = group / "0"

    on_disk = {
        os.path.join(dirpath, name)
        for dirpath, _d, filenames in os.walk(level0 / "c")
        for name in filenames
    }
    enumerated = {
        str(p)
        for tp in bf.enumerate_timepoints_for_level(level0)
        for p in bf.shard_files_for_timepoint(level0, tp)
    }
    assert enumerated == on_disk


def test_build_tasks_covers_every_shard_and_marks_them_deletable(tmp_path):
    group = _flexible_tree(tmp_path, shard_per_z=True)
    target = bf.UploadTarget(
        enabled=True, remote_root=str(tmp_path / "remote"),
        local_base=str(tmp_path), delete_after_verify=True,
    )
    tasks = bf.build_tasks_for_fov(group, tmp_path, target, in_progress=False)

    assert [t.time_point for t in tasks] == [0, 1]
    for task in tasks:
        assert task.region_id == "R0" and task.fov == 0
        assert len(task.files) == NZ
        assert task.deletable_local_paths == {local for local, _ in task.files}
        for local, remote in task.files:
            assert os.path.isfile(local)
            assert remote.startswith(str(tmp_path / "remote"))


def test_in_progress_skips_only_the_newest_timepoint(tmp_path):
    group = _flexible_tree(tmp_path, shard_per_z=True)
    target = bf.UploadTarget(
        enabled=True, remote_root=str(tmp_path / "remote"),
        local_base=str(tmp_path), delete_after_verify=True,
    )
    tasks = bf.build_tasks_for_fov(group, tmp_path, target, in_progress=True)
    assert [t.time_point for t in tasks] == [0]


def test_single_z_stack_still_works(tmp_path):
    """Z == 1 was the one case the old code happened to handle; keep it."""
    group = _flexible_tree(tmp_path, shard_per_z=True, nz=1)
    ok, _reason = bf.check_fov_layout(group)
    assert ok
    assert len(bf.shard_files_for_timepoint(group / "0", 0)) == 1


# ---------------------------------------------------------------------------
# Discovery across dense and keyed non-HCS stores
# ---------------------------------------------------------------------------


def test_discovery_finds_dense_store(tmp_path):
    group = _flexible_tree(tmp_path, shard_per_z=True)
    assert bf.find_fov_groups(tmp_path) == [group]
    assert bf.parse_fov_identity(group, tmp_path) == ("R0", 0)


def test_discovery_finds_keyed_derived_plate(tmp_path):
    """Regression: a postprocessing-derived output lives one level deeper than
    the dense store. Scanning only the dense depth found nothing here and the
    script still reported success, silently never uploading the plate."""
    (tmp_path / "acquisition.yaml").write_text("x: 1\n", encoding="utf-8")
    derived = _keyed_tree(tmp_path, "DPC_phase")

    assert bf.find_fov_groups(tmp_path) == [derived]
    # Namespaced so it cannot collide with the raw R0/fov_0 it derives from.
    assert bf.parse_fov_identity(derived, tmp_path) == ("DPC_phase/R0", 0)


def test_discovery_finds_dense_and_keyed_side_by_side(tmp_path):
    """The real shape of an online-postprocessing run: raw regions and derived
    plates share one ``zarr/`` root at different depths."""
    dense = _flexible_tree(tmp_path, shard_per_z=True)
    derived = _keyed_tree(tmp_path, "DPC_phase")

    found = bf.find_fov_groups(tmp_path)
    assert sorted(found) == sorted([dense, derived])

    ids = {bf.parse_fov_identity(g, tmp_path) for g in found}
    assert ids == {("R0", 0), ("DPC_phase/R0", 0)}


def test_keyed_derived_plate_shards_are_enumerated(tmp_path):
    """Discovery is only half of it — the derived plate's shards have to reach
    real tasks with correctly-mapped remote paths."""
    (tmp_path / "acquisition.yaml").write_text("x: 1\n", encoding="utf-8")
    derived = _keyed_tree(tmp_path, "DPC_phase")
    target = bf.UploadTarget(
        enabled=True, remote_root=str(tmp_path / "remote"),
        local_base=str(tmp_path), delete_after_verify=True,
    )
    tasks = bf.build_tasks_for_fov(derived, tmp_path, target, in_progress=False)

    assert [t.time_point for t in tasks] == [0, 1]
    for task in tasks:
        assert task.region_id == "DPC_phase/R0" and task.fov == 0
        assert len(task.files) == NZ
        for local, remote in task.files:
            assert os.path.isfile(local)
            # The key stays in the remote path, so the mirror matches on-disk.
            assert remote.startswith(str(tmp_path / "remote"))
            assert os.path.join("zarr", "DPC_phase", "R0") in remote


# ---------------------------------------------------------------------------
# Follow-mode file dedupe
# ---------------------------------------------------------------------------


def test_drop_already_submitted_strips_seen_files():
    task = bf.UploadTask(
        task_id="t1", time_point=0, region_id="R0", fov=0,
        files=[("/a", "/r/a"), ("/b", "/r/b")],
        deletable_local_paths={"/a", "/b"},
    )
    seen: set = set()

    first = bf._drop_already_submitted(task, seen)
    assert [local for local, _ in first.files] == ["/a", "/b"]
    assert seen == {"/a", "/b"}

    # A rescan that adds one more shard to the same timepoint must submit only
    # the new file — the old per-timepoint dedupe would have dropped it all.
    grown = bf.UploadTask(
        task_id="t2", time_point=0, region_id="R0", fov=0,
        files=[("/a", "/r/a"), ("/b", "/r/b"), ("/c", "/r/c")],
        deletable_local_paths={"/a", "/b", "/c"},
    )
    second = bf._drop_already_submitted(grown, seen)
    assert [local for local, _ in second.files] == ["/c"]
    assert second.deletable_local_paths == {"/c"}

    assert bf._drop_already_submitted(grown, seen) is None
