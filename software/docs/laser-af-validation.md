# Laser autofocus validation runs

## Start a run

1. Calibrate laser AF and set its reference, either globally or for every region.
2. In Flexible Multipoint or Wellplate Multipoint, select the positions and timepoints
   to visit and enable Laser AF.
3. Under Saving, enable **AF validation run (skip imaging)** and start acquisition.

The worker uses the normal region references, seed scan, correction, refresh cadence,
table fallback, last-FOV checks, and inter-region Z retraction. Progress counts FOV
visits. Channel capture, Z stacks, stimuli, fluidics, image postprocessing, and upload
are skipped. Diagnostics are still saved locally even if the save format says dry run.
The acquisition YAML records the validation setting and restores it when loaded.
Uncheck validation for subsequent imaging runs and select the intended save format.

## Saved evidence

Evidence is under `<experiment>/af_validation/`:

| Path | Contents |
| --- | --- |
| `events.csv` | Every AF operation and unmeasured table visit, including seed/acquisition phase, timepoint, region, FOV, outcome, displacement, correlation, Z, warnings, and reference ID. |
| `references/<id>/` | AF configuration and reference crop, deduplicated by content. |
| `baseline/` | First successful native AF frame, full-sensor snapshot when available, and metadata. |
| `events/<event_id>/` | Failed or noteworthy operations: native frame, full-sensor snapshot, rejected frames, and metadata. A previous success is included when the retained success has the same region, reference, and operation kind. |

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

Only one previous success is held in memory. Routine successful snapshots are discarded
after they cease to be that previous success; failures and warnings accumulate on disk.
Full-sensor capture restores the actual focus-camera ROI, streaming state, and callback
state, and turns off the laser. A restoration failure stops the run. Evidence-write
failures request acquisition abort.

## Interpreting a comparison

Use the same regions, references, AF settings, seed mode, and timepoints for comparisons.
Separate seed corrections, acquisition corrections, and displacement-only checks.
Inspect failures and residual displacement by region and timepoint, including rollback
and stale fallback. Full-sensor capture changes run timing, so these runs are not a
benchmark of normal imaging throughput or a complete reproduction of its thermal load.

This mode keeps the existing spot detector, acceptance thresholds, calibration, and
closed-loop correction. The independent matched-filter acceptance and next-FOV drift
prediction experiments are not enabled. Hardware validation is required to establish
whether focus reliability or residual displacement improves on the microscope.
