from unittest.mock import Mock
from threading import Event

import pytest

from control.core import upload_manager_client as client_module
from control.core.upload_manager_client import UploadManagerClient, UploadManagerUnavailable


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(client_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def test_registration_waits_for_queue_recovery(clock, monkeypatch, tmp_path):
    client = UploadManagerClient(state_dir=tmp_path)
    launch = Mock()
    requests = []

    def request(method, params):
        requests.append((method, params))
        # The reported Windows startup took about 16 seconds to recover its queue.
        if clock[0] < 16:
            return None
        return {"ok": True, "result": {"dataset_id": params["dataset_id"]}}

    monkeypatch.setattr(client, "start_manager", launch)
    monkeypatch.setattr(client, "_request_once", request)
    assert client.register_dataset(dataset_id="acquisition") == {"dataset_id": "acquisition"}
    launch.assert_called_once_with()
    assert 16 <= clock[0] < 17
    assert client.timeout_ms == 5000
    assert all(item == ("register_dataset", {"dataset_id": "acquisition"}) for item in requests)


def test_running_manager_is_reused_without_waiting(clock, monkeypatch, tmp_path):
    client = UploadManagerClient(state_dir=tmp_path)
    launch = Mock()
    monkeypatch.setattr(client, "start_manager", launch)
    monkeypatch.setattr(client, "_request_once", lambda *args: {"ok": True, "result": "registered"})
    assert client.register_dataset(dataset_id="acquisition") == "registered"
    launch.assert_not_called()
    assert clock[0] == 0


def test_startup_timeout_reports_log_path(clock, monkeypatch, tmp_path):
    client = UploadManagerClient(state_dir=tmp_path, startup_timeout_ms=1000)
    monkeypatch.setattr(client, "start_manager", Mock())
    monkeypatch.setattr(client, "_request_once", lambda *args: None)
    with pytest.raises(UploadManagerUnavailable) as error:
        client.register_dataset(dataset_id="acquisition")
    assert 1 <= clock[0] < 1.1
    assert "1s for startup" in str(error.value)
    assert str(tmp_path / "manager.stderr.log") in str(error.value)


def test_no_start_does_not_launch_or_wait(clock, monkeypatch, tmp_path):
    client = UploadManagerClient(state_dir=tmp_path)
    launch = Mock()
    monkeypatch.setattr(client, "start_manager", launch)
    monkeypatch.setattr(client, "_request_once", lambda *args: None)
    with pytest.raises(UploadManagerUnavailable):
        client.request("get_status", start=False)
    launch.assert_not_called()
    assert clock[0] == 0


def test_registration_rejection_is_not_retried(clock, monkeypatch, tmp_path):
    client = UploadManagerClient(state_dir=tmp_path)
    launch = Mock()
    monkeypatch.setattr(client, "start_manager", launch)
    monkeypatch.setattr(client, "_request_once", lambda *args: {"ok": False, "error": "destination mismatch"})
    with pytest.raises(RuntimeError, match="destination mismatch"):
        client.register_dataset(dataset_id="acquisition")
    launch.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_background_readiness_returns_before_connection_and_propagates_failure(monkeypatch, tmp_path, failure):
    entered = Event()
    release = Event()
    client = UploadManagerClient(state_dir=tmp_path)

    def status():
        entered.set()
        assert release.wait(5)
        if failure:
            raise UploadManagerUnavailable("startup failed")
        return {"datasets": []}

    monkeypatch.setattr(client, "get_status", status)
    ready = client.ensure_ready_async()
    try:
        assert entered.wait(5)
        assert not ready.done()
    finally:
        release.set()
    if failure:
        with pytest.raises(UploadManagerUnavailable, match="startup failed"):
            ready.result(timeout=5)
    else:
        assert ready.result(timeout=5) is None


@pytest.mark.parametrize("result", [(False, 0), False, (True, 123), True])
def test_detached_launch_checks_success_flag(result, monkeypatch, tmp_path):
    from qtpy import QtCore

    process = Mock()
    process.startDetached.return_value = result
    process.errorString.return_value = "launch failed"
    monkeypatch.setattr(QtCore, "QProcess", lambda: process)
    client = UploadManagerClient(state_dir=tmp_path)
    success = result[0] if isinstance(result, tuple) else result
    if success:
        client.start_manager()
    else:
        with pytest.raises(UploadManagerUnavailable, match="launch failed"):
            client.start_manager()
