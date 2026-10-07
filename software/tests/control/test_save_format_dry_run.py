"""The save-format dropdown carries the old "Skip Saving" checkbox as its last entry.

Selecting ``Don't save (dry run)`` has to reach ``MultiPointController.set_skip_saving``
(the model still has the flag; only the checkbox went away), and it must never be handed
to ``set_file_saving_option`` — it is not a ``FileSavingOption`` and the enum conversion
would raise. Building either multipoint widget needs hardware, so — as in
``test_flexible_region_state`` — these drive the module-level helpers the widgets use
against the real combobox and a mock controller.
"""

import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("qtpy")
from qtpy.QtWidgets import QApplication

from control._def import FileSavingOption
from gui.widgets.multipoint import (
    DRY_RUN_SAVE_FORMAT,
    _apply_save_format_from_yaml,
    _format_acquisition_size_estimate,
    _is_dry_run,
    _make_file_saving_format_row,
    _push_save_format_to_controller,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


class _Widget:
    """The two attributes the save-format helpers touch on a multipoint widget."""

    def __init__(self, initial_option=FileSavingOption.INDIVIDUAL_IMAGES):
        _row, self.combobox_fileSavingFormat, self.label_size_estimate = _make_file_saving_format_row(
            initial_option=initial_option
        )
        self.skip_saving_calls = []
        self.file_saving_option_calls = []
        self.multipointController = SimpleNamespace(
            file_saving_option=initial_option,
            set_skip_saving=self.skip_saving_calls.append,
            set_file_saving_option=self.file_saving_option_calls.append,
        )


def test_dry_run_is_the_last_entry_after_every_real_format(qt_app):
    widget = _Widget()
    combo = widget.combobox_fileSavingFormat
    names = [combo.itemText(i) for i in range(combo.count())]
    assert names[:-1] == [opt.name for opt in FileSavingOption]
    assert names[-1] == DRY_RUN_SAVE_FORMAT


def test_selecting_dry_run_sets_skip_saving_and_no_format(qt_app):
    widget = _Widget()
    widget.combobox_fileSavingFormat.setCurrentText(DRY_RUN_SAVE_FORMAT)

    _push_save_format_to_controller(widget)

    assert widget.skip_saving_calls == [True]
    # "Don't save (dry run)" is not a FileSavingOption; handing it over would raise.
    assert widget.file_saving_option_calls == []
    assert _is_dry_run(widget.combobox_fileSavingFormat) is True


def test_selecting_a_real_format_clears_skip_saving(qt_app):
    widget = _Widget()
    widget.combobox_fileSavingFormat.setCurrentText(FileSavingOption.ZARR_V3.name)

    _push_save_format_to_controller(widget)

    assert widget.skip_saving_calls == [False]
    assert widget.file_saving_option_calls == [FileSavingOption.ZARR_V3.name]
    assert _is_dry_run(widget.combobox_fileSavingFormat) is False


def test_dry_run_size_estimate_says_nothing_is_written(qt_app):
    controller = SimpleNamespace()  # never consulted when saving is off
    assert _format_acquisition_size_estimate(controller, True, True, "hint") == "Saving disabled — no files written"


def test_yaml_with_skip_saving_selects_the_dry_run_entry(qt_app):
    widget = _Widget()

    _apply_save_format_from_yaml(widget, SimpleNamespace(skip_saving=True))

    assert widget.combobox_fileSavingFormat.currentText() == DRY_RUN_SAVE_FORMAT


def test_yaml_without_skip_saving_leaves_dry_run(qt_app):
    widget = _Widget(initial_option=FileSavingOption.OME_TIFF)
    widget.combobox_fileSavingFormat.setCurrentText(DRY_RUN_SAVE_FORMAT)

    _apply_save_format_from_yaml(widget, SimpleNamespace(skip_saving=False))

    assert widget.combobox_fileSavingFormat.currentText() == FileSavingOption.OME_TIFF.name
