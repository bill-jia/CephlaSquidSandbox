"""Flexible-region scans saving as an OME-NGFF HCS plate.

Covers the acquisition-time half of the feature (the offline converter is
covered by ``tests/tools/test_flexible_to_hcs_zarr.py``):

- ``hcs_region_mapping`` grid assignment and its NGFF constraints.
- ``ZarrWriterInfo`` path/metadata routing through the region -> well map.
- A real ``SaveZarrJob`` run producing a valid plate with region annotations.
- ``manifest_path`` depth correctness for every layout, including the ragged
  non-HCS namespaces.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("tensorstore")

import squid.abc
from control._def import ZarrCompression
from control.core import hcs_region_mapping as hrm
from control.core.job_processing import CaptureInfo, JobImage, SaveZarrJob, ZarrWriterInfo


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def test_grid_assignment_is_row_major_and_square_ish():
    regions = ["R0", "cortex slice", "R2", "organoid_7", "R4"]
    mapping = hrm.region_well_map(regions, hrm.LAYOUT_GRID)
    assert mapping == {
        "R0": ("A", "1"),
        "cortex slice": ("A", "2"),
        "R2": ("A", "3"),
        "organoid_7": ("B", "1"),
        "R4": ("B", "2"),
    }


def test_mapping_output_satisfies_ngff_patterns():
    regions = ["weird name!", "R-2", "z" * 40, "3"]
    for row, col in hrm.region_well_map(regions).values():
        assert hrm.NGFF_NAME_RE.match(row)
        assert hrm.NGFF_NAME_RE.match(col)


def test_row_and_column_strip_layouts():
    regions = ["a", "b", "c"]
    assert hrm.region_well_map(regions, hrm.LAYOUT_ROW) == {
        "a": ("A", "1"), "b": ("A", "2"), "c": ("A", "3")
    }
    assert hrm.region_well_map(regions, hrm.LAYOUT_COLUMN) == {
        "a": ("A", "1"), "b": ("B", "1"), "c": ("C", "1")
    }


def test_preserve_layout_is_opt_in_and_validated():
    # Squid's default flexible names parse as well ids but must NOT be sniffed.
    assert hrm.region_well_map(["R0", "R1"]) == {"R0": ("A", "1"), "R1": ("A", "2")}
    assert hrm.region_well_map(["A1", "B2"], hrm.LAYOUT_PRESERVE) == {
        "A1": ("A", "1"), "B2": ("B", "2")
    }
    with pytest.raises(ValueError):
        hrm.region_well_map(["A1", "not a well"], hrm.LAYOUT_PRESERVE)


def test_region_annotations_indices_point_at_their_axes():
    regions = [f"r{i}" for i in range(7)]
    mapping = hrm.region_well_map(regions)
    rows, cols = hrm.plate_axes(mapping)
    for entry in hrm.region_annotations(mapping, fov_counts={r: 2 for r in regions}):
        assert rows[entry["rowIndex"]] == entry["row"]
        assert cols[entry["columnIndex"]] == entry["column"]
        assert entry["field_count"] == 2


# ---------------------------------------------------------------------------
# The shared layout decision
# ---------------------------------------------------------------------------


def _coords(regions, nx=2, ny=2):
    return {r: [(1.0 * i, 2.0 * j, 3.0) for i in range(nx) for j in range(ny)] for r in regions}


def test_select_wells_is_always_a_wellplate():
    assert hrm.is_wellplate_acquisition("Select Wells", _coords(["A1", "B2"]))


def test_flexible_modes_are_never_a_wellplate():
    for mode in ("Current Position", "Manual", ""):
        assert not hrm.is_wellplate_acquisition(mode, _coords(["A1", "B2"]))


def test_load_coordinates_needs_well_names_and_a_uniform_grid():
    assert hrm.is_wellplate_acquisition("Load Coordinates", _coords(["A1", "B2"]))
    assert not hrm.is_wellplate_acquisition("Load Coordinates", _coords(["A1", "cortex"]))
    ragged = _coords(["A1"])
    ragged["B2"] = [(0.0, 0.0, 0.0)]
    assert not hrm.is_wellplate_acquisition("Load Coordinates", ragged)
    assert not hrm.is_wellplate_acquisition("Load Coordinates", {})


def test_resolve_plate_mapping_passes_wellplates_through():
    m = hrm.resolve_plate_mapping(["A1", "B2"], is_wellplate=True, flexible_as_hcs=True)
    assert m.is_hcs and m.region_well_ids == {} and m.layout is None
    assert m.well_id_for("B2") == "B2"


def test_resolve_plate_mapping_maps_flexible_regions():
    m = hrm.resolve_plate_mapping(["R0", "cortex slice"], is_wellplate=False, flexible_as_hcs=True)
    assert m.is_hcs
    assert m.region_well_ids == {"R0": "A1", "cortex slice": "A2"}
    assert m.well_id_for("cortex slice") == "A2"
    assert m.layout == "grid"


def test_resolve_plate_mapping_opt_out_stays_flat():
    m = hrm.resolve_plate_mapping(["R0", "R1"], is_wellplate=False, flexible_as_hcs=False)
    assert not m.is_hcs and m.region_well_ids == {}
    # No regions at all -> nothing to map, and nothing to claim.
    empty = hrm.resolve_plate_mapping([], is_wellplate=False, flexible_as_hcs=True)
    assert not empty.is_hcs


def test_gui_live_view_and_writer_agree_on_paths(tmp_path):
    """The live NDViewer path builder used its own region-name regex, which
    disagreed with the writer for names like R0/R1. Both must now resolve the
    same mapping and therefore the same paths."""
    import control.utils

    for xy_mode, regions in [
        ("Current Position", ["R0", "R1", "R2"]),          # names look like wells but aren't
        ("Current Position", ["cortex slice", "R2"]),
        ("Select Wells", ["A1", "B2"]),
        ("Load Coordinates", ["A1", "B2"]),
    ]:
        coords = _coords(regions)
        mapping = hrm.resolve_plate_mapping(
            list(coords),
            is_wellplate=hrm.is_wellplate_acquisition(xy_mode, coords),
            flexible_as_hcs=True,
        )
        writer = ZarrWriterInfo(
            base_path=str(tmp_path),
            t_size=1,
            c_size=1,
            z_size=1,
            is_hcs=mapping.is_hcs,
            region_fov_counts={r: 1 for r in regions},
            region_well_ids=mapping.region_well_ids,
            region_layout=mapping.layout,
        )
        for region in regions:
            gui_path = control.utils.build_hcs_zarr_fov_path(
                str(tmp_path), mapping.well_id_for(region), 0
            )
            assert gui_path == writer.get_group_path(region, 0), (xy_mode, region)


# ---------------------------------------------------------------------------
# ZarrWriterInfo routing
# ---------------------------------------------------------------------------


def _writer_info(base, regions, well_ids=None, is_hcs=True, **kw):
    return ZarrWriterInfo(
        base_path=str(base),
        t_size=1,
        c_size=1,
        z_size=1,
        is_hcs=is_hcs,
        region_fov_counts={r: 2 for r in regions},
        region_well_ids=well_ids or {},
        region_layout="grid" if well_ids else None,
        pixel_size_um=0.5,
        **kw,
    )


def test_group_path_routes_through_well_map(tmp_path):
    info = _writer_info(tmp_path, ["cortex slice", "R2"], {"cortex slice": "A1", "R2": "A2"})
    assert info.get_group_path("cortex slice", 1).endswith(os.path.join("plate.ome.zarr", "A", "1", "1"))
    assert info.get_group_path("R2", 0).endswith(os.path.join("plate.ome.zarr", "A", "2", "0"))
    assert info.get_well_path("R2").endswith(os.path.join("plate.ome.zarr", "A", "2"))
    # A ragged namespace becomes a sibling plate at the same depth.
    assert info.get_group_path("R2", 0, "BF_refz").endswith(
        os.path.join("BF_refz.ome.zarr", "A", "2", "0")
    )


def test_real_wellplate_scan_is_unaffected(tmp_path):
    """No mapping -> region ids are used verbatim, exactly as before."""
    info = _writer_info(tmp_path, ["A1", "B12"], well_ids=None)
    assert info.well_id_for("B12") == "B12"
    assert info.get_group_path("B12", 3).endswith(os.path.join("plate.ome.zarr", "B", "12", "3"))
    assert info.get_region_annotations() == []


def test_hcs_structure_and_annotations_from_mapping(tmp_path):
    regions = ["R0", "cortex slice", "R2"]
    mapping = hrm.region_well_map(regions)
    well_ids = {r: f"{row}{col}" for r, (row, col) in mapping.items()}
    info = _writer_info(tmp_path, regions, well_ids)

    # 3 regions -> ceil(sqrt(3)) = 2 columns, so the third wraps to row B.
    rows, cols, wells = info.get_hcs_structure()
    assert rows == ["A", "B"]
    assert cols == [1, 2]
    assert wells == [("A", 1), ("A", 2), ("B", 1)]

    annotations = info.get_region_annotations()
    assert [a["name"] for a in annotations] == regions
    assert [a["path"] for a in annotations] == ["A/1", "A/2", "B/1"]
    assert all(a["layout"] == "grid" for a in annotations)


@pytest.mark.parametrize(
    "is_hcs,array_key,expected",
    [
        (True, None, "../../../../acquisition.yaml"),
        (True, "BF_refz", "../../../../acquisition.yaml"),
        (False, None, "../../../acquisition.yaml"),
        # Regression: the ragged non-HCS store is one level deeper.
        (False, "BF_refz", "../../../../acquisition.yaml"),
    ],
)
def test_manifest_path_depth(tmp_path, is_hcs, array_key, expected):
    info = _writer_info(tmp_path, ["A1"], is_hcs=is_hcs)
    rel = info.get_manifest_path("A1", 0, array_key)
    assert rel == expected
    group = Path(info.get_group_path("A1", 0, array_key))
    assert (group / rel).resolve() == (tmp_path / "acquisition.yaml").resolve()


# ---------------------------------------------------------------------------
# End to end through SaveZarrJob
# ---------------------------------------------------------------------------


class _State:
    def __init__(self, name):
        self.name = name


def _capture_info(region_id, fov, save_dir):
    return CaptureInfo(
        position=squid.abc.Pos(x_mm=1.0, y_mm=2.0, z_mm=3.0, theta_rad=None),
        z_index=0,
        capture_time=1700000000.0,
        observation_state=_State("BF"),
        save_directory=str(save_dir),
        file_id=f"{region_id}_{fov}",
        region_id=region_id,
        fov=fov,
        configuration_idx=0,
        time_point=0,
    )


def test_save_zarr_job_writes_valid_synthetic_plate(tmp_path, monkeypatch):
    monkeypatch.setattr("control._def.ZARR_COMPRESSION", ZarrCompression.FAST, raising=False)
    SaveZarrJob.clear_writers()

    regions = ["R0", "cortex slice", "R2"]
    mapping = hrm.region_well_map(regions)
    well_ids = {r: f"{row}{col}" for r, (row, col) in mapping.items()}
    info = ZarrWriterInfo(
        base_path=str(tmp_path),
        t_size=1,
        c_size=1,
        z_size=1,
        is_hcs=True,
        region_fov_counts={r: 1 for r in regions},
        fov_translations_um={r: {0: (1000.0 * i, 250.0)} for i, r in enumerate(regions)},
        region_well_ids=well_ids,
        region_layout="grid",
        pixel_size_um=0.5,
        channel_names=["BF"],
        channel_colors=["#FFFFFF"],
        channel_wavelengths=[None],
    )
    (tmp_path / "acquisition.yaml").write_text("experiment: t\n", encoding="utf-8")

    try:
        for region in regions:
            job = SaveZarrJob(
                capture_info=_capture_info(region, 0, tmp_path),
                capture_image=JobImage(image_array=np.full((16, 16), 7, dtype=np.uint16)),
                zarr_writer_info=info,
            )
            job.run()
        assert SaveZarrJob.finalize_all_writers()
    finally:
        SaveZarrJob.clear_writers()

    plate = tmp_path / "plate.ome.zarr"
    attrs = json.loads((plate / "zarr.json").read_text(encoding="utf-8"))["attributes"]
    assert [w["path"] for w in attrs["ome"]["plate"]["wells"]] == ["A/1", "A/2", "B/1"]
    assert {e["name"]: e["path"] for e in attrs["_squid"]["regions"]} == {
        "R0": "A/1", "cortex slice": "A/2", "R2": "B/1"
    }
    assert attrs["_squid"]["region_layout"] == "grid"

    well_attrs = json.loads((plate / "A" / "2" / "zarr.json").read_text(encoding="utf-8"))["attributes"]
    assert well_attrs["_squid"]["region"] == "cortex slice"
    assert [i["path"] for i in well_attrs["ome"]["well"]["images"]] == ["0"]

    fov_attrs = json.loads((plate / "A" / "2" / "0" / "zarr.json").read_text(encoding="utf-8"))["attributes"]
    assert fov_attrs["_squid"]["region"] == "cortex slice"
    assert fov_attrs["_squid"]["well"] == "A2"
    assert fov_attrs["_squid"]["fov_index"] == 0
    assert fov_attrs["_squid"]["stage_position_um"] == {"y": 1000.0, "x": 250.0}
    assert fov_attrs["_squid"]["manifest_path"] == "../../../../acquisition.yaml"

    # The converter's validator is the same NGFF check used on offline output.
    import sys

    software = Path(__file__).resolve().parents[3]
    if str(software) not in sys.path:
        sys.path.insert(0, str(software))
    from tools import flexible_to_hcs_zarr as f2h

    assert f2h.validate_plate(str(plate)) == []
