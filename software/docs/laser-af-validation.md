# Laser autofocus validation runs

## Start a run

1. Calibrate laser AF and set its reference, either globally or for every region.
2. In Flexible Multipoint or Wellplate Multipoint, select the positions and timepoints
   to visit and enable Laser AF.
3. Under Saving, enable **AF validation run**.
4. To cross-reference normal images against AF, also enable **Also image and save all
   AF artifacts**, select channels and an image-saving format, then start acquisition.
   Leave this second checkbox off for the original AF-only walk.

The worker uses the normal region references, seed scan, correction, refresh cadence,
table fallback, last-FOV checks, and inter-region Z retraction.

With imaging enabled, normal channels, Z stacks, imaging plans, saving, and configured
upload remain active. The normal imaging preflight checks apply. Dry run is rejected
because this mode requires saved images. AF artifacts stay in the local experiment
folder; the image upload pipeline does not upload those diagnostics. The size estimate
covers imaging only, so allow additional disk space for AF artifacts.

In AF-only mode, progress counts FOV visits. Channel capture, Z stacks, stimuli,
fluidics, image postprocessing, and upload are skipped. Diagnostics are still saved
locally even if the save format says dry run. The acquisition YAML records both
validation settings and restores them when loaded. Uncheck validation for ordinary
imaging runs and select the intended save format.

## Saved evidence

Evidence is under `<experiment>/af_validation/`:

| Path | Contents |
| --- | --- |
| `events.csv` | Every AF operation and unmeasured table visit, including seed/acquisition phase, timepoint, region, FOV, outcome, displacement, correlation, Z, warnings, and reference ID. |
| `references/<id>/` | AF configuration and reference crop, deduplicated by content. |
| `baseline/` | First successful native AF frame, full-sensor snapshot when available, and metadata. |
| `events/<event_id>/` | Failed or noteworthy operations in AF-only mode; **every operation** in imaging mode, including successful corrections and table-visit metadata. Native frame, full-sensor snapshot, rejected frames, and metadata. A previous success is included when the retained success has the same region, reference, and operation kind. |
| `events/<event_id>/native_*.tiff` | With imaging enabled, every received native AF frame across measurement and verification, with a corresponding YAML recording Z, piezo position, and timestamp. |

## Cross-reference images and AF

Filter `events.csv` to `phase=acquisition`, then match its zero-based `time_point`,
`region_id`, and zero-based `fov` to the imaging acquisition's coordinate records.
`artifacts_path` points to that row's evidence folder relative to `af_validation`.
For individual image files, `image_file_prefix` gives the region/FOV portion of the
filename; Z index and channel follow it. For OME-TIFF/Zarr, use the same
timepoint/region/FOV keys with the normal image-store metadata and coordinate records.

AF runs before imaging that FOV's stack. One AF event therefore corresponds to all
channels and Z planes captured during that visit. Use the per-image Z coordinates
when comparing stack planes to the AF reference plane. A seed event is a preliminary
AF visit and has no corresponding channel image. The AF cadence is unchanged:
table-only visits have no newly measured displacement or camera artifacts. An AF
record describes the focus operation, not a guarantee that subsequent imaging
completed; check that the image exists if the run aborted.

A table-only visit has `af_attempted=False` and blank `af_success`; it is not a
measurement of successful focus. A failed correction followed by a stale-table
fallback remains `af_success=False`, with `af_status=stale`. Measurement success means
a finite displacement was obtained; a displacement exceeding the consistency threshold
is saved as a warning. Do not count table visits as measured AF successes.

Native frames come from the AF operation. Full-sensor snapshots are acquired after
the operation and may therefore show the position after rollback or fallback.
Compare `frame_z_mm` with `snapshot_z_mm`; event YAML also records individual
displacement measurements and piezo positions. Rejected frames from earlier correction
iterations remain available even if final verification succeeds.

Only one previous success is held in memory. In AF-only mode, routine successful
snapshots are discarded after they cease to be that previous success; failures and
warnings accumulate on disk. With imaging enabled, all successful artifacts are also
saved, and memory for native frames is released after each FOV's evidence is written.
Full-sensor capture restores the actual focus-camera ROI, streaming state, and callback
state, and turns off the laser. A restoration failure stops the run. Evidence-write
failures request acquisition abort.

## Interpreting a comparison

Use the same regions, references, AF settings, seed mode, and timepoints for comparisons.
Separate seed corrections, acquisition corrections, and displacement-only checks.
Inspect failures and residual displacement by region and timepoint, including rollback
and stale fallback. Full-sensor capture and saving all AF frames add time and disk I/O,
so validation runs are not a benchmark of normal imaging throughput. AF-only runs
also omit the thermal load from normal imaging.

This mode keeps the existing spot detector, acceptance thresholds, calibration, and
closed-loop correction. The independent matched-filter acceptance and next-FOV drift
prediction experiments are not enabled. Hardware validation is required to establish
whether focus reliability or residual displacement improves on the microscope.
