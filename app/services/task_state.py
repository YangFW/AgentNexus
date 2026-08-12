from __future__ import annotations

import base64
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from app import db


RUN_STATUSES = frozenset(
    {"queued", "running", "paused", "waiting_approval", "completed", "failed", "cancelled"}
)
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_RUN_STATUSES = frozenset({"running", "paused", "waiting_approval"})
RUN_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"paused", "waiting_approval", "completed", "failed", "cancelled"}),
    "paused": frozenset({"running", "failed", "cancelled"}),
    "waiting_approval": frozenset({"running", "completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

NODE_STATUSES = frozenset({"pending", "running", "completed", "failed", "skipped", "cancelled"})
TERMINAL_NODE_STATUSES = frozenset({"completed", "failed", "skipped", "cancelled"})
NODE_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "pending": frozenset({"running", "skipped", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "skipped": frozenset(),
    "cancelled": frozenset(),
}

COMMAND_STATUSES = frozenset({"queued", "claimed", "completed", "failed", "cancelled"})
COMMAND_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "queued": frozenset({"claimed", "cancelled"}),
    "claimed": frozenset({"queued", "completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

TASK_STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'paused', 'waiting_approval', 'completed', 'failed', 'cancelled')),
    current_node_id TEXT NOT NULL DEFAULT '',
    resumed_from_checkpoint_id TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (task_id, attempt)
);

CREATE TABLE IF NOT EXISTS task_nodes (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    parent_node_id TEXT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'step',
    sequence INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'cancelled')),
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, node_key),
    FOREIGN KEY (run_id) REFERENCES task_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_node_id) REFERENCES task_nodes(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS task_checkpoints (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    node_id TEXT,
    sequence INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    state_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    restored_at TEXT NOT NULL DEFAULT '',
    restore_count INTEGER NOT NULL DEFAULT 0,
    last_restore_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id) REFERENCES task_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (node_id) REFERENCES task_nodes(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS task_commands (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT,
    command_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'claimed', 'completed', 'failed', 'cancelled')),
    priority INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    worker_id TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    claimed_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_task_runs_task_attempt
    ON task_runs(task_id, attempt DESC);
CREATE INDEX IF NOT EXISTS idx_task_runs_status
    ON task_runs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_nodes_run_sequence
    ON task_nodes(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_task_checkpoints_run_sequence
    ON task_checkpoints(run_id, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_task_commands_queue
    ON task_commands(status, available_at, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_task_commands_task
    ON task_commands(task_id, run_id, created_at);
"""

_TYPE_KEY = "__task_state_type__"
_UNSET = object()


class TaskStateError(RuntimeError):
    """Base error raised by the persistent task-state layer."""


class StateNotFoundError(TaskStateError):
    """Raised when a requested run, node, checkpoint, or command does not exist."""


class InvalidStateTransition(TaskStateError):
    """Raised when a persisted state-machine transition is not allowed."""

    def __init__(self, entity: str, entity_id: str, old_status: str, new_status: str) -> None:
        super().__init__(f"{entity} {entity_id} cannot transition from {old_status!r} to {new_status!r}")
        self.entity = entity
        self.entity_id = entity_id
        self.old_status = old_status
        self.new_status = new_status


class TaskCancellationRequested(TaskStateError):
    """Cooperative-cancellation signal raised at safe execution boundaries."""

    def __init__(self, task_id: str, run_id: str | None = None) -> None:
        suffix = f" (run {run_id})" if run_id else ""
        super().__init__(f"Cancellation requested for task {task_id}{suffix}")
        self.task_id = task_id
        self.run_id = run_id


def _encode_state(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return {_TYPE_KEY: "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {_TYPE_KEY: "date", "value": value.isoformat()}
    if isinstance(value, Path):
        return {_TYPE_KEY: "path", "value": str(value)}
    if isinstance(value, uuid.UUID):
        return {_TYPE_KEY: "uuid", "value": str(value)}
    if isinstance(value, Decimal):
        return {_TYPE_KEY: "decimal", "value": str(value)}
    if isinstance(value, bytes):
        return {_TYPE_KEY: "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return {_TYPE_KEY: "tuple", "items": [_encode_state(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        items = [_encode_state(item) for item in value]
        items.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return {_TYPE_KEY: "set", "items": items}
    if isinstance(value, Mapping):
        if all(isinstance(key, str) for key in value) and _TYPE_KEY not in value:
            return {str(key): _encode_state(item) for key, item in value.items()}
        return {
            _TYPE_KEY: "mapping",
            "items": [[_encode_state(key), _encode_state(item)] for key, item in value.items()],
        }
    if isinstance(value, list):
        return [_encode_state(item) for item in value]
    raise TypeError(f"Checkpoint state contains unsupported value: {type(value).__name__}")


def _decode_state(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode_state(item) for item in value]
    if not isinstance(value, dict):
        return value
    marker = value.get(_TYPE_KEY)
    if not marker:
        return {key: _decode_state(item) for key, item in value.items()}
    if marker == "datetime":
        return datetime.fromisoformat(value["value"])
    if marker == "date":
        return date.fromisoformat(value["value"])
    if marker == "path":
        return Path(value["value"])
    if marker == "uuid":
        return uuid.UUID(value["value"])
    if marker == "decimal":
        return Decimal(value["value"])
    if marker == "bytes":
        return base64.b64decode(value["value"])
    if marker == "tuple":
        return tuple(_decode_state(item) for item in value.get("items", []))
    if marker == "set":
        return set(_decode_state(item) for item in value.get("items", []))
    if marker == "mapping":
        return {_decode_state(key): _decode_state(item) for key, item in value.get("items", [])}
    raise ValueError(f"Unknown checkpoint state type marker: {marker}")


def serialize_checkpoint_state(value: Any) -> str:
    """Serialize execution state as inspectable JSON without using unsafe pickle."""

    return json.dumps(_encode_state(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def deserialize_checkpoint_state(value: str | bytes | None) -> Any:
    """Restore state created by :func:`serialize_checkpoint_state`."""

    if value is None or value == "":
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return _decode_state(json.loads(value))


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    """Create the task-state schema, using the platform database by default."""

    owns_connection = conn is None
    connection = conn or db.get_conn()
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(TASK_STATE_SCHEMA_SQL)
        connection.commit()
    finally:
        if owns_connection:
            connection.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    return dict(value)


def _normalise_time(value: str | datetime | None, fallback: str) -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class TaskStateService:
    """Durable run, node, checkpoint, and command state for the Agent runtime.

    The service is independent from ``AgentRuntime`` so lifecycle state can be
    persisted without changing the user-facing task projection.
    """

    def __init__(
        self,
        connection: sqlite3.Connection | Callable[[], sqlite3.Connection] | None = None,
        *,
        clock: Callable[[], str] | None = None,
        auto_init: bool = True,
    ) -> None:
        self._shared_connection = connection if isinstance(connection, sqlite3.Connection) else None
        self._connection_factory = connection if callable(connection) else db.get_conn
        self._clock = clock or _utc_now
        self._lock = threading.RLock()
        if auto_init:
            self.init_schema()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._shared_connection or self._connection_factory()
            owns_connection = self._shared_connection is None
            original_row_factory = conn.row_factory
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
                if write:
                    conn.commit()
            except Exception:
                if write:
                    conn.rollback()
                raise
            finally:
                if owns_connection:
                    conn.close()
                else:
                    conn.row_factory = original_row_factory

    def _now(self) -> str:
        return self._clock()

    def init_schema(self) -> None:
        if self._shared_connection is not None:
            init_schema(self._shared_connection)
            return
        conn = self._connection_factory()
        try:
            init_schema(conn)
        finally:
            conn.close()

    # -- Runs -------------------------------------------------------------

    def create_run(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        resumed_from_checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        if not task_id.strip():
            raise ValueError("task_id cannot be empty")
        run_id = run_id or _new_id("trun")
        now = self._now()
        metadata_json = serialize_checkpoint_state(_json_object(metadata, field="metadata"))
        with self._connection(write=True) as conn:
            checkpoint_id = self._validate_resume_checkpoint(
                conn, task_id, resumed_from_checkpoint_id
            )
            attempt = int(
                conn.execute(
                    "SELECT COALESCE(MAX(attempt), 0) + 1 FROM task_runs WHERE task_id = ?",
                    (task_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO task_runs(
                    id, task_id, attempt, status, resumed_from_checkpoint_id,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (run_id, task_id, attempt, checkpoint_id, metadata_json, now, now),
            )
            row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return self._serialize_run(_row_dict(row) or {})

    def begin_run(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        resumed_from_checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        if not task_id.strip():
            raise ValueError("task_id cannot be empty")
        now = self._now()
        with self._connection(write=True) as conn:
            row: sqlite3.Row | None = None
            if run_id:
                row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
                if row is not None and row["task_id"] != task_id:
                    raise TaskStateError(f"Run {run_id} belongs to task {row['task_id']}, not {task_id}")
            else:
                row = conn.execute(
                    "SELECT * FROM task_runs WHERE task_id = ? AND status = 'queued' ORDER BY attempt LIMIT 1",
                    (task_id,),
                ).fetchone()
            if row is None:
                run_id = run_id or _new_id("trun")
                checkpoint_id = self._validate_resume_checkpoint(
                    conn, task_id, resumed_from_checkpoint_id
                )
                attempt = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(attempt), 0) + 1 FROM task_runs WHERE task_id = ?",
                        (task_id,),
                    ).fetchone()[0]
                )
                conn.execute(
                    """
                    INSERT INTO task_runs(
                        id, task_id, attempt, status, resumed_from_checkpoint_id,
                        metadata_json, started_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task_id,
                        attempt,
                        checkpoint_id,
                        serialize_checkpoint_state(_json_object(metadata, field="metadata")),
                        now,
                        now,
                        now,
                    ),
                )
            else:
                run_id = str(row["id"])
                self._assert_transition("run", run_id, row["status"], "running", RUN_TRANSITIONS)
                checkpoint_id = str(row["resumed_from_checkpoint_id"] or "")
                if resumed_from_checkpoint_id:
                    checkpoint_id = self._validate_resume_checkpoint(
                        conn, task_id, resumed_from_checkpoint_id
                    )
                merged_metadata = deserialize_checkpoint_state(row["metadata_json"]) or {}
                if metadata is not None:
                    merged_metadata.update(_json_object(metadata, field="metadata"))
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = 'running', resumed_from_checkpoint_id = ?, metadata_json = ?,
                        started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                        finished_at = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        checkpoint_id,
                        serialize_checkpoint_state(merged_metadata),
                        now,
                        now,
                        run_id,
                    ),
                )
            active = conn.execute(
                """
                SELECT id FROM task_runs
                WHERE task_id = ? AND id != ? AND status IN ('running', 'paused', 'waiting_approval')
                LIMIT 1
                """,
                (task_id, run_id),
            ).fetchone()
            if active is not None:
                raise TaskStateError(f"Task {task_id} already has active run {active['id']}")
            result = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return self._serialize_run(_row_dict(result) or {})

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return self._serialize_run(_row_dict(row)) if row else None

    def list_runs(
        self,
        *,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in RUN_STATUSES:
            raise ValueError(f"Unknown run status: {status}")
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM task_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, attempt DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._serialize_run(_row_dict(row) or {}) for row in rows]

    def transition_run(
        self,
        run_id: str,
        status: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        current_node_id: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        if status not in RUN_STATUSES:
            raise ValueError(f"Unknown run status: {status}")
        now = self._now()
        with self._connection(write=True) as conn:
            row = self._require_row(conn, "task_runs", run_id, "run")
            self._assert_transition("run", run_id, row["status"], status, RUN_TRANSITIONS)
            merged_metadata = deserialize_checkpoint_state(row["metadata_json"]) or {}
            if metadata is not None:
                merged_metadata.update(_json_object(metadata, field="metadata"))
            values: dict[str, Any] = {
                "status": status,
                "metadata_json": serialize_checkpoint_state(merged_metadata),
                "updated_at": now,
            }
            if result is not None:
                values["result_json"] = serialize_checkpoint_state(_json_object(result, field="result"))
            if error is not None:
                values["error_json"] = serialize_checkpoint_state(_json_object(error, field="error"))
            if current_node_id is not _UNSET:
                node_id = str(current_node_id or "")
                if node_id:
                    self._validate_node_run(conn, node_id, run_id)
                values["current_node_id"] = node_id
            if status == "running":
                values["started_at"] = row["started_at"] or now
                values["finished_at"] = ""
            elif status in TERMINAL_RUN_STATUSES:
                values["finished_at"] = now
                values["current_node_id"] = ""
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(
                f"UPDATE task_runs SET {assignments} WHERE id = ?",  # noqa: S608 - fixed column names
                [*values.values(), run_id],
            )
            updated = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return self._serialize_run(_row_dict(updated) or {})

    def finish_run(
        self,
        run_id: str,
        *,
        status: str = "completed",
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError("finish_run status must be completed, failed, or cancelled")
        return self.transition_run(
            run_id,
            status,
            result=result,
            error=error,
            metadata=metadata,
        )

    def update_run_metadata(
        self, run_id: str, metadata: Mapping[str, Any], *, merge: bool = True
    ) -> dict[str, Any]:
        with self._connection(write=True) as conn:
            row = self._require_row(conn, "task_runs", run_id, "run")
            value = deserialize_checkpoint_state(row["metadata_json"]) or {} if merge else {}
            value.update(_json_object(metadata, field="metadata"))
            conn.execute(
                "UPDATE task_runs SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (serialize_checkpoint_state(value), self._now(), run_id),
            )
            updated = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return self._serialize_run(_row_dict(updated) or {})

    def delete_run(self, run_id: str) -> bool:
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT status FROM task_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return False
            if row["status"] in ACTIVE_RUN_STATUSES:
                raise TaskStateError("An active run must be finished or cancelled before deletion")
            conn.execute("DELETE FROM task_runs WHERE id = ?", (run_id,))
        return True

    # -- Nodes ------------------------------------------------------------

    def create_node(
        self,
        run_id: str,
        node_key: str,
        title: str,
        *,
        node_id: str | None = None,
        parent_node_id: str | None = None,
        kind: str = "step",
        sequence: int | None = None,
        input_data: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not node_key.strip() or not title.strip():
            raise ValueError("node_key and title cannot be empty")
        node_id = node_id or _new_id("tnode")
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["status"] in TERMINAL_RUN_STATUSES:
                raise TaskStateError(f"Cannot add a node to terminal run {run_id}")
            if parent_node_id:
                self._validate_node_run(conn, parent_node_id, run_id)
            if sequence is None:
                sequence = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_nodes WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                )
            conn.execute(
                """
                INSERT INTO task_nodes(
                    id, run_id, task_id, node_key, parent_node_id, title, kind,
                    sequence, input_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    run_id,
                    run["task_id"],
                    node_key,
                    parent_node_id,
                    title,
                    kind or "step",
                    int(sequence),
                    serialize_checkpoint_state(_json_object(input_data, field="input_data")),
                    serialize_checkpoint_state(_json_object(metadata, field="metadata")),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
        return self._serialize_node(_row_dict(row) or {})

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
        return self._serialize_node(_row_dict(row)) if row else None

    def list_nodes(self, run_id: str, *, parent_node_id: str | None | object = _UNSET) -> list[dict[str, Any]]:
        params: list[Any] = [run_id]
        sql = "SELECT * FROM task_nodes WHERE run_id = ?"
        if parent_node_id is not _UNSET:
            if parent_node_id is None:
                sql += " AND parent_node_id IS NULL"
            else:
                sql += " AND parent_node_id = ?"
                params.append(parent_node_id)
        sql += " ORDER BY sequence, created_at"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._serialize_node(_row_dict(row) or {}) for row in rows]

    def transition_node(
        self,
        node_id: str,
        status: str,
        *,
        output: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in NODE_STATUSES:
            raise ValueError(f"Unknown node status: {status}")
        now = self._now()
        with self._connection(write=True) as conn:
            row = self._require_row(conn, "task_nodes", node_id, "node")
            self._assert_transition("node", node_id, row["status"], status, NODE_TRANSITIONS)
            if status == "running":
                run = self._require_row(conn, "task_runs", row["run_id"], "run")
                if run["status"] != "running":
                    raise TaskStateError(f"Cannot start a node while run {run['id']} is {run['status']}")
            merged_metadata = deserialize_checkpoint_state(row["metadata_json"]) or {}
            if metadata is not None:
                merged_metadata.update(_json_object(metadata, field="metadata"))
            values: dict[str, Any] = {
                "status": status,
                "metadata_json": serialize_checkpoint_state(merged_metadata),
                "updated_at": now,
            }
            if output is not None:
                values["output_json"] = serialize_checkpoint_state(_json_object(output, field="output"))
            if error is not None:
                values["error_json"] = serialize_checkpoint_state(_json_object(error, field="error"))
            if status == "running":
                values["started_at"] = row["started_at"] or now
            if status in TERMINAL_NODE_STATUSES:
                values["finished_at"] = now
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(
                f"UPDATE task_nodes SET {assignments} WHERE id = ?",  # noqa: S608 - fixed column names
                [*values.values(), node_id],
            )
            if status == "running":
                conn.execute(
                    "UPDATE task_runs SET current_node_id = ?, updated_at = ? WHERE id = ?",
                    (node_id, now, row["run_id"]),
                )
            elif status in TERMINAL_NODE_STATUSES:
                conn.execute(
                    """
                    UPDATE task_runs SET current_node_id = '', updated_at = ?
                    WHERE id = ? AND current_node_id = ?
                    """,
                    (now, row["run_id"], node_id),
                )
            updated = conn.execute("SELECT * FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
        return self._serialize_node(_row_dict(updated) or {})

    def start_node(self, node_id: str, *, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.transition_node(node_id, "running", metadata=metadata)

    def finish_node(
        self,
        node_id: str,
        *,
        output: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.transition_node(node_id, "completed", output=output, metadata=metadata)

    def fail_node(
        self,
        node_id: str,
        error: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.transition_node(node_id, "failed", error=error, metadata=metadata)

    def skip_node(
        self, node_id: str, *, metadata: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.transition_node(node_id, "skipped", metadata=metadata)

    def update_node_metadata(
        self, node_id: str, metadata: Mapping[str, Any], *, merge: bool = True
    ) -> dict[str, Any]:
        with self._connection(write=True) as conn:
            row = self._require_row(conn, "task_nodes", node_id, "node")
            value = deserialize_checkpoint_state(row["metadata_json"]) or {} if merge else {}
            value.update(_json_object(metadata, field="metadata"))
            conn.execute(
                "UPDATE task_nodes SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (serialize_checkpoint_state(value), self._now(), node_id),
            )
            updated = conn.execute("SELECT * FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
        return self._serialize_node(_row_dict(updated) or {})

    def update_node_definition(
        self,
        node_id: str,
        *,
        title: str | None = None,
        kind: str | None = None,
        sequence: int | None = None,
    ) -> dict[str, Any]:
        """Update display-only node definition fields without changing state."""

        values: dict[str, Any] = {"updated_at": self._now()}
        if title is not None:
            if not title.strip():
                raise ValueError("title cannot be empty")
            values["title"] = title.strip()
        if kind is not None:
            values["kind"] = kind.strip() or "step"
        if sequence is not None:
            values["sequence"] = int(sequence)
        with self._connection(write=True) as conn:
            self._require_row(conn, "task_nodes", node_id, "node")
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(
                f"UPDATE task_nodes SET {assignments} WHERE id = ?",  # noqa: S608 - fixed column names
                [*values.values(), node_id],
            )
            updated = conn.execute("SELECT * FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
        return self._serialize_node(_row_dict(updated) or {})

    def delete_node(self, node_id: str) -> bool:
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT status FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
            if row is None:
                return False
            if row["status"] == "running":
                raise TaskStateError("A running node cannot be deleted")
            conn.execute("DELETE FROM task_nodes WHERE id = ?", (node_id,))
        return True

    # -- Checkpoints ------------------------------------------------------

    def create_checkpoint(
        self,
        run_id: str,
        state: Any,
        *,
        checkpoint_id: str | None = None,
        node_id: str | None = None,
        reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        checkpoint_id = checkpoint_id or _new_id("tcp")
        state_json = serialize_checkpoint_state(state)
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if node_id:
                self._validate_node_run(conn, node_id, run_id)
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_checkpoints WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO task_checkpoints(
                    id, task_id, run_id, node_id, sequence, reason, state_json,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    run["task_id"],
                    run_id,
                    node_id,
                    sequence,
                    reason,
                    state_json,
                    serialize_checkpoint_state(_json_object(metadata, field="metadata")),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM task_checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
        return self._serialize_checkpoint(_row_dict(row) or {}, include_state=True)

    def get_checkpoint(self, checkpoint_id: str, *, include_state: bool = True) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM task_checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
        return self._serialize_checkpoint(_row_dict(row), include_state=include_state) if row else None

    def latest_checkpoint(self, run_id: str, *, include_state: bool = True) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM task_checkpoints WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._serialize_checkpoint(_row_dict(row), include_state=include_state) if row else None

    def list_checkpoints(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        include_state: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        sql = "SELECT * FROM task_checkpoints"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, sequence DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            self._serialize_checkpoint(_row_dict(row) or {}, include_state=include_state)
            for row in rows
        ]

    def restore_checkpoint(
        self,
        checkpoint_id: str,
        *,
        restore_metadata: Mapping[str, Any] | None = None,
        mark_restored: bool = True,
    ) -> dict[str, Any]:
        with self._connection(write=mark_restored) as conn:
            row = self._require_row(conn, "task_checkpoints", checkpoint_id, "checkpoint")
            if mark_restored:
                now = self._now()
                conn.execute(
                    """
                    UPDATE task_checkpoints
                    SET restored_at = ?, restore_count = restore_count + 1,
                        last_restore_metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        serialize_checkpoint_state(
                            _json_object(restore_metadata, field="restore_metadata")
                        ),
                        checkpoint_id,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM task_checkpoints WHERE id = ?", (checkpoint_id,)
                ).fetchone()
        return self._serialize_checkpoint(_row_dict(row) or {}, include_state=True)

    def delete_checkpoint(self, checkpoint_id: str) -> bool:
        with self._connection(write=True) as conn:
            cursor = conn.execute("DELETE FROM task_checkpoints WHERE id = ?", (checkpoint_id,))
        return cursor.rowcount > 0

    # -- Commands ---------------------------------------------------------

    def enqueue_command(
        self,
        task_id: str,
        command_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        priority: int = 0,
        available_at: str | datetime | None = None,
        command_id: str | None = None,
        deduplicate: bool = False,
    ) -> dict[str, Any]:
        if not task_id.strip() or not command_type.strip():
            raise ValueError("task_id and command_type cannot be empty")
        command_type = command_type.strip().lower()
        command_id = command_id or _new_id("tcmd")
        now = self._now()
        due_at = _normalise_time(available_at, now)
        with self._connection(write=True) as conn:
            if run_id:
                run = self._require_row(conn, "task_runs", run_id, "run")
                if run["task_id"] != task_id:
                    raise TaskStateError(f"Run {run_id} does not belong to task {task_id}")
            if deduplicate:
                if run_id is None:
                    duplicate = conn.execute(
                        """
                        SELECT * FROM task_commands
                        WHERE task_id = ? AND run_id IS NULL AND command_type = ?
                          AND status IN ('queued', 'claimed')
                        ORDER BY created_at LIMIT 1
                        """,
                        (task_id, command_type),
                    ).fetchone()
                else:
                    duplicate = conn.execute(
                        """
                        SELECT * FROM task_commands
                        WHERE task_id = ? AND run_id = ? AND command_type = ?
                          AND status IN ('queued', 'claimed')
                        ORDER BY created_at LIMIT 1
                        """,
                        (task_id, run_id, command_type),
                    ).fetchone()
                if duplicate is not None:
                    return self._serialize_command(_row_dict(duplicate) or {})
            conn.execute(
                """
                INSERT INTO task_commands(
                    id, task_id, run_id, command_type, payload_json, status,
                    priority, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    command_id,
                    task_id,
                    run_id,
                    command_type,
                    serialize_checkpoint_state(_json_object(payload, field="payload")),
                    int(priority),
                    due_at,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM task_commands WHERE id = ?", (command_id,)).fetchone()
        return self._serialize_command(_row_dict(row) or {})

    def request_cancel(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
        reason: str = "",
        requested_by: str = "",
    ) -> dict[str, Any]:
        return self.enqueue_command(
            task_id,
            "cancel",
            run_id=run_id,
            priority=100,
            payload={"reason": reason, "requested_by": requested_by},
            deduplicate=True,
        )

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM task_commands WHERE id = ?", (command_id,)).fetchone()
        return self._serialize_command(_row_dict(row)) if row else None

    def list_commands(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None | object = _UNSET,
        status: str | None = None,
        command_types: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in COMMAND_STATUSES:
            raise ValueError(f"Unknown command status: {status}")
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id is not _UNSET:
            if run_id is None:
                clauses.append("run_id IS NULL")
            else:
                clauses.append("run_id = ?")
                params.append(run_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        types = [item.strip().lower() for item in command_types or [] if item.strip()]
        if types:
            clauses.append("command_type IN (" + ",".join("?" for _ in types) + ")")
            params.extend(types)
        sql = "SELECT * FROM task_commands"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority DESC, created_at, id LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._serialize_command(_row_dict(row) or {}) for row in rows]

    def claim_command(
        self,
        worker_id: str,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        command_types: Iterable[str] | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        if not worker_id.strip():
            raise ValueError("worker_id cannot be empty")
        claim_time = _normalise_time(now, self._now())
        clauses = ["status = 'queued'", "available_at <= ?"]
        params: list[Any] = [claim_time]
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("(run_id IS NULL OR run_id = ?)")
            params.append(run_id)
        types = [item.strip().lower() for item in command_types or [] if item.strip()]
        if types:
            clauses.append("command_type IN (" + ",".join("?" for _ in types) + ")")
            params.extend(types)
        sql = (
            "SELECT * FROM task_commands WHERE "
            + " AND ".join(clauses)
            + " ORDER BY priority DESC, created_at, id LIMIT 1"
        )
        with self._connection(write=True) as conn:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE task_commands
                SET status = 'claimed', worker_id = ?, claimed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (worker_id, claim_time, claim_time, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM task_commands WHERE id = ?", (row["id"],)
            ).fetchone()
        return self._serialize_command(_row_dict(claimed) or {})

    def complete_command(
        self, command_id: str, *, result: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.transition_command(command_id, "completed", result=result)

    def fail_command(
        self, command_id: str, error: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.transition_command(command_id, "failed", error=error)

    def release_command(
        self, command_id: str, *, delay_seconds: float = 0
    ) -> dict[str, Any]:
        base_time = datetime.fromisoformat(_normalise_time(None, self._now()))
        available = base_time + timedelta(seconds=max(0, delay_seconds))
        return self.transition_command(command_id, "queued", available_at=available)

    def cancel_command(self, command_id: str) -> dict[str, Any]:
        return self.transition_command(command_id, "cancelled")

    def transition_command(
        self,
        command_id: str,
        status: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        available_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        if status not in COMMAND_STATUSES:
            raise ValueError(f"Unknown command status: {status}")
        now = self._now()
        with self._connection(write=True) as conn:
            row = self._require_row(conn, "task_commands", command_id, "command")
            self._assert_transition(
                "command", command_id, row["status"], status, COMMAND_TRANSITIONS
            )
            values: dict[str, Any] = {"status": status, "updated_at": now}
            if result is not None:
                values["result_json"] = serialize_checkpoint_state(
                    _json_object(result, field="result")
                )
            if error is not None:
                values["error_json"] = serialize_checkpoint_state(
                    _json_object(error, field="error")
                )
            if status == "queued":
                values.update(
                    {
                        "available_at": _normalise_time(available_at, now),
                        "worker_id": "",
                        "claimed_at": "",
                        "completed_at": "",
                    }
                )
            elif status in {"completed", "failed", "cancelled"}:
                values["completed_at"] = now
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(
                f"UPDATE task_commands SET {assignments} WHERE id = ?",  # noqa: S608 - fixed column names
                [*values.values(), command_id],
            )
            updated = conn.execute(
                "SELECT * FROM task_commands WHERE id = ?", (command_id,)
            ).fetchone()
        return self._serialize_command(_row_dict(updated) or {})

    def delete_command(self, command_id: str) -> bool:
        with self._connection(write=True) as conn:
            row = conn.execute(
                "SELECT status FROM task_commands WHERE id = ?", (command_id,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] == "claimed":
                raise TaskStateError("A claimed command cannot be deleted")
            conn.execute("DELETE FROM task_commands WHERE id = ?", (command_id,))
        return True

    def is_cancel_requested(self, task_id: str, *, run_id: str | None = None) -> bool:
        clauses = [
            "task_id = ?",
            "command_type = 'cancel'",
            "status IN ('queued', 'claimed')",
        ]
        params: list[Any] = [task_id]
        if run_id is not None:
            clauses.append("(run_id IS NULL OR run_id = ?)")
            params.append(run_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM task_commands WHERE " + " AND ".join(clauses) + " LIMIT 1",
                params,
            ).fetchone()
        return row is not None

    def raise_if_cancel_requested(self, task_id: str, *, run_id: str | None = None) -> None:
        if self.is_cancel_requested(task_id, run_id=run_id):
            raise TaskCancellationRequested(task_id, run_id)

    # -- Internal helpers -------------------------------------------------

    @staticmethod
    def _require_row(
        conn: sqlite3.Connection, table: str, entity_id: str, entity: str
    ) -> sqlite3.Row:
        if table not in {"task_runs", "task_nodes", "task_checkpoints", "task_commands"}:
            raise ValueError("Unknown task-state table")
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?",  # noqa: S608 - table allowlisted above
            (entity_id,),
        ).fetchone()
        if row is None:
            raise StateNotFoundError(f"{entity} {entity_id} was not found")
        return row

    @staticmethod
    def _assert_transition(
        entity: str,
        entity_id: str,
        old_status: str,
        new_status: str,
        transitions: Mapping[str, frozenset[str]],
    ) -> None:
        if new_status not in transitions.get(old_status, frozenset()):
            raise InvalidStateTransition(entity, entity_id, old_status, new_status)

    @staticmethod
    def _validate_node_run(conn: sqlite3.Connection, node_id: str, run_id: str) -> None:
        row = conn.execute("SELECT run_id FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            raise StateNotFoundError(f"node {node_id} was not found")
        if row["run_id"] != run_id:
            raise TaskStateError(f"Node {node_id} does not belong to run {run_id}")

    @staticmethod
    def _validate_resume_checkpoint(
        conn: sqlite3.Connection, task_id: str, checkpoint_id: str | None
    ) -> str:
        if not checkpoint_id:
            return ""
        row = conn.execute(
            "SELECT task_id FROM task_checkpoints WHERE id = ?", (checkpoint_id,)
        ).fetchone()
        if row is None:
            raise StateNotFoundError(f"checkpoint {checkpoint_id} was not found")
        if row["task_id"] != task_id:
            raise TaskStateError(
                f"Checkpoint {checkpoint_id} belongs to task {row['task_id']}, not {task_id}"
            )
        return checkpoint_id

    @staticmethod
    def _serialize_run(row: dict[str, Any] | None) -> dict[str, Any]:
        if not row:
            return {}
        return {
            **row,
            "attempt": int(row["attempt"]),
            "result": deserialize_checkpoint_state(row.get("result_json")) or {},
            "error": deserialize_checkpoint_state(row.get("error_json")) or {},
            "metadata": deserialize_checkpoint_state(row.get("metadata_json")) or {},
        }

    @staticmethod
    def _serialize_node(row: dict[str, Any] | None) -> dict[str, Any]:
        if not row:
            return {}
        return {
            **row,
            "sequence": int(row["sequence"]),
            "input": deserialize_checkpoint_state(row.get("input_json")) or {},
            "output": deserialize_checkpoint_state(row.get("output_json")) or {},
            "error": deserialize_checkpoint_state(row.get("error_json")) or {},
            "metadata": deserialize_checkpoint_state(row.get("metadata_json")) or {},
        }

    @staticmethod
    def _serialize_checkpoint(
        row: dict[str, Any] | None, *, include_state: bool
    ) -> dict[str, Any]:
        if not row:
            return {}
        result = {
            **row,
            "sequence": int(row["sequence"]),
            "restore_count": int(row["restore_count"]),
            "metadata": deserialize_checkpoint_state(row.get("metadata_json")) or {},
            "last_restore_metadata": deserialize_checkpoint_state(
                row.get("last_restore_metadata_json")
            )
            or {},
        }
        if include_state:
            result["state"] = deserialize_checkpoint_state(row.get("state_json"))
        else:
            result.pop("state_json", None)
        return result

    @staticmethod
    def _serialize_command(row: dict[str, Any] | None) -> dict[str, Any]:
        if not row:
            return {}
        return {
            **row,
            "type": row["command_type"],
            "priority": int(row["priority"]),
            "payload": deserialize_checkpoint_state(row.get("payload_json")) or {},
            "result": deserialize_checkpoint_state(row.get("result_json")) or {},
            "error": deserialize_checkpoint_state(row.get("error_json")) or {},
        }


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "COMMAND_STATUSES",
    "InvalidStateTransition",
    "NODE_STATUSES",
    "RUN_STATUSES",
    "StateNotFoundError",
    "TASK_STATE_SCHEMA_SQL",
    "TERMINAL_RUN_STATUSES",
    "TaskCancellationRequested",
    "TaskStateError",
    "TaskStateService",
    "deserialize_checkpoint_state",
    "init_schema",
    "serialize_checkpoint_state",
]
