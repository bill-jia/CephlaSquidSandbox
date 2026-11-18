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


def iter_ordered_visits(order, plans, nt, nz, reference_z, *, snake=False, position_nz=None, position_reference_z=None):
    """Stream visits without materializing the T × Pos × Z × C product.

    Positions and events keep their original indices for saving. Per-position
    plans may have different C lengths. A time group identifies the outer-loop
    values enclosing T, so each local time series gets its own interval clock.
    """
    order = validate_order(order)
    position_nz = list(position_nz) if position_nz is not None else [nz] * len(plans)
    position_reference_z = list(position_reference_z) if position_reference_z is not None else [reference_z] * len(plans)
    if len(position_nz) != len(plans) or len(position_reference_z) != len(plans):
        raise ValueError("Z counts and reference planes must match the number of positions.")
    if any(n < 1 or not 0 <= ref < n for n, ref in zip(position_nz, position_reference_z)):
        raise ValueError("Each position needs a positive Z count and a valid reference plane.")
    blocks = [channel_blocks(plan.events) for plan in plans]
    sizes = {"T": nt, "Pos": len(plans), "Z": max(position_nz, default=nz), "C": max(map(len, blocks), default=0)}
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
        if z >= position_nz[p]:
            return
        if c >= len(blocks[p]):
            return
        reverse = snake and (t * len(plans) + p) % 2
        if reverse:
            c = len(blocks[p]) - 1 - c
            if plans[p].events[0].acquisition_block_index is None:
                z = position_nz[p] - 1 - z
        events = [e for e in blocks[p][c] if e.is_wait or e.is_stimulus or e.acquire_z_stack or z == position_reference_z[p]]
        if events:
            yield (t, p, tuple(state[key] for key in outer_time)), (z, events)

    for (t, p, time_group), entries in groupby(walk(0), key=lambda entry: entry[0]):
        yield OrderedVisit(t, p, time_group, [plane for _, plane in entries])
