"""
Per-profile GUI state.

Persisted to ``user_profiles/{profile}/gui_state.yaml`` on shutdown and
restored on startup so the application reopens in the same configuration the
user left it (window geometry, selected tab, last-used objective, last-active
observation state, snap save directory, etc.).

This is distinct from ObservationState (a single light-path snapshot) and from
named observation presets (multiple saved light-paths). It captures *transient*
UI selections that are not part of the imaging configuration.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


class GuiState(BaseModel):
    """Transient UI state to round-trip across application restarts."""

    model_config = ConfigDict(extra="ignore")

    # Hardware selections
    last_active_objective: Optional[str] = None
    last_active_observation_state_name: Optional[str] = None

    # Main window
    window_geometry_b64: Optional[str] = None
    window_state_b64: Optional[str] = None
    record_tab_index: Optional[int] = None

    # Save folders (default to C:/Microscope_Data/<profile> when unset)
    snap_saving_dir: Optional[str] = None
    acquisition_saving_dir: Optional[str] = None

    # Live / snap
    snap_tag: Optional[str] = None
    live_display_fps: Optional[float] = None
    autolevel_enabled: Optional[bool] = None
    display_resolution_scaling: Optional[int] = None
