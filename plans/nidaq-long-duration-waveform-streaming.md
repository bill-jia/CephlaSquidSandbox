# Long-duration NI USB-6421 waveform generation

Date: 2026-11-07

## Problem and evidence

Fast acquisition has failed when starting `do_task`, with NI-DAQmx error
`-200946`: the driver could not transfer enough data to the device to start.
The observed failure was at task start, after the initial waveform write returned.
The camera subsequently received zero frames and acquisition timed out.

Hypothesis to test: preparing a large, full-duration output buffer contributes to
the startup failure. This is not yet established. USB transfer behavior, driver
state, concurrent camera activity, and task configuration remain possible causes.

Squid currently constructs full-duration arrays and writes them before starting
the tasks in `software/control/nidaq.py`. AO is the sample-clock master when
present; DO otherwise uses its internal sample clock.

## Documented limits

### Updated diagnosis and priority

Subsequent user observations: the same runs work after unplugging/reconnecting
the DAQ, while restarting Squid alone does not resolve the fault. This points
toward device, driver, or USB state surviving application restart; it does not
identify which component is responsible. A hard waveform-size limit is less
likely. On recurrence, compare NI MAX Reset Device with physical reconnection,
with Squid closed, keeping acquisition settings unchanged.

Keep full waveform preload as the default, especially for 100 kHz output with
1 kHz imaging. Streaming remains a memory-management/indefinite-run feature,
not a proven remedy for -200946. At 100 kHz the DO FIFO represents about 82 ms;
host buffering does not remove dependence on timely USB service.

Immediate priority: clean up partial startup on failure, report the original
exception, and avoid leaving a monitor waiting for the acquisition timeout.
Release partial DAQ tasks without waiting for their requested finite duration,
stop owned camera grabbing and writer resources, and restore live output state.
Start the acquisition monitor only after DAQ startup succeeds. Do not reset or
power-cycle hardware automatically.

| Resource | Capacity | Duration at 10 kHz |
| --- | --- | --- |
| Digital-output onboard FIFO | 8,191 port samples | 0.8191 s |
| Analog-output onboard FIFO | 16,383 samples shared among active channels | 1.6383 s with one AO; approximately 0.819 s with two |
| Host output buffer | Allocated in computer memory | Can exceed onboard FIFO duration |

The documentation reviewed does not specify a fixed USB-6421 maximum host write
size. FIFO capacity is not a maximum waveform length or DAQmx write-call limit.
Practical host allocation and API limits must be considered separately.

A ten-minute waveform at 10 kHz contains 6,000,000 samples. Packed as one 16-bit
digital-port word per sample, its payload is 12 MB and 20 kB/s, excluding protocol
overhead. Squid's per-line arrays, temporary conversions, and driver buffers may
consume additional memory. These calculations do not establish the error cause.

DAQmx normally determines the host output buffer size from the first multi-sample
write unless an output buffer is explicitly configured. The timing sample count
specifies the number of samples to generate, rather than independently defining
the output buffer size.

## Supported approaches

### Onboard FIFO regeneration

Load a short repeating pattern once and repeat it from device memory. The entire
pattern must fit in the FIFO. Enable the DO UseOnlyOnBoardMemory property and
appropriate regeneration settings. The pattern cannot be changed during onboard
regeneration; verify supported settings with the installed driver.

For example, a 2 Hz repeating pattern at 10 kHz is 5,000 samples and fits the DO
FIFO, provided every participating digital line repeats on that same period.
Patterns with slower changes may not fit. AO capacity must be checked separately
if analog channels also participate.

### Host-buffer regeneration

Keep one repeat period in host memory and let DAQmx repeatedly transfer it to the
device. This reduces application allocation but retains ongoing USB traffic.
It suits periodic patterns longer than the onboard FIFO.

### Non-regenerating buffered streaming (recommended general solution)

Keep one uninterrupted hardware-clocked task and append waveform chunks to a
bounded host buffer. Disable regeneration so missing data causes an underrun
instead of silently repeating a previous stimulus.

Initial implementation proposal:

1. Configure a host output buffer containing 5–10 seconds of samples.
2. Prefill the buffer before starting any master clock.
3. Generate and append 0.5–1-second chunks in a background worker.
4. Track sample indices globally; generate every chunk from that index so phase,
   pulse width, and boundary crossings remain consistent.
5. Preserve the shared AO/DO sample clock and start-trigger arrangement. Keep
   corresponding output streams supplied through the same sample horizon.
6. Use a finite hardware sample count for the requested endpoint, subject to
   validation of finite streaming with the installed driver. Avoid a Python timer
   as the precise stop mechanism.
7. Track available write space, accepted sample counts, generated samples, and
   errors. Prevent the GUI thread from blocking on writes.
8. On startup failure, underrun, cancellation, or completion, stop/release all
   participating tasks and restore the intended output state. Report startup
   failures immediately rather than waiting for acquisition timeout.

These buffer/chunk sizes are engineering starting values, not NI requirements.
Host buffering helps tolerate producer scheduling delays; it does not eliminate
USB service interruptions or increase onboard FIFO capacity.

Chunk boundaries should introduce no output gaps while data remains available.
The hardware sample clock determines edge timing, not the Python write schedule.
At 10 kHz, waveform transitions have a 100 microsecond sample grid. This is timing
resolution, not a guarantee of absolute clock accuracy or camera exposure latency.

## Queuing and overlays

Combine camera triggers and illumination patterns in software before queuing
samples. Digital channels can be represented as bits in a packed port sample;
define explicit conflict rules if multiple patterns target the same line.
Reserve only the intended lines and preserve unrelated live-output ownership.

Schedule edits against future sample indices with a defined lead time. Do not
promise immediate modification of samples already transferred to the device.
For initial implementation, prefer immutable queued chunks over in-place changes
to a regenerating buffer, which could mix old and new patterns.

A hardware counter pulse train is another candidate for simple periodic camera
triggers. Evaluate routing, synchronization, and finite pulse-count support
separately before integrating it with arbitrary illumination waveforms.

## Diagnostic and validation plan

1. Record device identity, DAQmx version, USB connection, active channels, sample
   rate/count, host and onboard buffer sizes, regeneration settings, and failure
   stage. Capture write/start timings and the full DAQmx error.
2. Compare the same ten-minute, 10 kHz output using the existing full-duration
   write and a bounded streaming buffer. Hold waveform content and camera load
   constant. Repeat to distinguish reproducible failures from intermittent ones.
3. Compare short and long finite tasks; test DO alone and AO+DO synchronization.
   Separately compare camera streaming enabled/disabled to isolate contention.
4. For periodic signals, compare host regeneration with an onboard pattern of
   at most 8,191 DO samples.
5. Use an oscilloscope or logic analyzer to verify pulse widths, intervals,
   inter-channel alignment, chunk boundaries, and the exact final sample count.
   Check camera exposure feedback when measuring actual exposure timing.
6. Exercise delayed producers and underruns. Confirm clear errors, no unintended
   replay, task cleanup, output restoration, and successful subsequent runs.
7. Confirm bounded memory usage and responsive UI during long runs. Ensure
   preview rendering does not require materializing the full output sequence.

Do not infer that streaming fixes error -200946 until the comparison demonstrates
it. A bus/driver startup issue can affect either buffering strategy.

## Sources

- [NI USB-6421 specifications](https://www.ni.com/docs/en-US/bundle/usb-6421-specs/page/specs.html)
- [USB-6421 digital-output generation methods](https://www.ni.com/docs/en-US/bundle/usb-6421/page/do-data-gen-method.html)
- [DAQmx buffer size allocation](https://knowledge.ni.com/KnowledgeArticleDetails?id=kA00Z000000P9PkSAK&l=en-US)
- [NI analog-output regeneration](https://www.ni.com/en/support/documentation/supplemental/06/analog-output-regeneration-in-ni-daqmx.html)
- [NI error -200946 troubleshooting](https://knowledge.ni.com/KnowledgeArticleDetails?id=kA00Z0000019PRTSA2&l=en-US)

Status: planning and documentation only; streaming implementation and hardware
validation remain to be performed.
