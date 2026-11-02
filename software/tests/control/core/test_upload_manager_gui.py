from concurrent.futures import Future
from unittest.mock import Mock
from uuid import uuid4

from qtpy.QtWidgets import QApplication, QMessageBox, QMainWindow

import upload_manager
from control.core.upload_manager import UploadStore


def test_history_and_cancel_controls_and_incremental_refresh(tmp_path, monkeypatch):
    # Invoke the real window with an isolated IPC name and a fake scheduler.
    # The test command uses QT_QPA_PLATFORM=offscreen.
    app = QApplication.instance() or QApplication([])
    state = tmp_path / "state"
    store = UploadStore(state / "uploads.sqlite3")
    local, remote = tmp_path / "local", tmp_path / "remote"
    local.mkdir(); remote.mkdir()
    store.register_dataset(dataset_id="run", name="run", local_root=str(local), remote_root=str(remote))
    snapshot = store.status()
    done = Future(); done.set_result({"cancelled": True})
    engine = Mock()
    engine.cancel_dataset_async.return_value = done
    monkeypatch.setattr(upload_manager, "UploadStore", lambda *_: store)
    monkeypatch.setattr(upload_manager, "UploadManagerEngine", lambda *_: engine)
    monkeypatch.setattr(upload_manager, "server_name", lambda: "UploadManagerTest-" + token)
    token = uuid4().hex
    windows = []

    def exercise():
        window = next(w for w in app.topLevelWidgets() if isinstance(w, QMainWindow)
                      and w.windowTitle() == "Squid Upload Manager")
        windows.append(window)
        window.show()
        window.status_poller = Mock()
        window.status_poller.poll.return_value = snapshot
        window.refresh()
        window.table.selectRow(0)
        first_item = window.table.item(0, 0)
        window.refresh()
        assert window.table.item(0, 0) is first_item
        assert window.selected()["id"] == "run"
        question = Mock(return_value=QMessageBox.No)
        monkeypatch.setattr(QMessageBox, "question", question)
        window._cancel_upload()
        engine.cancel_dataset_async.assert_not_called()
        assert "future files" in question.call_args.args[2]
        question.return_value = QMessageBox.Yes
        window._cancel_upload()
        engine.cancel_dataset_async.assert_called_once_with("run")
        # A second click while cancellation is pending must not submit twice.
        window._cancel_upload()
        engine.cancel_dataset_async.assert_called_once()
        window.refresh()
        assert not window.cancellations
        window.hide()
        window.refresh()
        window.status_poller.poll.assert_called_with(False)
        return 0

    monkeypatch.setattr(app, "exec_", exercise)
    try:
        assert upload_manager.main(["--state-dir", str(state), "--hidden"]) == 0
        engine.start.assert_called_once()
        engine.stop.assert_called_once()
    finally:
        for window in windows:
            window.hide()
            window.deleteLater()
        app.processEvents()
