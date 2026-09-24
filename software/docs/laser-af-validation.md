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
| `events/<event_id>/` | Every AF operation in both validation modes, including successful corrections: before/after shots, native frame, full-sensor snapshot, rejected frames, and metadata. Imaging mode also saves table-visit metadata. A previous success is included when the retained success has the same region, reference, and operation kind. |
| `events/<event_id>/native_*.tiff` | With imaging enabled, every received native AF frame across measurement and verification, with a corresponding YAML recording Z, piezo position, and timestamp. |
| `events/<event_id>/before_*` and `after_*` | Fresh native-ROI and full-sensor shots immediately before and after the operation, plus capture-position metadata. |
| `before_after_native.tif` | Two-channel ImageJ stack of complete before/after native pairs from correction events, compiled at the end of acquisition. |
| `before_after_index.csv` | Maps each correction event to its stack filename and one-based playback frame, or explains why its pair was omitted. |
| `tenengrad.csv`, `tenengrad.png`, `tenengrad.svg` | In imaging mode, per-data-image sharpness measurements and end-of-run plots linked to AF events. |

### Before and after correction

For each AF operation, `before_native.tiff` and `before_full_sensor.tiff` are captured
before the operation can move Z. `after_native.tiff` and `after_full_sensor.tiff` are
fresh captures after it finishes, including its rollback if correction failed.
The corresponding `before.yaml` and `after.yaml` contain a `capture` block with the
capture timestamp, stage Z, piezo position, original ROI, and frame-availability flags.
These diagnostic exposures restore the camera's ROI, streaming, and callback state.

The native and full-sensor shots are separate exposures taken sequentially. They are
additional diagnostics; the frames used by the AF algorithm remain in `current` and,
when saving all artifacts, `native_*`. In particular, `current_native.tiff` can show
the failed verification position, while `after_native.tiff` shows the position after
rollback. A later worker-level stale-table fallback or contrast-AF supervision move
can still change Z before data imaging; use each data image's recorded position.

Measurement-only operations also get before/after shots, but do not themselves
correct Z. Table-only visits have no operation to bracket and no new AF snapshots.
Fatal camera errors stop further diagnostic captures; missing shots remain missing
and are identified in metadata rather than replaced with earlier frames.

### Play the before/after sequence in ImageJ

At acquisition completion, including normal abort cleanup, the recorded correction
pairs are copied into `before_after_native.tif`. Open this file as an ImageJ
hyperstack. **Channel 1 is before correction; channel 2 is after correction.**
The time axis advances through correction events in recording order, including seed
events and failed corrections with complete pairs. These are event steps, not equally
spaced wall-clock times. Use the channel selector to compare before and after and
animate the time axis to review the run.

`before_after_index.csv` maps the one-based playback frame to the original event ID,
event index, phase, acquisition timepoint, region, FOV, and AF outcome. Corrections
missing either native shot are listed with a skip reason and have no playback frame.
Measurement-only and table-only events are excluded from this correction stack.

Pixels are copied without resizing or intensity normalization. If the native ROI
dimensions or pixel type change, separate `_groupN` stacks are written. Large groups
are split into `_partN` files at approximately 3 GiB to stay below the classic TIFF
limit. The index identifies the file and playback frame for each pair. Compilation
reads one pair at a time, keeping image memory bounded. An abrupt process crash
cannot run this end-of-acquisition step; the per-event files remain on disk.

### Tenengrad over focus events

With **Also image and save all AF artifacts** enabled, each acquired data-camera
image is scored before display processing. The metric is the mean of
`Sobel_x(image)^2 + Sobel_y(image)^2`, using 3-by-3 Sobel filters over the entire image.
RGB data is converted to grayscale. Values use raw intensity units without exposure
or intensity normalization; a constant image scores zero.

`tenengrad.csv` records each image's score, channel, Z index and physical Z, timepoint,
region, FOV, capture time, file ID, and image-store indices. It links to the last
correction in that FOV visit, or the last measurement/table event if no correction
occurred. `visit_event_ids` lists all AF events from that visit. Seed events are not
linked to data images. Table-only images remain explicitly marked as unmeasured AF.

At completion, `tenengrad.png` and `tenengrad.svg` plot these scores against
`event_index` from `events.csv`. Separate series distinguish region, channel, Z plane,
frame dimensions, and pixel type; an x marker denotes an image associated with a
failed AF operation. Multiple images from one event share the same x coordinate.
The plot includes raw inputs to configured image postprocessing, with their
`postprocess_group` recorded in the CSV. A captured image can have a metric even if
its later image-saving job fails; the acquisition logs report save failures.

Higher Tenengrad often indicates sharper detail, but specimen content, noise,
illumination, gain, and exposure also affect it. Compare similar images and inspect
the associated AF displacement, outcome, and actual imaging Z. This plot measures
data-image sharpness, not the laser spot's sharpness or a before/after data-image pair.

### What `current` and `previous_success` mean

Each `events/<event_id>/` folder belongs to one recorded event. The filename prefixes
identify which event supplied the evidence:

| Prefix | Meaning |
| --- | --- |
| `current` | The event named by this folder's `<event_id>`. It may be a successful correction, a failed correction, a displacement measurement, or a table-only visit. |
| `previous_success` | A copy of evidence from an earlier successful AF operation, included for comparison when it matches the current operation's region, reference ID, and kind (`correction` or `measurement`). |

For each prefix, the files have these meanings:

- **`*_native.tiff`**: the last native focus-camera frame retained during that
  operation's measurement/verification sequence. It can be a rejected frame if the
  operation failed. It is a single frame; `native_*.tiff` holds the current operation's
  full sequence when saving all artifacts.
- **`*_full_sensor.tiff`**: the full-sensor focus-camera snapshot taken after that
  event's AF work, potentially after rollback or fallback. Use the associated YAML's
  `frame_z_mm` and `snapshot_z_mm` to distinguish the two capture positions.
- **`*.yaml`**: that event's metadata. `previous_success.yaml` retains the earlier
  event's own ID, phase, timepoint, FOV, positions, and results. Use these fields to
  locate its row in `events.csv` and its corresponding imaging data.

The collector remembers **one most recent successful operation with a native frame
across the whole run**. It does not keep a separate history for each region or search
older successes for a match. A failure or table-only visit leaves that remembered
success unchanged; a new success with a native frame replaces it. Consequently:

- `previous_success` may come from a different FOV or timepoint, or from the seed
  phase. Those fields are not part of the matching rule.
- Several consecutive failures can share the same `previous_success`.
- The files are absent if no success has been remembered yet, or if the remembered
  success has a different region, reference ID, or operation kind. A table-only event
  saves only `current.yaml` in save-all mode.

For example, if a correction succeeds at FOV 4 and fails at FOV 5 in the same region
with the same reference, the FOV 5 folder contains `current` evidence from FOV 5 and
`previous_success` evidence from FOV 4. Compare their YAML before interpreting the
images as a change in focus. A successful measurement means a finite displacement was
obtained; it may still carry a large-displacement warning.

Both prefixes describe observed events. The configured AF reference crop is stored
under `references/<reference_id>/`; `baseline/` preserves the first successful event
with a native frame. Individual TIFFs are omitted when their frame was unavailable.

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

Only one previous success is held in memory. Both validation modes retain each
operation's before/after shots and `current` evidence on disk. With imaging enabled,
the full native measurement/verification sequence is saved too. Memory for that
sequence is released after each FOV's evidence is written.
Full-sensor capture restores the actual focus-camera ROI, streaming state, and callback
state, and turns off the laser. A restoration failure stops the run. Evidence-write
failures request acquisition abort.

## Interpreting a comparison

Use the same regions, references, AF settings, seed mode, and timepoints for comparisons.
Separate seed corrections, acquisition corrections, and displacement-only checks.
Inspect failures and residual displacement by region and timepoint, including rollback
and stale fallback. Before/after snapshots, saving AF frames, and scoring data images add time and disk I/O,
so validation runs are not a benchmark of normal imaging throughput. AF-only runs
also omit the thermal load from normal imaging.

This mode keeps the existing spot detector, acceptance thresholds, calibration, and
closed-loop correction. The independent matched-filter acceptance and next-FOV drift
prediction experiments are not enabled. Hardware validation is required to establish
whether focus reliability or residual displacement improves on the microscope.
