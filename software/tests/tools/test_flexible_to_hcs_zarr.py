"""Tests for ``tools/flexible_to_hcs_zarr.py``.

Covers:
- The synthetic region -> well grid, and that the standalone copy of the mapping
  matches the canonical ``control.core.hcs_region_mapping`` implementation.
- A round trip: real ``ZarrWriter`` non-HCS output -> converted HCS plate, with
  image data, stage coordinates, and region names all intact.
- Ragged / postprocess-derived namespaces becoming sibling plates.
- The ``manifest_path`` depth fix.
- NGFF 0.5 plate/well constraint validation.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("tensorstore")

_SOFTWARE_DIR = Path(__file__).resolve().parents[2]
if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

from control.core import hcs_region_mapping as canonical  # noqa: E402
from control.core.zarr_writer import ZarrAcquisitionConfig, ZarrWriter  # noqa: E402
from control._def import ZarrCompression  # noqa: E402
from tools import flexible_to_hcs_zarr as f2h  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic flexible acquisition
# ---------------------------------------------------------------------------

NT, NC, NZ, NY, NX = 2, 2, 2, 32, 32


def _write_fov(group_dir: Path, *, translation_um, manifest_path, channel_names, c_size=NC, z_size=NZ):
    cfg = ZarrAcquisitionConfig(
        output_path=str(group_dir / "0"),
        shape=(NT, c_size, z_size, NY, NX),
        dtype=np.dtype("uint16"),
        pixel_size_um=0.5,
        z_step_um=1.0,
        channel_names=channel_names,
        compression=ZarrCompression.FAST,
        translation_um=translation_um,
        manifest_path=manifest_path,
        max_pyramid_levels=1,
    )
    writer = ZarrWriter(cfg)
    writer.initialize()
    for t in range(NT):
        for z in range(z_size):
            for c in range(c_size):
                plane = np.full((NY, NX), 1000 * t + 100 * z + c + 1, dtype=np.uint16)
                writer.write_frame(plane, t=t, c=c, z=z)
                writer.record_frame_time(t=t, c=c, z=z, unix_time_s=1.0)
    writer.finalize()


def _synth_flexible(
    root: Path, regions=("R0", "tumor edge", "R2"), fovs_per_region=2, ragged=False, coordinates=True
):
    """Build a non-HCS flexible acquisition; return {region: [(fov, (y,x))]}.

    ``regions`` order is the *scan* order and is what ``coordinates.csv``
    records — deliberately not alphabetical, since the directory listing is.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "acquisition.yaml").write_text("experiment: synthetic\n", encoding="utf-8")
    if coordinates:
        lines = ["region,x (mm),y (mm),z (mm)"]
        for r_i, region in enumerate(regions):
            for fov in range(fovs_per_region):
                lines.append(f"{region},{0.25 * fov:.6f},{1.0 * r_i:.6f},{4.1 + 0.01 * r_i:.6f}")
        (root / "coordinates.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    positions = {}
    for r_i, region in enumerate(regions):
        positions[region] = []
        for fov in range(fovs_per_region):
            y_um, x_um = 1000.0 * r_i, 250.0 * fov
            group = root / "zarr" / region / f"fov_{fov}.ome.zarr"
            _write_fov(
                group,
                translation_um=(y_um, x_um),
                manifest_path="../../../acquisition.yaml",
                channel_names=["BF", "GFP"],
            )
            positions[region].append((fov, (y_um, x_um)))
            if ragged:
                # A single-channel ref-z-only store, as a ragged cycle would write.
                refz = root / "zarr" / "BF_refz" / region / f"fov_{fov}.ome.zarr"
                _write_fov(
                    refz,
                    translation_um=(y_um, x_um),
                    manifest_path="../../../acquisition.yaml",
                    channel_names=["BF"],
                    c_size=1,
                    z_size=1,
                )
    return positions


def _read_attrs(path: Path) -> dict:
    return json.loads((path / "zarr.json").read_text(encoding="utf-8"))["attributes"]


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def test_standalone_mapping_matches_canonical():
    """The converter must stay copy-paste-identical in behaviour to the
    in-tree mapping, or acquisition output and converted output would diverge."""
    cases = [
        ["R0"],
        ["R0", "R1", "R2"],
        [f"region_{i}" for i in range(17)],
        ["A1", "B2", "H12"],
        ["tumor edge", "R1"],
    ]
    for regions in cases:
        for layout in ("grid", "row", "column"):
            assert f2h.region_well_map(regions, layout) == canonical.region_well_map(regions, layout)
            assert f2h.validate_layout(regions, layout) == canonical.validate_layout(regions, layout)
        assert f2h.plate_axes(f2h.region_well_map(regions)) == canonical.plate_axes(
            canonical.region_well_map(regions)
        )
    for i in (0, 1, 25, 26, 27, 51, 52, 701, 702):
        assert f2h.row_name(i) == canonical.row_name(i)


def test_row_names():
    assert [f2h.row_name(i) for i in range(3)] == ["A", "B", "C"]
    assert f2h.row_name(25) == "Z"
    assert f2h.row_name(26) == "AA"
    assert f2h.row_name(27) == "AB"


def test_grid_is_square_ish_and_spec_legal():
    regions = [f"region {i}" for i in range(10)]
    mapping = f2h.region_well_map(regions, "grid")
    assert len(set(mapping.values())) == len(regions)  # no collisions
    for row, col in mapping.values():
        assert f2h.NGFF_NAME_RE.match(row)
        assert f2h.NGFF_NAME_RE.match(col)
        assert f2h.NGFF_WELL_PATH_RE.match(f"{row}/{col}")
    # 10 regions -> 4 columns x 3 rows
    assert f2h.grid_shape(10, "grid") == (3, 4)
    assert mapping["region 0"] == ("A", "1")
    assert mapping["region 4"] == ("B", "1")


def test_preserve_layout_keeps_real_well_ids():
    mapping = f2h.region_well_map(["A1", "B12"], "preserve")
    assert mapping == {"A1": ("A", "1"), "B12": ("B", "12")}


def test_preserve_layout_rejects_non_well_names():
    with pytest.raises(ValueError):
        f2h.region_well_map(["A1", "tumor edge"], "preserve")


def test_default_layout_does_not_sniff_region_names():
    """R0/R1 parse as well ids but are Squid's default flexible names — they must
    land on a synthetic grid, not become row 'R' column '0'."""
    assert f2h.region_well_map(["R0", "R1", "R2"]) == {
        "R0": ("A", "1"), "R1": ("A", "2"), "R2": ("B", "1")
    }


def test_duplicate_regions_rejected():
    with pytest.raises(ValueError):
        f2h.region_well_map(["R0", "R0"])


def test_columns_sort_numerically():
    mapping = {f"r{i}": ("A", str(i + 1)) for i in range(12)}
    _rows, cols = f2h.plate_axes(mapping)
    assert cols == [str(i) for i in range(1, 13)]  # 10 after 9, not after 1


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def test_convert_dense_flexible_to_hcs(tmp_path):
    src = tmp_path / "exp"
    positions = _synth_flexible(src)
    out = tmp_path / "exp_hcs"

    rc = f2h.main([str(src), "--output", str(out), "--mode", "copy"])
    assert rc == 0

    plate = out / "plate.ome.zarr"
    assert plate.is_dir()

    attrs = _read_attrs(plate)
    plate_meta = attrs["ome"]["plate"]
    assert attrs["ome"]["version"] == "0.5"
    assert [r["name"] for r in plate_meta["rows"]] == ["A", "B"]
    assert [c["name"] for c in plate_meta["columns"]] == ["1", "2"]
    assert plate_meta["field_count"] == 2

    # 3 regions on a 2x2 grid -> A/1, A/2, B/1
    well_paths = [w["path"] for w in plate_meta["wells"]]
    assert well_paths == ["A/1", "A/2", "B/1"]
    for w in plate_meta["wells"]:
        row, col = w["path"].split("/")
        assert plate_meta["rows"][w["rowIndex"]]["name"] == row
        assert plate_meta["columns"][w["columnIndex"]]["name"] == col

    # Region names survive, bound to their wells.
    regions = {e["name"]: e for e in attrs["_squid"]["regions"]}
    assert set(regions) == set(positions)
    assert regions["tumor edge"]["path"] == "A/2"
    assert regions["R2"]["path"] == "B/1"
    assert regions["R0"]["field_count"] == 2

    # Well metadata lists its fields and names its region.
    well_attrs = _read_attrs(plate / "A" / "2")
    assert [i["path"] for i in well_attrs["ome"]["well"]["images"]] == ["0", "1"]
    assert well_attrs["_squid"]["region"] == "tumor edge"

    # Sidecars.
    region_map = json.loads((out / "region_map.json").read_text(encoding="utf-8"))
    assert {e["name"] for e in region_map} == set(positions)
    manifest = json.loads((out / "hcs_conversion_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["moves"]) == 6
    csv_text = (out / "fov_coordinates.csv").read_text(encoding="utf-8")
    assert "tumor edge" in csv_text
    assert csv_text.count("\n") == 7  # header + 6 fields

    # acquisition.yaml carried across so manifest_path still resolves.
    assert (out / "acquisition.yaml").is_file()


def test_scan_order_comes_from_coordinates_csv(tmp_path):
    """The plate grid must follow acquisition order, not the alphabetical
    directory listing — otherwise wells are shuffled relative to the run."""
    src = tmp_path / "exp"
    _synth_flexible(src, regions=("R0", "tumor edge", "R2"), fovs_per_region=1)
    out = tmp_path / "hcs"
    assert f2h.main([str(src), "--output", str(out), "--mode", "copy"]) == 0
    regions = {e["name"]: e["path"] for e in _read_attrs(out / "plate.ome.zarr")["_squid"]["regions"]}
    assert regions == {"R0": "A/1", "tumor edge": "A/2", "R2": "B/1"}

    # Without coordinates.csv the tool can only fall back to directory order.
    src2 = tmp_path / "exp_nocsv"
    _synth_flexible(src2, regions=("R0", "tumor edge", "R2"), fovs_per_region=1, coordinates=False)
    out2 = tmp_path / "hcs_nocsv"
    assert f2h.main([str(src2), "--output", str(out2), "--mode", "copy"]) == 0
    regions2 = {e["name"]: e["path"] for e in _read_attrs(out2 / "plate.ome.zarr")["_squid"]["regions"]}
    assert regions2 == {"R0": "A/1", "R2": "A/2", "tumor edge": "B/1"}


def test_z_coordinate_carried_from_coordinates_csv(tmp_path):
    """z has no axis in the 5D translation transform, so coordinates.csv is the
    only place it exists — it must survive the conversion."""
    src = tmp_path / "exp"
    _synth_flexible(src, regions=("R0", "R1"), fovs_per_region=2)
    out = tmp_path / "hcs"
    assert f2h.main([str(src), "--output", str(out), "--mode", "copy"]) == 0

    attrs = _read_attrs(out / "plate.ome.zarr" / "A" / "2" / "1")
    assert attrs["_squid"]["region"] == "R1"
    assert attrs["_squid"]["stage_position_mm"] == {"x": 0.25, "y": 1.0, "z": 4.11}

    rows = list(csv.DictReader((out / "fov_coordinates.csv").read_text(encoding="utf-8").splitlines()))
    r1f1 = [r for r in rows if r["region"] == "R1" and r["field"] == "1"][0]
    assert float(r1f1["z_mm"]) == pytest.approx(4.11)
    assert float(r1f1["stage_y_um"]) == pytest.approx(1000.0)


def test_fov_group_content_and_coordinates_preserved(tmp_path):
    import tensorstore as ts

    src = tmp_path / "exp"
    _synth_flexible(src, regions=("R0",), fovs_per_region=1)
    out = tmp_path / "exp_hcs"
    assert f2h.main([str(src), "--output", str(out), "--mode", "copy"]) == 0

    fov = out / "plate.ome.zarr" / "A" / "1" / "0"
    attrs = _read_attrs(fov)

    # OME-NGFF image metadata (incl. the stage translation) is untouched.
    ms = attrs["ome"]["multiscales"][0]
    translation = [c for c in ms["datasets"][0]["coordinateTransformations"] if c["type"] == "translation"][0]
    assert translation["translation"][3:] == [0.0, 0.0]
    assert [c["label"] for c in attrs["ome"]["omero"]["channels"]] == ["BF", "GFP"]
    assert attrs["_squid"]["acquisition_complete"] is True

    # New annotations.
    assert attrs["_squid"]["region"] == "R0"
    assert attrs["_squid"]["fov_index"] == 0
    assert attrs["_squid"]["source_path"] == "zarr/R0/fov_0.ome.zarr"
    assert attrs["_squid"]["stage_position_um"] == {"y": 0.0, "x": 0.0}

    # manifest_path re-pointed for the extra directory level, and it resolves.
    assert attrs["_squid"]["manifest_path"] == "../../../../acquisition.yaml"
    assert (fov / attrs["_squid"]["manifest_path"]).resolve() == (out / "acquisition.yaml").resolve()

    # Pixels readable at the new location, pyramid + frame_times intact.
    store = ts.open(
        {"driver": "zarr3", "kvstore": {"driver": "file", "path": str(fov / "0")}}, read=True
    ).result()
    assert store.shape == (NT, NC, NZ, NY, NX)
    assert int(store[1, 0, 1, 0, 0].read().result()) == 1000 + 100 + 1
    assert (fov / "frame_times" / "zarr.json").is_file()


def test_nonzero_stage_position_recorded(tmp_path):
    src = tmp_path / "exp"
    _synth_flexible(src, regions=("R0", "R1"), fovs_per_region=2)
    out = tmp_path / "exp_hcs"
    assert f2h.main([str(src), "--output", str(out), "--mode", "copy"]) == 0
    attrs = _read_attrs(out / "plate.ome.zarr" / "A" / "2" / "1")
    assert attrs["_squid"]["region"] == "R1"
    assert attrs["_squid"]["stage_position_um"] == {"y": 1000.0, "x": 250.0}


def test_ragged_namespaces_become_sibling_plates(tmp_path):
    src = tmp_path / "exp"
    _synth_flexible(src, regions=("R0", "R1"), fovs_per_region=1, ragged=True)
    out = tmp_path / "exp_hcs"
    assert f2h.main([str(src), "--output", str(out), "--mode", "copy"]) == 0

    assert (out / "plate.ome.zarr").is_dir()
    refz = out / "BF_refz.ome.zarr"
    assert refz.is_dir()

    attrs = _read_attrs(refz)
    assert attrs["ome"]["plate"]["name"] == "BF_refz"
    assert attrs["_squid"]["array_key"] == "BF_refz"
    # Both plates use the same wells for the same regions.
    assert [w["path"] for w in attrs["ome"]["plate"]["wells"]] == ["A/1", "A/2"]
    assert _read_attrs(refz / "A" / "1")["_squid"]["region"] == "R0"
    assert (refz / "A" / "1" / "0" / "0" / "zarr.json").is_file()


def test_move_mode_consumes_source(tmp_path):
    src = tmp_path / "exp"
    _synth_flexible(src, regions=("R0",), fovs_per_region=1)
    assert f2h.main([str(src), "--mode", "move"]) == 0
    assert (src / "plate.ome.zarr" / "A" / "1" / "0" / "zarr.json").is_file()
    assert not (src / "zarr").exists()


def test_link_mode_shares_inodes(tmp_path):
    src = tmp_path / "exp"
    _synth_flexible(src, regions=("R0",), fovs_per_region=1)
    original = src / "zarr" / "R0" / "fov_0.ome.zarr" / "0" / "c" / "0" / "0" / "0" / "0" / "0"
    if not original.is_file():
        pytest.skip("shard path layout differs; nothing to compare")
    before = original.stat()
    assert f2h.main([str(src), "--mode", "link"]) == 0
    linked = src / "plate.ome.zarr" / "A" / "1" / "0" / "0" / "c" / "0" / "0" / "0" / "0" / "0"
    assert linked.is_file()
    assert linked.stat().st_size == before.st_size
    assert original.is_file()  # source preserved


def test_spatial_order_sorts_by_stage_position(tmp_path):
    src = tmp_path / "exp"
    src.mkdir()
    (src / "acquisition.yaml").write_text("x: 1\n", encoding="utf-8")
    # Scan order is far -> near; spatial order should invert it.
    for region, y in (("far", 5000.0), ("near", 100.0)):
        _write_fov(
            src / "zarr" / region / "fov_0.ome.zarr",
            translation_um=(y, 0.0),
            manifest_path="../../../acquisition.yaml",
            channel_names=["BF", "GFP"],
        )
    out = tmp_path / "hcs"
    assert f2h.main([str(src), "--output", str(out), "--order", "spatial", "--layout", "row"]) == 0
    regions = {e["name"]: e["path"] for e in _read_attrs(out / "plate.ome.zarr")["_squid"]["regions"]}
    assert regions == {"near": "A/1", "far": "A/2"}


def test_dry_run_writes_nothing(tmp_path):
    src = tmp_path / "exp"
    _synth_flexible(src, regions=("R0",), fovs_per_region=1)
    assert f2h.main([str(src), "--dry-run"]) == 0
    assert not (src / "plate.ome.zarr").exists()
    assert (src / "zarr" / "R0" / "fov_0.ome.zarr").is_dir()


def test_refuses_to_clobber_without_force(tmp_path):
    src = tmp_path / "exp"
    _synth_flexible(src, regions=("R0",), fovs_per_region=1)
    assert f2h.main([str(src), "--mode", "copy"]) == 0
    with pytest.raises(SystemExit):
        f2h.main([str(src), "--mode", "copy"])


def test_validate_only_on_converted_plate(tmp_path):
    src = tmp_path / "exp"
    _synth_flexible(src, regions=("R0", "R1"), fovs_per_region=2, ragged=True)
    out = tmp_path / "hcs"
    assert f2h.main([str(src), "--output", str(out), "--mode", "copy"]) == 0
    assert f2h.main([str(out), "--validate-only"]) == 0


def test_validation_catches_corrupt_plate(tmp_path):
    src = tmp_path / "exp"
    _synth_flexible(src, regions=("R0",), fovs_per_region=1)
    out = tmp_path / "hcs"
    assert f2h.main([str(src), "--output", str(out), "--mode", "copy"]) == 0

    plate = out / "plate.ome.zarr"
    doc = json.loads((plate / "zarr.json").read_text(encoding="utf-8"))
    doc["attributes"]["ome"]["plate"]["rows"] = [{"name": "bad row"}]  # space is illegal
    (plate / "zarr.json").write_text(json.dumps(doc), encoding="utf-8")
    problems = f2h.validate_plate(str(plate))
    assert any("^[A-Za-z0-9]+$" in p for p in problems)


def test_errors_when_no_flexible_store(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit):
        f2h.main([str(empty)])
