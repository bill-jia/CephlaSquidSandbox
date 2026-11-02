from threading import Event
from unittest.mock import Mock

from control.core.upload_manager_view import StatusPoller


def test_status_polling_is_nonblocking_single_flight_and_suspends_when_hidden():
    started, release = Event(), Event()

    def read():
        started.set()
        assert release.wait(5)
        return {"datasets": []}

    read = Mock(side_effect=read)
    poller = StatusPoller(read, interval_s=1000)
    assert poller.poll(visible=False) is None
    read.assert_not_called()
    assert poller.poll() is None
    try:
        assert started.wait(5)
        for _ in range(10):
            assert poller.poll() is None
        read.assert_called_once()
    finally:
        release.set()
    assert poller.pending.result(timeout=5) == {"datasets": []}
    assert poller.poll(visible=False) is None
    assert poller.poll() == {"datasets": []}
    read.assert_called_once()
