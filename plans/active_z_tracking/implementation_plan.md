# Active Z tracking: implementation plan

Status: proposed design; no acquisition or hardware behavior has been implemented.

## 1. Objective and agreed assumptions

Maintain fluorescent moving tissue inside a multipoint Z stack over time by updating the next stack's lower and upper Z limits. Preserve useful empty space between the tissue and both stack boundaries, while independently enforcing calibrated objective/stage clearance limits.

This is tissue-envelope tracking. The tissue may translate, deform, tilt, expand, or contract; a single rigid axial displacement is insufficient to describe all of these cases.

Agreed assumptions and scope:

- Images are optically sectioned fluorescence images.
- The first stack contains the relevant labeled tissue completely, with empty Z buffers at both ends.
- The buffer is sufficient to prevent escape during a single stack/timepoint. This does not establish an unlimited bound on motion between observations.
- Stack acquisition probably occupies at most about 25% of the timepoint interval; actual slice timestamps and revisit times must still be used.
- Timepoint 1 establishes the initial tissue envelope and buffers. Timepoint 2 provides the first motion estimate. Tracking updates apply to timepoint 3 onward. These are human-facing numbers; existing zero-based indices must be converted explicitly.
- Detection uses whole-slice foreground components. XY tiles are not part of the initial algorithm.
- This feature applies only to multipoint acquisition, including a multipoint time series at a single position. Standalone fast acquisition is out of scope, with no changes planned to its controller, buffers, or writer.
- No extra exposures, full-volume segmentation, neural-network inference, or image registration are required for the baseline implementation.

The detector measures the extent of the fluorescent label, not necessarily the anatomical extent of unlabeled tissue. A stable structural label is the preferred tracking channel.

## 2. Recommended delivery scope

Implement fixed-depth stack translation first, with independent boundary estimates. Keep `NZ` and the Z step fixed and move both stack endpoints together. This fits the existing acquisition and storage assumptions and can maintain both buffers while the required tissue envelope fits within the configured stack depth.

Then add adaptive depth within multipoint acquisition: independently adjust the endpoints and change `NZ` while keeping the Z step fixed. This is required when tissue deformation makes the desired envelope wider than the initial stack. Fixed-depth tracking must report this situation rather than silently discard one buffer.

These are implementation stages, not an assertion that fixed-depth translation handles all tissue motion. Both stages belong to this plan; adaptive depth requires explicit scheduler and storage work described below.

Initial supported operating combination:

- Ordinary multipoint acquisition with one unambiguous, raw, full-stack observation state selected for tracking.
- Stage-driven Z stacks, fixed `NZ`, fixed Z step, and independently tracked acquisition positions.
- Complete-stack orders `T, Pos, Z, C` and `Pos, T, Z, C`, including traversal reversal where already supported.
- Runtime contrast autofocus and reflection autofocus disabled while active tracking owns the stack coordinates. Autofocus may be used to set up the initial acquisition before tracking starts.
- All channels use the committed stack plan; tracking runs on only the selected observation state.
- At least three timepoints, multiple Z slices, and an initial stack with detectable tissue plus credible blank space at both ends.

Add piezo support, additional loop orders, and autofocus composition only after their coordinate and scheduling contracts are implemented and tested. Unsupported combinations must fail preflight with an actionable explanation, rather than silently disable tracking or change the user's acquisition order.

## 3. Existing integration points

Paths below are relative to the repository root and were inspected for this plan.

| Existing location | Relevance and planned integration |
| --- | --- |
| `software/control/core/multi_point_utils.py` | `AcquisitionParameters` snapshots acquisition settings. Add an optional tracking configuration and immutable safety-calibration reference. |
| `software/control/core/multi_point_controller.py` | Owns `NZ`, `deltaZ`, `z_range`, parameter construction, preflight, and `acquisition.yaml` serialization. Validate supported combinations and persist tracking settings. |
| `software/control/acquisition_yaml_loader.py` | Load the new optional settings with disabled defaults for older files. |
| `software/gui/widgets/multipoint.py` | Expose tracking controls in the shared multipoint UI, including tracking observation state and buffers. |
| `software/control/core/multi_point_worker.py` | Integrate with capture metadata, decoded-image dispatch, per-position stack completion, and stack positioning. Relevant methods include `_on_frame_arrived`, `_image_callback`, `acquire_at_position`, `initialize_z_stack`, `prepare_z_stack`, and stack return/move methods. |
| `software/control/core/ordered_acquisition.py` | Manages per-timepoint context and partial position visits. Freeze one plan across all visits belonging to the same logical stack. |
| `software/control/models/acquisition_order.py` | `iter_ordered_visits` receives fixed `nz` and reference-Z values. Adaptive depth cannot be implemented by changing a global `NZ` during this traversal. |
| `software/control/core/job_processing.py` | `CaptureInfo` carries position, timepoint, observation state, Z index, and optional piezo Z. Extend immutable capture context and audit storage metadata. `AcquisitionInfo` currently assumes an acquisition-wide Z count. |
| `software/control/core/backpressure.py` and `software/docs/development/acquisition-backpressure.md` | Existing bounded job processing. Tracking needs its own result/deadline contract and must coexist with saving backpressure. |
| `software/control/core/objective_store.py` | Identifies the objective and pixel calibration. The inspected code does not establish a validated working-distance-based collision envelope. Add explicit calibration data rather than infer it from magnification. |
| `software/squid/abc.py`, `software/squid/stage/cephla.py`, `software/squid/stage/utils.py` | Stage movement, coordinate conversion, limit programming, backlash compensation, and retraction. Validate intermediate movements as well as final stack endpoints. |
| `software/squid/stage/prior.py` | `set_limits` is currently a no-op. Do not treat all stage implementations as having equivalent enforced-limit capability. |
| `software/control/core/zarr_writer.py`, `software/control/core/utils_ome_tiff_writer.py` | Audit and extend output metadata for moving stack origins and, later, variable depth. |

The multipoint callback already associates incoming frames with capture metadata. Tracking should reuse that association. The fast-acquisition implementation is not an integration target.

## 4. Architecture and ownership

Use small hardware-independent components with a thin multipoint adapter:

```text
Multipoint capture + immutable capture context
    -> decoded image dispatch
    -> bounded tracking worker
    -> per-slice foreground evidence
    -> complete per-position profile
    -> boundary estimate + motion model
    -> proposed next stack
    -> independent safety validation
    -> acquisition worker commits plan before the next stack
```

Proposed new modules:

- `software/control/models/active_z_tracking.py`: validated user configuration and serialized tracking records.
- `software/control/core/active_z_tracking/detector.py`: whole-slice foreground extraction.
- `software/control/core/active_z_tracking/profile.py`: slice aggregation, completeness, and boundary estimation.
- `software/control/core/active_z_tracking/predictor.py`: independent boundary motion models and uncertainty.
- `software/control/core/active_z_tracking/planner.py`: fixed-depth and adaptive-depth stack proposals.
- `software/control/core/active_z_tracking/service.py`: bounded asynchronous execution, result delivery, and lifecycle.
- `software/control/models/z_safety_calibration.py`: calibration identity, validity, and mechanical-clearance inputs.
- `software/squid/stage/z_safety.py`: shared motion validation and stage capability contracts. Final placement should follow existing stage abstractions during implementation.

The detector and predictor cannot move hardware. The acquisition worker is the sole owner of stack-plan commits. The safety layer can reject a plan regardless of tracking confidence.

### 4.1 Tracking identity and records

Maintain independent state keyed by `(run_id, region_id, fov_id, tracking_source_id)`. Use stable position identity, not current visitation order. Add a generation identifier so callbacks from an aborted or previous run cannot affect the new run.

Define explicit records:

| Record | Required contents |
| --- | --- |
| Slice observation | Run/stack/plan IDs, source/event identity, logical plane index, commanded Z, measured Z when available, monotonic capture time, foreground statistics, validity flags. |
| Stack profile | Expected plane identities, received/failed plane identities, physical Z order, per-slice evidence, acquisition time span, completeness state. |
| Boundary estimate | Conservative lower/upper coordinates, each boundary's observation time or time bracket, localization uncertainty, truncation flags, quality reasons. |
| Tracker state | Last accepted measurements, separate lower/upper velocities, prediction-error history, baseline buffers, last committed plan, state/reason. |
| Stack plan | Explicit physical plane coordinates, scan direction, `NZ`, step, expected duration, reference-plane semantics, calibration ID, requested versus accepted limits, reason. |

Track commanded and measured positions separately; do not label commanded coordinates as encoder measurements. All tracking calculations use one canonical physical Z coordinate in micrometers. Conversion to the stage API's millimeters occurs at a documented boundary. Positive canonical Z must not be assumed to mean motion toward the sample.

## 5. Streaming whole-slice detection

### 5.1 Select a consistent tracking source

Select one raw observation state present at every Z plane of every tracked position. Preflight checks that it has compatible optical sectioning and that its full-stack capture can be identified unambiguously.

For the first implementation, reject a tracking source with multiple indistinguishable repeats at a plane or one requiring online postprocessing. Later support can specify a deterministic repeat index or explicit reduction rule. Never count duplicate frames as additional Z evidence.

Keep exposure, illumination, gain, and detector parameters fixed during a run. If supported settings change, invalidate the previous detection calibration and explicitly reinitialize; do not interpret the resulting intensity change as tissue motion.

### 5.2 Per-slice processing

1. Read the quantitative image before display contrast/color transformations. Apply known bad-pixel correction and existing calibrated intensity correction where available.
2. Optionally reduce resolution for analysis, preserving the smallest biologically relevant feature. Choose the reduction from physical pixel size and a declared minimum component area, not an arbitrary thumbnail size.
3. Estimate background and noise robustly, producing a threshold such as `background + k * noise`. Bootstrap from the known empty outer slices of the initial stack. For reverse traversal, use whichever empty edge arrives first; retain bounded bootstrap inputs if needed.
4. Do not estimate background solely from the median of a tissue-filled slice. Freeze or slowly update the background model using only confidently blank observations, and apply a noise floor to avoid zero-MAD thresholds.
5. Generate a foreground mask and extract connected components across the whole slice. Reject known bad pixels and components smaller than the validated minimum area in square micrometers.
6. Preserve any credible component, including a small protrusion that occupies a tiny fraction of the full image. Record total area, largest component area, significant component count, signal-to-background evidence, and saturation fraction.
7. Emit compact statistics and quality flags; release the image as soon as its ownership contract permits.

Thresholds, component size, connectivity, and downsampling must be selected on representative data. No fixed global fluorescence percentile defines the tissue bounds. Total intensity may be logged as a diagnostic, but is not the primary control signal.

A small spatial background correction may eventually be useful; it does not require an XY-tiled tracking architecture. Avoid aggressive morphological operations that erase thin tissue structures.

### 5.3 Turn the profile into an envelope

- Sort observations by physical Z, independent of arrival order and acquisition direction.
- Classify each slice as credible foreground, credible background, or uncertain. Missing or failed slices are unknown, never blank.
- Find the lowest and highest credible foreground across the entire profile. Internal empty slices do not split the envelope or cause the controller to drop disconnected tissue.
- Use adjacent blank slices to bracket the boundaries conservatively. For example, the lower boundary lies between the last credible blank plane below the tissue and the first occupied plane. Use the outer side of the bracket for coverage planning.
- Account for finite Z sampling and optical section thickness with an explicitly calibrated localization allowance. Do not claim sub-slice accuracy from a binary detector.
- Adjacent-slice persistence can strengthen weak detections. A strong component in only one slice must not be rejected automatically, because real thin structures may have that appearance.
- Foreground on the first or last acquired physical plane marks that boundary as censored/truncated. Absence of foreground everywhere marks a lost or unobservable target, not a zero-thickness sample.
- Return explicit quality reasons instead of treating an uncalibrated scalar confidence as a probability.

No 3D image volume needs to be retained. With `N` analyzed pixels and `Z` planes, expected detector work is approximately `O(NZ)`, with `O(Z)` persistent summary storage per stack plus bounded in-flight image memory.

## 6. Initialization, prediction, and timing

### 6.1 Timepoints 1 and 2

Acquire the first two stacks at the user-configured range. For each position independently:

1. At timepoint 1, verify the initial envelope is observable and both outer buffers are credible. Store the envelope, acquisition geometry, timestamps, and baseline buffers.
2. Define minimum acceptable buffers and target buffers separately. The UI may initialize these from the first stack, but the usable buffer must subtract boundary-localization uncertainty and reserve some space for prediction uncertainty.
3. If the first stack cannot support the requested buffers, report the problem before arming tracking. Do not silently redefine a minimum buffer to make the configuration pass.
4. At timepoint 2, validate the second envelope and estimate the velocities of its lower and upper boundaries.
5. Prepare the first updated plan for timepoint 3.

The first stack's successful initialization is not proof that the second remains complete. If a bootstrap stack is truncated or invalid, stop automatic progression before the first adaptive move and report the assumption violation.

### 6.2 Independent boundary prediction

For a valid boundary measurement `b` acquired at representative time `tau`, initialize velocity from successive measurements:

```text
v_b = (b_current - b_previous) / (tau_current - tau_previous)
predicted_b(t) = b_current + filtered_v_b * (t - tau_current)
```

Maintain separate models for lower and upper boundaries. Use light velocity smoothing after the initial estimate, physical plausibility checks, and observed prediction residuals to grow or reduce the uncertainty allowance. Preserve large credible outward innovations instead of filtering them away as noise. Implausible innovations lower confidence and trigger the failure policy.

Boundary times come from the relevant edge-slice observations, not one global timepoint-start timestamp. A bracket spanning multiple slices has timing uncertainty. Use a monotonic time base for differences and explicitly map hardware timestamps when available.

Do not infer tissue velocity from stack-relative plane indices: moving the stack changes those indices even if tissue is stationary.

### 6.3 Cover the entire next stack

At the next visit to a position, estimate its actual start time `t_start` and full stack completion time `t_end`, including all channels, settling, waits, and supported cycle events.

```text
required_low  = min(predicted_lower(t_start), predicted_lower(t_end))
                - lower_buffer - lower_uncertainty
required_high = max(predicted_upper(t_start), predicted_upper(t_end))
                + upper_buffer + upper_uncertainty
```

This endpoint calculation applies to the initial linear motion model. A later nonlinear model must bound its entire time trajectory.

Timepoint spacing is local to a position's visits. Use actual revisit timing, including scheduling delays; do not assume the UI interval equals the elapsed time between measurements. Refresh the proposal before committing it if the visit is delayed. Detect overruns during a stack and mark coverage assurance invalid if the validated time horizon is exceeded.

Expose uncertainty as a physical allowance informed by measurement resolution, timing uncertainty, residual motion error, and configured motion bounds. Initially this is an engineering bound, not a claimed statistical confidence interval. Arbitrary tissue acceleration cannot be guaranteed away without a validated motion bound.

## 7. Plan selection

### 7.1 Fixed-depth translation

Let `W = (NZ - 1) * abs(delta_z)` and let `[R_low, R_high]` be the required envelope including buffers and uncertainty. A lower stack endpoint `s` covers it exactly when:

```text
R_high - W <= s <= R_low
```

Intersect that interval with the admissible endpoints for the calibrated mechanical range, actuator travel, intermediate-motion constraints, and any maximum per-timepoint shift. If a safe interval is `[S_low, S_high]`, its endpoint constraint is `S_low <= s <= S_high - W`; the motion-path validator supplies additional constraints.

Choose the valid endpoint nearest the preferred buffered placement. Preserve the current plan inside a small deadband only if it still satisfies all minimum coverage constraints. Initial observed buffers can serve as preferred spacing, while explicitly configured smaller minimum buffers leave headroom for uncertainty and modest deformation.

Round to realizable motor positions, construct the actual plane list, then validate again. Quantization must not quietly remove a buffer or cross a safe limit. If no representable plan exists, declare infeasibility.

Do not silently cap an excessive requested movement and call the result successful. A movement cap can make the coverage constraints infeasible.

### 7.2 Adaptive-depth extension

Keep the Z step fixed; round endpoints outward to cover the required envelope. Compute `NZ = ceil((high - low) / abs(delta_z)) + 1`, then validate the realized endpoint coordinates.

- Expand promptly when credible outward motion requires additional coverage.
- Require repeated valid inward observations before shrinking. Expansion and shrinkage thresholds are independently configurable.
- Enforce maximum `NZ`, total stack depth, acquisition duration, and calibrated mechanical range.
- Recalculate predicted motion over the new duration. Expansion lengthens acquisition, which may require more coverage; use a bounded planning iteration and reject a plan that does not converge within the allowed budgets.
- Never coarsen the Z step automatically to force the range into a plane-count budget.

Make per-position/per-timepoint plane lists explicit in the scheduler. Update event enumeration, progress, expected frame counts, and output layouts from the committed plan rather than mutating shared global `NZ` mid-run.

## 8. Multipoint integration and concurrency

### 8.1 Image dispatch

- Keep `_on_frame_arrived` limited to its existing frame/metadata correlation and pacing responsibilities.
- In the decoded-image dispatch path, enqueue only matching tracking-source frames with immutable capture context.
- Use a dedicated bounded worker and result channel. A thread is a reasonable initial candidate for NumPy/OpenCV operations; benchmark it before deciding whether process isolation is needed.
- Prove image lifetime: an enqueued image must remain valid until analysis completes. Use an owned buffer, an explicit lease, or a bounded analysis copy; do not retain a pointer to camera memory that may be reused.
- Avoid a second full-resolution copy where existing ownership permits safe sharing. Any copy or downsampling in callback context counts toward callback latency and must be measured.
- Return small summaries, not masks or whole images, unless explicitly collecting diagnostics.

Tracking queue capacity is bounded separately from saving. If enqueue fails, record that plane as unavailable for tracking and mark the stack incomplete. Do not silently drop its evidence, block a camera callback, or delay image saving indefinitely.

### 8.2 Completion and commit

Define an expected manifest of tracking plane identities for every logical stack. Duplicate, missing, out-of-order, late, and failed results must be handled explicitly. Repeated visits to the same physical plane do not complete unrelated expected observations.

Finalize as soon as that position's tracking stack is complete, even if other positions are still being acquired. `_wait_for_outstanding_callback_images()` only establishes callback dispatch completion; it must not be treated as proof that tracking analysis has finished.

Before the next stack starts, wait only within a configured tracking deadline and then follow the failure policy. Use immutable result handoff; analysis callbacks must not modify the active stage coordinates, GUI controls, or acquisition counters.

Commit exactly one plan per `(run, position, logical timepoint)`. All observation states and revisits associated with that stack use the same plan. Freeze it before the first relevant hardware move and keep it unchanged until the stack finishes.

Audit all positioning paths, not just `prepare_z_stack`: nominal FOV navigation, `initialize_z_stack`, `_ordered_focus` restoration, stack return moves, and end-of-run restoration must not overwrite the committed range or issue an unsafe intermediate move.

### 8.3 Acquisition order and channels

Initial orders ensure a position's complete stack is observed before its next timepoint. Reject orders that place Z outside time in a way that advances to later timepoints before completing the current profile.

Additional supported orders must pass a dependency check: the prior complete tracking profile is available before committing the next stack, and all channels for a logical stack share its plan. Handle partial ordered visits using the manifest, not a generic visit-end event.

Reference-Z-only captures require an explicit physical reference-plane rule under tracking. For the first release, keep the existing top/bottom/center interpretation relative to the committed stack and record the resulting physical coordinate. Reject protocols needing an incompatible fixed anatomical reference until that behavior is implemented.

If a tracked position lacks the selected source, fail preflight. Independent per-position tracking does not provide XY following or a globally simultaneous envelope across a moving tissue spanning many FOVs.

### 8.4 Autofocus and piezo

Runtime autofocus can otherwise reset Z before each stack and undo tracking. The initial implementation rejects concurrent runtime autofocus. A later composition must define whether autofocus provides a mechanical reference correction and how that correction transforms all stored tissue coordinates without double counting motion.

Piezo support needs a calibrated mapping from coarse-stage position and piezo displacement to effective focal Z, plus a separate mechanical-clearance calculation. Do not simply add their nominal values without validating sign, scale, offsets, and which actuator moves the objective or sample.

Before enabling piezo stacks, validate combined travel, any coarse-stage recentering, actuator reset/return paths, and per-frame metadata. This extension remains within multipoint acquisition.

## 9. Independent collision protection and calibration

### 9.1 Calibration contract

Use a flat sample to establish a repeatable coverglass reference, but do not equate nominal objective working distance with a complete collision model. Objective front geometry, stage/holder geometry, coverslip seating, tilt, immersion conditions, and actuator behavior all matter.

Store a versioned calibration containing:

- Microscope/stage identity, objective identity, holder/sample-mount identity, and coordinate-system version.
- Measured reference position, which glass interface was identified, measurement method, and repeatability/uncertainty.
- Objective working distance with provenance, configured conservative allowance, and validated mechanical-clearance limit.
- Direction toward the sample in canonical coordinates and the complete actuator mapping.
- Valid XY region or measured surface/clearance map, permissible travel corridor, and a verified retraction position.
- Stage/piezo travel constraints, backlash/overshoot allowance, and limit-enforcement capabilities.
- Date, calibration version, homing/reference identity, and invalidation conditions.

A single flat reference transfers to another sample only when the mounting geometry is repeatable within the declared allowance or the new sample surface is re-referenced. For multipoint travel, either validate clearance over the entire used XY region or use a conservative global limit derived from that region. A local focus height alone does not establish safe lateral travel past a holder rim.

Calibration must begin from a known safe pose and a bounded search interval. Do not find the surface by driving toward it without an existing safe search bound, and never use physical contact as a calibration step. Laser reflection may identify a glass interface; its identity and offset must be verified before using it as the safety reference.

Changing objective, holder, seating, stage zero/homing relationship, coordinate configuration, or relevant mechanical setup invalidates calibration unless a verified transformation preserves it. A tissue tracking estimate can never expand mechanical limits or recalibrate them automatically.

### 9.2 Enforcement

Validate the complete motion sequence, including:

- Entry into the first plane and every stack step.
- Relative and absolute moves, motor rounding, backlash overshoot, and return moves.
- Observation-state offsets and any supported autofocus move.
- XY travel/retraction/restore, final return to the acquisition start, and abort recovery.
- Piezo reset and coarse recentering when piezo support is added.

Enforce limits at a shared stage/motion boundary, with controller/firmware limits where available. The Cephla implementation has canonical/raw coordinate conversions and explicit backlash movements; both must be exercised in validation. The Prior implementation's no-op `set_limits` cannot satisfy an enforced-limit contract by itself.

Gate automatic operation on validated stage capabilities. Audit joystick/direct-controller paths that can bypass a host-side wrapper. Existing valid hardware limits must never be widened automatically. Do not program local imaging-only limits that prevent a verified safe retraction; distinguish the imageable Z range from the admissible mechanical travel envelope.

The planner may choose another safe placement if both minimum buffers still fit. If clipping to a safety limit would lose required coverage, report `coverage_unachievable` and stop before the next stack. Safety checks must not silently convert an invalid request into a supposedly successful one.

## 10. State transitions and failure policy

Suggested states: `disabled`, `initializing`, `learning_motion`, `tracking`, `degraded`, `blocked`, and `finished`. Log a structured reason for every transition.

| Condition | Required behavior |
| --- | --- |
| Valid first stack | Establish baseline; acquire timepoint 2 at the initial plan. |
| Valid second stack | Initialize velocity; prepare timepoint 3. |
| Complete confident profile | Update model and prepare a candidate next plan. |
| Missing/late analysis, source frame failure, or queue overload | Mark profile incomplete; never treat missing data as blank. |
| No detectable tissue or severe signal degradation | Do not shrink or chase noise. Enter degraded handling. |
| Tissue on an outer plane | Mark that boundary censored; stop claiming a complete envelope. |
| Required envelope cannot fit fixed depth | Report depth infeasibility; adaptive-depth mode may expand within budgets. |
| Mechanical limit, invalid calibration, or unsafe path | Block the next stack before motion; report exact constraint. |
| User abort or tracking exception | Cancel pending plans, drain/release tracking resources, and follow verified acquisition cleanup paths. |

Initial degraded policy: stop cleanly before the next stack, turn illumination off through existing acquisition cleanup, and preserve the last good result plus diagnostics. An acquisition pause is suitable only if the existing pause lifecycle is reliable; otherwise abort cleanly rather than invent an implicit pause.

A later optional continuation policy may hold or expand a plan for a limited number of visits, but only when a validated reachable envelope still fits safely. Holding the previous range alone is not evidence that moving tissue will remain covered. Automatic search/recovery after losing tissue is outside this implementation.

## 11. User configuration and observability

Expose a compact multipoint control group:

- Enable active Z tracking; disabled by default.
- Tracking observation state.
- Lower/upper minimum buffers and preferred buffers, in micrometers; optionally initialize preferred values from timepoint 1.
- Range mode: fixed depth initially; adaptive depth once supported.
- Maximum depth/plane count and maximum allowed displacement per revisit.
- Calibration status and calibrated safe imaging range.
- Analysis-only preview mode that produces proposals without modifying the configured acquisition.

Keep detector thresholds, minimum component area, velocity smoothing, uncertainty parameters, queue capacity, and deadlines in an advanced configuration. Defaults must be established from replay and target-hardware measurements, with physical units and validation.

Show the measured tissue envelope, acquired range, proposed next range, actual buffers, and tracking state per position. A simple Z-versus-time plot with tissue and stack boundaries is useful. Display distinct messages for detection failure, unavailable analysis, insufficient depth, and safety-limit exhaustion.

Persist configuration in `acquisition.yaml`, including algorithm version, selected source, coordinate convention, buffer policy, and calibration identifier/snapshot. Older YAML files load with tracking disabled. Loading a saved calibration reference does not bypass current validity checks.

## 12. Data and metadata

Even fixed-depth tracking changes physical Z at the same array index over time. Preserve plane indices as storage indices, and record actual physical coordinates separately.

- Extend capture context with plan ID and physical tracking Z; retain stage and piezo coordinates separately.
- Persist a per-stack decision record, for example `active_z_tracking.jsonl`, with observed bounds/times, uncertainties, velocities, requested and committed ranges, achieved buffers, reasons, and calibration ID.
- Persist per-plane positions and timestamps in the acquisition's appropriate coordinate metadata. Include commanded versus measured provenance.
- Audit TIFF, OME-TIFF, Zarr, and live-view behavior. Do not represent moving stacks as having one invariant physical origin across all timepoints.
- If a format cannot express a time-varying origin directly, write an authoritative sidecar and document how downstream software should use it. Verify that export does not silently claim a false common coordinate transform.
- For adaptive depth, choose and implement an explicit layout: separate stack arrays/files or a padded fixed-capacity array with valid-plane metadata. Missing/padded planes are not biological zeros. Do not enable adaptive depth for a writer until its layout, resume behavior, and reader interpretation are tested.
- Make decision-record writes occur outside the camera callback and include analysis-only proposals in preview runs.

Changing Z endpoints must not rewrite previous stack metadata or alter previous image data.

## 13. Validation and acceptance criteria

### 13.1 Detector and planner tests

Use synthetic images with known foreground geometry and recorded examples. Cover:

- Rigid movement in both Z directions; changing thickness; independent boundary velocities.
- Thin, dim protrusions; disconnected tissue; internal empty slices; strong single-slice components.
- Background gradients, bleaching, camera offset, hot pixels, saturation, low SNR, and tissue-filled slices.
- Entirely empty profiles, clipped edges, missing/duplicate/out-of-order slices, reverse traversal, and variable timestamps.
- Shifted stack origins: stationary tissue must yield zero physical velocity despite changes in stack-relative index.
- Buffer/deadband behavior, inward/outward quantization, infeasible widths, motion caps, and adaptive-depth planning convergence.

Assert physical coverage and correct uncertainty/state handling, not merely equality to the implementation's arithmetic.

### 13.2 Multipoint and safety integration tests

Use simulated cameras/stages and instrument command traces to verify:

- The first two stacks use the initial plan; the first update applies to the third stack.
- Positions maintain independent state under both initial supported orders and traversal reversal.
- All channels/revisits of one logical stack share one immutable plan.
- Late callbacks after abort/restart cannot move hardware or update the new run.
- Queue saturation and deadline misses terminate or degrade predictably without unbounded memory or camera-callback deadlock.
- No committed or intermediate movement exceeds the validated mechanical envelope, including backlash, retraction, final restoration, and canonical/raw sign inversion.
- Unsupported stage capabilities, stale calibration, autofocus combinations, and tracking sources are rejected before acquisition.
- Output metadata reconstructs the physical Z of every acquired plane, including changing origins and, later, variable depth.
- Tracking-disabled acquisition retains existing behavior and output layout.

Run relevant existing regressions for acquisition order, observation-state snaking, autofocus dispatch, Z retraction, YAML loading, and the affected writers. Exact test selection should follow the implemented changes rather than running unrelated hardware tests.

### 13.3 Closed-loop replay

Build a replay harness under `software/tools/` that feeds frames in acquisition order and applies each proposed range to the next timepoint's observable data. The tracker must see only the slices its simulated acquisition would have captured.

Use generously sampled reference volumes plus synthetic continuous-motion scenes. Ordinary recorded volumes cannot fully reveal how a different scan trajectory would have observed motion during that stack; report this limitation and use simulation for scan-time effects. Annotate or independently estimate ground-truth envelopes on a representative subset rather than use the same detector as ground truth.

Measure:

- Minimum lower and upper retained buffers and frequency/duration of coverage failures.
- Boundary errors, false loss/truncation alarms, and unnecessary stack movement.
- Added planes, exposure burden, and timepoint overruns in adaptive-depth mode.
- Foreground-detection cost, result tail latency after the last tracking slice, queue depth, memory use, and acquisition slowdown.

### 13.4 Performance and hardware release gates

Measure with representative resolution, bit depth, `NZ`, channels, positions, and saving enabled. Average tracking service capacity must exceed the tracking-source arrival rate, queues must remain bounded, and the completed plan must meet the next-stack commit deadline.

Set explicit latency budgets after measuring the existing acquisition cadence. A reasonable provisional engineering target is a p95 final-result tail below 50 ms and below the available inter-stack slack; this is a target to validate, not a promised capability. Also inspect p99/worst-case stalls and deadline-miss behavior.

Run analysis-only acquisition before allowing motion, then a known-clearance fixed sample, controlled motion, and finally moving tissue. Verify stage limits, calibration repeatability, and all recovery paths on each supported hardware configuration before enabling automatic operation there. Do not test collision protection by deliberately contacting the objective with the sample or stage.

Acceptance requires demonstrated buffer retention within the declared motion/detection assumptions, explicit failure when those assumptions fail, and no out-of-envelope commands in simulation and validated hardware trials. No algorithm can guarantee full tissue coverage when relevant fluorescence becomes unobservable or motion exceeds the configured reachable bounds.

## 14. Implementation phases and completion gates

1. **Contracts and replay data.** Finalize units, record schemas, configuration, initial supported multipoint combinations, source selection, timing semantics, and a representative annotated dataset. Deliver pure detector/predictor/planner interfaces and replay scaffolding.
2. **Detection and analysis-only multipoint integration.** Implement bounded per-slice processing and per-position profiles. Produce preview proposals and diagnostics without adaptive moves. Demonstrate foreground reliability, thin-feature preservation, and analysis throughput.
3. **Calibration and independent motion enforcement.** Implement the flat-reference workflow, validity rules, hardware capability checks, and complete-path validation. Exercise backlash, sign conversions, XY clearance, and cleanup. This is a prerequisite for automatic tracking motion, not a post-release enhancement.
4. **Fixed-depth automatic tracking.** Integrate immutable plans into multipoint positioning; apply updates from the third timepoint. Add UI, YAML persistence, metadata, failure handling, and supported-order regressions. Validate in closed-loop replay and staged hardware runs.
5. **Adaptive-depth multipoint tracking.** Replace fixed-global-depth scheduling assumptions for supported paths; implement per-stack output layouts and reader metadata. Validate duration-aware expansion/shrinkage and writer compatibility before exposing the mode.
6. **Additional multipoint combinations, as needed.** Add piezo/coarse-stage composition, further loop orders, and explicit autofocus composition. Each remains disabled until its coordinate, timing, safety, and metadata tests pass. Fast mode remains out of scope.

## 15. Decisions to settle with representative data or hardware

- Which structural label and observation state gives reliable boundary visibility?
- What minimum protrusion/component size must be preserved, and what analysis resolution retains it?
- Which minimum buffers and maximum inter-visit motion bounds are appropriate for the tissue?
- Does fixed-depth translation cover the intended experiments, or is adaptive depth required for the first usable release?
- Which multipoint hardware configurations require piezo support immediately?
- Which holder/objective geometry and stage-limit capabilities can be validated for the first release?
- Which save formats must support moving physical origins and adaptive depth initially?
- What latency and acquisition-slowdown budgets does the measured stack cadence permit?

These are validation inputs and implementation sequencing choices. The core design remains whole-slice component detection, independent boundary prediction, explicit per-position stack plans, and independent calibrated motion enforcement.
