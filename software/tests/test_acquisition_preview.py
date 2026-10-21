"""Preview scheduling is independent of channel/Z counts and result ordering."""

import numpy as np
import pytest

from control.core.acquisition_preview import (
    AcquisitionPreview,
    PreviewStarted,
    PreviewCaptured,
    PreviewFrame,
    PreviewProcessed,
)


def key(fov=0, t=0, region="A1", root="run"):
    return (root, t, region, fov)


def frame(position, channel, value, z=0, nz=1):
    return PreviewFrame(position, channel, np.full((2, 3), value, dtype=np.uint16), z, nz)


def values(preview):
    return {name: int(image[0, 0]) for name, image in preview.snapshot().images.items()}


def test_per_z_holds_sparse_channels_and_does_not_mix_positions():
    p = AcquisitionPreview()
    p.accept(PreviewStarted(key(), 5))
    p.accept(frame(key(), "reference", 10))
    for z in range(4):
        p.accept(frame(key(), "stack", z, z, 4))
        assert values(p) == {"reference": 10, "stack": z}
    p.accept(PreviewCaptured(key()))
    p.accept(PreviewStarted(key(1), 1))
    p.accept(frame(key(1), "different", 25))
    assert values(p) == {"different": 25}


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("choice,expected", [("middle", 2), ("last", 3)])
def test_position_waits_for_capture_callbacks_and_processing(reverse, choice, expected):
    p = AcquisitionPreview()
    p.mode, p.z_choice = "position", choice
    p.accept(PreviewStarted(key(), 5, ("group",)))
    # Capture end can precede the deferred camera callbacks.
    p.accept(PreviewCaptured(key()))
    p.accept(PreviewProcessed(key(), "group", {"output": np.full((1, 1), 20)}, {"output": np.full((1, 1), 30)}))
    assert p.snapshot() is None
    p.accept(frame(key(), "reference", 10))
    for z in (reversed(range(4)) if reverse else range(4)):
        p.accept(frame(key(), "stack", z, z, 4))
    assert p.snapshot().complete
    assert values(p) == {"reference": 10, "stack": expected, "output": 20 if choice == "middle" else 30}
    p.accept(PreviewStarted(key(1), 2, ("group",)))
    p.accept(frame(key(1), "reference", 90))
    assert p.snapshot().key == key()  # hold until the whole next position completes


def test_delayed_processing_never_rewinds_either_display_mode():
    p = AcquisitionPreview()
    for fov in range(2):
        p.accept(PreviewStarted(key(fov), 1, ("group",)))
        p.accept(frame(key(fov), "raw", fov))
        p.accept(PreviewCaptured(key(fov)))
    p.accept(PreviewProcessed(key(1), "group", {"derived": np.ones((1, 1))}, {}))
    p.accept(PreviewProcessed(key(), "group", {"derived": np.zeros((1, 1))}, {}))
    assert p.snapshot().key == key(1)
    assert values(p)["derived"] == 1
    p.mode = "position"
    assert p.snapshot().key == key(1)


def test_processing_failure_completes_with_explicit_error():
    p = AcquisitionPreview()
    p.mode = "position"
    p.accept(PreviewStarted(key(), 1, ("bad",)))
    p.accept(frame(key(), "raw", 5))
    p.accept(PreviewCaptured(key()))
    assert p.snapshot() is None
    p.accept(PreviewProcessed(key(), "bad", {}, {}, "failed"))
    assert p.snapshot().complete
    assert p.snapshot().errors == ("bad: failed",)


def test_pending_memory_is_bounded_and_old_results_and_runs_are_ignored():
    p = AcquisitionPreview(max_pending=2)
    for fov in range(10):
        p.accept(PreviewStarted(key(fov), 1, ("slow",)))
        p.accept(frame(key(fov), "raw", fov))
    assert len(p.pending) == 2
    assert p.dropped_positions == 8
    assert not p.accept(PreviewProcessed(key(), "slow", {}, {}))
    p.reset()
    p.accept(PreviewStarted(key(root="new"), 1))
    assert not p.accept(frame(key(), "raw", 99))
    assert values(p) == {}


def test_repeated_visits_do_not_reset_accumulation_and_inputs_are_owned():
    p = AcquisitionPreview()
    start = PreviewStarted(key(), 2)
    p.accept(start)
    image = frame(key(), "raw", 8)
    p.accept(image)
    image.image[:] = 0
    assert values(p) == {"raw": 8}
    assert not p.accept(start)
    p.accept(frame(key(), "raw", 9))
    p.accept(PreviewCaptured(key()))
    p.mode = "position"
    assert values(p) == {"raw": 9}
