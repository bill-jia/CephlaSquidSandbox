"""Durable upload queue used by the standalone Squid Upload Manager.

This module deliberately has no Qt imports.  The manager application, command
line clients, and tests can all use the same SQLite inventory and scheduling
engine without constructing a QApplication.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import shutil
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from uuid import uuid4

from control.core.zarr_upload import (
    append_manifest_record,
    read_manifest,
    upload_one_file,
    verify_manifest_copy,
)


PROTOCOL_VERSION = 1
APPLICATION_VERSION = "1"
FILE_STATES = ("pending", "uploading", "verified", "delete_pending", "deleted", "failed")
DATASET_STATES = (
    "receiving", "draining", "waiting_for_share", "paused", "needs_attention", "complete"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / ".local" / "share")
    return Path(base) / "Squid" / "UploadManager"


def normalize_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def _is_under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((normalize_path(path), normalize_path(root))) == normalize_path(root)
    except (ValueError, OSError):
        return False


@dataclass(frozen=True)
class SubmittedFile:
    relative_path: str
    size: int
    generation: str = "1"
    deletable: bool = False
    stable_read: bool = False

    @classmethod
    def from_value(cls, value: Any) -> "SubmittedFile":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(value, -1)
        if isinstance(value, dict):
            return cls(
                relative_path=str(value["relative_path"]),
                size=int(value.get("size", -1)),
                generation=str(value.get("generation", "1")),
                deletable=bool(value.get("deletable", False)),
                stable_read=bool(value.get("stable_read", False)),
            )
        raise TypeError(f"unsupported submitted file: {value!r}")


@dataclass
class TransferResult:
    file_id: int
    ok: bool
    checksum: Optional[str]
    size: Optional[int]
    error: Optional[str]
    transferred_bytes: int
    elapsed_s: float


def _transfer_process(payload: dict, result_queue: multiprocessing.Queue) -> None:
    """One blocking transfer lane.  Kept at module scope for Windows spawn."""
    import squid.logging

    started = time.monotonic()
    rate_limit = float(payload.get("rate_limit_bps") or 0)
    moved = 0

    def throttle(chunk_bytes: int) -> None:
        nonlocal moved
        if rate_limit <= 0 or chunk_bytes <= 0:
            return
        moved += chunk_bytes
        delay = moved / rate_limit - (time.monotonic() - started)
        if delay > 0:
            time.sleep(delay)

    log = squid.logging.get_logger("UploadManagerTransfer")
    historical = payload.get("historical_checksum")
    historical_size = payload.get("historical_size")
    if historical and historical_size is not None and verify_manifest_copy(
        {"sha256": historical, "bytes": historical_size},
        payload["local_path"],
        payload["remote_path"],
        throttle,
    ):
        ok, digest, size, error, transferred = True, historical, historical_size, None, 0
    else:
        ok, digest, size, error = upload_one_file(
            payload["local_path"],
            payload["remote_path"],
            log=log,
            stable_read=bool(payload["stable_read"]),
            heartbeat=throttle,
        )
        transferred = int(size or 0) if ok else 0
    result_queue.put(
        asdict(
            TransferResult(
                file_id=int(payload["id"]),
                ok=ok,
                checksum=digest,
                size=size,
                error=error,
                transferred_bytes=transferred,
                elapsed_s=time.monotonic() - started,
            )
        )
    )


class DatasetOwnershipError(RuntimeError):
    pass


class UploadStore:
    """The manager's single-writer durable inventory.

    Every mutating method commits before returning.  A submission ID is unique
    per dataset, which makes retrying a producer outbox entry safe.
    """

    def __init__(self, db_path: os.PathLike[str] | str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._lock = threading.RLock()
        self._create_schema()
        self.recover_interrupted_work()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _create_schema(self) -> None:
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    local_root TEXT NOT NULL,
                    local_key TEXT NOT NULL,
                    remote_root TEXT NOT NULL,
                    remote_key TEXT NOT NULL,
                    delete_after_verify INTEGER NOT NULL,
                    deletion_mode TEXT NOT NULL DEFAULT 'individual',
                    state TEXT NOT NULL DEFAULT 'receiving',
                    producer_state TEXT NOT NULL DEFAULT 'connected',
                    sealed INTEGER NOT NULL DEFAULT 0,
                    seal_status TEXT,
                    created_utc TEXT NOT NULL,
                    sealed_utc TEXT,
                    last_error TEXT,
                    last_verified_utc TEXT,
                    unknown_historical_reclamation_bytes INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS datasets_local ON datasets(local_key);
                CREATE TABLE IF NOT EXISTS batches (
                    dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    submission_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    PRIMARY KEY(dataset_id, submission_id)
                );
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    task_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    generation TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    deletable INTEGER NOT NULL,
                    stable_read INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    checksum TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    retry_after_epoch REAL NOT NULL DEFAULT 0,
                    transferred_bytes INTEGER NOT NULL DEFAULT 0,
                    verification_bytes INTEGER NOT NULL DEFAULT 0,
                    verified_utc TEXT,
                    delete_intent_utc TEXT,
                    deleted_utc TEXT,
                    UNIQUE(dataset_id, relative_path, generation)
                );
                CREATE INDEX IF NOT EXISTS files_ready ON files(state, dataset_id, id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dataset_id TEXT NOT NULL,
                    file_id INTEGER,
                    kind TEXT NOT NULL,
                    bytes INTEGER NOT NULL DEFAULT 0,
                    detail TEXT,
                    created_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS legacy_history (
                    dataset_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    remote_path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    PRIMARY KEY(dataset_id, remote_path)
                );
                """
            )
            dataset_columns = {row[1] for row in self._db.execute("PRAGMA table_info(datasets)")}
            if "unknown_historical_reclamation_bytes" not in dataset_columns:
                self._db.execute(
                    "ALTER TABLE datasets ADD COLUMN unknown_historical_reclamation_bytes INTEGER NOT NULL DEFAULT 0"
                )
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(files)")}
            if "retry_after_epoch" not in columns:
                self._db.execute(
                    "ALTER TABLE files ADD COLUMN retry_after_epoch REAL NOT NULL DEFAULT 0"
                )

    def register_dataset(
        self,
        *,
        dataset_id: str,
        name: str,
        local_root: str,
        remote_root: str,
        delete_after_verify: bool = True,
        deletion_mode: str = "individual",
    ) -> dict:
        if deletion_mode not in ("individual", "whole_task"):
            raise ValueError("deletion_mode must be 'individual' or 'whole_task'")
        local_root = os.path.abspath(local_root)
        remote_root = os.path.abspath(remote_root)
        local_key, remote_key = normalize_path(local_root), normalize_path(remote_root)
        now = utc_now()
        with self._lock, self._db:
            existing = self._db.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
            if existing:
                if existing["local_key"] != local_key or existing["remote_key"] != remote_key:
                    raise DatasetOwnershipError("dataset identity cannot be retargeted")
                return dict(existing)
            owner = self._db.execute(
                "SELECT id, remote_key FROM datasets WHERE local_key=? AND state != 'complete'", (local_key,)
            ).fetchone()
            if owner:
                raise DatasetOwnershipError(f"local dataset is already owned by {owner['id']}")
            self._db.execute(
                """INSERT INTO datasets
                   (id,name,local_root,local_key,remote_root,remote_key,delete_after_verify,
                    deletion_mode,state,producer_state,sealed,created_utc)
                   VALUES (?,?,?,?,?,?,?,?, 'receiving','connected',0,?)""",
                (dataset_id, name, local_root, local_key, remote_root, remote_key,
                 int(delete_after_verify), deletion_mode, now),
            )
            self._event(dataset_id, None, "dataset_registered", detail=remote_root)
            self._import_legacy_manifests(dataset_id)
            return dict(self._db.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone())

    def submit_batch(
        self,
        dataset_id: str,
        submission_id: str,
        files: Iterable[SubmittedFile | dict | str],
        *,
        task_id: Optional[str] = None,
    ) -> dict:
        task_id = task_id or submission_id
        values = [SubmittedFile.from_value(item) for item in files]
        with self._lock, self._db:
            dataset = self._dataset(dataset_id)
            prior = self._db.execute(
                "SELECT task_id FROM batches WHERE dataset_id=? AND submission_id=?",
                (dataset_id, submission_id),
            ).fetchone()
            if prior:
                count = self._db.execute(
                    "SELECT COUNT(*) FROM files WHERE dataset_id=? AND task_id=?",
                    (dataset_id, prior["task_id"]),
                ).fetchone()[0]
                return {"accepted": True, "duplicate": True, "task_id": prior["task_id"], "file_count": count}
            if dataset["sealed"]:
                raise ValueError("dataset is sealed")
            self._db.execute(
                "INSERT INTO batches(dataset_id,submission_id,task_id,created_utc) VALUES(?,?,?,?)",
                (dataset_id, submission_id, task_id, utc_now()),
            )
            inserted = 0
            for item in values:
                rel = item.relative_path.replace("\\", "/").lstrip("/")
                local = os.path.abspath(os.path.join(dataset["local_root"], *rel.split("/")))
                if not rel or not _is_under(local, dataset["local_root"]):
                    raise ValueError(f"file escapes dataset root: {item.relative_path!r}")
                size = item.size
                if size < 0:
                    size = os.path.getsize(local)
                cur = self._db.execute(
                    """INSERT OR IGNORE INTO files
                       (dataset_id,task_id,relative_path,generation,size,deletable,stable_read,state)
                       VALUES(?,?,?,?,?,?,?,'pending')""",
                    (dataset_id, task_id, rel, item.generation, size,
                     int(item.deletable), int(item.stable_read)),
                )
                inserted += cur.rowcount
            self._event(dataset_id, None, "batch_submitted", detail=submission_id)
            return {"accepted": True, "duplicate": False, "task_id": task_id, "file_count": inserted}

    def seal_dataset(self, dataset_id: str, status: str = "complete") -> dict:
        if status not in ("complete", "aborted", "needs_attention"):
            raise ValueError("invalid seal status")
        with self._lock, self._db:
            self._dataset(dataset_id)
            state = "needs_attention" if status == "needs_attention" else "draining"
            self._db.execute(
                """UPDATE datasets SET sealed=1, seal_status=?, sealed_utc=?,
                   producer_state='disconnected', state=? WHERE id=?""",
                (status, utc_now(), state, dataset_id),
            )
            self._event(dataset_id, None, "dataset_sealed", detail=status)
            self._update_dataset_state(dataset_id)
            return self.dataset_status(dataset_id)

    def pause(self, dataset_id: Optional[str] = None) -> None:
        with self._lock, self._db:
            if dataset_id:
                self._dataset(dataset_id)
                self._db.execute("UPDATE datasets SET state='paused' WHERE id=?", (dataset_id,))
            else:
                self._db.execute("UPDATE datasets SET state='paused' WHERE state != 'complete'")

    def resume(self, dataset_id: Optional[str] = None) -> None:
        with self._lock, self._db:
            where, args = (("id=?", (dataset_id,)) if dataset_id else ("state='paused'", ()))
            rows = self._db.execute(f"SELECT id,sealed FROM datasets WHERE {where}", args).fetchall()
            for row in rows:
                state = "draining" if row["sealed"] else "receiving"
                self._db.execute("UPDATE datasets SET state=? WHERE id=?", (state, row["id"]))

    def retry_failed(self, dataset_id: Optional[str] = None) -> int:
        with self._lock, self._db:
            if dataset_id:
                cur = self._db.execute(
                    "UPDATE files SET state='pending',error=NULL,retry_after_epoch=0 WHERE state='failed' AND dataset_id=?",
                    (dataset_id,),
                )
            else:
                cur = self._db.execute("UPDATE files SET state='pending',error=NULL,retry_after_epoch=0 WHERE state='failed'")
            ids = ([dataset_id] if dataset_id else
                   [row[0] for row in self._db.execute("SELECT id FROM datasets WHERE state='needs_attention'")])
            for item in ids:
                d = self._dataset(item)
                self._db.execute(
                    "UPDATE datasets SET state=? WHERE id=?",
                    ("draining" if d["sealed"] else "receiving", item),
                )
            return cur.rowcount

    def claim_next(self, excluded_datasets: Sequence[str] = ()) -> Optional[dict]:
        """Fairly claim one file, rotating by the dataset's last event time."""
        with self._lock, self._db:
            excluded_sql = ""
            args: List[Any] = []
            if excluded_datasets:
                excluded_sql = " AND d.id NOT IN (%s)" % ",".join("?" for _ in excluded_datasets)
                args.extend(excluded_datasets)
            row = self._db.execute(
                """SELECT f.*, d.local_root,d.remote_root,d.delete_after_verify,
                          d.deletion_mode,d.name,
                          h.checksum AS historical_checksum,h.size AS historical_size
                   FROM files f JOIN datasets d ON d.id=f.dataset_id
                   LEFT JOIN legacy_history h ON h.dataset_id=f.dataset_id
                                             AND h.relative_path=f.relative_path
                   WHERE f.state='pending' AND f.retry_after_epoch<=? 
                     AND d.state NOT IN ('paused','complete','needs_attention')"""
                + excluded_sql
                + " ORDER BY COALESCE((SELECT MAX(e.id) FROM events e WHERE e.dataset_id=d.id AND e.kind='transfer_started'),0), f.id LIMIT 1",
                [time.time(), *args],
            ).fetchone()
            if row is None:
                return None
            cur = self._db.execute(
                "UPDATE files SET state='uploading',attempts=attempts+1,error=NULL WHERE id=? AND state='pending'",
                (row["id"],),
            )
            if cur.rowcount != 1:
                return None
            self._event(row["dataset_id"], row["id"], "transfer_started")
            result = dict(row)
            result["local_path"] = os.path.join(row["local_root"], *row["relative_path"].split("/"))
            result["remote_path"] = os.path.join(row["remote_root"], *row["relative_path"].split("/"))
            return result

    def record_transfer(self, result: TransferResult) -> None:
        with self._lock, self._db:
            row = self._file(result.file_id)
            if result.ok:
                now = utc_now()
                self._db.execute(
                    """UPDATE files SET state='verified',checksum=?,size=?,transferred_bytes=?,
                       verification_bytes=?,verified_utc=?,error=NULL,retry_after_epoch=0 WHERE id=?""",
                    (result.checksum, result.size, result.transferred_bytes, result.size or 0, now, result.file_id),
                )
                self._db.execute(
                    "UPDATE datasets SET last_verified_utc=?,last_error=NULL WHERE id=?",
                    (now, row["dataset_id"]),
                )
                self._event(row["dataset_id"], result.file_id, "verified", result.transferred_bytes)
            else:
                permanent = bool(result.error and result.error.startswith("source missing:"))
                state = "failed" if permanent else "pending"
                retry_at = 0.0 if permanent else time.time() + min(60.0, 2.0 ** min(int(row["attempts"]), 6))
                self._db.execute(
                    "UPDATE files SET state=?,error=?,retry_after_epoch=? WHERE id=?",
                    (state, result.error, retry_at, result.file_id),
                )
                self._db.execute(
                    "UPDATE datasets SET last_error=?,state=? WHERE id=?",
                    (result.error, "needs_attention" if permanent else "waiting_for_share", row["dataset_id"]),
                )
                self._event(row["dataset_id"], result.file_id, "failed", detail=result.error)
            self._update_dataset_state(row["dataset_id"])

    def eligible_deletions(self) -> List[dict]:
        with self._lock:
            rows = self._db.execute(
                """SELECT f.*,d.local_root,d.remote_root,d.deletion_mode
                   FROM files f JOIN datasets d ON d.id=f.dataset_id
                   WHERE f.state='verified' AND f.deletable=1 AND d.delete_after_verify=1"""
            ).fetchall()
            eligible = []
            for row in rows:
                if row["deletion_mode"] == "whole_task":
                    outstanding = self._db.execute(
                        """SELECT COUNT(*) FROM files WHERE dataset_id=? AND task_id=?
                           AND state NOT IN ('verified','delete_pending','deleted')""",
                        (row["dataset_id"], row["task_id"]),
                    ).fetchone()[0]
                    if outstanding:
                        continue
                item = dict(row)
                item["local_path"] = os.path.join(row["local_root"], *row["relative_path"].split("/"))
                item["remote_path"] = os.path.join(row["remote_root"], *row["relative_path"].split("/"))
                eligible.append(item)
            return eligible

    def begin_delete(self, file_id: int) -> bool:
        with self._lock, self._db:
            row = self._file(file_id)
            cur = self._db.execute(
                "UPDATE files SET state='delete_pending',delete_intent_utc=? WHERE id=? AND state='verified'",
                (utc_now(), file_id),
            )
            if cur.rowcount:
                self._event(row["dataset_id"], file_id, "delete_intent")
            return bool(cur.rowcount)

    def finish_delete(self, file_id: int, error: Optional[str] = None) -> None:
        with self._lock, self._db:
            row = self._file(file_id)
            if error:
                self._db.execute("UPDATE files SET state='verified',error=? WHERE id=?", (error, file_id))
                self._event(row["dataset_id"], file_id, "delete_failed", detail=error)
            else:
                self._db.execute(
                    "UPDATE files SET state='deleted',deleted_utc=?,error=NULL WHERE id=?",
                    (utc_now(), file_id),
                )
                self._event(row["dataset_id"], file_id, "deleted", int(row["size"]))
                dataset = self._dataset(row["dataset_id"])
                append_manifest_record(
                    os.path.join(dataset["local_root"], "upload_manifest.jsonl"),
                    {
                        "event": "deleted",
                        "task_id": row["task_id"],
                        "local_path": os.path.join(
                            dataset["local_root"], *row["relative_path"].split("/")
                        ),
                        "remote_root": dataset["remote_root"],
                        "bytes": int(row["size"]),
                        "deleted_utc": utc_now(),
                    },
                )
            self._update_dataset_state(row["dataset_id"])

    def recover_interrupted_work(self) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE files SET state='pending' WHERE state='uploading'")
            rows = self._db.execute("SELECT id,dataset_id,relative_path FROM files WHERE state='delete_pending'").fetchall()
            for row in rows:
                dataset = self._dataset(row["dataset_id"])
                local = os.path.join(dataset["local_root"], *row["relative_path"].split("/"))
                state = "deleted" if not os.path.exists(local) else "verified"
                self._db.execute(
                    "UPDATE files SET state=?,deleted_utc=CASE WHEN ?='deleted' THEN ? ELSE deleted_utc END WHERE id=?",
                    (state, state, utc_now(), row["id"]),
                )

    def dataset_status(self, dataset_id: str) -> dict:
        with self._lock:
            d = dict(self._dataset(dataset_id))
            totals = self._db.execute(
                """SELECT COUNT(*) file_count, COALESCE(SUM(size),0) total_bytes,
                   COALESCE(SUM(CASE WHEN state IN ('verified','delete_pending','deleted') THEN size ELSE 0 END),0) verified_bytes,
                   COALESCE(SUM(CASE WHEN deletable=1 THEN size ELSE 0 END),0) deletable_bytes,
                   COALESCE(SUM(CASE WHEN state='deleted' THEN size ELSE 0 END),0) deleted_bytes,
                   COALESCE(SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END),0) failed_files,
                   COALESCE(SUM(CASE WHEN state IN ('pending','uploading') THEN size ELSE 0 END),0) backlog_bytes
                   FROM files WHERE dataset_id=?""",
                (dataset_id,),
            ).fetchone()
            d.update(dict(totals))
            d["uploaded_percent"] = (100.0 * d["verified_bytes"] / d["total_bytes"]) if d["total_bytes"] else 0.0
            d["deleted_percent"] = (100.0 * d["deleted_bytes"] / d["deletable_bytes"]) if d["deletable_bytes"] else None
            rate = self._recent_rate(dataset_id)
            d["upload_rate_bps"] = rate
            d["eta_seconds"] = (d["backlog_bytes"] / rate) if d["sealed"] and rate > 0 and d["backlog_bytes"] else None
            if d["state"] in ("paused", "waiting_for_share", "needs_attention"):
                d["eta_seconds"] = None
            try:
                d["source_free_bytes"] = shutil.disk_usage(d["local_root"]).free
            except OSError:
                d["source_free_bytes"] = None
            return d

    def status(self) -> dict:
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM datasets ORDER BY created_utc DESC")]
            rows = [self.dataset_status(dataset_id) for dataset_id in ids]
            return {
                "protocol_version": PROTOCOL_VERSION,
                "application_version": APPLICATION_VERSION,
                "datasets": rows,
                "active_count": sum(row["state"] != "complete" for row in rows),
                "backlog_bytes": sum(row["backlog_bytes"] for row in rows),
            }

    def file_row(self, file_id: int) -> dict:
        with self._lock:
            return dict(self._file(file_id))

    def _recent_rate(self, dataset_id: str, window_s: int = 30) -> float:
        cutoff = datetime.fromtimestamp(time.time() - window_s, timezone.utc).isoformat()
        row = self._db.execute(
            "SELECT COALESCE(SUM(bytes),0) FROM events WHERE dataset_id=? AND kind='verified' AND created_utc>=?",
            (dataset_id, cutoff),
        ).fetchone()
        return float(row[0]) / window_s

    def _import_legacy_manifests(self, dataset_id: str) -> None:
        dataset = self._dataset(dataset_id)
        unknown = 0
        seen_missing = set()
        for name in ("upload_manifest.jsonl", "upload_manifest_backfill.jsonl"):
            path = os.path.join(dataset["local_root"], name)
            try:
                records = read_manifest(path)
            except OSError:
                continue
            for record in records:
                remote = record.get("remote_path")
                checksum = record.get("sha256")
                size = record.get("bytes")
                if (
                    not record.get("verified_utc")
                    or not isinstance(remote, str)
                    or not isinstance(checksum, str)
                    or len(checksum) != 64
                    or type(size) is not int
                    or size < 0
                ):
                    continue
                recorded_root = record.get("remote_root")
                if recorded_root and normalize_path(recorded_root) != dataset["remote_key"]:
                    continue
                if not _is_under(remote, dataset["remote_root"]):
                    continue
                rel = os.path.relpath(remote, dataset["remote_root"]).replace("\\", "/")
                self._db.execute(
                    """INSERT OR REPLACE INTO legacy_history
                       (dataset_id,relative_path,remote_path,checksum,size) VALUES(?,?,?,?,?)""",
                    (dataset_id, rel, remote, checksum, size),
                )
                local = os.path.join(dataset["local_root"], *rel.split("/"))
                if not os.path.exists(local) and rel not in seen_missing:
                    seen_missing.add(rel)
                    unknown += size
        self._db.execute(
            "UPDATE datasets SET unknown_historical_reclamation_bytes=? WHERE id=?",
            (unknown, dataset_id),
        )

    def _dataset(self, dataset_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown dataset: {dataset_id}")
        return row

    def _file(self, file_id: int) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown file: {file_id}")
        return row

    def _event(self, dataset_id: str, file_id: Optional[int], kind: str, bytes_: int = 0, detail: Optional[str] = None) -> None:
        self._db.execute(
            "INSERT INTO events(dataset_id,file_id,kind,bytes,detail,created_utc) VALUES(?,?,?,?,?,?)",
            (dataset_id, file_id, kind, bytes_, detail, utc_now()),
        )

    def _update_dataset_state(self, dataset_id: str) -> None:
        d = self._dataset(dataset_id)
        if d["state"] in ("paused", "needs_attention"):
            return
        counts = self._db.execute(
            """SELECT
               SUM(CASE WHEN state IN ('pending','uploading','delete_pending') THEN 1 ELSE 0 END),
               SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END)
               FROM files WHERE dataset_id=?""",
            (dataset_id,),
        ).fetchone()
        active, failed = int(counts[0] or 0), int(counts[1] or 0)
        delete_remaining = self._db.execute(
            """SELECT COUNT(*) FROM files f JOIN datasets d ON d.id=f.dataset_id
               WHERE f.dataset_id=? AND d.delete_after_verify=1 AND f.deletable=1
                 AND f.state IN ('verified','delete_pending')""",
            (dataset_id,),
        ).fetchone()[0]
        waiting = self._db.execute(
            "SELECT COUNT(*) FROM files WHERE dataset_id=? AND state='pending' AND retry_after_epoch>?",
            (dataset_id, time.time()),
        ).fetchone()[0]
        if failed:
            state = "needs_attention"
        elif waiting:
            state = "waiting_for_share"
        elif d["sealed"] and not active and not delete_remaining:
            state = "complete"
        elif d["sealed"]:
            state = "draining"
        else:
            state = "receiving"
        self._db.execute("UPDATE datasets SET state=? WHERE id=?", (state, dataset_id))


class UploadManagerEngine:
    """Small scheduler with a global process lane budget across all datasets."""

    def __init__(self, store: UploadStore, max_lanes: int = 2, rate_limit_bps: float = 0):
        self.store = store
        self.max_lanes = max(1, int(max_lanes))
        self.rate_limit_bps = max(0.0, float(rate_limit_bps))
        self._ctx = multiprocessing.get_context("spawn")
        self._results = self._ctx.Queue()
        self._active: Dict[int, multiprocessing.Process] = {}

    def tick(self) -> None:
        self._collect_results()
        self._reconcile_dead_processes()
        while len(self._active) < self.max_lanes:
            item = self.store.claim_next()
            if item is None:
                break
            if self.rate_limit_bps:
                item["rate_limit_bps"] = self.rate_limit_bps / self.max_lanes
            process = self._ctx.Process(target=_transfer_process, args=(item, self._results), daemon=True)
            process.start()
            self._active[int(item["id"])] = process
        self._delete_verified_files()

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while self._active and time.monotonic() < deadline:
            self.tick()
            time.sleep(0.05)
        for process in self._active.values():
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
        self._active.clear()
        self.store.recover_interrupted_work()
        try:
            self._results.close()
            self._results.cancel_join_thread()
        except (OSError, ValueError):
            pass

    def _collect_results(self) -> None:
        while True:
            try:
                raw = self._results.get_nowait()
            except queue.Empty:
                return
            result = TransferResult(**raw)
            process = self._active.pop(result.file_id, None)
            if process is not None:
                process.join(timeout=1)
            row = self.store.file_row(result.file_id)
            if result.ok:
                dataset = self.store.dataset_status(row["dataset_id"])
                manifest_path = os.path.join(dataset["local_root"], "upload_manifest.jsonl")
                append_manifest_record(
                    manifest_path,
                    {
                        "task_id": row["task_id"], "local_path": os.path.join(dataset["local_root"], *row["relative_path"].split("/")),
                        "remote_path": os.path.join(dataset["remote_root"], *row["relative_path"].split("/")),
                        "remote_root": dataset["remote_root"], "sha256": result.checksum,
                        "bytes": result.size, "verified_utc": utc_now(), "deletable": bool(row["deletable"]),
                    },
                )
            self.store.record_transfer(result)

    def _reconcile_dead_processes(self) -> None:
        for file_id, process in list(self._active.items()):
            if process.is_alive() or process.exitcode is None:
                continue
            # A clean child always enqueues a result before returning.  The
            # queue feeder can publish it just after the process exits, so let
            # the next tick collect it instead of creating a false failure.
            if process.exitcode == 0:
                continue
            process.join(timeout=0)
            self._active.pop(file_id, None)
            self.store.record_transfer(TransferResult(file_id, False, None, None, "transfer process exited", 0, 0.0))

    def _delete_verified_files(self) -> None:
        for item in self.store.eligible_deletions():
            if not self.store.begin_delete(item["id"]):
                continue
            error = None
            try:
                # The exact destination is immutable in the dataset record.  A
                # mismatched destination can therefore never authorize this unlink.
                if normalize_path(item["remote_path"]).startswith(normalize_path(item["remote_root"]) + os.sep):
                    if os.path.exists(item["local_path"]):
                        os.remove(item["local_path"])
                else:
                    error = "destination mismatch"
            except OSError as exc:
                error = str(exc)
            self.store.finish_delete(item["id"], error)


class ManagerInstanceLock:
    def __init__(self, state_dir: os.PathLike[str] | str):
        self._path = Path(state_dir) / "manager.lock"
        self._handle = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+b")
        try:
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise RuntimeError("Upload Manager is already running") from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def handle_request(store: UploadStore, request: dict) -> dict:
    """Dispatch one versioned IPC request.  Responses are always JSON-safe."""
    request_id = request.get("request_id")
    try:
        if int(request.get("protocol_version", -1)) != PROTOCOL_VERSION:
            raise ValueError(f"incompatible protocol; manager requires {PROTOCOL_VERSION}")
        if request.get("application_version", APPLICATION_VERSION) != APPLICATION_VERSION:
            raise ValueError(
                f"incompatible application; manager requires {APPLICATION_VERSION}"
            )
        method = request["method"]
        params = request.get("params") or {}
        if method == "register_dataset":
            result = store.register_dataset(**params)
        elif method == "submit_batch":
            result = store.submit_batch(**params)
        elif method == "seal_dataset":
            result = store.seal_dataset(**params)
        elif method == "get_status":
            result = store.status()
        elif method == "pause":
            store.pause(params.get("dataset_id")); result = {"ok": True}
        elif method == "resume":
            store.resume(params.get("dataset_id")); result = {"ok": True}
        elif method == "retry_failed":
            result = {"retried": store.retry_failed(params.get("dataset_id"))}
        elif method == "show_window":
            result = {"show_window": True}
        else:
            raise ValueError(f"unknown method: {method}")
        return {"request_id": request_id, "ok": True, "result": result}
    except Exception as exc:
        return {"request_id": request_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
