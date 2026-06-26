"""End-to-end save-layout tests for cycle acquisitions (ZARR_V3).

Drives the real ``SaveZarrJob`` with self-describing ``CaptureInfo.save_*``
fields (as the worker populates them via ``SaveLayout``) and asserts:

  * Dense cycle -> one multichannel plate, frames folded into ``T``.
  * Ragged cycle -> one single-channel plate per imaged state, each with its
    own ``T`` size.

No hardware; writes to a tmp dir and reads back via tensorstore.
"""

import os
import tempfile
import time

import numpy as np
import pytest

import squid.abc
from control._def import FileSavingOption
from control.core.job_processing import (
    CaptureInfo,
    JobImage,
    SaveZarrJob,
    ZarrWriterInfo,
    ZarrWriteResult,
)
from control.models.acquisition_cycle import (
    AcquisitionCycle,
    CycleStep,
    RegionPlan,
    frame_coord,
    resolve_cycle,
)
from control.models.observation_state import CameraSettings, IlluminatorState, ObservationState


def _obs(name):
    return ObservationState(
        name=name,
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[IlluminatorState(illumination_channel=name, intensity=50.0, on=True)],
    )


def _read_tensorstore(array_path):
    import tensorstore as ts

    return ts.open(
        {"driver": "zarr3", "kvstore": {"driver": "file", "path": array_path}},
        create=False,
        open=True,
    ).result()


def _run_plan(tmpdir, plan, Nt, channel_colors, channel_wavelengths):
    """Drive SaveZarrJob over a region plan exactly as the worker would, for one FOV."""
    info = ZarrWriterInfo(
        base_path=tmpdir,
        t_size=Nt * (plan.frames_per_position // max(1, len(plan.channel_order))) if plan.dense else Nt,
        c_size=len(plan.channel_order),
        z_size=1,
        is_hcs=True,
        region_fov_counts={"A1": 1},
        fov_translations_um={"A1": {0: (0.0, 0.0)}},
        pixel_size_um=0.325,
        channel_names=list(plan.channel_order),
        channel_colors=channel_colors,
        channel_wavelengths=channel_wavelengths,
    )
    pix = 0
    for t_scan in range(Nt):
        for ev in plan.events:
            if ev.is_stimulus:
                continue
            coord = frame_coord(plan, Nt, t_scan, ev)
            if coord.array_key is None:
                names = list(plan.channel_order)
            else:
                names = [ev.observation_state]
            img = np.full((32, 32), pix % 60000, dtype=np.uint16)
            cap = CaptureInfo(
                position=squid.abc.Pos(x_mm=1.0, y_mm=2.0, z_mm=0.0, theta_rad=None),
                z_index=0,
                capture_time=time.time(),
                observation_state=_obs(ev.observation_state),
                save_directory=tmpdir,
                file_id=f"A1_0_0",
                region_id="A1",
                fov=0,
                configuration_idx=coord.c_index,
                time_point=t_scan,
                filename_channel_label=ev.observation_state,
                file_saving_option=FileSavingOption.ZARR_V3,
                acquisition_root=tmpdir,
                array_key=coord.array_key,
                save_t_index=coord.t_index,
                save_c_index=coord.c_index,
                save_t_size=coord.t_size,
                save_c_size=coord.c_size,
                cycle_event_index=ev.cycle_event_index,
                state_frame_index=ev.state_frame_index,
                array_channel_names=names,
                array_channel_colors=[channel_colors[plan.channel_order.index(n)] if n in plan.channel_order else "#FFFFFF" for n in names],
                array_channel_wavelengths=[channel_wavelengths[plan.channel_order.index(n)] if n in plan.channel_order else None for n in names],
            )
            job = SaveZarrJob(capture_info=cap, capture_image=JobImage(image_array=img))
            job.zarr_writer_info = info
            assert isinstance(job.run(), ZarrWriteResult)
            pix += 1
    SaveZarrJob.finalize_all_writers()
    return info


def test_dense_cycle_folds_into_single_plate():
    cyc = AcquisitionCycle(
        name="dense", repeat=3, items=[CycleStep(observation_state="GFP"), CycleStep(observation_state="RFP")]
    )
    plan = RegionPlan.from_events(resolve_cycle(cyc))
    assert plan.dense
    with tempfile.TemporaryDirectory() as tmp:
        try:
            info = _run_plan(tmp, plan, Nt=2, channel_colors=["#00FF00", "#FF0000"], channel_wavelengths=[488, 561])
            # One standard plate, no per-state plates.
            assert os.path.isdir(os.path.join(tmp, "plate.ome.zarr"))
            assert not os.path.isdir(os.path.join(tmp, "GFP.ome.zarr"))
            out = info.get_output_path("A1", 0, None)
            ds = _read_tensorstore(out)
            # Shape: T = Nt(2) * frames_per_state(3) = 6, C = 2, Z = 1
            assert ds.shape[:3] == (6, 2, 1)
        finally:
            SaveZarrJob.clear_writers()


def test_ragged_cycle_makes_per_state_plates():
    cyc = AcquisitionCycle(
        name="ragged",
        items=[CycleStep(observation_state="GFP", n_frames=4), CycleStep(observation_state="RFP", n_frames=2)],
    )
    plan = RegionPlan.from_events(resolve_cycle(cyc))
    assert not plan.dense
    with tempfile.TemporaryDirectory() as tmp:
        try:
            info = _run_plan(tmp, plan, Nt=2, channel_colors=["#00FF00", "#FF0000"], channel_wavelengths=[488, 561])
            # Two single-channel plates, one per imaged state; no shared plate.
            assert os.path.isdir(os.path.join(tmp, "GFP.ome.zarr"))
            assert os.path.isdir(os.path.join(tmp, "RFP.ome.zarr"))
            assert not os.path.isdir(os.path.join(tmp, "plate.ome.zarr"))
            gfp = _read_tensorstore(info.get_output_path("A1", 0, "GFP"))
            rfp = _read_tensorstore(info.get_output_path("A1", 0, "RFP"))
            # GFP: T = Nt(2)*4 = 8, C=1 ; RFP: T = Nt(2)*2 = 4, C=1
            assert gfp.shape[:3] == (8, 1, 1)
            assert rfp.shape[:3] == (4, 1, 1)
        finally:
            SaveZarrJob.clear_writers()


def test_ragged_by_z_mode_makes_separate_single_z_array():
    """A full-z step + a reference-z-only step are ragged by Z extent: the full-z
    state writes a Z=NZ array, the reference-only state its own Z=1 _refz array."""
    NZ = 3
    cyc = AcquisitionCycle(
        name="zmode",
        items=[
            CycleStep(observation_state="GFP"),                          # full z-stack
            CycleStep(observation_state="RFP", acquire_z_stack=False),   # reference plane only
        ],
    )
    plan = RegionPlan.from_events(resolve_cycle(cyc))
    assert not plan.dense  # mixed z-mode => ragged
    assert plan.array_keys == ["GFP", "RFP_refz"]
    colors, waves = ["#00FF00", "#FF0000"], [488, 561]
    with tempfile.TemporaryDirectory() as tmp:
        try:
            info = ZarrWriterInfo(
                base_path=tmp,
                t_size=1,
                c_size=len(plan.channel_order),
                z_size=NZ,
                is_hcs=True,
                region_fov_counts={"A1": 1},
                fov_translations_um={"A1": {0: (0.0, 0.0)}},
                pixel_size_um=0.325,
                channel_names=list(plan.channel_order),
                channel_colors=colors,
                channel_wavelengths=waves,
            )
            for ev in plan.events:
                if ev.is_stimulus:
                    continue
                coord = frame_coord(plan, 1, 0, ev)
                names = list(plan.channel_order) if coord.array_key is None else [ev.observation_state]
                z_size = NZ if ev.acquire_z_stack else 1
                z_levels = range(NZ) if ev.acquire_z_stack else [0]
                for z in z_levels:
                    cap = CaptureInfo(
                        position=squid.abc.Pos(x_mm=1.0, y_mm=2.0, z_mm=0.0, theta_rad=None),
                        z_index=(z if ev.acquire_z_stack else 0),
                        capture_time=time.time(),
                        observation_state=_obs(ev.observation_state),
                        save_directory=tmp,
                        file_id="A1_0_0",
                        region_id="A1",
                        fov=0,
                        configuration_idx=coord.c_index,
                        time_point=0,
                        filename_channel_label=ev.observation_state,
                        file_saving_option=FileSavingOption.ZARR_V3,
                        acquisition_root=tmp,
                        array_key=coord.array_key,
                        save_t_index=coord.t_index,
                        save_c_index=coord.c_index,
                        save_t_size=coord.t_size,
                        save_c_size=coord.c_size,
                        save_z_size=z_size,
                        cycle_event_index=ev.cycle_event_index,
                        state_frame_index=ev.state_frame_index,
                        array_channel_names=names,
                        array_channel_colors=[colors[plan.channel_order.index(n)] for n in names],
                        array_channel_wavelengths=[waves[plan.channel_order.index(n)] for n in names],
                    )
                    job = SaveZarrJob(capture_info=cap, capture_image=JobImage(image_array=np.full((32, 32), 123, np.uint16)))
                    job.zarr_writer_info = info
                    assert isinstance(job.run(), ZarrWriteResult)
            SaveZarrJob.finalize_all_writers()

            assert os.path.isdir(os.path.join(tmp, "GFP.ome.zarr"))
            assert os.path.isdir(os.path.join(tmp, "RFP_refz.ome.zarr"))
            assert not os.path.isdir(os.path.join(tmp, "RFP.ome.zarr"))
            assert not os.path.isdir(os.path.join(tmp, "plate.ome.zarr"))
            gfp = _read_tensorstore(info.get_output_path("A1", 0, "GFP"))
            rfp = _read_tensorstore(info.get_output_path("A1", 0, "RFP_refz"))
            assert gfp.shape[:3] == (1, 1, NZ)   # full z-stack
            assert rfp.shape[:3] == (1, 1, 1)    # reference plane only
        finally:
            SaveZarrJob.clear_writers()
