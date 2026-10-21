"""Consolidation must preserve heterogeneous fields without reading chunks."""

import json
from pathlib import Path

import pytest

from control.core.zarr_metadata import consolidate_acquisition_metadata, consolidate_store_metadata


def _write(path, metadata):
    path.mkdir(parents=True, exist_ok=True)
    (path / "zarr.json").write_text(json.dumps(metadata), encoding="utf-8")


def _group(attributes=None):
    return {"zarr_format": 3, "node_type": "group", "attributes": attributes or {}}


def _array(shape):
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": shape,
        "data_type": "uint16",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [1] * len(shape)}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "."}},
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "fill_value": 0,
        "attributes": {},
    }


def _plate(store):
    _write(store, _group({"ome": {"plate": {"wells": [{"path": "A/1"}]}}}))
    # Deliberately omit row metadata to cover archives written before this fix.
    _write(store / "A/1", _group({"ome": {"well": {"images": [{"path": "0"}, {"path": "1"}]}}}))
    for field, channels, shape in (("0", ["BF"], [1, 1, 1, 8, 9]), ("1", ["BF", "GFP"], [2, 2, 3, 4, 5])):
        _write(
            store / "A/1" / field,
            _group({"ome": {"omero": {"channels": channels}}, "_squid": {"acquisition_complete": True}}),
        )
        _write(store / "A/1" / field / "0", _array(shape))
        _write(store / "A/1" / field / "frame_times", _array(shape[:3]))
    return store


def _index(store):
    return json.loads((store / "zarr.json").read_text())["consolidated_metadata"]["metadata"]


def test_preserves_every_field_and_materializes_missing_rows(tmp_path, monkeypatch):
    store = _plate(tmp_path / "plate.ome.zarr")
    array_dir = store / "A/1/0/0"
    (array_dir / "c").mkdir()
    (array_dir / "c" / "zarr.json").write_text("not metadata")
    real_iterdir = Path.iterdir

    def guarded_iterdir(path):
        assert path != array_dir, "consolidation must not enumerate image chunks"
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    consolidate_store_metadata(str(store))
    index = _index(store)
    assert index["A/1/0/0"]["shape"] == [1, 1, 1, 8, 9]
    assert index["A/1/1/0"]["shape"] == [2, 2, 3, 4, 5]
    assert index["A/1/1"]["attributes"]["ome"]["omero"]["channels"] == ["BF", "GFP"]
    assert index["A/1/0"]["attributes"]["_squid"]["acquisition_complete"] is True
    assert index["A/1/1/frame_times"]["shape"] == [2, 2, 3]
    assert json.loads((store / "A/zarr.json").read_text()) == _group()


def test_rebuild_uses_current_metadata_and_removes_old_nested_indexes(tmp_path):
    store = _plate(tmp_path / "plate.ome.zarr")
    consolidate_store_metadata(str(store / "A/1/0"))
    consolidate_store_metadata(str(store))
    array = store / "A/1/0/0"
    _write(array, _array([5, 1, 1, 8, 9]))
    consolidate_store_metadata(str(store))
    assert _index(store)["A/1/0/0"]["shape"] == [5, 1, 1, 8, 9]
    assert "consolidated_metadata" not in _index(store)["A/1/0"]


def test_discovers_dense_ragged_derived_and_non_hcs_stores(tmp_path):
    stores = [
        _plate(tmp_path / name)
        for name in ("plate.ome.zarr", "BF.ome.zarr", "BF_refz.ome.zarr", "dpc_phase.ome.zarr")
    ]
    non_hcs = tmp_path / "zarr/BF/region/fov_0.ome.zarr"
    _write(non_hcs, _group({"_squid": {"acquisition_complete": True}}))
    _write(non_hcs / "0", _array([1, 1, 1, 8, 9]))
    assert consolidate_acquisition_metadata(str(tmp_path)) == []
    assert all("A/1/1/0" in _index(store) for store in stores)
    assert "0" in _index(non_hcs)


def test_failed_store_keeps_root_and_does_not_skip_other_stores(tmp_path):
    broken = _plate(tmp_path / "broken.ome.zarr")
    good = _plate(tmp_path / "good.ome.zarr")
    original = (broken / "zarr.json").read_bytes()
    (broken / "A/1/1/0/zarr.json").write_text("invalid json")
    assert consolidate_acquisition_metadata(str(tmp_path)) == [str(broken)]
    assert (broken / "zarr.json").read_bytes() == original
    assert "A/1/1/0" in _index(good)


def test_failed_atomic_replace_leaves_root_intact(tmp_path, monkeypatch):
    import control.core.zarr_metadata as metadata_module

    store = _plate(tmp_path / "plate.ome.zarr")
    consolidate_store_metadata(str(store))
    original = (store / "zarr.json").read_bytes()

    def fail_replace(*args):
        raise OSError("simulated failure")

    monkeypatch.setattr(metadata_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated failure"):
        consolidate_store_metadata(str(store))
    assert (store / "zarr.json").read_bytes() == original
    assert not list(store.glob(".zarr-metadata-*"))


def test_zarr3_reads_children_entirely_from_index(tmp_path):
    zarr = pytest.importorskip("zarr", minversion="3.0")
    store = _plate(tmp_path / "plate.ome.zarr")
    consolidate_store_metadata(str(store))
    # No child metadata files remain available: nested lookup must use root.
    for metadata_path in store.rglob("zarr.json"):
        if metadata_path.parent != store:
            metadata_path.unlink()
    root = zarr.open_group(str(store), mode="r", use_consolidated=None)
    assert root["A/1/0/0"].shape == (1, 1, 1, 8, 9)
    assert root["A/1/1/0"].shape == (2, 2, 3, 4, 5)
    assert root["A/1/1"].attrs["ome"]["omero"]["channels"] == ["BF", "GFP"]
    assert root["A/1/1/frame_times"].shape == (2, 2, 3)
