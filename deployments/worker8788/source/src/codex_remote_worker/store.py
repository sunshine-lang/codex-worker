from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading

from .common import JobError, TERMINAL, now, accept_directory


class Store:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.data_root = Path(path).parent
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
            fingerprint TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, record TEXT NOT NULL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at)")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        self.db.close()

    def _get(self, field: str, value: str):
        # Only internal callers select the fixed column names.
        assert field in {"job_id", "request_key"}
        row = self.db.execute(f"SELECT record FROM jobs WHERE {field}=?", (value,)).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, job_id: str):
        with self.lock:
            job = self._get("job_id", job_id)
            if job is None:
                raise JobError("NOT_FOUND", "任务不存在。", 404)
            return job

    def by_key(self, key: str):
        with self.lock:
            return self._get("request_key", key)

    def _save(self, job):
        job["updated_at"] = now()
        self.db.execute("UPDATE jobs SET status=?,record=? WHERE job_id=?",
                        (job["status"], json.dumps(job, ensure_ascii=False), job["job_id"]))

    def accept(self, record: dict, stage: Path, target: Path, capacity: int):
        moved = False
        try:
            with self.transaction():
                existing = self._get("request_key", record["request_key"])
                if existing:
                    if existing["fingerprint"] != record["fingerprint"]:
                        raise JobError("IDEMPOTENCY_CONFLICT", "同一提交编号携带了不同资料或参数。", 409)
                    return existing, True
                if (self.data_root / '.maintenance.json').exists():
                    raise JobError('MAINTENANCE', '服务正在维护，尚未接受新任务；请稍后使用原提交编号重试。', 503)
                count = self.db.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
                if count >= capacity:
                    raise JobError("QUEUE_FULL", "任务队列已满，请稍后使用原提交编号重试。", 429)
                accept_directory(stage, target, self.data_root)
                moved = True
                self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?)",
                                (record["job_id"], record["request_key"], record["fingerprint"],
                                 record["status"], record["created_at"], json.dumps(record, ensure_ascii=False)))
            return record, False
        except BaseException:
            if moved and not self.by_key(record["request_key"]):
                target.rename(stage)
            raise

    def update(self, job_id: str, expected=None, **fields):
        with self.transaction():
            job = self._get("job_id", job_id)
            if job is None:
                raise JobError("NOT_FOUND", "任务不存在。", 404)
            if expected is not None and job["status"] not in expected:
                return job, False
            job.update(fields)
            self._save(job)
            return job, True

    def claim(self, max_active=1):
        if type(max_active) is not int or not 1 <= max_active <= 16:
            raise ValueError('max_active must be an integer from 1 to 16')
        with self.transaction():
            if (self.data_root / '.maintenance.json').exists():
                return None
            active = self.db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('running','checking','cancelling')").fetchone()[0]
            if active >= max_active:
                return None
            row = self.db.execute("SELECT record FROM jobs WHERE status='queued' ORDER BY created_at,job_id LIMIT 1").fetchone()
            if row is None:
                return None
            job = json.loads(row[0])
            job.update(status="running", stage="starting", started_at=now())
            self._save(job)
            return job

    def has_queued(self):
        with self.lock:
            return self.db.execute("SELECT 1 FROM jobs WHERE status='queued' LIMIT 1").fetchone() is not None

    def cancel(self, job_id: str, *, defer_project=False):
        with self.transaction():
            job = self._get("job_id", job_id)
            if job is None:
                raise JobError("NOT_FOUND", "任务不存在。", 404)
            if job["status"] not in TERMINAL:
                if job["status"] == "queued":
                    if defer_project and job.get('project_name'):
                        job.update(status='cancelling', stage='cancelling', cancel_reason='user')
                        self._save(job)
                        return {**job, '_finish_queued_cancel': True}
                    job.update(status="cancelled", stage="cancelled", completed_at=now())
                else:
                    job.update(status="cancelling", stage="cancelling", cancel_reason="user")
                self._save(job)
            return job

    def unfinished(self):
        with self.lock:
            rows = self.db.execute("SELECT record FROM jobs WHERE status IN ('running','checking','cancelling')").fetchall()
            return [json.loads(row[0]) for row in rows]


class InstanceLock:
    """OS-held lock, released on process exit; no stale PID-file heuristic."""
    def __init__(self, path: Path):
        self.stream = path.open("a+b")
        try:
            self.stream.seek(0)
            if self.stream.read(1) == b"":
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            self.stream.close()
            raise RuntimeError("Another worker instance owns this data directory") from None

    def close(self):
        self.stream.close()
