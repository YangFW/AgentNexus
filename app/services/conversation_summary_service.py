from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, Sequence

from app import db
from app.services.context_service import CONTEXT_SCHEMA_SQL, ExecutionScope


class ConversationSummaryError(RuntimeError):
    """Base error for durable conversation-summary operations."""


class ConversationSummaryNotFoundError(ConversationSummaryError):
    """The summary is absent or belongs to another execution scope."""


class ConversationSummaryConflictError(ConversationSummaryError):
    """A globally unique conversation id is already owned by another scope."""


_CONSTRAINT_RE = re.compile(
    r"(?:必须|不要|不得|不能|需要|要求|默认|只(?:能|要)|输出|格式|语言|截止|优先|"
    r"must\b|do not\b|don't\b|never\b|required\b|default\b|format\b|language\b)",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(r"\s+")


class ConversationSummaryService:
    """Stores scope-bound summaries and compacts older dialogue deterministically.

    Conversation ids are generated as globally unique ids by the web client.  A
    second scope therefore cannot reuse an existing id; refusing that collision
    is safer than ever returning another user's summary.
    """

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection] = db.get_conn,
        *,
        auto_init: bool = True,
    ) -> None:
        self._connection_factory = connection_factory
        if auto_init:
            with self._connection() as conn:
                conn.executescript(CONTEXT_SCHEMA_SQL)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connection_factory()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _scope(value: ExecutionScope | Mapping[str, Any]) -> ExecutionScope:
        return ExecutionScope.normalise(value)

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "conversation_id": row["conversation_id"],
            "organization_id": row["organization_id"],
            "workspace_id": row["workspace_id"],
            "user_id": row["user_id"],
            "through_task_id": row.get("through_task_id", ""),
            "summary": row.get("summary", ""),
            "preserved_constraints": db.json_loads(row.get("preserved_constraints_json"), []),
            "model_id": row.get("model_id", ""),
            "token_count": int(row.get("token_count") or 0),
            "version": int(row.get("version") or 1),
            "created_at": row.get("created_at", ""),
            "updated_at": row.get("updated_at", ""),
        }

    @staticmethod
    def _owned(row: Mapping[str, Any], scope: ExecutionScope) -> bool:
        return all(
            str(row.get(field) or "") == getattr(scope, field)
            for field in ("organization_id", "workspace_id", "user_id")
        )

    def get(
        self,
        scope: ExecutionScope | Mapping[str, Any],
        conversation_id: str | None = None,
    ) -> dict[str, Any] | None:
        effective = self._scope(scope)
        target = (conversation_id or effective.conversation_id).strip()
        if not target:
            raise ValueError("conversation_id cannot be empty")
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                (target,),
            ).fetchone()
        if not row:
            return None
        record = dict(row)
        return self._public(record) if self._owned(record, effective) else None

    def list(
        self,
        scope: ExecutionScope | Mapping[str, Any],
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        effective = self._scope(scope)
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT * FROM conversation_summaries
                   WHERE organization_id = ? AND workspace_id = ? AND user_id = ?
                   ORDER BY updated_at DESC LIMIT ?""",
                (
                    effective.organization_id,
                    effective.workspace_id,
                    effective.user_id,
                    max(1, min(int(limit), 1000)),
                ),
            ).fetchall()
        return [self._public(dict(row)) for row in rows]

    def upsert(
        self,
        scope: ExecutionScope | Mapping[str, Any],
        *,
        conversation_id: str | None = None,
        summary: str,
        preserved_constraints: Sequence[str] = (),
        through_task_id: str = "",
        model_id: str = "",
        token_count: int | None = None,
    ) -> dict[str, Any]:
        effective = self._scope(scope)
        target = (conversation_id or effective.conversation_id).strip()
        cleaned_summary = summary.strip()
        if not target:
            raise ValueError("conversation_id cannot be empty")
        if not cleaned_summary:
            raise ValueError("summary cannot be empty")
        constraints = self._dedupe_constraints(preserved_constraints)
        now = db.utc_now()
        with self._connection() as conn:
            existing_row = conn.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                (target,),
            ).fetchone()
            existing = dict(existing_row) if existing_row else None
            if existing and not self._owned(existing, effective):
                raise ConversationSummaryConflictError(
                    "conversation_id already belongs to another execution scope"
                )
            version = int(existing.get("version") or 0) + 1 if existing else 1
            created_at = str(existing.get("created_at") or now) if existing else now
            count = max(0, int(token_count if token_count is not None else len(cleaned_summary) / 4))
            conn.execute(
                """INSERT OR REPLACE INTO conversation_summaries(
                       conversation_id, organization_id, workspace_id, user_id,
                       through_task_id, summary, preserved_constraints_json,
                       model_id, token_count, version, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    target,
                    effective.organization_id,
                    effective.workspace_id,
                    effective.user_id,
                    through_task_id.strip(),
                    cleaned_summary,
                    db.json_dumps(constraints),
                    model_id.strip(),
                    count,
                    version,
                    created_at,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                (target,),
            ).fetchone()
        return self._public(dict(row))

    def delete(
        self,
        scope: ExecutionScope | Mapping[str, Any],
        conversation_id: str | None = None,
    ) -> bool:
        effective = self._scope(scope)
        target = (conversation_id or effective.conversation_id).strip()
        if not target:
            raise ValueError("conversation_id cannot be empty")
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                (target,),
            ).fetchone()
            if not row or not self._owned(dict(row), effective):
                return False
            conn.execute(
                "DELETE FROM conversation_summaries WHERE conversation_id = ?",
                (target,),
            )
        return True

    def compact(
        self,
        scope: ExecutionScope | Mapping[str, Any],
        messages: Sequence[Mapping[str, Any]],
        *,
        conversation_id: str | None = None,
        through_task_id: str = "",
        model_id: str = "deterministic-compactor",
        max_summary_chars: int = 6000,
    ) -> dict[str, Any]:
        """Compact completed older turns while preserving explicit constraints.

        The compactor extracts facts without asking a model to invent a
        narrative. Stored constraints remain independently inspectable.
        """
        normalised: list[tuple[str, str]] = []
        constraints: list[str] = []
        for item in messages:
            role = "assistant" if str(item.get("role") or "") == "assistant" else "user"
            content = _SPACE_RE.sub(" ", str(item.get("content") or "")).strip()
            if not content:
                continue
            normalised.append((role, content))
            if role == "user" and _CONSTRAINT_RE.search(content):
                constraints.append(content[:500])
        if not normalised:
            raise ValueError("messages cannot be empty")
        lines = [
            f"{'用户' if role == 'user' else '助手'}：{content}"
            for role, content in normalised
        ]
        summary = "\n".join(lines)
        limit = max(500, min(int(max_summary_chars), 50_000))
        if len(summary) > limit:
            marker = "\n…（中间较早内容已压缩）\n"
            available = limit - len(marker)
            head = max(1, int(available * 0.55))
            summary = summary[:head].rstrip() + marker + summary[-(available - head):].lstrip()
        return self.upsert(
            scope,
            conversation_id=conversation_id,
            summary=summary,
            preserved_constraints=constraints,
            through_task_id=through_task_id,
            model_id=model_id,
            token_count=max(1, len(summary) // 4),
        )

    def history_prefix(
        self,
        scope: ExecutionScope | Mapping[str, Any],
        conversation_id: str | None = None,
    ) -> list[dict[str, str]]:
        item = self.get(scope, conversation_id)
        if not item:
            return []
        constraints = item.get("preserved_constraints") or []
        content = "较早对话摘要（不是本次新目标）：\n" + item["summary"]
        if constraints:
            content += "\n\n仍需保留的明确约束：\n- " + "\n- ".join(constraints)
        return [{"role": "assistant", "content": content}]

    @staticmethod
    def _dedupe_constraints(values: Sequence[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = _SPACE_RE.sub(" ", str(value)).strip()
            marker = item.casefold()
            if not item or marker in seen:
                continue
            seen.add(marker)
            result.append(item[:500])
            if len(result) >= 50:
                break
        return result
