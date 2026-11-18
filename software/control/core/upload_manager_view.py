"""Nonblocking status polling for the Upload Manager display."""

from concurrent.futures import Future
from threading import Thread
import time


class StatusPoller:
    """At most one background snapshot; hidden windows request no new work."""

    def __init__(self, read_status, interval_s=1.0):
        self.read_status = read_status
        self.interval_s = interval_s
        self.pending = None
        self.next_request = 0.0

    def poll(self, visible=True):
        if not visible:
            return None
        snapshot = None
        if self.pending is not None and self.pending.done():
            done, self.pending = self.pending, None
            snapshot = done.result()
        if self.pending is None and time.monotonic() >= self.next_request:
            future = Future()
            self.pending = future
            self.next_request = time.monotonic() + self.interval_s

            def read():
                try:
                    future.set_result(self.read_status())
                except Exception as exc:
                    future.set_exception(exc)

            Thread(target=read, name="Upload status reader", daemon=True).start()
        return snapshot
