from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app import db
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.secret_store import secret_store


TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled", "waiting_approval"}
TRIGGER_TYPES = {"interval", "cron", "once", "webhook"}
_SECRET_PARTS = ("api_key", "apikey", "authorization", "cookie", "password", "secret", "token")


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused with different content."""


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def _safe_json(value: Any) -> Any:
    """Redact credentials before state or webhook metadata becomes public."""
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            safe[str(key)] = "[已隐藏]" if any(part in lowered for part in _SECRET_PARTS) else _safe_json(item)
        return safe
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


def serialize_loop(row: dict[str, Any]) -> dict[str, Any]:
    """Return only documented public fields; encrypted webhook data never leaves."""
    if not row:
        return {}
    public_fields = (
        "id", "name", "prompt", "agent_id", "model_id", "trigger_type",
        "cron_expression", "once_at", "organization_id", "workspace_id", "user_id",
        "status", "next_run_at", "last_run_at", "last_task_id", "created_at", "updated_at",
    )
    result = {key: row.get(key, "") for key in public_fields}
    for key, default in (
        ("interval_seconds", 3600), ("max_runs", 10), ("run_count", 0),
        ("consecutive_failures", 0), ("max_failures", 3), ("max_attempts", 1),
        ("retry_backoff_seconds", 0), ("webhook_tolerance_seconds", 300),
    ):
        result[key] = int(row.get(key, default) or default)
    result["trigger_type"] = str(row.get("trigger_type") or "interval")
    result["state"] = _safe_json(db.json_loads(row.get("state_json"), {}))
    result["last_diff"] = _safe_json(db.json_loads(row.get("last_diff_json"), {}))
    result["webhook_secret_configured"] = bool(row.get("webhook_secret_ciphertext"))
    return result


def serialize_run(row: dict[str, Any]) -> dict[str, Any]:
    if not row:
        return {}
    result = {
        key: row.get(key, "")
        for key in ("id", "loop_id", "task_id", "status", "started_at", "finished_at", "trigger_event_id")
    }
    result["run_number"] = int(row.get("run_number", 0) or 0)
    result["attempt"] = int(row.get("attempt", 1) or 1)
    result["result"] = _safe_json(db.json_loads(row.get("result_json"), {}))
    result["decision"] = _safe_json(db.json_loads(row.get("decision_json"), {}))
    result["input_state"] = _safe_json(db.json_loads(row.get("input_state_json"), {}))
    result["output_state"] = _safe_json(db.json_loads(row.get("output_state_json"), {}))
    result["diff"] = _safe_json(db.json_loads(row.get("diff_json"), {}))
    result["error"] = _safe_json(db.json_loads(row.get("error_json"), {}))
    return result


def serialize_trigger_event(row: dict[str, Any]) -> dict[str, Any]:
    if not row:
        return {}
    return {
        key: row.get(key, "")
        for key in (
            "id", "loop_id", "organization_id", "workspace_id", "user_id", "trigger_type",
            "idempotency_key", "payload_sha256", "status", "run_id", "error",
            "received_at", "started_at", "finished_at",
        )
    }


def serialize_notification(row: dict[str, Any]) -> dict[str, Any]:
    if not row:
        return {}
    result = {
        key: row.get(key, "")
        for key in (
            "id", "organization_id", "workspace_id", "user_id", "kind", "title", "content",
            "status", "entity_type", "entity_id", "created_at", "read_at",
        )
    }
    result["data"] = _safe_json(db.json_loads(row.get("data_json"), {}))
    return result


def _cron_field(expression: str, minimum: int, maximum: int, *, sunday: bool = False) -> set[int]:
    values: set[int] = set()
    for part in expression.split(","):
        token = part.strip()
        if not token:
            raise ValueError("Cron 字段不能为空")
        base, separator, step_raw = token.partition("/")
        try:
            step = int(step_raw) if separator else 1
        except ValueError as exc:
            raise ValueError("Cron 步长必须是整数") from exc
        if step < 1:
            raise ValueError("Cron 步长必须大于 0")
        field_max = 7 if sunday else maximum
        if base == "*":
            start, end = minimum, field_max
        elif "-" in base:
            start_raw, end_raw = base.split("-", 1)
            try:
                start, end = int(start_raw), int(end_raw)
            except ValueError as exc:
                raise ValueError("Cron 范围必须是整数") from exc
        else:
            try:
                start = int(base)
                end = field_max if separator else start
            except ValueError as exc:
                raise ValueError("Cron 字段必须是整数、范围、列表或通配符") from exc
        if start < minimum or end > field_max or start > end:
            raise ValueError(f"Cron 字段超出范围 {minimum}-{field_max}")
        for item in range(start, end + 1, step):
            values.add(0 if sunday and item == 7 else item)
    return values


def next_cron_time(expression: str, after: datetime | None = None) -> datetime:
    """Calculate the next UTC fire time for a standard five-field cron."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Cron 表达式必须包含 5 个字段：分 时 日 月 周")
    minutes = _cron_field(fields[0], 0, 59)
    hours = _cron_field(fields[1], 0, 23)
    month_days = _cron_field(fields[2], 1, 31)
    months = _cron_field(fields[3], 1, 12)
    week_days = _cron_field(fields[4], 0, 6, sunday=True)
    day_wildcard = fields[2] == "*"
    week_wildcard = fields[4] == "*"
    candidate = _utc(after).astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(2 * 366 * 24 * 60):
        cron_weekday = (candidate.weekday() + 1) % 7
        day_match = candidate.day in month_days
        week_match = cron_weekday in week_days
        if day_wildcard:
            date_match = week_match
        elif week_wildcard:
            date_match = day_match
        else:
            date_match = day_match or week_match
        if (
            candidate.minute in minutes
            and candidate.hour in hours
            and candidate.month in months
            and date_match
        ):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("Cron 表达式在未来两年内没有可执行时间")


def validate_trigger_config(
    trigger_type: str,
    *,
    cron_expression: str = "",
    once_at: str = "",
    webhook_secret_configured: bool = False,
) -> None:
    if trigger_type not in TRIGGER_TYPES:
        raise ValueError("不支持的自动化触发类型")
    if trigger_type == "cron":
        next_cron_time(cron_expression)
    elif trigger_type == "once":
        if not _parse_time(once_at):
            raise ValueError("once 触发必须提供有效的 once_at ISO 时间")
    elif trigger_type == "webhook" and not webhook_secret_configured:
        raise ValueError("webhook 触发必须配置至少 16 位签名密钥")


def next_schedule_at(
    loop: dict[str, Any], *, after: datetime | None = None, immediate_interval: bool = False
) -> str:
    current = _utc(after)
    trigger_type = str(loop.get("trigger_type") or "interval")
    if trigger_type == "interval":
        if immediate_interval:
            return current.isoformat()
        return (current + timedelta(seconds=int(loop.get("interval_seconds") or 3600))).isoformat()
    if trigger_type == "cron":
        return next_cron_time(str(loop.get("cron_expression") or ""), current).isoformat()
    if trigger_type == "once":
        scheduled = _parse_time(str(loop.get("once_at") or ""))
        if not scheduled:
            raise ValueError("once 触发缺少有效执行时间")
        return max(scheduled, current).isoformat()
    return ""


def create_webhook_event(
    loop: dict[str, Any], idempotency_key: str, raw_body: bytes
) -> tuple[dict[str, Any], bool]:
    """Persist webhook content encrypted and enforce key/body idempotency."""
    digest = hashlib.sha256(raw_body).hexdigest()
    existing = db.query_one(
        "SELECT * FROM automation_trigger_events WHERE loop_id = ? AND idempotency_key = ?",
        (loop["id"], idempotency_key),
    )
    if existing:
        if existing.get("payload_sha256") != digest:
            raise IdempotencyConflictError("同一 Idempotency-Key 已用于不同请求正文")
        return serialize_trigger_event(existing), True
    event_id = "trigger_" + uuid.uuid4().hex[:16]
    encoded = base64.b64encode(raw_body).decode("ascii")
    now = db.utc_now()
    try:
        db.execute(
            """INSERT INTO automation_trigger_events(
                   id, loop_id, organization_id, workspace_id, user_id, trigger_type,
                   idempotency_key, payload_sha256, payload_ciphertext, status, received_at
               ) VALUES (?, ?, ?, ?, ?, 'webhook', ?, ?, ?, 'accepted', ?)""",
            (
                event_id, loop["id"], loop.get("organization_id") or "local-org",
                loop.get("workspace_id") or "default", loop.get("user_id") or "local-user",
                idempotency_key, digest, secret_store.encrypt(encoded), now,
            ),
        )
    except Exception:
        # A concurrent identical request may win the UNIQUE constraint.
        existing = db.query_one(
            "SELECT * FROM automation_trigger_events WHERE loop_id = ? AND idempotency_key = ?",
            (loop["id"], idempotency_key),
        )
        if not existing:
            raise
        if existing.get("payload_sha256") != digest:
            raise IdempotencyConflictError("同一 Idempotency-Key 已用于不同请求正文")
        return serialize_trigger_event(existing), True
    return serialize_trigger_event(
        db.query_one("SELECT * FROM automation_trigger_events WHERE id = ?", (event_id,)) or {}
    ), False


def _state_diff(before: Any, after: Any, path: str = "$") -> dict[str, Any]:
    changes: list[dict[str, Any]] = []

    def walk(old: Any, new: Any, current_path: str) -> None:
        if len(changes) >= 200:
            return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                child = f"{current_path}.{key}"
                if key not in old:
                    changes.append({"path": child, "before": None, "after": new[key]})
                elif key not in new:
                    changes.append({"path": child, "before": old[key], "after": None})
                else:
                    walk(old[key], new[key], child)
            return
        if old != new:
            changes.append({"path": current_path, "before": old, "after": new})

    walk(_safe_json(before), _safe_json(after), path)
    return {"changed": bool(changes), "changes": changes, "truncated": len(changes) >= 200}


def _output_state(
    previous: dict[str, Any], task_result: dict[str, Any], task_status: str, artifacts: list[Any]
) -> dict[str, Any]:
    explicit = task_result.get("automation_state") or task_result.get("loop_state")
    if isinstance(explicit, dict):
        return _safe_json(explicit)
    result = dict(_safe_json(previous))
    summary = task_result.get("summary")
    result["last_run"] = {
        "task_status": task_status,
        "summary": str(summary or task_result.get("error") or "")[:2_000],
        "artifact_count": len(artifacts),
        "goal_complete": bool(task_result.get("loop_complete") or task_result.get("goal_complete")),
    }
    return result


class LoopScheduler:
    """Persistent interval/cron/once/webhook automation scheduler."""

    def __init__(self, runtime: AgentRuntime, poll_seconds: float = 1.0) -> None:
        self.runtime = runtime
        self.poll_seconds = poll_seconds
        self._runner: asyncio.Task[None] | None = None
        self._active: set[str] = set()
        self._pending: set[str] = set()
        self._jobs: set[asyncio.Task[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if not self._runner:
            return
        self._runner.cancel()
        try:
            await self._runner
        except asyncio.CancelledError:
            pass
        self._runner = None
        jobs = [item for item in self._jobs if not item.done()]
        for item in jobs:
            item.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    async def _poll(self) -> None:
        while True:
            # Accepted webhook events are durable and survive a process restart.
            queued = db.query_all(
                """SELECT e.id, e.loop_id, e.trigger_type
                   FROM automation_trigger_events e JOIN loops l ON l.id = e.loop_id
                   WHERE e.status = 'accepted' AND l.status = 'active'
                   ORDER BY e.received_at LIMIT 20"""
            )
            for event in queued:
                if not self.is_busy(event["loop_id"]):
                    self.dispatch_once(
                        event["loop_id"], scheduled=True,
                        trigger_event_id=event["id"], trigger_type=event["trigger_type"],
                    )
            now = db.utc_now()
            due = db.query_all(
                """SELECT id, trigger_type FROM loops
                   WHERE status = 'active' AND trigger_type IN ('interval', 'cron', 'once')
                     AND next_run_at != '' AND next_run_at <= ?
                   ORDER BY next_run_at LIMIT 10""",
                (now,),
            )
            for item in due:
                if not self.is_busy(item["id"]):
                    self.dispatch_once(item["id"], scheduled=True, trigger_type=item["trigger_type"])
            await asyncio.sleep(self.poll_seconds)

    def is_busy(self, loop_id: str) -> bool:
        return loop_id in self._pending or loop_id in self._active

    def dispatch_once(
        self,
        loop_id: str,
        *,
        scheduled: bool = False,
        trigger_event_id: str = "",
        trigger_type: str = "manual",
    ) -> asyncio.Task[dict[str, Any]]:
        """Queue one logical iteration without keeping the HTTP request open."""
        if self.is_busy(loop_id):
            raise RuntimeError("这个自动化正在执行，请勿重复运行")
        self._pending.add(loop_id)

        async def dispatched() -> dict[str, Any]:
            try:
                return await self.run_once(
                    loop_id, scheduled=scheduled, trigger_event_id=trigger_event_id,
                    trigger_type=trigger_type,
                )
            finally:
                self._pending.discard(loop_id)

        job = asyncio.create_task(dispatched())
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
        return job

    def _create_event(self, loop: dict[str, Any], trigger_type: str) -> str:
        event_id = "trigger_" + uuid.uuid4().hex[:16]
        now = db.utc_now()
        db.execute(
            """INSERT INTO automation_trigger_events(
                   id, loop_id, organization_id, workspace_id, user_id, trigger_type,
                   idempotency_key, status, received_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?)""",
            (
                event_id, loop["id"], loop.get("organization_id") or "local-org",
                loop.get("workspace_id") or "default", loop.get("user_id") or "local-user",
                trigger_type, f"{trigger_type}:{uuid.uuid4().hex}", now,
            ),
        )
        return event_id

    def _event_payload(self, trigger_event_id: str) -> bytes:
        if not trigger_event_id:
            return b""
        event = db.query_one(
            "SELECT payload_ciphertext FROM automation_trigger_events WHERE id = ?", (trigger_event_id,)
        ) or {}
        encrypted = str(event.get("payload_ciphertext") or "")
        if not encrypted:
            return b""
        return base64.b64decode(secret_store.decrypt(encrypted).encode("ascii"), validate=True)

    @staticmethod
    def _notify(loop: dict[str, Any], title: str, content: str, data: dict[str, Any]) -> None:
        db.execute(
            """INSERT INTO notifications(
                   id, organization_id, workspace_id, user_id, kind, title, content,
                   data_json, status, entity_type, entity_id, created_at
               ) VALUES (?, ?, ?, ?, 'automation', ?, ?, ?, 'unread', 'loop', ?, ?)""",
            (
                "notice_" + uuid.uuid4().hex[:16], loop.get("organization_id") or "local-org",
                loop.get("workspace_id") or "default", loop.get("user_id") or "local-user",
                title, content, db.json_dumps(_safe_json(data)), loop["id"], db.utc_now(),
            ),
        )

    def recover_interrupted_runs(self) -> set[str]:
        """Close orphaned loop attempts and pause their parent automations safely."""
        rows = db.query_all("SELECT * FROM loop_runs WHERE status = 'running' ORDER BY started_at")
        interrupted_tasks: set[str] = set()
        affected: dict[str, int] = {}
        now = db.utc_now()
        for row in rows:
            interrupted_tasks.add(str(row.get("task_id") or ""))
            affected[row["loop_id"]] = max(affected.get(row["loop_id"], 0), int(row["run_number"]))
            error = {"message": "平台服务重启，本次自动化尝试已中断", "error_type": "ServiceRestart"}
            decision = {"continue": False, "reason": error["message"], "task_status": "failed", "interrupted": True}
            db.execute(
                """UPDATE loop_runs SET status = 'failed', finished_at = ?, error_json = ?,
                       result_json = ?, decision_json = ? WHERE id = ?""",
                (now, db.json_dumps(error), db.json_dumps({"error": error["message"]}), db.json_dumps(decision), row["id"]),
            )
            if row.get("trigger_event_id"):
                db.execute(
                    """UPDATE automation_trigger_events SET status = 'failed', error = ?, finished_at = ?
                       WHERE id = ? AND status = 'running'""",
                    (error["message"], now, row["trigger_event_id"]),
                )
        for loop_id, run_number in affected.items():
            loop = db.query_one("SELECT * FROM loops WHERE id = ?", (loop_id,))
            if not loop:
                continue
            db.execute(
                """UPDATE loops SET status = 'paused', next_run_at = '', run_count = ?,
                       consecutive_failures = consecutive_failures + 1, last_run_at = ?, updated_at = ?
                   WHERE id = ?""",
                (max(int(loop.get("run_count") or 0), run_number), now, now, loop_id),
            )
            self._notify(loop, "自动化因服务重启暂停", "检测到未完成的执行尝试，请确认后重新启动。", {"run_number": run_number})
        # Legacy rows may have a running parent without a surviving run row.
        db.execute(
            "UPDATE loops SET status = 'paused', next_run_at = '', updated_at = ? WHERE status = 'running'",
            (now,),
        )
        db.execute(
            """UPDATE automation_trigger_events SET status = 'failed', error = ?, finished_at = ?
               WHERE status = 'running'""",
            ("平台服务重启，触发事件对应的执行已中断", now),
        )
        return {task_id for task_id in interrupted_tasks if task_id}

    async def _attempt(
        self,
        loop: dict[str, Any],
        logical_run: int,
        attempt: int,
        trigger_event_id: str,
        input_state: dict[str, Any],
        trigger_payload: bytes,
    ) -> dict[str, Any]:
        run_id = "run_" + uuid.uuid4().hex[:12]
        started_at = db.utc_now()
        db.execute(
            """INSERT INTO loop_runs(
                   id, loop_id, run_number, attempt, status, started_at, trigger_event_id,
                   input_state_json
               ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)""",
            (run_id, loop["id"], logical_run, attempt, started_at, trigger_event_id, db.json_dumps(input_state)),
        )
        if trigger_event_id:
            db.execute(
                """UPDATE automation_trigger_events SET status = 'running', run_id = ?, started_at = ?
                   WHERE id = ?""",
                (run_id, started_at, trigger_event_id),
            )
        task_id = ""
        try:
            message = f"[自动化：{loop['name']}｜第 {logical_run} 轮｜尝试 {attempt}]\n{loop['prompt']}"
            if trigger_payload:
                try:
                    parsed_payload = json.loads(trigger_payload.decode("utf-8"))
                    displayed = json.dumps(parsed_payload, ensure_ascii=False, separators=(",", ":"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    displayed = trigger_payload.decode("utf-8", errors="replace")
                message += f"\n\n[触发输入]\n{displayed[:100_000]}"
            task = create_task_record(
                message,
                loop["agent_id"],
                # Keep automation conversations isolated while ownership is
                # carried by the loop's explicit organization/workspace/user scope.
                workspace=f"loop:{loop['id']}",
                model_id=loop["model_id"],
                conversation_id=f"loop_{loop['id']}",
                organization_id=loop.get("organization_id") or "local-org",
                user_id=loop.get("user_id") or "local-user",
            )
            task_id = task["id"]
            db.execute("UPDATE loop_runs SET task_id = ? WHERE id = ?", (task_id, run_id))
            db.execute("UPDATE loops SET last_task_id = ? WHERE id = ?", (task_id, loop["id"]))
            await self.runtime.run_task(task_id)
            finished_task = db.query_one(
                "SELECT status, result_json, artifacts_json FROM tasks WHERE id = ?", (task_id,)
            ) or {}
            task_status = str(finished_task.get("status") or "failed")
            task_result = db.json_loads(finished_task.get("result_json"), {})
            if not isinstance(task_result, dict):
                task_result = {"value": task_result}
            artifacts = db.json_loads(finished_task.get("artifacts_json"), [])
            if not isinstance(artifacts, list):
                artifacts = []
            error: dict[str, Any] = {}
        except Exception as exc:
            task_status = "failed"
            task_result = {"error": str(exc)}
            artifacts = []
            error = {"message": str(exc), "error_type": type(exc).__name__}
        output_state = _output_state(input_state, task_result, task_status, artifacts)
        diff = _state_diff(input_state, output_state)
        waiting = task_status == "waiting_approval"
        blocked = bool(task_result.get("needs_clarification"))
        success = task_status == "completed" and not blocked
        run_status = "waiting_approval" if waiting else "blocked" if blocked else "completed" if success else "failed"
        finished_at = db.utc_now()
        result = {"task_result": task_result, "artifacts": artifacts}
        db.execute(
            """UPDATE loop_runs SET status = ?, finished_at = ?, result_json = ?, error_json = ?,
                   input_state_json = ?, output_state_json = ?, diff_json = ? WHERE id = ?""",
            (
                run_status, finished_at, db.json_dumps(result), db.json_dumps(error),
                db.json_dumps(input_state), db.json_dumps(output_state), db.json_dumps(diff), run_id,
            ),
        )
        return {
            "id": run_id, "task_id": task_id, "task_status": task_status, "task_result": task_result,
            "artifacts": artifacts, "waiting": waiting, "blocked": blocked, "success": success,
            "output_state": output_state, "diff": diff, "error": error,
        }

    async def run_once(
        self,
        loop_id: str,
        scheduled: bool = False,
        *,
        trigger_event_id: str = "",
        trigger_type: str = "manual",
    ) -> dict[str, Any]:
        async with self._lock:
            loop = db.query_one("SELECT * FROM loops WHERE id = ?", (loop_id,))
            if not loop:
                raise ValueError("Loop not found")
            if loop_id in self._active or loop["status"] == "running":
                raise RuntimeError("这个自动化正在执行，请勿重复运行")
            if scheduled and loop["status"] != "active":
                raise RuntimeError("自动化已暂停")
            if int(loop["run_count"]) >= int(loop["max_runs"]):
                db.execute(
                    "UPDATE loops SET status = 'completed', next_run_at = '', updated_at = ? WHERE id = ?",
                    (db.utc_now(), loop_id),
                )
                raise RuntimeError("自动化已达到最大轮数")
            self._active.add(loop_id)
            previous_status = str(loop["status"])
            logical_run = int(loop["run_count"]) + 1
            input_state = db.json_loads(loop.get("state_json"), {})
            if not isinstance(input_state, dict):
                input_state = {}
            if not trigger_event_id:
                event_trigger = (
                    str(loop.get("trigger_type") or "interval")
                    if scheduled and (not trigger_type or trigger_type == "manual")
                    else (trigger_type or "manual")
                )
                trigger_event_id = self._create_event(loop, event_trigger)
            now = db.utc_now()
            db.execute(
                "UPDATE loops SET status = 'running', next_run_at = '', updated_at = ? WHERE id = ?",
                (now, loop_id),
            )

        try:
            trigger_payload = self._event_payload(trigger_event_id)
            max_attempts = int(loop.get("max_attempts") or 1)
            final: dict[str, Any] | None = None
            for attempt in range(1, max_attempts + 1):
                current = await self._attempt(
                    loop, logical_run, attempt, trigger_event_id, input_state, trigger_payload
                )
                final = current
                retry = not (current["success"] or current["waiting"] or current["blocked"]) and attempt < max_attempts
                attempt_decision = {
                    "continue": False,
                    "retry": retry,
                    "reason": "本次尝试失败，将按策略重试" if retry else "本次尝试已结束",
                    "task_status": current["task_status"],
                    "logical_run_number": logical_run,
                    "attempt": attempt,
                }
                db.execute(
                    "UPDATE loop_runs SET decision_json = ? WHERE id = ?",
                    (db.json_dumps(attempt_decision), current["id"]),
                )
                if not retry:
                    break
                delay = int(loop.get("retry_backoff_seconds") or 0)
                if delay:
                    await asyncio.sleep(delay)
            if final is None:  # pragma: no cover - defensive guard
                raise RuntimeError("自动化未创建执行尝试")

            task_result = final["task_result"]
            waiting = final["waiting"]
            blocked = final["blocked"]
            success = final["success"]
            goal_complete = bool(task_result.get("loop_complete") or task_result.get("goal_complete"))
            failures = (
                int(loop["consecutive_failures"])
                if waiting or blocked
                else (0 if success else int(loop["consecutive_failures"]) + 1)
            )
            reached_limit = logical_run >= int(loop["max_runs"])
            fused = failures >= int(loop["max_failures"])
            current_loop = db.query_one("SELECT status FROM loops WHERE id = ?", (loop_id,)) or {}
            manually_paused = current_loop.get("status") == "paused"
            trigger_kind = str(loop.get("trigger_type") or "interval")

            if waiting:
                next_status, reason = "waiting_approval", "本轮需要人工审批，已暂停后续调度"
            elif blocked:
                next_status, reason = "blocked", "本轮缺少必要信息，已暂停后续调度"
            elif success and (goal_complete or reached_limit or trigger_kind == "once"):
                next_status = "completed"
                reason = "目标已完成" if goal_complete else "一次性任务已完成" if trigger_kind == "once" else "已达到最大轮数"
            elif not success and (fused or reached_limit or trigger_kind == "once"):
                next_status = "failed"
                reason = "最后一轮执行失败" if reached_limit else "一次性任务执行失败" if trigger_kind == "once" else "连续失败次数已达到熔断阈值"
            elif manually_paused or previous_status == "paused":
                next_status, reason = "paused", "单轮执行完成，自动化保持暂停"
            else:
                next_status, reason = "active", "等待下一次触发"

            next_run = ""
            if next_status == "active" and trigger_kind in {"interval", "cron"}:
                next_run = next_schedule_at(loop)
            finished_at = db.utc_now()
            decision = {
                "continue": next_status == "active", "reason": reason,
                "task_status": final["task_status"], "goal_complete": goal_complete,
                "artifact_count": len(final["artifacts"]),
                "attempts": int(
                    (db.query_one("SELECT attempt FROM loop_runs WHERE id = ?", (final["id"],)) or {}).get("attempt") or 1
                ),
            }
            db.execute(
                "UPDATE loop_runs SET decision_json = ? WHERE id = ?",
                (db.json_dumps(decision), final["id"]),
            )
            db.execute(
                """UPDATE loops SET status = ?, run_count = ?, consecutive_failures = ?, next_run_at = ?,
                       last_run_at = ?, last_task_id = ?, state_json = ?, last_diff_json = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    next_status, logical_run, failures, next_run, finished_at, final["task_id"],
                    db.json_dumps(final["output_state"]), db.json_dumps(final["diff"]), finished_at, loop_id,
                ),
            )
            event_status = "completed" if success else "waiting_approval" if waiting else "blocked" if blocked else "failed"
            db.execute(
                """UPDATE automation_trigger_events SET status = ?, run_id = ?, error = ?, finished_at = ?
                   WHERE id = ?""",
                (
                    event_status, final["id"], str(final["error"].get("message") or ""),
                    finished_at, trigger_event_id,
                ),
            )
            if next_status in {"completed", "failed", "waiting_approval", "blocked"}:
                title = {
                    "completed": "自动化已完成", "failed": "自动化执行失败",
                    "waiting_approval": "自动化等待审批", "blocked": "自动化需要补充信息",
                }[next_status]
                self._notify(loop, title, reason, {"run_number": logical_run, "run_id": final["id"], "status": next_status})
            elif not success:
                self._notify(
                    loop, "自动化本轮执行失败", "尚未达到熔断阈值，将等待下一次调度。",
                    {"run_number": logical_run, "run_id": final["id"], "status": next_status},
                )
            return serialize_run(db.query_one("SELECT * FROM loop_runs WHERE id = ?", (final["id"],)) or {})
        except Exception as exc:
            finished_at = db.utc_now()
            db.execute(
                """UPDATE loops SET status = 'failed', run_count = ?, consecutive_failures = consecutive_failures + 1,
                       next_run_at = '', last_run_at = ?, updated_at = ? WHERE id = ?""",
                (logical_run, finished_at, finished_at, loop_id),
            )
            db.execute(
                """UPDATE automation_trigger_events SET status = 'failed', error = ?, finished_at = ?
                   WHERE id = ?""",
                (str(exc), finished_at, trigger_event_id),
            )
            self._notify(loop, "自动化执行失败", str(exc), {"run_number": logical_run, "error_type": type(exc).__name__})
            raise
        finally:
            self._active.discard(loop_id)
