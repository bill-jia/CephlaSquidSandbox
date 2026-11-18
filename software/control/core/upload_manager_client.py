"""Synchronous client for the detached Squid Upload Manager."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from concurrent.futures import Future
from pathlib import Path
from threading import Thread
from typing import Any, Iterable, Optional
from uuid import uuid4

from control.core.upload_manager import APPLICATION_VERSION, PROTOCOL_VERSION, default_state_dir


def server_name() -> str:
    identity = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"SquidUploadManager-{suffix}"


class UploadManagerUnavailable(ConnectionError):
    pass


class UploadManagerClient:
    def __init__(
        self,
        *,
        timeout_ms: int = 5000,
        startup_timeout_ms: int = 60000,
        state_dir: Optional[os.PathLike[str] | str] = None,
    ):
        self.timeout_ms = timeout_ms
        # Restoring the durable queue and reconciling existing datasets happens
        # before the manager accepts IPC, and can exceed a normal request timeout.
        self.startup_timeout_ms = startup_timeout_ms
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()

    def request(self, method: str, params: Optional[dict] = None, *, start: bool = True) -> Any:
        response = self._request_once(method, params or {})
        if response is None and start:
            self.start_manager()
            deadline = time.monotonic() + self.startup_timeout_ms / 1000.0
            while response is None and time.monotonic() < deadline:
                time.sleep(0.05)
                response = self._request_once(method, params or {})
        if response is None:
            raise UploadManagerUnavailable(
                f"Upload Manager did not respond to {method!r}"
                + (f" after waiting up to {self.startup_timeout_ms / 1000:g}s for startup." if start else ".")
                + f" See {self.state_dir / 'manager.stderr.log'} for details."
            )
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "Upload Manager request failed"))
        return response.get("result")

    def _request_once(self, method: str, params: dict) -> Optional[dict]:
        from qtpy.QtCore import QIODevice
        from qtpy.QtNetwork import QLocalSocket

        socket = QLocalSocket()
        socket.connectToServer(server_name(), QIODevice.ReadWrite)
        if not socket.waitForConnected(min(500, self.timeout_ms)):
            return None
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "application_version": APPLICATION_VERSION,
            "request_id": str(uuid4()),
            "method": method,
            "params": params,
        }
        encoded = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
        if socket.write(encoded) < 0 or not socket.waitForBytesWritten(self.timeout_ms):
            return None
        deadline = time.monotonic() + self.timeout_ms / 1000.0
        data = bytearray()
        while b"\n" not in data and time.monotonic() < deadline:
            if socket.bytesAvailable() == 0 and not socket.waitForReadyRead(250):
                continue
            data.extend(bytes(socket.readAll()))
        socket.disconnectFromServer()
        if b"\n" not in data:
            return None
        return json.loads(data.split(b"\n", 1)[0].decode("utf-8"))

    def start_manager(self) -> None:
        from qtpy.QtCore import QIODevice, QProcess

        self.state_dir.mkdir(parents=True, exist_ok=True)
        entrypoint = Path(__file__).resolve().parents[2] / "upload_manager.py"
        process = QProcess()
        process.setProgram(sys.executable)
        process.setArguments([str(entrypoint), "--state-dir", str(self.state_dir)])
        process.setWorkingDirectory(str(entrypoint.parent))
        process.setStandardOutputFile(str(self.state_dir / "manager.stdout.log"), QIODevice.Append)
        process.setStandardErrorFile(str(self.state_dir / "manager.stderr.log"), QIODevice.Append)
        launched = process.startDetached()
        # PyQt returns (success, pid); a failed (False, 0) tuple is still truthy.
        success = launched[0] if isinstance(launched, tuple) else launched
        if not success:
            raise UploadManagerUnavailable(f"could not start Upload Manager: {process.errorString()}")

    def register_dataset(self, **params) -> dict:
        return self.request("register_dataset", params)

    def ensure_ready_async(self) -> Future:
        """Start/connect in the background; result() propagates startup failures.

        The thread owns its Qt socket/process objects and only performs IPC.
        No dataset is created, so an abandoned acquisition setup leaves no queue
        entry behind. The detached manager may continue draining older datasets.
        """
        ready = Future()

        def connect():
            if not ready.set_running_or_notify_cancel():
                return
            try:
                self.get_status()
            except Exception as exc:
                ready.set_exception(exc)
            else:
                ready.set_result(None)

        Thread(target=connect, name="Upload Manager startup", daemon=True).start()
        return ready

    def submit_batch(self, dataset_id: str, submission_id: str, files: Iterable[dict], *, task_id: Optional[str] = None) -> dict:
        """Durably retain the request locally until the manager acknowledges it."""
        outbox = self.state_dir / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        payload = {
            "dataset_id": dataset_id,
            "submission_id": submission_id,
            "files": list(files),
            "task_id": task_id or submission_id,
        }
        outbox_key = hashlib.sha256(
            f"{dataset_id}\0{submission_id}".encode("utf-8")
        ).hexdigest()
        path = outbox / f"{outbox_key}.json"
        temporary = path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        result = self.request("submit_batch", payload)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return result

    def replay_outbox(self) -> int:
        count = 0
        outbox = self.state_dir / "outbox"
        if not outbox.is_dir():
            return count
        for path in sorted(outbox.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.request("submit_batch", payload)
                path.unlink()
                count += 1
            except (OSError, ValueError, RuntimeError, UploadManagerUnavailable):
                continue
        return count

    def seal_dataset(self, dataset_id: str, status: str = "complete") -> dict:
        self.replay_outbox()
        return self.request("seal_dataset", {"dataset_id": dataset_id, "status": status})

    def get_status(self) -> dict:
        return self.request("get_status")

    def show_window(self) -> None:
        self.request("show_window")
