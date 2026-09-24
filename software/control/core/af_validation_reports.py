"""Offline outputs built from validation artifacts at acquisition completion."""

import csv
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import tifffile


# Keep each classic ImageJ TIFF below the 4 GiB offset limit.
STACK_PART_BYTES = 3 * 1024 ** 3


def _rows(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as src:
        return list(csv.DictReader(src))


def build_native_stacks(output_dir):
    """Stream native before/after pairs to ImageJ TCYX files without resizing."""
    root = Path(output_dir)
    groups = defaultdict(list)
    index = []
    for event in _rows(root / "events.csv"):
        if event["kind"] != "correction":
            continue
        row = {key: event.get(key, "") for key in (
            "event_id", "event_index", "phase", "time_point", "region_id", "fov", "af_success", "af_status",
        )}
        row.update(stack_file="", frame_1based="", status="missing_before_or_after")
        index.append(row)
        folder = root / "events" / event["event_id"]
        paths = [folder / f"{name}_native.tiff" for name in ("before", "after")]
        if not all(path.exists() for path in paths):
            continue
        # Read only TIFF headers here; actual pixels are copied one pair at a time.
        with tifffile.TiffFile(paths[0]) as before, tifffile.TiffFile(paths[1]) as after:
            a, b = before.series[0], after.series[0]
            if a.shape != b.shape or a.dtype != b.dtype or len(a.shape) != 2:
                row["status"] = "incompatible_before_after_shape_or_dtype"
                continue
            shape, dtype = a.shape, a.dtype
        if dtype not in (np.dtype("uint8"), np.dtype("uint16"), np.dtype("float32")):
            row["status"] = "unsupported_imagej_dtype"
            continue
        groups[(shape, dtype.str)].append((row, paths))

    for group_number, ((shape, dtype_str), pairs) in enumerate(groups.items(), 1):
        dtype = np.dtype(dtype_str)
        per_part = max(1, STACK_PART_BYTES // (2 * int(np.prod(shape)) * dtype.itemsize))
        for start in range(0, len(pairs), per_part):
            part = pairs[start:start + per_part]
            suffix = f"_group{group_number}" if len(groups) > 1 else ""
            if len(pairs) > per_part:
                suffix += f"_part{start // per_part + 1}"
            name = f"before_after_native{suffix}.tif"
            temporary = root / (name + ".partial")
            stack = tifffile.memmap(
                temporary, shape=(len(part), 2, *shape), dtype=dtype, imagej=True,
                photometric="minisblack", metadata={"axes": "TCYX", "mode": "composite"},
            )
            try:
                for frame_index, (row, paths) in enumerate(part):
                    for channel, path in enumerate(paths):
                        # Artifacts are written with OpenCV's TIFF codec (LZW
                        # by default). Use the same codec to avoid requiring
                        # the optional tifffile imagecodecs package.
                        frame = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
                        if frame is None:
                            raise OSError(f"Could not read AF artifact: {path}")
                        stack[frame_index, channel] = frame
                    row.update(stack_file=name, frame_1based=frame_index + 1, status="included")
                stack.flush()
            finally:
                # Explicitly release the file mapping before renaming on Windows.
                stack._mmap.close()
                del stack
            temporary.replace(root / name)

    with (root / "before_after_index.csv").open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=[
            "event_id", "event_index", "phase", "time_point", "region_id", "fov", "af_success", "af_status",
            "stack_file", "frame_1based", "status",
        ])
        writer.writeheader()
        writer.writerows(index)


def plot_tenengrad(output_dir):
    """Plot scalar data-image scores, preserving channel/region/Z distinctions."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    root = Path(output_dir)
    groups = defaultdict(list)
    for row in _rows(root / "tenengrad.csv"):
        if row["event_index"]:
            key = (row["region_id"], row["channel"], row["z_index"], row["height"], row["width"], row["dtype"])
            groups[key].append(row)
    fig = Figure(figsize=(12, 6), constrained_layout=True)
    FigureCanvasAgg(fig)
    ax = fig.subplots()
    for (region, channel, z, height, width, dtype), rows in groups.items():
        rows.sort(key=lambda row: (int(row["event_index"]), float(row["capture_time"])))
        xs = [int(row["event_index"]) for row in rows]
        ys = [float(row["tenengrad"]) for row in rows]
        label = f"{region} / {channel} / Z{z} ({width}x{height}, {dtype})"
        line, = ax.plot(xs, ys, ".-", linewidth=0.8, markersize=4, label=label)
        failed = [i for i, row in enumerate(rows) if row["af_success"] == "False"]
        ax.scatter([xs[i] for i in failed], [ys[i] for i in failed], marker="x", color=line.get_color(), s=55)
    ax.set(xlabel="Focus event index (events.csv)", ylabel="Tenengrad: mean squared Sobel gradient",
           title="Data-image sharpness by focus event (x marks a failed AF operation)")
    ax.grid(True, alpha=0.25)
    if groups:
        ax.legend(fontsize="x-small", loc="best")
    else:
        ax.text(0.5, 0.5, "No data images with linked focus events", ha="center", transform=ax.transAxes)
    fig.savefig(root / "tenengrad.png", dpi=160)
    fig.savefig(root / "tenengrad.svg")
    fig.clear()
