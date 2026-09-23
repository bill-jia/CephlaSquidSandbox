# Frequency-assisted contrast autofocus (phase 1)

This is an experimental, manual stage-Z method. The observation state's `contrast_af`
block selects `frequency_assisted`; states without the block keep the legacy scan.
The selected top-level focus measure remains LAPE, GLVA, or TENENGRAD. Multipoint
acquisition refuses a frequency-assisted autofocus state until phase 3 validates
frame ownership in that path.

Before a run, enter explicit coarse, medium, and fine steps in micrometres, window
extents below and above the current Z, and the **encoded sensor full scale**. The
last value must reflect the actual pixel processing path: a 12-bit signal in a
16-bit array may use a scale of 4095 when unshifted, or a different scale when
shifted. The software does not infer it from a single frame. E uses T=0.50 as an
unvalidated initial amplitude threshold; it is not a spatial-frequency cutoff.
Set bounded frame, move, elapsed-time, and exposure budgets before trying a
specimen. The panel shows the physical interval after intersecting the requested
window with configured stage limits and reserving CephlaStage's backlash travel
inside the permitted bounds. The search starts at the lower end of that
interval and will restore the starting plane after a non-success outcome when
motion remains healthy.

The code has only software/simulation validation. For hardware acceptance on a
representative textured field:

1. Establish a safe, finite stage-Z interval for the specimen and independently
   identify a reference focus plane. Record the camera pixel encoding/full scale,
   exposure, gain, ROI, binning, and objective.
2. Run the manual method from several positions on both sides of that plane.
   Inspect the scalar trace, accepted candidate, final readback, verification
   score, frame/move counts, and any fallback. Compare repeatability with the
   independently identified plane; do not treat a software `success` as
   calibration of T or the step sizes.
3. Exercise the configured Tucsen or Toupcam trigger route, including the NI-DAQ
   hardware-line route where installed. Check that one trigger owns one AF frame
   and that the final AF frame never appears in live/acquisition callbacks.
4. Cancel during a frame wait and during a scan. Exercise a camera timeout and a
   stage fault in a safe setup. Confirm legal rollback only after camera failures
   or cancellation, and no blind move after a stage fault.
5. Repeat with live view initially on and off. Confirm trigger mode, streaming,
   callbacks, camera exposure/gain, illumination, and GUI button state are
   restored, with no extra scan when the button resets.

This procedure is a separate rig gate. It does not validate a universal T,
sample-specific search parameters, unattended multipoint use, or FESE.
