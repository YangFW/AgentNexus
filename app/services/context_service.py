from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping, Sequence

from app import db


MEMORY_SCOPE_PRECEDENCE: tuple[str, ...] = (
    "organization",
    "workspace",
    "user",
    "agent",
    "conversation",
)
MEMORY_SCOPE_RANK = {name: index for index, name in enumerate(MEMORY_SCOPE_PRECEDENCE)}

CONTEXT_SCHEMA_SQL = """
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

CREATE INDEX IF NOT EXISTS idx_memory_effective
    ON memory_entries(organization_id, workspace_id, user_id, scope_type, scope_id, enabled);
CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_entries(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_revisions ON memory_revisions(memory_id, revision DESC);
"""

_WHITESPACE_RE = re.compile(r"\s+")
_PUBLIC_MEMORY_FIELDS = frozenset(
    {
        "id",
        "execution_scope",
        "scope_type",
        "scope_id",
        "kind",
        "title",
        "content",
        "tags",
        "source_type",
        "source_ref",
        "trust_level",
        "enabled",
        "expires_at",
        "created_by",
        "created_at",
        "updated_at",
    }
)


class ContextServiceError(RuntimeError):
    """Base error raised by the durable context and memory service."""


class MemoryNotFoundError(ContextServiceError):
    """Raised when a memory is absent or outside the caller's execution scope."""


def _clean_identifier(value: Any, *, field: str, optional: bool = False) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    result = value.strip()
    if not result and not optional:
        raise ValueError(f"{field} cannot be empty")
    if any(ord(character) < 32 for character in result):
        raise ValueError(f"{field} cannot contain control characters")
    return result


def _clean_text(value: Any, *, field: str, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    result = value.strip()
    if not allow_empty and not result:
        raise ValueError(f"{field} cannot be empty")
    return result


def _normalise_timestamp(value: str | datetime | None, *, allow_empty: bool = True) -> str:
    if value is None or value == "":
        if allow_empty:
            return ""
        raise ValueError("timestamp cannot be empty")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate and allow_empty:
            return ""
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid ISO timestamp: {value}") from exc
    else:
        raise TypeError("timestamp must be an ISO string or datetime")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _timestamp_value(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _normalise_tags(value: Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise TypeError("tags must be a sequence of strings")
    result: list[str] = []
    seen: set[str] = set()
    for raw_tag in value:
        tag = _clean_text(raw_tag, field="tag")
        if not tag:
            continue
        marker = tag.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(tag)
    return tuple(result)


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _json_list(value: str | None) -> tuple[str, ...]:
    try:
        loaded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(loaded, list):
        return ()
    return _normalise_tags([item for item in loaded if isinstance(item, str)])


@dataclass(frozen=True, slots=True)
class ExecutionScope:
    """Normalised tenant and runtime identity used for all context operations.

    Identifiers remain case-sensitive because they are platform keys.  Leading
    and trailing whitespace is removed; organization, workspace and user are
    mandatory, while agent and conversation are optional runtime refinements.
    """

    organization_id: str = "local-org"
    workspace_id: str = "default"
    user_id: str = "local-user"
    agent_id: str = ""
    conversation_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "organization_id",
            _clean_identifier(self.organization_id, field="organization_id"),
        )
        object.__setattr__(
            self,
            "workspace_id",
            _clean_identifier(self.workspace_id, field="workspace_id"),
        )
        object.__setattr__(self, "user_id", _clean_identifier(self.user_id, field="user_id"))
        object.__setattr__(
            self,
            "agent_id",
            _clean_identifier(self.agent_id, field="agent_id", optional=True),
        )
        object.__setattr__(
            self,
            "conversation_id",
            _clean_identifier(self.conversation_id, field="conversation_id", optional=True),
        )

    @classmethod
    def normalise(cls, value: ExecutionScope | Mapping[str, Any] | None = None, **overrides: Any) -> ExecutionScope:
        if value is None:
            data: dict[str, Any] = {}
        elif isinstance(value, cls):
            data = value.to_dict()
        elif isinstance(value, Mapping):
            data = dict(value)
        else:
            raise TypeError("execution scope must be an ExecutionScope or mapping")
        aliases = {
            "organization": "organization_id",
            "org_id": "organization_id",
            "workspace": "workspace_id",
            "user": "user_id",
            "agent": "agent_id",
            "conversation": "conversation_id",
        }
        for alias, canonical in aliases.items():
            if canonical not in data and alias in data:
                data[canonical] = data[alias]
        data.update({key: item for key, item in overrides.items() if item is not None})
        allowed = {
            "organization_id",
            "workspace_id",
            "user_id",
            "agent_id",
            "conversation_id",
        }
        return cls(**{key: data[key] for key in allowed if key in data})

    # American spelling is convenient for API callers while the service uses
    # the British spelling consistently internally.
    normalize = normalise

    def to_dict(self) -> dict[str, str]:
        return {
            "organization_id": self.organization_id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "conversation_id": self.conversation_id,
        }

    def id_for(self, scope_type: str) -> str:
        scope_type = _normalise_scope_type(scope_type)
        value = {
            "organization": self.organization_id,
            "workspace": self.workspace_id,
            "user": self.user_id,
            "agent": self.agent_id,
            "conversation": self.conversation_id,
        }[scope_type]
        if not value:
            raise ValueError(f"{scope_type}_id is required for {scope_type} memory")
        return value


def _normalise_scope_type(value: Any) -> str:
    scope_type = _clean_text(value, field="scope_type", allow_empty=False).lower()
    if scope_type not in MEMORY_SCOPE_RANK:
        allowed = ", ".join(MEMORY_SCOPE_PRECEDENCE)
        raise ValueError(f"scope_type must be one of: {allowed}")
    return scope_type


@dataclass(frozen=True, slots=True)
class Memory:
    """Normalised, public representation of one persisted memory."""

    id: str
    execution_scope: ExecutionScope
    scope_type: str
    scope_id: str
    kind: str
    title: str
    content: str
    tags: tuple[str, ...]
    source_type: str
    source_ref: str
    trust_level: int
    enabled: bool
    expires_at: str
    created_by: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | sqlite3.Row) -> Memory:
        data = dict(row)
        scope_type = _normalise_scope_type(data["scope_type"])
        scope_id = str(data["scope_id"])
        return cls(
            id=str(data["id"]),
            execution_scope=ExecutionScope(
                organization_id=str(data["organization_id"]),
                workspace_id=str(data["workspace_id"]),
                user_id=str(data["user_id"]),
                agent_id=scope_id if scope_type == "agent" else "",
                conversation_id=scope_id if scope_type == "conversation" else "",
            ),
            scope_type=scope_type,
            scope_id=scope_id,
            kind=str(data["kind"]),
            title=str(data["title"]),
            content=str(data["content"]),
            tags=_json_list(data.get("tags_json")),
            source_type=str(data["source_type"]),
            source_ref=str(data["source_ref"]),
            trust_level=int(data["trust_level"]),
            enabled=bool(data["enabled"]),
            expires_at=str(data["expires_at"] or ""),
            created_by=str(data["created_by"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "execution_scope": self.execution_scope.to_dict(),
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "tags": list(self.tags),
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "trust_level": self.trust_level,
            "enabled": self.enabled,
            "expires_at": self.expires_at,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    owns_connection = conn is None
    connection = conn or db.get_conn()
    try:
        connection.executescript(CONTEXT_SCHEMA_SQL)
        connection.commit()
    finally:
        if owns_connection:
            connection.close()


class ContextService:
    """CRUD, audit and effective-context resolution for layered memories.

    Scope precedence is deterministic and intentionally policy-like:
    organization > workspace > user > agent > conversation.  A conflict is two
    memories with the same non-empty ``kind`` and ``title``.  The first scope in
    that order wins; within one scope, higher trust wins, then the most recently
    updated entry.  Non-conflicting memories from every applicable scope are
    retained in the model context.
    """

    def __init__(
        self,
        connection: sqlite3.Connection | Callable[[], sqlite3.Connection] | None = None,
        *,
        clock: Callable[[], str | datetime] | None = None,
        auto_init: bool = True,
    ) -> None:
        self._shared_connection = connection if isinstance(connection, sqlite3.Connection) else None
        self._connection_factory = connection if callable(connection) else db.get_conn
        self._clock = clock or (lambda: datetime.now(timezone.utc))
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

    def init_schema(self) -> None:
        if self._shared_connection is not None:
            init_schema(self._shared_connection)
            return
        conn = self._connection_factory()
        try:
            init_schema(conn)
        finally:
            conn.close()

    def _now(self) -> str:
        return _normalise_timestamp(self._clock(), allow_empty=False)

    @staticmethod
    def _scope(value: ExecutionScope | Mapping[str, Any] | None) -> ExecutionScope:
        return ExecutionScope.normalise(value)

    @staticmethod
    def _new_id() -> str:
        return f"mem_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _serialise(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
        return Memory.from_row(row).to_dict()

    @staticmethod
    def _is_expired(memory: Mapping[str, Any], now: str) -> bool:
        expires_at = str(memory.get("expires_at") or "")
        return bool(expires_at and _timestamp_value(expires_at) <= _timestamp_value(now))

    @staticmethod
    def _visible(memory: Mapping[str, Any], scope: ExecutionScope) -> bool:
        if str(memory.get("organization_id")) != scope.organization_id:
            return False
        scope_type = str(memory.get("scope_type"))
        scope_id = str(memory.get("scope_id"))
        if scope_type == "organization":
            return scope_id == scope.organization_id
        if str(memory.get("workspace_id")) != scope.workspace_id:
            return False
        if scope_type == "workspace":
            return scope_id == scope.workspace_id
        if str(memory.get("user_id")) != scope.user_id:
            return False
        if scope_type == "user":
            return scope_id == scope.user_id
        if scope_type == "agent":
            return bool(scope.agent_id and scope_id == scope.agent_id)
        if scope_type == "conversation":
            return bool(scope.conversation_id and scope_id == scope.conversation_id)
        return False

    @staticmethod
    def _sort_key(memory: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            MEMORY_SCOPE_RANK.get(str(memory.get("scope_type")), len(MEMORY_SCOPE_RANK)),
            -int(memory.get("trust_level", 0)),
            -_timestamp_value(str(memory.get("updated_at", ""))),
            str(memory.get("id", "")),
        )

    @staticmethod
    def _conflict_key(memory: Mapping[str, Any]) -> tuple[str, str] | None:
        title = _WHITESPACE_RE.sub(" ", str(memory.get("title", "")).strip()).casefold()
        if not title:
            return None
        kind = _WHITESPACE_RE.sub(" ", str(memory.get("kind", "")).strip()).casefold()
        return kind, title

    def _insert_revision(
        self,
        conn: sqlite3.Connection,
        memory_id: str,
        *,
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any] | None,
        actor_id: str,
        reason: str,
        created_at: str,
    ) -> None:
        revision = int(
            conn.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM memory_revisions WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO memory_revisions(
                memory_id, revision, before_json, after_json, actor_id, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                revision,
                json.dumps(dict(before or {}), ensure_ascii=False, separators=(",", ":")),
                json.dumps(dict(after or {}), ensure_ascii=False, separators=(",", ":")),
                actor_id,
                reason,
                created_at,
            ),
        )

    def _find_visible_row(
        self, conn: sqlite3.Connection, memory_id: str, scope: ExecutionScope
    ) -> sqlite3.Row | None:
        row = conn.execute(
            "SELECT * FROM memory_entries WHERE id = ? AND organization_id = ?",
            (memory_id, scope.organization_id),
        ).fetchone()
        return row if row is not None and self._visible(dict(row), scope) else None

    def create_memory(
        self,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
        *,
        scope_type: str = "user",
        content: str,
        title: str = "",
        kind: str = "preference",
        tags: Sequence[str] | None = None,
        source_type: str = "user_explicit",
        source_ref: str = "",
        trust_level: int = 80,
        enabled: bool = True,
        expires_at: str | datetime | None = None,
        created_by: str | None = None,
        memory_id: str | None = None,
        reason: str = "created",
    ) -> dict[str, Any]:
        scope = self._scope(execution_scope)
        scope_type = _normalise_scope_type(scope_type)
        scope_id = scope.id_for(scope_type)
        content = _clean_text(content, field="content", allow_empty=False)
        title = _clean_text(title, field="title")
        kind = _clean_text(kind, field="kind", allow_empty=False).lower()
        normalised_tags = _normalise_tags(tags)
        source_type = _clean_text(source_type, field="source_type", allow_empty=False).lower()
        source_ref = _clean_text(source_ref, field="source_ref")
        if isinstance(trust_level, bool) or not isinstance(trust_level, int):
            raise TypeError("trust_level must be an integer")
        if not 0 <= trust_level <= 100:
            raise ValueError("trust_level must be between 0 and 100")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        expires = _normalise_timestamp(expires_at)
        actor = _clean_identifier(created_by or scope.user_id, field="created_by")
        memory_id = _clean_identifier(memory_id or self._new_id(), field="memory_id")
        reason = _clean_text(reason, field="reason")
        now = self._now()
        with self._connection(write=True) as conn:
            conn.execute(
                """
                INSERT INTO memory_entries(
                    id, organization_id, workspace_id, user_id, scope_type, scope_id,
                    kind, title, content, tags_json, source_type, source_ref, trust_level,
                    enabled, expires_at, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    scope.organization_id,
                    scope.workspace_id,
                    scope.user_id,
                    scope_type,
                    scope_id,
                    kind,
                    title,
                    content,
                    json.dumps(normalised_tags, ensure_ascii=False, separators=(",", ":")),
                    source_type,
                    source_ref,
                    trust_level,
                    int(enabled),
                    expires,
                    actor,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM memory_entries WHERE id = ?", (memory_id,)).fetchone()
            assert row is not None
            public = self._serialise(row)
            self._insert_revision(
                conn,
                memory_id,
                before=None,
                after=public,
                actor_id=actor,
                reason=reason or "created",
                created_at=now,
            )
        return public

    # Concise aliases are useful for REST adapters without weakening the more
    # descriptive service API.
    create = create_memory

    def list_memories(
        self,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
        *,
        scope_type: str | None = None,
        include_disabled: bool = True,
        include_expired: bool = True,
        effective_only: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        scope = self._scope(execution_scope)
        selected_scope_type = _normalise_scope_type(scope_type) if scope_type else None
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        now = self._now()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_entries WHERE organization_id = ?",
                (scope.organization_id,),
            ).fetchall()
        memories: list[dict[str, Any]] = []
        for row in rows:
            raw = dict(row)
            if not self._visible(raw, scope):
                continue
            public = self._serialise(raw)
            if selected_scope_type and public["scope_type"] != selected_scope_type:
                continue
            if not include_disabled and not public["enabled"]:
                continue
            if not include_expired and self._is_expired(public, now):
                continue
            memories.append(public)
        memories.sort(key=self._sort_key)
        if effective_only:
            memories = self._resolve_conflicts(memories)
        return memories[:limit]

    list = list_memories

    def get_memory(
        self,
        memory_id: str,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        memory_id = _clean_identifier(memory_id, field="memory_id")
        scope = self._scope(execution_scope)
        with self._connection() as conn:
            row = self._find_visible_row(conn, memory_id, scope)
        return self._serialise(row) if row is not None else None

    get = get_memory

    def update_memory(
        self,
        memory_id: str,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
        *,
        actor_id: str | None = None,
        reason: str = "updated",
        **changes: Any,
    ) -> dict[str, Any]:
        memory_id = _clean_identifier(memory_id, field="memory_id")
        scope = self._scope(execution_scope)
        actor = _clean_identifier(actor_id or scope.user_id, field="actor_id")
        reason = _clean_text(reason, field="reason") or "updated"
        allowed = {
            "kind",
            "title",
            "content",
            "tags",
            "source_type",
            "source_ref",
            "trust_level",
            "enabled",
            "expires_at",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValueError(f"Fields cannot be updated: {', '.join(unknown)}")
        with self._connection(write=True) as conn:
            row = self._find_visible_row(conn, memory_id, scope)
            if row is None:
                raise MemoryNotFoundError(f"Memory {memory_id} was not found")
            before = self._serialise(row)
            current = dict(before)
            if "kind" in changes:
                current["kind"] = _clean_text(changes["kind"], field="kind", allow_empty=False).lower()
            if "title" in changes:
                current["title"] = _clean_text(changes["title"], field="title")
            if "content" in changes:
                current["content"] = _clean_text(changes["content"], field="content", allow_empty=False)
            if "tags" in changes:
                current["tags"] = list(_normalise_tags(changes["tags"]))
            if "source_type" in changes:
                current["source_type"] = _clean_text(
                    changes["source_type"], field="source_type", allow_empty=False
                ).lower()
            if "source_ref" in changes:
                current["source_ref"] = _clean_text(changes["source_ref"], field="source_ref")
            if "trust_level" in changes:
                trust = changes["trust_level"]
                if isinstance(trust, bool) or not isinstance(trust, int):
                    raise TypeError("trust_level must be an integer")
                if not 0 <= trust <= 100:
                    raise ValueError("trust_level must be between 0 and 100")
                current["trust_level"] = trust
            if "enabled" in changes:
                if not isinstance(changes["enabled"], bool):
                    raise TypeError("enabled must be a boolean")
                current["enabled"] = changes["enabled"]
            if "expires_at" in changes:
                current["expires_at"] = _normalise_timestamp(changes["expires_at"])
            comparable_before = {key: before[key] for key in allowed}
            comparable_after = {key: current[key] for key in allowed}
            if comparable_before == comparable_after:
                return before
            now = self._now()
            conn.execute(
                """
                UPDATE memory_entries
                SET kind = ?, title = ?, content = ?, tags_json = ?, source_type = ?,
                    source_ref = ?, trust_level = ?, enabled = ?, expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    current["kind"],
                    current["title"],
                    current["content"],
                    json.dumps(current["tags"], ensure_ascii=False, separators=(",", ":")),
                    current["source_type"],
                    current["source_ref"],
                    current["trust_level"],
                    int(current["enabled"]),
                    current["expires_at"],
                    now,
                    memory_id,
                ),
            )
            updated_row = conn.execute("SELECT * FROM memory_entries WHERE id = ?", (memory_id,)).fetchone()
            assert updated_row is not None
            after = self._serialise(updated_row)
            self._insert_revision(
                conn,
                memory_id,
                before=before,
                after=after,
                actor_id=actor,
                reason=reason,
                created_at=now,
            )
        return after

    update = update_memory

    def set_memory_enabled(
        self,
        memory_id: str,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
        enabled: bool,
        *,
        actor_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        return self.update_memory(
            memory_id,
            execution_scope,
            enabled=enabled,
            actor_id=actor_id,
            reason=reason or ("enabled" if enabled else "disabled"),
        )

    def enable_memory(
        self,
        memory_id: str,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
        *,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        return self.set_memory_enabled(memory_id, execution_scope, True, actor_id=actor_id)

    def disable_memory(
        self,
        memory_id: str,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
        *,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        return self.set_memory_enabled(memory_id, execution_scope, False, actor_id=actor_id)

    enable = enable_memory
    disable = disable_memory

    def delete_memory(
        self,
        memory_id: str,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
        *,
        actor_id: str | None = None,
        reason: str = "deleted",
    ) -> dict[str, Any]:
        memory_id = _clean_identifier(memory_id, field="memory_id")
        scope = self._scope(execution_scope)
        actor = _clean_identifier(actor_id or scope.user_id, field="actor_id")
        reason = _clean_text(reason, field="reason") or "deleted"
        now = self._now()
        with self._connection(write=True) as conn:
            row = self._find_visible_row(conn, memory_id, scope)
            if row is None:
                raise MemoryNotFoundError(f"Memory {memory_id} was not found")
            before = self._serialise(row)
            self._insert_revision(
                conn,
                memory_id,
                before=before,
                after=None,
                actor_id=actor,
                reason=reason,
                created_at=now,
            )
            conn.execute("DELETE FROM memory_entries WHERE id = ?", (memory_id,))
        return {"id": memory_id, "deleted": True}

    delete = delete_memory

    @staticmethod
    def _sanitise_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value[key] for key in _PUBLIC_MEMORY_FIELDS if key in value}

    def _revision_is_visible(
        self, rows: Sequence[sqlite3.Row], scope: ExecutionScope
    ) -> bool:
        for row in rows:
            before = _json_object(row["before_json"])
            after = _json_object(row["after_json"])
            snapshot = after or before
            execution = snapshot.get("execution_scope")
            if not isinstance(execution, Mapping):
                continue
            raw = {
                "organization_id": execution.get("organization_id", ""),
                "workspace_id": execution.get("workspace_id", ""),
                "user_id": execution.get("user_id", ""),
                "scope_type": snapshot.get("scope_type", ""),
                "scope_id": snapshot.get("scope_id", ""),
            }
            return self._visible(raw, scope)
        return False

    def list_revisions(
        self,
        memory_id: str,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        memory_id = _clean_identifier(memory_id, field="memory_id")
        scope = self._scope(execution_scope)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_revisions WHERE memory_id = ? ORDER BY revision",
                (memory_id,),
            ).fetchall()
            current = self._find_visible_row(conn, memory_id, scope)
        if not rows or (current is None and not self._revision_is_visible(rows, scope)):
            return []
        return [
            {
                "id": int(row["id"]),
                "memory_id": str(row["memory_id"]),
                "revision": int(row["revision"]),
                "before": self._sanitise_snapshot(_json_object(row["before_json"])),
                "after": self._sanitise_snapshot(_json_object(row["after_json"])),
                "actor_id": str(row["actor_id"]),
                "reason": str(row["reason"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def _resolve_conflicts(self, memories: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for memory in sorted((dict(item) for item in memories), key=self._sort_key):
            key = self._conflict_key(memory)
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            result.append(memory)
        return result

    def get_effective_context(
        self,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        memories = self.list_memories(
            execution_scope,
            include_disabled=False,
            include_expired=False,
            effective_only=True,
            limit=limit,
        )
        labels = {
            "organization": "组织规则",
            "workspace": "工作区规则",
            "user": "用户偏好",
            "agent": "Agent 记忆",
            "conversation": "本次对话",
        }
        blocks: list[str] = []
        for memory in memories:
            heading = labels[memory["scope_type"]]
            if memory["title"]:
                heading += f" · {memory['title']}"
            blocks.append(f"[{heading}]\n{memory['content']}")
        return {
            "effective_context": "\n\n".join(blocks),
            "used_memory_ids": [memory["id"] for memory in memories],
            "memories": memories,
        }

    effective_context = get_effective_context

    def remember(
        self,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
        content: str,
        *,
        scope_type: str = "user",
        **metadata: Any,
    ) -> dict[str, Any]:
        metadata.setdefault("source_type", "user_explicit")
        metadata.setdefault("reason", "explicit_remember")
        return self.create_memory(
            execution_scope,
            scope_type=scope_type,
            content=content,
            **metadata,
        )

    def forget(
        self,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
        memory_id: str | None = None,
        *,
        query: str | None = None,
        scope_type: str | None = None,
        actor_id: str | None = None,
    ) -> list[str]:
        """Explicitly delete a memory by id or an exact, scoped user query.

        Query deletion intentionally uses exact case-insensitive title/content
        matching.  It never performs a fuzzy or cross-scope bulk delete.
        """

        scope = self._scope(execution_scope)
        if memory_id:
            deleted = self.delete_memory(
                memory_id,
                scope,
                actor_id=actor_id,
                reason="explicit_forget",
            )
            return [deleted["id"]]
        if not query:
            raise ValueError("memory_id or query is required")
        needle = _WHITESPACE_RE.sub(" ", _clean_text(query, field="query", allow_empty=False)).casefold()
        candidates = self.list_memories(
            scope,
            scope_type=scope_type,
            include_disabled=True,
            include_expired=True,
        )
        matches = [
            item
            for item in candidates
            if needle
            in {
                _WHITESPACE_RE.sub(" ", item["title"].strip()).casefold(),
                _WHITESPACE_RE.sub(" ", item["content"].strip()).casefold(),
            }
        ]
        deleted_ids: list[str] = []
        for item in matches:
            self.delete_memory(
                item["id"],
                scope,
                actor_id=actor_id,
                reason="explicit_forget",
            )
            deleted_ids.append(item["id"])
        return deleted_ids


__all__ = [
    "CONTEXT_SCHEMA_SQL",
    "ContextService",
    "ContextServiceError",
    "ExecutionScope",
    "MEMORY_SCOPE_PRECEDENCE",
    "Memory",
    "MemoryNotFoundError",
    "init_schema",
]
