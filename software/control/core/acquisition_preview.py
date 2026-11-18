"""Bounded, GUI-independent live previews for ragged multipoint acquisitions.

Events may arrive from camera and processing threads in either order. A position
is publishable only after capture has ended, every raw frame has arrived, and
every expected processing group has returned (including failed groups).
"""

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import numpy as np

PositionKey = Tuple[str, int, str, int]  # acquisition root, timepoint, region, FOV


def position_key(info) -> PositionKey:
    return (info.acquisition_root or "", info.time_point or 0, str(info.region_id), info.fov)


@dataclass
class PreviewStarted:
    key: PositionKey
    expected_frames: int
    groups: Tuple[str, ...] = ()


@dataclass
class PreviewCaptured:
    key: PositionKey


@dataclass
class PreviewFrame:
    key: PositionKey
    channel: str
    image: np.ndarray
    z: int
    nz: int


@dataclass
class PreviewProcessed:
    key: PositionKey
    group: str
    middle: Dict[str, np.ndarray]
    last: Dict[str, np.ndarray]
    error: Optional[str] = None


@dataclass
class PreviewSnapshot:
    key: PositionKey
    images: Dict[str, np.ndarray]
    complete: bool
    errors: Tuple[str, ...] = ()


@dataclass
class _Position:
    start: PreviewStarted
    order: int
    received: int = 0
    captured: bool = False
    finished_groups: set = field(default_factory=set)
    errors: list = field(default_factory=list)
    latest: dict = field(default_factory=dict)
    middle: dict = field(default_factory=dict)
    last: dict = field(default_factory=dict)
    middle_distance: dict = field(default_factory=dict)
    last_z: dict = field(default_factory=dict)

    @property
    def complete(self):
        return (
            self.captured
            and self.received >= self.start.expected_frames
            and set(self.start.groups) <= self.finished_groups
        )


class AcquisitionPreview:
    def __init__(self, max_pending=8):
        self.max_pending = max_pending
        self.mode = "z"
        self.z_choice = "middle"
        self.reset()

    def reset(self):
        self.pending = OrderedDict()
        self.active = None
        self.completed = None
        self._order = 0
        self.dropped_positions = 0

    def accept(self, event):
        if isinstance(event, PreviewStarted):
            # Ordered acquisitions can revisit a position in multiple blocks.
            if event.key in self.pending or (self.completed and self.completed.start.key == event.key):
                return False
            self._order += 1
            position = _Position(event, self._order)
            self.pending[event.key] = position
            self.active = position
            while len(self.pending) > self.max_pending:
                self.pending.popitem(last=False)
                self.dropped_positions += 1
            return True

        position = self.pending.get(event.key)
        if position is None:
            return False  # late result for an evicted or previous acquisition position
        if isinstance(event, PreviewFrame):
            position.received += 1
            name = event.channel
            # Own retained data; camera drivers may reuse their capture buffer.
            image = np.array(event.image, copy=True)
            position.latest[name] = image
            distance = abs(event.z - event.nz // 2)
            if distance <= position.middle_distance.get(name, float("inf")):
                position.middle[name] = image
                position.middle_distance[name] = distance
            if event.z >= position.last_z.get(name, -1):
                position.last[name] = image
                position.last_z[name] = event.z
        elif isinstance(event, PreviewCaptured):
            position.captured = True
        elif isinstance(event, PreviewProcessed):
            position.finished_groups.add(event.group)
            position.middle.update(event.middle)
            position.last.update(event.last)
            position.latest.update(event.middle)
            if event.error:
                position.errors.append(f"{event.group}: {event.error}")

        if position.complete:
            # A delayed result must never replace a more recent completed FOV.
            if self.completed is None or position.order > self.completed.order:
                self.completed = position
            del self.pending[event.key]
        return True

    def snapshot(self):
        position = self.active if self.mode == "z" else self.completed
        if position is None:
            return None
        images = position.latest if self.mode == "z" else getattr(position, self.z_choice)
        return PreviewSnapshot(position.start.key, dict(images), position.complete, tuple(position.errors))
