"""Detached Qt application for persistent Squid uploads."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_SOFTWARE_DIR = Path(__file__).resolve().parent
if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

from control.core.upload_manager import (  # noqa: E402
    ManagerInstanceLock,
    UploadManagerEngine,
    UploadStore,
    default_state_dir,
    handle_request,
)
from control.core.upload_manager_client import server_name  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Squid persistent upload manager")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--max-lanes", type=int, default=2)
    parser.add_argument("--rate-limit-mbps", type=float, default=0,
                        help="Aggregate transfer cap in MB/s (0 disables the cap)")
    parser.add_argument("--hidden", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock = ManagerInstanceLock(args.state_dir)
    try:
        lock.acquire()
    except RuntimeError:
        return 0

    from qtpy.QtCore import QTimer, Qt
    from qtpy.QtGui import QDesktopServices
    from qtpy.QtNetwork import QLocalServer
    from qtpy.QtWidgets import (
        QApplication, QAbstractItemView, QHBoxLayout, QLabel, QMainWindow,
        QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QTableWidget,
        QTableWidgetItem, QVBoxLayout, QWidget,
    )
    from qtpy.QtCore import QUrl

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Squid Upload Manager")
    app.setQuitOnLastWindowClosed(False)
    store = UploadStore(args.state_dir / "uploads.sqlite3")
    # Producer requests are written here before IPC.  Replay them before the
    # first scheduling tick so a crash after writer finalization cannot lose a
    # batch. Duplicate submission IDs make this safe after uncertain acks.
    outbox = args.state_dir / "outbox"
    if outbox.is_dir():
        for entry in sorted(outbox.glob("*.json")):
            try:
                store.submit_batch(**json.loads(entry.read_text(encoding="utf-8")))
                entry.unlink()
            except Exception:
                pass
    engine = UploadManagerEngine(store, args.max_lanes, args.rate_limit_mbps * 1_000_000)

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Squid Upload Manager")
            self.resize(1050, 390)
            root = QWidget(); layout = QVBoxLayout(root)
            self.table = QTableWidget(0, 8)
            self.table.setHorizontalHeaderLabels([
                "Acquisition", "State", "Uploaded", "Deleted", "Rate", "ETA", "Local folder", "Remote folder"
            ])
            self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            layout.addWidget(self.table)
            self.details = QLabel("")
            layout.addWidget(self.details)
            controls = QHBoxLayout()
            for label, callback in (
                ("Pause", lambda: self._selected_action("pause")),
                ("Resume", lambda: self._selected_action("resume")),
                ("Retry failed", lambda: self._selected_action("retry_failed")),
                ("Open local folder", lambda: self._open_folder("local_root")),
                ("Open remote folder", lambda: self._open_folder("remote_root")),
                ("View log", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(args.state_dir / "manager.stderr.log")))),
            ):
                button = QPushButton(label); button.clicked.connect(callback); controls.addWidget(button)
            controls.addStretch(1); layout.addLayout(controls)
            self.footer = QLabel(""); layout.addWidget(self.footer)
            self.setCentralWidget(root)
            self.rows = []
            self.table.itemSelectionChanged.connect(self._show_details)

        def refresh(self):
            snapshot = store.status(); self.rows = snapshot["datasets"]
            self.table.setRowCount(len(self.rows))
            for row_index, row in enumerate(self.rows):
                deleted = "Disabled" if not row["delete_after_verify"] else f"{(row['deleted_percent'] or 0):.1f}%"
                rate = f"{row['upload_rate_bps'] / 1e6:.1f} MB/s"
                if row["eta_seconds"] is None:
                    eta = "—"
                else:
                    finish = datetime.now() + timedelta(seconds=row["eta_seconds"])
                    eta = f"~{finish:%H:%M} ({int(row['eta_seconds'])} s)"
                uploaded = f"{row['uploaded_percent']:.1f}%"
                if not row["sealed"]:
                    uploaded += " of data written so far"
                display_state = (
                    "Cleaning up" if row["uploaded_percent"] >= 100 and row["state"] == "draining"
                    else row["state"]
                )
                created = str(row["created_utc"]).replace("T", " ")[:19]
                values = [f"{row['name']}\n{created}", display_state, uploaded, deleted,
                          rate, eta, row["local_root"], row["remote_root"]]
                for column, value in enumerate(values):
                    self.table.setItem(row_index, column, QTableWidgetItem(str(value)))
            free = [row["source_free_bytes"] for row in self.rows if row["source_free_bytes"] is not None]
            free_text = f" · source free {min(free) / 1e9:.1f} GB" if free else ""
            self.footer.setText(f"{snapshot['active_count']} active acquisition(s) · {snapshot['backlog_bytes'] / 1e9:.2f} GB queued{free_text}")
            self._show_details()

        def selected(self):
            row = self.table.currentRow()
            return self.rows[row] if 0 <= row < len(self.rows) else None

        def _show_details(self):
            row = self.selected()
            if row:
                self.details.setText(
                    f"{row['verified_bytes']:,} / {row['total_bytes']:,} bytes verified · "
                    f"{row['file_count']} files · {row['failed_files']} failed · "
                    f"last verification: {row['last_verified_utc'] or 'none'} · "
                    f"retry reason: {row['last_error'] or 'none'} · "
                    f"unknown historical reclamation: "
                    f"{row['unknown_historical_reclamation_bytes']:,} bytes"
                )

        def _selected_action(self, method):
            row = self.selected()
            if row:
                handle_request(store, {"protocol_version": 1, "method": method, "params": {"dataset_id": row["id"]}})
                self.refresh()

        def _open_folder(self, key):
            row = self.selected()
            if row: QDesktopServices.openUrl(QUrl.fromLocalFile(row[key]))

        def closeEvent(self, event):
            event.ignore(); self.hide()

    window = Window()
    tray = QSystemTrayIcon(window.windowIcon(), app)
    menu = QMenu()
    show_action = menu.addAction("Show uploads")
    show_action.triggered.connect(lambda: (window.show(), window.raise_(), window.activateWindow()))
    stop_action = menu.addAction("Stop uploads and exit")
    stop_action.triggered.connect(app.quit)
    tray.setContextMenu(menu); tray.show()

    server = QLocalServer(app)
    try:
        server.setSocketOptions(QLocalServer.UserAccessOption)
    except AttributeError:
        pass
    QLocalServer.removeServer(server_name())
    if not server.listen(server_name()):
        store.close(); lock.release(); return 2
    buffers = {}

    def accept_connections():
        while server.hasPendingConnections():
            socket = server.nextPendingConnection(); buffers[socket] = bytearray()
            socket.readyRead.connect(lambda s=socket: read_socket(s))
            socket.disconnected.connect(lambda s=socket: buffers.pop(s, None))

    def read_socket(socket):
        buffer = buffers.setdefault(socket, bytearray()); buffer.extend(bytes(socket.readAll()))
        while b"\n" in buffer:
            line, remainder = buffer.split(b"\n", 1); buffer[:] = remainder
            try:
                request = json.loads(line.decode("utf-8")); response = handle_request(store, request)
            except Exception as exc:
                response = {"request_id": None, "ok": False, "error": str(exc)}
            if response.get("result", {}).get("show_window"):
                window.show(); window.raise_(); window.activateWindow()
            socket.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")); socket.flush()

    server.newConnection.connect(accept_connections)
    timer = QTimer(); timer.timeout.connect(lambda: (engine.tick(), window.refresh())); timer.start(1000)
    window.refresh()
    if not args.hidden: window.show()
    try:
        return app.exec_()
    finally:
        server.close(); engine.stop(); store.close(); lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
