from control._def import OBJECTIVES, DEFAULT_OBJECTIVE, MULTIPOINT_AUTOFOCUS_CHANNEL
from control.core.auto_focus_controller import AutoFocusController
from control.core.laser_auto_focus_controller import LaserAutofocusController
from control.core.live_controller import LiveController
from control.core.multi_point_controller import NoOpCallbacks, MultiPointController
from control.core.multi_point_utils import MultiPointControllerFunctions
from control.core.objective_store import ObjectiveStore
from control.core.scan_coordinates import ScanCoordinates
from control.microcontroller import Microcontroller
from control.microscope import Microscope
from squid.abc import AbstractStage, AbstractCamera


def _ensure_test_observation_presets(microscope: Microscope) -> None:
    """Seed a few selectable Observation State presets, one per illumination channel.

    A freshly generated profile only has the single working state in
    general.yaml; ``ConfigRepository.get_observation_states()`` falls back to
    that state so callers always have *something* to iterate, but
    ``MultiPointController.set_selected_configurations`` only accepts names
    that are actually saved under ``observation_presets/`` (matching what the
    GUI's save/load dropdown lists). Without a real preset saved, every
    channel selection in a test is silently dropped. Explicit setup here
    (rather than relying on that lazy fallback) mirrors how other tests in
    this suite (e.g. test_observation_state_call_site_migration.py) seed
    presets via ``save_observation_preset``.

    Presets are named after real illumination channels (rather than made-up
    names) so MULTIPOINT_AUTOFOCUS_CHANNEL - the contrast-AF default - always
    resolves to a real preset, the way a user's saved profile would have it.
    """
    repo = microscope.config_repo
    general = repo.get_observation_state()
    if general is None or not general.illuminator_states:
        return

    all_channel_names = [ist.illumination_channel for ist in general.illuminator_states]
    # Always include the contrast-AF default channel first, then a couple more
    # to exercise multi-channel selection (e.g. mosaic RAM scaling by count).
    ordered = [n for n in (MULTIPOINT_AUTOFOCUS_CHANNEL, *all_channel_names) if n in all_channel_names]
    preset_names = list(dict.fromkeys(ordered))[:3]

    if set(repo.list_observation_presets()) >= set(preset_names):
        return  # already seeded (e.g. a prior test run in this profile)

    for name in preset_names:
        state = general.model_copy(
            update={
                "illuminator_states": [
                    ist.model_copy(update={"on": ist.illumination_channel == name})
                    for ist in general.illuminator_states
                ]
            }
        )
        repo.save_observation_preset(name, state)


def get_test_live_controller(microscope: Microscope, starting_objective) -> LiveController:
    _ensure_test_observation_presets(microscope)

    controller = LiveController(microscope=microscope, camera=microscope.camera)
    # LiveController no longer owns observation state (post Observation State
    # migration) - it delegates to ObservationStateController. Wire it the same
    # way Microscope.__init__ wires its own live_controller.
    controller.obs_controller = microscope.obs_controller

    channels = controller.get_observation_states()
    if channels:
        controller.obs_controller.apply_full_observation_state(channels[0])
    return controller


def get_test_autofocus_controller(
    camera,
    stage: AbstractStage,
    live_controller: LiveController,
    microcontroller: Microcontroller,
):
    return AutoFocusController(
        camera=camera,
        stage=stage,
        liveController=live_controller,
        microcontroller=microcontroller,
        nl5=None,
        finished_fn=lambda: None,
        image_to_display_fn=lambda image: None,
    )


def get_test_scan_coordinates(
    objective_store: ObjectiveStore,
    stage: AbstractStage,
    camera: AbstractCamera,
):
    return ScanCoordinates(objectiveStore=objective_store, stage=stage, camera=camera)


def get_test_objective_store():
    return ObjectiveStore(objectives_dict=OBJECTIVES, default_objective=DEFAULT_OBJECTIVE)


def get_test_laser_autofocus_controller(microscope: Microscope):
    return LaserAutofocusController(
        microcontroller=microscope.low_level_drivers.microcontroller,
        camera=microscope.addons.camera_focus,
        liveController=LiveController(microscope=microscope, camera=microscope.addons.camera_focus),
        stage=microscope.stage,
        piezo=microscope.addons.piezo_stage,
        objectiveStore=microscope.objective_store,
    )


def get_test_multi_point_controller(
    microscope: Microscope,
    callbacks: MultiPointControllerFunctions = NoOpCallbacks,
) -> MultiPointController:
    live_controller = get_test_live_controller(
        microscope=microscope, starting_objective=microscope.objective_store.default_objective
    )

    multi_point_controller = MultiPointController(
        microscope=microscope,
        live_controller=live_controller,
        autofocus_controller=get_test_autofocus_controller(
            microscope.camera,
            microscope.stage,
            live_controller,
            microscope.low_level_drivers.microcontroller,
        ),
        scan_coordinates=get_test_scan_coordinates(
            objective_store=microscope.objective_store, stage=microscope.stage, camera=microscope.camera
        ),
        callbacks=callbacks,
        objective_store=microscope.objective_store,
        laser_autofocus_controller=get_test_laser_autofocus_controller(microscope),
    )

    multi_point_controller.set_base_path("/tmp/")
    multi_point_controller.start_new_experiment("unit test experiment")

    return multi_point_controller
