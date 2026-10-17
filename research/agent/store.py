"""Task-scoped immutable artifacts and durable events for the local research agent."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.blobs = self.directory / "artifacts"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory / "research.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
            body TEXT NOT NULL, created TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, task TEXT NOT NULL,
            kind TEXT NOT NULL, created TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(task TEXT NOT NULL, seq INTEGER NOT NULL,
            body TEXT NOT NULL, PRIMARY KEY(task,seq));
          CREATE TABLE IF NOT EXISTS responses(task TEXT NOT NULL, id TEXT NOT NULL,
            body TEXT NOT NULL, PRIMARY KEY(task,id));
        """)
        self.db.commit()

    def create(self, task, owner, body):
        with self.lock, self.db:
            self.db.execute("INSERT INTO tasks VALUES(?,?,?,?)",
                            (task, str(owner), canonical(body).decode(), now()))

    def get(self, task, owner=None):
        with self.lock:
            row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
        if row is None or (owner is not None and row["owner"] != str(owner)):
            raise LookupError("task_not_found")
        return json.loads(row["body"])

    def update(self, task, **changes):
        with self.lock, self.db:
            body = {**self.get(task), **changes, "updated_at": now()}
            self.db.execute("UPDATE tasks SET body=? WHERE id=?", (canonical(body).decode(), task))

    def transition(self, task, expected, **changes):
        """Compare-and-set the task status; returns False when another worker won.

        Read and write happen inside one IMMEDIATE transaction so two processes
        sharing the SQLite file cannot both claim the same task. Callers use the
        return value to decide whether to run, so a recovered task is never
        executed twice.
        """
        allowed = {expected} if isinstance(expected, str) else set(expected)
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                row = self.db.execute("SELECT body FROM tasks WHERE id=?", (task,)).fetchone()
                if row is None:
                    self.db.rollback()
                    raise LookupError("task_not_found")
                body = json.loads(row[0])
                if body.get("status") not in allowed:
                    self.db.rollback()
                    return False
                next_body = {**body, **changes, "updated_at": now()}
                self.db.execute("UPDATE tasks SET body=? WHERE id=?",
                                (canonical(next_body).decode(), task))
                self.db.commit()
                return True
            except sqlite3.OperationalError:
                self.db.rollback()
                return False

    def count_status(self, statuses):
        """Number of tasks in the given statuses (used for queue bounds)."""
        wanted = {statuses} if isinstance(statuses, str) else set(statuses)
        with self.lock:
            rows = self.db.execute("SELECT body FROM tasks").fetchall()
        return sum(1 for row in rows if json.loads(row[0]).get("status") in wanted)

    def event(self, task, event_type, **fields):
        with self.lock, self.db:
            seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE task=?",
                                  (task,)).fetchone()[0]
            event = {"task_id": task, "seq": seq, "type": event_type, "timestamp": now(), **fields}
            self.db.execute("INSERT INTO events VALUES(?,?,?)", (task, seq, canonical(event).decode()))
        return event

    def events(self, task, after=0):
        with self.lock:
            rows = self.db.execute("SELECT body FROM events WHERE task=? AND seq>? ORDER BY seq LIMIT 200",
                                   (task, after)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def put(self, task, kind, payload):
        value = {"schema_version": "agent-artifact/1", "task_id": task, "kind": kind, "payload": payload}
        data = canonical(value)
        sha = hashlib.sha256(data).hexdigest()
        path = self.blobs / (sha + ".json")
        with self.lock:
            if path.exists():
                if path.read_bytes() != data:
                    raise ValueError("artifact_corrupted")
            else:
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(data)
                temporary.replace(path)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?)", (sha, task, kind, now()))
        return sha

    def read(self, task, artifact_id):
        if not isinstance(artifact_id, str) or not re.fullmatch(r"[a-f0-9]{64}", artifact_id):
            raise ValueError("invalid_artifact_id")
        with self.lock:
            row = self.db.execute("SELECT task FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None or row[0] != task:
            raise LookupError("artifact_not_found")
        data = (self.blobs / (artifact_id + ".json")).read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact_id:
            raise ValueError("artifact_hash_mismatch")
        value = json.loads(data)
        if value["task_id"] != task or value["schema_version"] != "agent-artifact/1":
            raise ValueError("artifact_context_mismatch")
        return value

    def list(self, task):
        with self.lock:
            return [dict(row) for row in self.db.execute(
                "SELECT id,kind,created FROM artifacts WHERE task=? ORDER BY created", (task,))]

    def response(self, task, response_id, body=None):
        with self.lock, self.db:
            row = self.db.execute("SELECT body FROM responses WHERE task=? AND id=?", (task, response_id)).fetchone()
            if row:
                if body is not None and json.loads(row[0]) != body:
                    raise ValueError("response_id_conflict")
                return json.loads(row[0])
            if body is not None:
                self.db.execute("INSERT INTO responses VALUES(?,?,?)", (task, response_id, canonical(body).decode()))
        return None

    def close(self):
        self.db.close()
