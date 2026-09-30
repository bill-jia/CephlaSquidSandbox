"""Hardware-independent traversal of the multipoint acquisition dimensions."""

from dataclasses import dataclass
from itertools import groupby


DEFAULT_ORDER = ("T", "Pos", "Z", "C")


def validate_order(order):
    order = tuple(order)
    if len(order) != 4 or set(order) != set(DEFAULT_ORDER):
        raise ValueError("Acquisition order must contain T, Z, C and Pos exactly once")
    return order


def channel_blocks(events):
    """Simple channels are single events; Advanced items retain all nested events."""
    blocks = []
    previous_index = None
    for event in events:
        index = event.channel_block_index
        if index is None:
            index = event.acquisition_block_index
        if index is None or not blocks or previous_index != index:
            blocks.append([])
        blocks[-1].append(event)
        previous_index = index
    return blocks


@dataclass
class OrderedVisit:
    time_point: int
    position: int
    time_group: tuple
    planes: list


def iter_ordered_visits(order, plans, nt, nz, reference_z, *, snake=False):
    """Stream visits without materializing the T × Pos × Z × C product.

    Positions and events keep their original indices for saving. Per-position
    plans may have different C lengths. A time group identifies the outer-loop
    values enclosing T, so each local time series gets its own interval clock.
    """
    order = validate_order(order)
    blocks = [channel_blocks(plan.events) for plan in plans]
    sizes = {"T": nt, "Pos": len(plans), "Z": nz, "C": max(map(len, blocks), default=0)}
    outer_time = order[:order.index("T")]
    state = {}

    def walk(depth):
        if depth < len(order):
            axis = order[depth]
            for index in range(sizes[axis]):
                state[axis] = index
                yield from walk(depth + 1)
            return
        t, p, z, c = (state[key] for key in ("T", "Pos", "Z", "C"))
        if c >= len(blocks[p]):
            return
        reverse = snake and (t * len(plans) + p) % 2
        if reverse:
            c = len(blocks[p]) - 1 - c
            if plans[p].events[0].acquisition_block_index is None:
                z = nz - 1 - z
        events = [e for e in blocks[p][c] if e.is_wait or e.is_stimulus or e.acquire_z_stack or z == reference_z]
        if events:
            yield (t, p, tuple(state[key] for key in outer_time)), (z, events)

    for (t, p, time_group), entries in groupby(walk(0), key=lambda entry: entry[0]):
        yield OrderedVisit(t, p, time_group, [plane for _, plane in entries])
