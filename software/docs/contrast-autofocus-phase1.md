# Frequency-assisted contrast autofocus (phase 1)

This is an experimental, manual stage-Z method. The observation state's `contrast_af`
block selects `frequency_assisted`; states without the block keep the legacy scan.
The selected top-level focus measure remains LAPE, GLVA, or TENENGRAD. Multipoint
acquisition refuses a frequency-assisted autofocus state until phase 3 validates
frame ownership in that path.

The Contrast AF tab keeps the legacy Δ Z and plane count, Autolevel, focus
measure, Method, and run button visible. Tools → Contrast AF Tuning opens the
advanced frequency-assisted settings popup. A state with no contrast-AF block displays a provisional
20× preset and starts the frequency-assisted method when the operator presses
Autofocus: coarse/medium/fine steps of 10/2/1 µm, a ±30 µm window about current
Z, T=0.50, a 100-frame/130-move limit, 120-second elapsed and 10-second total
exposure budgets. Existing states with an explicit legacy method retain it.

**Check the encoded sensor full scale before the first hardware run.** The preset
uses 65535 encoded counts, but a 12-bit signal in a 16-bit array may use 4095
when unshifted or a different scale when shifted. The software does not infer
this from a frame. The defaults are only a starting point for a 20× objective;
they do not establish a specimen-safe travel interval or calibrated focus
performance. E uses T=0.50 as an unvalidated amplitude threshold, not a
spatial-frequency cutoff. The popup shows the physical interval after clipping
the requested window to configured stage limits and reserving CephlaStage's
backlash travel. The search starts at the lower end of that interval and
restores the starting plane after a non-success outcome when motion is healthy.

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
