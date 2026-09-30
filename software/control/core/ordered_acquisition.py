"""Execution context for acquisitions that revisit positions/timepoints out of order."""

import time

from control.models.acquisition_order import iter_ordered_visits


class OrderedAcquisitionMixin:
    _ORDERED_TIME_FIELDS = (
        "_timepoint_start_time", "_timepoint_image_count", "_timepoint_fov_count",
        "_laser_af_successes", "_laser_af_failures", "af_fov_count",
        "_coordinate_rows", "_acquired_positions_appended", "_acquired_positions_row_count",
        "_downsampled_view_manager",
        "_ordered_recorded_coordinates",
    )

    def run_ordered_acquisition(self):
        positions = [
            (region, fov) for region, coords in self.scan_region_fov_coords_mm.items()
            for fov in range(len(coords))
        ]
        plans = [self._get_region_plan(region) for region, _ in positions]
        if any(not plan.events for plan in plans):
            raise ValueError("No observation states selected for acquisition.")
        ref_z = self._reference_z_level()
        counts = [sum(
            self.NZ if e.is_wait or e.is_stimulus or e.acquire_z_stack else 1
            for e in plan.events
        ) for plan in plans]
        self._ordered_time_states = {}
        self._ordered_focus = {}
        self._ordered_frames_completed = 0
        self._ordered_frames_total = self.Nt * sum(self._captured_frames_per_fov(plan) for plan in plans)
        self._ordered_prewarmed = False
        remaining = {}
        position_remaining = {}
        clocks = {}
        total = sum(counts)
        visits = iter_ordered_visits(
            self.acquisition_order, plans, self.Nt, self.NZ, ref_z,
            snake=self.snake_observation_states,
        )
        try:
            for visit in visits:
                if self.abort_requested_fn():
                    break
                # T's interval is local to the loops enclosing it. A series at
                # one position can therefore finish before moving to the next.
                previous = clocks.get(visit.time_group)
                if previous is None or previous[0] != visit.time_point:
                    if previous is not None and self.dt > 0:
                        self._interruptible_sleep(max(0, previous[1] + self.dt - time.time()))
                    if self.abort_requested_fn():
                        break
                    clocks[visit.time_group] = (visit.time_point, time.time())
                self.time_point = visit.time_point
                self._ordered_visit = visit
                self._ordered_position = positions[visit.position]
                count = sum(len(events) for _, events in visit.planes)
                remaining.setdefault(self.time_point, total)
                key = (self.time_point, visit.position)
                position_remaining.setdefault(key, counts[visit.position])
                remaining[self.time_point] -= count
                position_remaining[key] -= count
                self._ordered_final_timepoint = remaining[self.time_point] == 0
                self._ordered_final_position = position_remaining[key] == 0
                first_time = self.time_point not in self._ordered_time_states
                if self.fluidics and self.use_fluidics and first_time:
                    self.fluidics.update_port(self.time_point)
                    self.fluidics.run_before_imaging()
                    self.fluidics.wait_for_completion()
                    if self.abort_requested_fn():
                        break
                drops = self._camera_dropped_frame_count()
                self.run_single_time_point()
                # Both counters and image-derived plate views are updated from
                # callbacks/jobs. Drain them before switching timepoint context.
                self._wait_for_outstanding_callback_images()
                if self._generate_downsampled_views:
                    self._wait_for_downsampled_view_jobs()
                self._report_timepoint_frame_drops(drops)
                self._ordered_time_states[self.time_point] = {
                    name: getattr(self, name, None) for name in self._ORDERED_TIME_FIELDS
                }
                if self._ordered_final_position:
                    self._ordered_focus.pop(key, None)
                if self._ordered_final_timepoint:
                    self._ordered_time_states.pop(self.time_point, None)
                    if self.fluidics and self.use_fluidics:
                        self.fluidics.run_after_imaging()
                        self.fluidics.wait_for_completion()
        finally:
            self._ordered_visit = None
            self._ordered_time_states.clear()
            self._ordered_focus.clear()

    def _restore_ordered_timepoint(self):
        if getattr(self, "_ordered_visit", None) is None:
            return
        state = self._ordered_time_states.get(self.time_point)
        if state is not None:
            for name, value in state.items():
                setattr(self, name, value)
        else:
            self.initialize_coordinates_dataframe()
            self._downsampled_view_manager = None
