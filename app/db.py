from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("APP_DB_PATH", str(DATA_DIR / "platform.db")))
_LOCK = threading.RLock()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _shared_scope_schema(conn: sqlite3.Connection) -> None:
    """Create the shared scope, memory, expert and artifact metadata tables."""
    for column, definition in (
        ("organization_id", "TEXT NOT NULL DEFAULT 'local-org'"),
        ("user_id", "TEXT NOT NULL DEFAULT 'local-user'"),
        ("parent_task_id", "TEXT NOT NULL DEFAULT ''"),
        ("executor_type", "TEXT NOT NULL DEFAULT 'agent'"),
        ("executor_id", "TEXT NOT NULL DEFAULT ''"),
    ):
        _ensure_column(conn, "tasks", column, definition)
    for column, definition in (
        ("run_id", "TEXT NOT NULL DEFAULT ''"),
        ("workspace_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("relative_path", "TEXT NOT NULL DEFAULT ''"),
        ("mime_type", "TEXT NOT NULL DEFAULT 'application/octet-stream'"),
        ("size", "INTEGER NOT NULL DEFAULT 0"),
        ("sha256", "TEXT NOT NULL DEFAULT ''"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
        ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        _ensure_column(conn, "artifacts", column, definition)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_entries (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL DEFAULT 'local-org',
            workspace_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL DEFAULT 'local-user',
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'preference',
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_type TEXT NOT NULL DEFAULT 'user_explicit',
            source_ref TEXT NOT NULL DEFAULT '',
            trust_level INTEGER NOT NULL DEFAULT 80,
            enabled INTEGER NOT NULL DEFAULT 1,
            expires_at TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT 'local-user',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            actor_id TEXT NOT NULL DEFAULT 'local-user',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL DEFAULT 'local-org',
            workspace_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL DEFAULT 'local-user',
            through_task_id TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            preserved_constraints_json TEXT NOT NULL DEFAULT '[]',
            model_id TEXT NOT NULL DEFAULT '',
            token_count INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS expert_templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            version TEXT NOT NULL DEFAULT '0.1.0',
            source TEXT NOT NULL DEFAULT 'local',
            manifest_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS expert_installations (
            id TEXT PRIMARY KEY,
            template_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            installed_version TEXT NOT NULL,
            installed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_teams (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            supervisor_agent_id TEXT NOT NULL,
            aggregation_prompt TEXT NOT NULL DEFAULT '',
            acceptance_json TEXT NOT NULL DEFAULT '[]',
            budget_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_team_members (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            execution_mode TEXT NOT NULL DEFAULT 'parallel',
            depends_on_json TEXT NOT NULL DEFAULT '[]',
            member_prompt TEXT NOT NULL DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS team_runs (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            parent_task_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            result_json TEXT NOT NULL DEFAULT '{}',
            error_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS team_member_runs (
            id TEXT PRIMARY KEY,
            team_run_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            child_task_id TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'queued',
            output_json TEXT NOT NULL DEFAULT '{}',
            error_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS artifact_previews (
            artifact_id TEXT PRIMARY KEY,
            renderer TEXT NOT NULL DEFAULT '',
            renderer_version TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            preview_kind TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_memory_effective
            ON memory_entries(organization_id, workspace_id, user_id, scope_type, scope_id, enabled);
        CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_entries(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_revisions ON memory_revisions(memory_id, revision DESC);
        CREATE INDEX IF NOT EXISTS idx_team_members ON agent_team_members(team_id, position);
        CREATE INDEX IF NOT EXISTS idx_team_runs_parent ON team_runs(parent_task_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_team_member_runs ON team_member_runs(team_run_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_artifacts_task_run ON artifacts(task_id, run_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_scope ON tasks(organization_id, user_id, workspace, created_at DESC);
        """
    )


def _expert_team_scope_schema(conn: sqlite3.Connection) -> None:
    """Add durable ownership and permission metadata for expert teams.

    Expert templates ultimately create regular ``agents``.  Keeping the scope
    on both the installation and the created agent makes authorization checks
    possible without trusting a client supplied installation id.  The team
    run tables also retain the execution scope so audits remain meaningful if
    a template or team is edited later.
    """
    for column, definition in (
        ("organization_id", "TEXT NOT NULL DEFAULT 'local-org'"),
        ("workspace_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("owner_user_id", "TEXT NOT NULL DEFAULT 'local-user'"),
        ("visibility", "TEXT NOT NULL DEFAULT 'organization'"),
        ("permissions_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("enabled", "INTEGER NOT NULL DEFAULT 1"),
    ):
        _ensure_column(conn, "expert_templates", column, definition)
    for column, definition in (
        ("organization_id", "TEXT NOT NULL DEFAULT 'local-org'"),
        ("workspace_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("user_id", "TEXT NOT NULL DEFAULT 'local-user'"),
        ("permissions_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("enabled", "INTEGER NOT NULL DEFAULT 1"),
    ):
        _ensure_column(conn, "expert_installations", column, definition)
    for column, definition in (
        ("organization_id", "TEXT NOT NULL DEFAULT 'local-org'"),
        ("workspace_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("owner_user_id", "TEXT NOT NULL DEFAULT 'local-user'"),
        ("visibility", "TEXT NOT NULL DEFAULT 'organization'"),
        ("permissions_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        _ensure_column(conn, "agent_teams", column, definition)
    _ensure_column(conn, "agent_team_members", "permissions_json", "TEXT NOT NULL DEFAULT '{}'")
    for column, definition in (
        ("organization_id", "TEXT NOT NULL DEFAULT 'local-org'"),
        ("workspace_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("user_id", "TEXT NOT NULL DEFAULT 'local-user'"),
        ("parent_run_id", "TEXT NOT NULL DEFAULT ''"),
        ("supervisor_child_task_id", "TEXT NOT NULL DEFAULT ''"),
        ("aggregation_attempt", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _ensure_column(conn, "team_runs", column, definition)
    for column, definition in (
        ("conversation_id", "TEXT NOT NULL DEFAULT ''"),
        ("permissions_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("input_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        _ensure_column(conn, "team_member_runs", column, definition)
    for column, definition in (
        ("organization_id", "TEXT NOT NULL DEFAULT 'local-org'"),
        ("workspace_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("owner_user_id", "TEXT NOT NULL DEFAULT 'local-user'"),
        ("visibility", "TEXT NOT NULL DEFAULT 'organization'"),
        ("expert_installation_id", "TEXT NOT NULL DEFAULT ''"),
    ):
        _ensure_column(conn, "agents", column, definition)
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_expert_templates_scope
            ON expert_templates(organization_id, workspace_id, owner_user_id, visibility, enabled);
        CREATE INDEX IF NOT EXISTS idx_expert_installations_scope
            ON expert_installations(organization_id, workspace_id, user_id, enabled);
        CREATE INDEX IF NOT EXISTS idx_agent_teams_scope
            ON agent_teams(organization_id, workspace_id, owner_user_id, visibility, enabled);
        CREATE INDEX IF NOT EXISTS idx_team_runs_scope
            ON team_runs(organization_id, workspace_id, user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agents_scope
            ON agents(organization_id, workspace_id, owner_user_id, visibility);
        """
    )


def _backfill_legacy_artifact_metadata(conn: sqlite3.Connection) -> None:
    """Populate public metadata for files created before immutable artifacts.

    Old rows stored an absolute path only.  The migration never follows a file
    outside the configured artifact root and skips missing/unsafe rows instead
    of making startup fail because a historic download was manually removed.
    """
    root = Path(os.getenv("APP_ARTIFACT_DIR", str(BASE_DIR / "data" / "artifacts")))
    try:
        resolved_root = root.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return
    rows = conn.execute(
        """SELECT id, task_id, name, path, relative_path, size, sha256,
                  mime_type, workspace_id, run_id, created_at
           FROM artifacts ORDER BY task_id, name, created_at, id"""
    ).fetchall()
    versions: dict[tuple[str, str], int] = {}
    for raw_row in rows:
        row = dict(raw_row)
        key = (str(row.get("task_id") or ""), str(row.get("name") or ""))
        versions[key] = versions.get(key, 0) + 1
        try:
            candidate = Path(str(row.get("path") or "")).resolve(strict=True)
            relative = candidate.relative_to(resolved_root).as_posix()
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            continue
        if not candidate.is_file():
            continue
        digest = hashlib.sha256()
        try:
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            size = candidate.stat().st_size
        except OSError:
            continue
        task = conn.execute(
            "SELECT workspace FROM tasks WHERE id = ?", (row.get("task_id") or "",)
        ).fetchone()
        run = conn.execute(
            """SELECT id FROM task_runs WHERE task_id = ?
               ORDER BY attempt DESC, created_at DESC LIMIT 1""",
            (row.get("task_id") or "",),
        ).fetchone()
        mime_type = mimetypes.guess_type(str(row.get("name") or candidate.name))[0] or "application/octet-stream"
        conn.execute(
            """UPDATE artifacts
               SET relative_path = ?, size = ?, sha256 = ?, mime_type = ?,
                   workspace_id = ?, run_id = ?, version = ?
               WHERE id = ?""",
            (
                relative,
                size,
                digest.hexdigest(),
                mime_type,
                str(task[0] if task else row.get("workspace_id") or "default"),
                str(run[0] if run else row.get("run_id") or ""),
                versions[key],
                row["id"],
            ),
        )


def _automation_schema(conn: sqlite3.Connection) -> None:
    """Add durable triggers, retries, scoped state and audit records to loops."""
    for column, definition in (
        ("trigger_type", "TEXT NOT NULL DEFAULT 'interval'"),
        ("cron_expression", "TEXT NOT NULL DEFAULT ''"),
        ("once_at", "TEXT NOT NULL DEFAULT ''"),
        ("organization_id", "TEXT NOT NULL DEFAULT 'local-org'"),
        ("workspace_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("user_id", "TEXT NOT NULL DEFAULT 'local-user'"),
        ("webhook_secret_ciphertext", "TEXT NOT NULL DEFAULT ''"),
        ("webhook_tolerance_seconds", "INTEGER NOT NULL DEFAULT 300"),
        ("max_attempts", "INTEGER NOT NULL DEFAULT 1"),
        ("retry_backoff_seconds", "INTEGER NOT NULL DEFAULT 0"),
        ("state_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("last_diff_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        _ensure_column(conn, "loops", column, definition)
    for column, definition in (
        ("attempt", "INTEGER NOT NULL DEFAULT 1"),
        ("trigger_event_id", "TEXT NOT NULL DEFAULT ''"),
        ("input_state_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("output_state_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("diff_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("error_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        _ensure_column(conn, "loop_runs", column, definition)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS automation_trigger_events (
            id TEXT PRIMARY KEY,
            loop_id TEXT NOT NULL,
            organization_id TEXT NOT NULL DEFAULT 'local-org',
            workspace_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL DEFAULT 'local-user',
            trigger_type TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL DEFAULT '',
            payload_ciphertext TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'accepted',
            run_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            UNIQUE(loop_id, idempotency_key)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL DEFAULT 'local-org',
            workspace_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL DEFAULT 'local-user',
            kind TEXT NOT NULL DEFAULT 'automation',
            title TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            data_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'unread',
            entity_type TEXT NOT NULL DEFAULT '',
            entity_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            read_at TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_loops_scope_trigger
            ON loops(organization_id, workspace_id, user_id, trigger_type, status, next_run_at);
        CREATE INDEX IF NOT EXISTS idx_loop_runs_attempt
            ON loop_runs(loop_id, run_number DESC, attempt DESC);
        CREATE INDEX IF NOT EXISTS idx_automation_trigger_queue
            ON automation_trigger_events(status, received_at);
        CREATE INDEX IF NOT EXISTS idx_automation_trigger_loop
            ON automation_trigger_events(loop_id, received_at DESC);
        CREATE INDEX IF NOT EXISTS idx_notifications_scope
            ON notifications(organization_id, workspace_id, user_id, status, created_at DESC);
        """
    )


def _rename_legacy_recommended_skill_category(conn: sqlite3.Connection) -> None:
    """Rename an old internal category without changing user-authored skills."""

    old_content_digest = (
        "c689129b192949626aafbc1a44701978c60dc75546e1dbed7ee9a5abc935311f"
    )
    row = conn.execute(
        "SELECT id, category, content FROM skills WHERE id = ?",
        ("mermaid_diagram",),
    ).fetchone()
    if not row or row[1] != "marketplace":
        return
    if hashlib.sha256(str(row[2] or "").encode("utf-8")).hexdigest() != old_content_digest:
        return
    updated_content = str(row[2]).replace(
        "category: marketplace", "category: recommended", 1
    )
    now = utc_now()
    conn.execute(
        "UPDATE skills SET category = 'recommended', content = ?, updated_at = ? WHERE id = ?",
        (updated_content, now, row[0]),
    )
    entry = conn.execute(
        "SELECT content FROM skill_files WHERE skill_id = ? AND path = 'SKILL.md'",
        (row[0],),
    ).fetchone()
    if entry and bytes(entry[0]).decode("utf-8", errors="replace") == str(row[2]):
        raw = updated_content.encode("utf-8")
        conn.execute(
            "UPDATE skill_files SET content = ?, size = ?, updated_at = ? "
            "WHERE skill_id = ? AND path = 'SKILL.md'",
            (raw, len(raw), now, row[0]),
        )


SCHEMA_MIGRATIONS: tuple[tuple[int, Any], ...] = (
    (1, _shared_scope_schema),
    (2, _expert_team_scope_schema),
    (3, _backfill_legacy_artifact_metadata),
    (4, _automation_schema),
    (6, _rename_legacy_recommended_skill_category),
)

# Keep retired version numbers reserved so existing databases do not
# reinterpret them as new migrations.
_RETIRED_SCHEMA_MIGRATIONS = frozenset({5})


def _apply_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        int(row[0])
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    } | _RETIRED_SCHEMA_MIGRATIONS
    for version, migration in SCHEMA_MIGRATIONS:
        if version in applied:
            continue
        migration(conn)
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (version, migration.__name__, utc_now()),
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with closing(get_conn()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    version TEXT NOT NULL DEFAULT '0.1.0',
                    content TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    required_mcps TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS skill_files (
                    skill_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content BLOB NOT NULL,
                    content_type TEXT NOT NULL DEFAULT 'text/plain',
                    is_binary INTEGER NOT NULL DEFAULT 0,
                    size INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (skill_id, path)
                );

                CREATE TABLE IF NOT EXISTS mcp_servers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'builtin',
                    description TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    tools_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT 'deterministic',
                    system_prompt TEXT NOT NULL DEFAULT '',
                    skills_json TEXT NOT NULL DEFAULT '[]',
                    mcp_servers_json TEXT NOT NULL DEFAULT '[]',
                    permissions_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    model_id TEXT NOT NULL DEFAULT '',
                    conversation_id TEXT NOT NULL DEFAULT '',
                    workspace TEXT NOT NULL DEFAULT 'default',
                    status TEXT NOT NULL DEFAULT 'queued',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                    size INTEGER NOT NULL DEFAULT 0,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_configs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    base_url TEXT NOT NULL DEFAULT '',
                    api_key_env TEXT NOT NULL DEFAULT '',
                    api_key_ciphertext TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS loops (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    agent_id TEXT NOT NULL DEFAULT 'general-agent',
                    model_id TEXT NOT NULL DEFAULT 'deterministic',
                    interval_seconds INTEGER NOT NULL DEFAULT 3600,
                    status TEXT NOT NULL DEFAULT 'paused',
                    max_runs INTEGER NOT NULL DEFAULT 10,
                    run_count INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    max_failures INTEGER NOT NULL DEFAULT 3,
                    next_run_at TEXT NOT NULL DEFAULT '',
                    last_run_at TEXT NOT NULL DEFAULT '',
                    last_task_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS loop_runs (
                    id TEXT PRIMARY KEY,
                    loop_id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    run_number INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    decision_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS policy_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    event TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'organization',
                    scope_id TEXT NOT NULL DEFAULT '',
                    priority INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    rule_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "attachments_json" not in task_columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
            if "model_id" not in task_columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN model_id TEXT NOT NULL DEFAULT ''")
            if "conversation_id" not in task_columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN conversation_id TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_conversation_created ON tasks(conversation_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_files_skill ON skill_files(skill_id, path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_loops_due ON loops(status, next_run_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_runs_loop ON loop_runs(loop_id, run_number DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_policy_rules_event_scope ON policy_rules(event, scope, priority DESC)")
            model_columns = {row[1] for row in conn.execute("PRAGMA table_info(model_configs)").fetchall()}
            if "api_key_ciphertext" not in model_columns:
                conn.execute("ALTER TABLE model_configs ADD COLUMN api_key_ciphertext TEXT NOT NULL DEFAULT ''")
            _apply_schema_migrations(conn)
            conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def query_all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with _LOCK:
        with closing(get_conn()) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [row_to_dict(r) for r in rows if r is not None]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    with _LOCK:
        with closing(get_conn()) as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
            return row_to_dict(row)


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with _LOCK:
        with closing(get_conn()) as conn:
            conn.execute(sql, tuple(params))
            conn.commit()


def execute_returning_id(sql: str, params: Iterable[Any] = ()) -> int:
    with _LOCK:
        with closing(get_conn()) as conn:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return int(cur.lastrowid)


def insert_event(task_id: str, event_type: str, title: str, content: str = "", data: Any | None = None) -> int:
    return execute_returning_id(
        """
        INSERT INTO task_events(task_id, ts, type, title, content, data_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (task_id, utc_now(), event_type, title, content, json_dumps(data or {})),
    )


def update_task_status(task_id: str, status: str, result: dict[str, Any] | None = None, artifacts: list[dict[str, Any]] | None = None) -> None:
    current = query_one("SELECT result_json, artifacts_json FROM tasks WHERE id = ?", (task_id,))
    if not current:
        return
    result_json = json_dumps(result) if result is not None else current["result_json"]
    artifacts_json = json_dumps(artifacts) if artifacts is not None else current["artifacts_json"]
    execute(
        "UPDATE tasks SET status = ?, result_json = ?, artifacts_json = ?, updated_at = ? WHERE id = ?",
        (status, result_json, artifacts_json, utc_now(), task_id),
    )
