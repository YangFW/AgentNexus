from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from app import db
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.context_service import ExecutionScope
from app.services.event_bus import emit
from app.services.task_state import TaskStateError, TaskStateService


class ExpertTeamError(RuntimeError):
    """Base error exposed by the expert-team API as a friendly 4xx."""


class ExpertNotFoundError(ExpertTeamError):
    pass


class ExpertConflictError(ExpertTeamError):
    pass


class ExpertPermissionError(ExpertTeamError):
    pass


class ExpertValidationError(ExpertTeamError):
    pass


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _json(value: str | None, default: Any) -> Any:
    return db.json_loads(value, default)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _acceptance_title(item: Any, index: int) -> str:
    """Return a stable, user-facing title for old and structured rules."""
    if isinstance(item, str):
        return item.strip() or f"验收标准 {index + 1}"
    if not isinstance(item, dict):
        return f"验收标准 {index + 1}"
    if str(item.get("title") or "").strip():
        return str(item["title"]).strip()
    rule_type = str(item.get("type") or "").strip().lower().replace("-", "_")
    if rule_type == "min_chars" or "min_chars" in item:
        value = item.get("value", item.get("min_chars"))
        return f"最终答复不少于 {value} 字"
    if rule_type == "required_keywords" or "required_keywords" in item:
        value = item.get(
            "value", item.get("required_keywords", item.get("keywords"))
        )
        keywords = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        return "最终答复包含关键词：" + "、".join(str(keyword) for keyword in keywords)
    if rule_type == "requires_artifact" or "requires_artifact" in item:
        return "已生成要求的可下载产物"
    return f"验收标准 {index + 1}"


def _merge_permissions(*values: dict[str, Any]) -> dict[str, Any]:
    """Merge permission layers while preserving common restrictive fields.

    The platform accepts provider-specific permission keys, so an exhaustive
    schema would be counterproductive.  Known capability lists are narrowed,
    deny/approval lists are accumulated, read-only can only become stricter,
    and numeric limits use the smallest configured value.
    """
    result: dict[str, Any] = {}
    allow_keys = {"allowed_tools", "allowed_mcp_servers", "allowed_skills"}
    deny_keys = {"denied_tools", "denied_mcp_servers", "approval_required"}
    limit_keys = {"max_tool_steps", "max_tool_calls", "timeout_seconds", "budget_tokens"}
    for value in values:
        if not isinstance(value, dict):
            continue
        for key, incoming in value.items():
            if key in allow_keys and isinstance(incoming, list):
                incoming_values = list(dict.fromkeys(str(item) for item in incoming))
                if isinstance(result.get(key), list):
                    current = set(result[key])
                    result[key] = [item for item in incoming_values if item in current]
                else:
                    result[key] = incoming_values
            elif key in deny_keys and isinstance(incoming, list):
                result[key] = list(
                    dict.fromkeys([*(result.get(key) or []), *(str(item) for item in incoming)])
                )
            elif key in limit_keys and isinstance(incoming, (int, float)) and not isinstance(incoming, bool):
                current = result.get(key)
                result[key] = min(current, incoming) if isinstance(current, (int, float)) else incoming
            elif key == "read_only":
                result[key] = bool(result.get(key)) or bool(incoming)
            else:
                result[key] = incoming
    return result


_ROUTING_STOP_TERMS = {
    "任务", "问题", "专家", "专家团", "分析", "评估", "给出", "需要", "进行",
    "平台", "用户", "结果", "方案", "工作", "内容", "相关", "使用", "完成",
}

_ROUTING_EDGE_CONNECTORS = set("的和与及并或为在对将从由把被让向")


def _public_matched_terms(terms: set[str]) -> list[str]:
    """Keep route explanations readable instead of exposing n-gram noise."""
    candidates = sorted(
        (
            item for item in terms
            if len(item) >= 2
            and item[0] not in _ROUTING_EDGE_CONNECTORS
            and item[-1] not in _ROUTING_EDGE_CONNECTORS
        ),
        key=lambda item: (-len(item), item),
    )
    selected: list[str] = []
    for item in candidates:
        if any(item in existing for existing in selected):
            continue
        selected.append(item)
        if len(selected) >= 8:
            break
    return selected


def _routing_terms(value: str) -> set[str]:
    """Build deterministic terms for Chinese/Latin team routing.

    This is intentionally a transparent lexical router.  It never claims to
    understand hidden model reasoning and is stable enough to explain in the
    task event shown to users.
    """
    text = str(value or "").lower()
    terms = {
        item for item in re.findall(r"[a-z0-9][a-z0-9_.-]{1,}", text)
        if item not in _ROUTING_STOP_TERMS
    }
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for size in (2, 3, 4):
            terms.update(
                segment[index:index + size]
                for index in range(max(0, len(segment) - size + 1))
            )
    return {item for item in terms if item not in _ROUTING_STOP_TERMS}


class ExpertTeamService:
    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        task_state: TaskStateService | None = None,
    ) -> None:
        self.runtime = runtime
        self.task_state = task_state or runtime.task_state

    # -- Acceptance ----------------------------------------------------

    @staticmethod
    def _artifact_kind(artifact: dict[str, Any]) -> str:
        kind = str(artifact.get("kind") or "").strip().lower()
        if kind:
            return "md" if kind == "markdown" else kind
        name = str(artifact.get("name") or "").strip().lower()
        suffix = name.rsplit(".", 1)[-1] if "." in name else ""
        return "md" if suffix == "markdown" else suffix

    @classmethod
    def _evaluate_structured_acceptance(
        cls,
        rule: dict[str, Any],
        *,
        summary: str,
        artifacts: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        """Evaluate a machine-checkable acceptance item.

        Both the explicit ``{"type": ..., "value": ...}`` shape and the
        shorthand ``{"min_chars": ...}`` shape are supported so persisted
        teams do not need a migration.
        """
        rule_type = str(rule.get("type") or "").strip().lower().replace("-", "_")
        known_types = {"min_chars", "required_keywords", "requires_artifact"}
        if rule_type and rule_type not in known_types:
            return False, f"不支持的验收类型：{rule_type}"

        checks: list[tuple[bool, str]] = []
        handled = False
        if rule_type == "min_chars" or (not rule_type and "min_chars" in rule):
            handled = True
            raw_value = rule.get("value", rule.get("min_chars"))
            try:
                minimum = int(raw_value)
                if isinstance(raw_value, bool) or minimum < 0:
                    raise ValueError
            except (TypeError, ValueError):
                checks.append((False, "min_chars 必须是非负整数。"))
            else:
                actual = len(re.sub(r"\s+", "", summary))
                checks.append((actual >= minimum, f"最终答复 {actual} 字，要求不少于 {minimum} 字。"))

        if rule_type == "required_keywords" or (not rule_type and "required_keywords" in rule):
            handled = True
            raw_keywords = (
                rule.get("value", rule.get("required_keywords", rule.get("keywords")))
                if rule_type
                else rule.get("required_keywords")
            )
            if isinstance(raw_keywords, str):
                keywords = [raw_keywords.strip()] if raw_keywords.strip() else []
            elif isinstance(raw_keywords, (list, tuple, set)):
                keywords = [str(item).strip() for item in raw_keywords if str(item).strip()]
            else:
                keywords = []
            if not keywords:
                checks.append((False, "required_keywords 必须包含至少一个非空关键词。"))
            else:
                case_sensitive = bool(rule.get("case_sensitive", False))
                haystack = summary if case_sensitive else summary.casefold()
                matched = [
                    keyword
                    for keyword in keywords
                    if (keyword if case_sensitive else keyword.casefold()) in haystack
                ]
                mode = str(rule.get("match") or rule.get("operator") or "all").lower()
                passed = bool(matched) if mode == "any" else len(matched) == len(keywords)
                missing = [keyword for keyword in keywords if keyword not in matched]
                detail = (
                    f"已命中关键词：{'、'.join(matched)}。"
                    if passed
                    else f"缺少关键词：{'、'.join(missing)}。"
                )
                checks.append((passed, detail))

        if rule_type == "requires_artifact" or (not rule_type and "requires_artifact" in rule):
            handled = True
            required_value = (
                rule.get("value", rule.get("requires_artifact", True))
                if rule_type
                else rule.get("requires_artifact")
            )
            required = bool(required_value)
            raw_kinds = rule.get("artifact_kinds", rule.get("kinds", []))
            if isinstance(raw_kinds, str):
                expected_kinds = [raw_kinds.strip().lower()] if raw_kinds.strip() else []
            elif isinstance(raw_kinds, (list, tuple, set)):
                expected_kinds = [str(item).strip().lower() for item in raw_kinds if str(item).strip()]
            else:
                expected_kinds = []
            expected_kinds = ["md" if item == "markdown" else item for item in expected_kinds]
            actual_kinds = [cls._artifact_kind(item) for item in artifacts]
            artifact_names = [
                str(item.get("name") or item.get("id") or "").strip()
                for item in artifacts
            ]
            downloadable = [
                item for item in artifacts
                if str(item.get("download_url") or "").strip()
            ]
            if not required:
                checks.append((True, "当前标准未要求生成产物。"))
            elif expected_kinds:
                matches = [
                    item for item in artifacts
                    if cls._artifact_kind(item) in expected_kinds
                    and str(item.get("download_url") or "").strip()
                ]
                checks.append((bool(matches), f"要求格式：{'、'.join(expected_kinds)}；实际可下载产物：{'、'.join(artifact_names) or '无'}。"))
            else:
                checks.append((bool(downloadable), f"检测到 {len(downloadable)} 个可下载产物。"))

        if not handled:
            # Existing teams may persist descriptive dicts with just id/title.
            # Keep those usable, but still require an actual final response.
            passed = bool(summary.strip())
            return passed, "旧版描述性标准按最终答复非空进行兼容校验。"
        passed = all(item[0] for item in checks)
        return passed, " ".join(item[1] for item in checks)

    @staticmethod
    def _evaluate_legacy_acceptance(
        title: str,
        *,
        summary: str,
        member_runs: list[dict[str, Any]],
        goal_snapshot: str,
    ) -> tuple[bool, str]:
        """Give common persisted Chinese descriptions deterministic meaning."""
        normalized = title.strip()
        if "所有成员" in normalized and "结论" in normalized:
            missing = [
                str(item.get("member_id") or "未知成员")
                for item in member_runs
                if not str((item.get("output") or {}).get("summary") or "").strip()
            ]
            return (
                not missing,
                "所有成员均已交付非空结论。"
                if not missing
                else "缺少成员结论：" + "、".join(missing) + "。",
            )
        keyword_match = re.search(r"(?:必须|应)?包含[\s：:]*[\"“”']?(.+?)[\"“”']?$", normalized)
        if keyword_match:
            keywords = [
                item.strip("。；; ")
                for item in re.split(r"[、，,]|以及|及|和", keyword_match.group(1))
                if item.strip("。；; ")
            ]
            missing = [item for item in keywords if item.casefold() not in summary.casefold()]
            return (
                not missing,
                "已包含关键词：" + "、".join(keywords) + "。"
                if not missing
                else "缺少关键词：" + "、".join(missing) + "。",
            )
        if "最终汇总" in normalized and "目标" in normalized:
            passed = bool(summary.strip()) and bool(goal_snapshot.strip())
            return passed, "共同目标与最终答复均已保留。" if passed else "缺少共同目标或最终答复。"
        passed = bool(summary.strip())
        return passed, "旧版描述性标准按最终答复非空进行兼容校验。"

    @classmethod
    def _validate_team_acceptance(
        cls,
        acceptance: list[Any],
        *,
        summary: str,
        artifacts: list[dict[str, Any]],
        member_runs: list[dict[str, Any]],
        goal_snapshot: str,
    ) -> dict[str, Any]:
        criteria: list[dict[str, Any]] = [
            {
                "id": "team-response",
                "title": "已生成可交付的最终答复",
                "status": "passed" if summary.strip() else "failed",
                "detail": f"最终答复共 {len(re.sub(r'\s+', '', summary))} 字。",
            }
        ]
        for index, item in enumerate(acceptance):
            if isinstance(item, str):
                passed, detail = cls._evaluate_legacy_acceptance(
                    item,
                    summary=summary,
                    member_runs=member_runs,
                    goal_snapshot=goal_snapshot,
                )
            elif isinstance(item, dict):
                passed, detail = cls._evaluate_structured_acceptance(
                    item, summary=summary, artifacts=artifacts
                )
            else:
                passed, detail = False, "验收标准必须是字符串或对象。"
            configured_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
            criteria.append({
                "id": configured_id or f"team-criterion-{index + 1}",
                "title": _acceptance_title(item, index),
                "status": "passed" if passed else "failed",
                "detail": detail,
            })
        passed = all(item["status"] == "passed" for item in criteria)
        failed_count = sum(item["status"] == "failed" for item in criteria)
        return {
            "passed": passed,
            "message": (
                f"已按 {len(criteria)} 项验收标准完成检查，全部通过。"
                if passed
                else f"专家团最终验收失败：{failed_count} 项未通过。"
            ),
            "artifact_count": len(artifacts),
            "criteria": criteria,
        }

    # -- Scope ----------------------------------------------------------

    @staticmethod
    def _visible(row: dict[str, Any], scope: ExecutionScope, *, owner_key: str = "owner_user_id") -> bool:
        visibility = str(row.get("visibility") or "organization")
        if visibility == "public":
            return True
        if str(row.get("organization_id") or "local-org") != scope.organization_id:
            return False
        if visibility == "organization":
            return True
        if str(row.get("workspace_id") or "default") != scope.workspace_id:
            return False
        if visibility == "workspace":
            return True
        return str(row.get(owner_key) or "local-user") == scope.user_id

    @classmethod
    def _owned(cls, row: dict[str, Any], scope: ExecutionScope, *, owner_key: str = "owner_user_id") -> bool:
        return (
            str(row.get("organization_id") or "local-org") == scope.organization_id
            and str(row.get("workspace_id") or "default") == scope.workspace_id
            and str(row.get(owner_key) or "local-user") == scope.user_id
        )

    # -- Expert templates and installations ---------------------------

    @staticmethod
    def _template_api(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **{key: value for key, value in row.items() if key not in {"manifest_json", "permissions_json"}},
            "manifest": _json(row.get("manifest_json"), {}),
            "permissions": _json(row.get("permissions_json"), {}),
            "enabled": bool(row.get("enabled")),
        }

    def list_templates(self, scope: ExecutionScope, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        rows = db.query_all("SELECT * FROM expert_templates ORDER BY name, id")
        return [
            self._template_api(row)
            for row in rows
            if self._visible(row, scope) and (include_disabled or bool(row.get("enabled")))
        ]

    def get_template(self, template_id: str, scope: ExecutionScope) -> dict[str, Any] | None:
        row = db.query_one("SELECT * FROM expert_templates WHERE id = ?", (template_id,))
        if not row or not self._visible(row, scope):
            return None
        return self._template_api(row)

    def create_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        if db.query_one("SELECT id FROM expert_templates WHERE id = ?", (payload["id"],)):
            raise ExpertConflictError("专家模板 ID 已存在")
        now = db.utc_now()
        db.execute(
            """
            INSERT INTO expert_templates(
                id, name, description, version, source, manifest_json,
                organization_id, workspace_id, owner_user_id, visibility,
                permissions_json, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["id"], payload["name"], payload.get("description", ""),
                payload.get("version", "0.1.0"), payload.get("source", "local"),
                db.json_dumps(payload.get("manifest", {})), payload.get("organization_id", "local-org"),
                payload.get("workspace_id", "default"), payload.get("owner_user_id", "local-user"),
                payload.get("visibility", "organization"), db.json_dumps(payload.get("permissions", {})),
                1 if payload.get("enabled", True) else 0, now, now,
            ),
        )
        return self._template_api(db.query_one("SELECT * FROM expert_templates WHERE id = ?", (payload["id"],)) or {})

    def update_template(
        self, template_id: str, scope: ExecutionScope, changes: dict[str, Any]
    ) -> dict[str, Any]:
        row = db.query_one("SELECT * FROM expert_templates WHERE id = ?", (template_id,))
        if not row or not self._visible(row, scope):
            raise ExpertNotFoundError("专家模板不存在")
        if not self._owned(row, scope):
            raise ExpertPermissionError("只有模板所有者可以修改该专家模板")
        current = self._template_api(row)
        merged = {**current, **changes}
        db.execute(
            """
            UPDATE expert_templates SET name = ?, description = ?, version = ?, source = ?,
                manifest_json = ?, visibility = ?, permissions_json = ?, enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                merged["name"], merged.get("description", ""), merged.get("version", "0.1.0"),
                merged.get("source", "local"), db.json_dumps(merged.get("manifest", {})),
                merged.get("visibility", "organization"), db.json_dumps(merged.get("permissions", {})),
                1 if merged.get("enabled", True) else 0, db.utc_now(), template_id,
            ),
        )
        return self.get_template(template_id, scope) or {}

    def delete_template(self, template_id: str, scope: ExecutionScope) -> None:
        row = db.query_one("SELECT * FROM expert_templates WHERE id = ?", (template_id,))
        if not row or not self._visible(row, scope):
            raise ExpertNotFoundError("专家模板不存在")
        if not self._owned(row, scope):
            raise ExpertPermissionError("只有模板所有者可以删除该专家模板")
        installed = db.query_one(
            "SELECT id FROM expert_installations WHERE template_id = ? AND enabled = 1 LIMIT 1",
            (template_id,),
        )
        if installed:
            raise ExpertConflictError("该模板仍有启用中的安装，不能删除")
        db.execute("DELETE FROM expert_templates WHERE id = ?", (template_id,))

    @staticmethod
    def _installation_api(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **{key: value for key, value in row.items() if key != "permissions_json"},
            "permissions": _json(row.get("permissions_json"), {}),
            "enabled": bool(row.get("enabled")),
        }

    def list_installations(self, scope: ExecutionScope, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        rows = db.query_all(
            """SELECT * FROM expert_installations
               WHERE organization_id = ? AND workspace_id = ? AND user_id = ?
               ORDER BY installed_at DESC""",
            (scope.organization_id, scope.workspace_id, scope.user_id),
        )
        return [
            self._installation_api(row)
            for row in rows
            if include_disabled or bool(row.get("enabled"))
        ]

    def install_template(
        self, template_id: str, scope: ExecutionScope, request: dict[str, Any]
    ) -> dict[str, Any]:
        template = self.get_template(template_id, scope)
        if not template or not template.get("enabled"):
            raise ExpertNotFoundError("专家模板不存在、不可见或已停用")
        manifest = template.get("manifest") if isinstance(template.get("manifest"), dict) else {}
        overrides = request.get("overrides") if isinstance(request.get("overrides"), dict) else {}
        allowed_overrides = {
            "name", "description", "model", "system_prompt", "skills", "mcp_servers", "permissions"
        }
        unknown = sorted(set(overrides) - allowed_overrides)
        if unknown:
            raise ExpertValidationError("不允许覆盖模板字段：" + "、".join(unknown))
        config = {**manifest, **overrides}
        installation_id = str(request.get("installation_id") or _new_id("xinst"))
        agent_id = str(request.get("agent_id") or config.get("agent_id") or f"expert-{template_id}-{uuid.uuid4().hex[:6]}")
        if db.query_one("SELECT id FROM expert_installations WHERE id = ?", (installation_id,)):
            raise ExpertConflictError("专家安装 ID 已存在")
        if db.query_one("SELECT id FROM agents WHERE id = ?", (agent_id,)):
            raise ExpertConflictError("目标 Agent ID 已存在")
        skills = config.get("skills") if isinstance(config.get("skills"), list) else []
        mcp_servers = config.get("mcp_servers") if isinstance(config.get("mcp_servers"), list) else []
        requested_permissions = request.get("permissions") if isinstance(request.get("permissions"), dict) else {}
        permissions = _merge_permissions(
            config.get("permissions") if isinstance(config.get("permissions"), dict) else {},
            template.get("permissions") if isinstance(template.get("permissions"), dict) else {},
            requested_permissions,
        )
        now = db.utc_now()
        db.execute(
            """
            INSERT INTO agents(
                id, name, description, model, system_prompt, skills_json, mcp_servers_json,
                permissions_json, organization_id, workspace_id, owner_user_id, visibility,
                expert_installation_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id, str(config.get("name") or template["name"]),
                str(config.get("description") or template.get("description") or ""),
                str(config.get("model") or "deterministic"), str(config.get("system_prompt") or ""),
                db.json_dumps(skills), db.json_dumps(mcp_servers), db.json_dumps(permissions),
                scope.organization_id, scope.workspace_id, scope.user_id,
                str(request.get("visibility") or "private"), installation_id, now, now,
            ),
        )
        try:
            db.execute(
                """
                INSERT INTO expert_installations(
                    id, template_id, agent_id, installed_version, organization_id,
                    workspace_id, user_id, permissions_json, enabled, installed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    installation_id, template_id, agent_id, template.get("version", "0.1.0"),
                    scope.organization_id, scope.workspace_id, scope.user_id,
                    db.json_dumps(permissions), now,
                ),
            )
        except Exception:
            db.execute("DELETE FROM agents WHERE id = ? AND expert_installation_id = ?", (agent_id, installation_id))
            raise
        row = db.query_one("SELECT * FROM expert_installations WHERE id = ?", (installation_id,)) or {}
        return {**self._installation_api(row), "agent": self.get_agent(agent_id, scope)}

    def disable_installation(self, installation_id: str, scope: ExecutionScope) -> dict[str, Any]:
        row = db.query_one("SELECT * FROM expert_installations WHERE id = ?", (installation_id,))
        if not row or not self._owned(row, scope, owner_key="user_id"):
            raise ExpertNotFoundError("专家安装不存在或不属于当前作用域")
        active_team = db.query_one(
            """SELECT tm.id FROM agent_team_members tm
               JOIN agent_teams t ON t.id = tm.team_id
               WHERE tm.agent_id = ? AND t.enabled = 1 LIMIT 1""",
            (row["agent_id"],),
        )
        if active_team:
            raise ExpertConflictError("该专家仍被启用中的专家团使用，不能卸载")
        db.execute("UPDATE expert_installations SET enabled = 0 WHERE id = ?", (installation_id,))
        return self._installation_api(
            db.query_one("SELECT * FROM expert_installations WHERE id = ?", (installation_id,)) or {}
        )

    def get_agent(self, agent_id: str, scope: ExecutionScope) -> dict[str, Any] | None:
        row = db.query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if not row or not self._visible(row, scope):
            return None
        return {
            **{key: value for key, value in row.items() if key not in {"skills_json", "mcp_servers_json", "permissions_json"}},
            "skills": _json(row.get("skills_json"), []),
            "mcp_servers": _json(row.get("mcp_servers_json"), []),
            "permissions": _json(row.get("permissions_json"), {}),
        }

    # -- Teams ---------------------------------------------------------

    @staticmethod
    def _member_api(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **{key: value for key, value in row.items() if key not in {"depends_on_json", "permissions_json"}},
            "depends_on": _json(row.get("depends_on_json"), []),
            "permissions": _json(row.get("permissions_json"), {}),
        }

    def _team_api(self, row: dict[str, Any], *, include_members: bool = True) -> dict[str, Any]:
        result = {
            **{key: value for key, value in row.items() if key not in {"acceptance_json", "budget_json", "permissions_json"}},
            "acceptance": _json(row.get("acceptance_json"), []),
            "budget": _json(row.get("budget_json"), {}),
            "permissions": _json(row.get("permissions_json"), {}),
            "enabled": bool(row.get("enabled")),
        }
        if include_members:
            result["members"] = [
                self._member_api(member)
                for member in db.query_all(
                    "SELECT * FROM agent_team_members WHERE team_id = ? ORDER BY position, id",
                    (row["id"],),
                )
            ]
        return result

    def list_teams(self, scope: ExecutionScope, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        rows = db.query_all("SELECT * FROM agent_teams ORDER BY name, id")
        return [
            self._team_api(row)
            for row in rows
            if self._visible(row, scope) and (include_disabled or bool(row.get("enabled")))
        ]

    def get_team(self, team_id: str, scope: ExecutionScope) -> dict[str, Any] | None:
        row = db.query_one("SELECT * FROM agent_teams WHERE id = ?", (team_id,))
        if not row or not self._visible(row, scope):
            return None
        return self._team_api(row)

    def recommend_team(self, message: str, scope: ExecutionScope) -> dict[str, Any]:
        """Pick the best visible enabled team for an explicit expert-mode task.

        The router selects a configured team, not an ad-hoc roster.  Therefore
        the chosen team's full, versioned member list remains auditable.
        """
        teams = self.list_teams(scope)
        if not teams:
            raise ExpertNotFoundError("当前没有可用专家团，请先在“专家团”中创建并启用团队")
        message_terms = _routing_terms(message)
        ranked: list[tuple[float, dict[str, Any], list[str]]] = []
        for team in teams:
            agent_descriptions: list[str] = []
            for member in team.get("members") or []:
                agent = self.get_agent(str(member.get("agent_id") or ""), scope) or {}
                agent_descriptions.extend([
                    str(agent.get("name") or ""), str(agent.get("description") or ""),
                    str(member.get("role") or ""), str(member.get("member_prompt") or ""),
                ])
            corpus = " ".join([
                str(team.get("name") or ""), str(team.get("description") or ""),
                str(team.get("aggregation_prompt") or ""),
                " ".join(str(item) for item in team.get("acceptance") or []),
                *agent_descriptions,
            ])
            raw_overlap = message_terms & _routing_terms(corpus)
            matched_terms = _public_matched_terms(raw_overlap)
            score = sum(1.0 + max(0, len(item) - 2) * 0.35 for item in raw_overlap)
            ranked.append((score, team, matched_terms))
        ranked.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
        best_score, best_team, matched_terms = ranked[0]
        if len(ranked) > 1 and best_score <= 0:
            raise ExpertValidationError(
                "没有找到与当前目标明确匹配的专家团，请在专家模式旁手动选择团队"
            )
        reason = (
            "当前只有一个可用专家团，已按专家模式使用该团队"
            if len(ranked) == 1 and best_score <= 0
            else "任务主题与团队职责匹配" + (f"：{'、'.join(matched_terms[:5])}" if matched_terms else "")
        )
        return {
            "team": best_team,
            "score": round(best_score, 3),
            "matched_terms": matched_terms,
            "reason": reason,
        }

    def _validate_team_agents(
        self, supervisor_agent_id: str, members: list[dict[str, Any]], scope: ExecutionScope
    ) -> None:
        if not self.get_agent(supervisor_agent_id, scope):
            raise ExpertValidationError("主管 Agent 不存在或不属于当前可见作用域")
        if len(members) < 2:
            raise ExpertValidationError("专家团至少需要两个成员")
        agent_ids = [str(member.get("agent_id") or "") for member in members]
        if len(set(agent_ids)) != len(agent_ids):
            raise ExpertValidationError("同一个 Agent 不能在专家团中重复出现")
        for member in members:
            if member.get("execution_mode", "parallel") != "parallel" or member.get("depends_on"):
                raise ExpertValidationError("当前最小闭环仅支持无依赖的并行成员")
            if not self.get_agent(str(member.get("agent_id") or ""), scope):
                raise ExpertValidationError(f"成员 Agent 不存在或不可见：{member.get('agent_id')}")

    def _replace_members(self, team_id: str, members: list[dict[str, Any]]) -> None:
        prepared: list[tuple[str, dict[str, Any], int]] = []
        used_ids: set[str] = set()
        for index, member in enumerate(members):
            member_id = str(member.get("id") or f"member_{uuid.uuid4().hex[:10]}")
            existing = db.query_one("SELECT team_id FROM agent_team_members WHERE id = ?", (member_id,))
            if member_id in used_ids or (existing and existing.get("team_id") != team_id):
                raise ExpertConflictError(f"专家团成员 ID 重复：{member_id}")
            used_ids.add(member_id)
            prepared.append((member_id, member, index))
        db.execute("DELETE FROM agent_team_members WHERE team_id = ?", (team_id,))
        for member_id, member, index in prepared:
            db.execute(
                """
                INSERT INTO agent_team_members(
                    id, team_id, agent_id, role, execution_mode, depends_on_json,
                    member_prompt, position, permissions_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    member_id, team_id, member["agent_id"], member.get("role", "member"),
                    "parallel", db.json_dumps(member.get("depends_on", [])),
                    member.get("member_prompt", ""), int(member.get("position", index)),
                    db.json_dumps(member.get("permissions", {})),
                ),
            )

    def create_team(self, payload: dict[str, Any]) -> dict[str, Any]:
        if db.query_one("SELECT id FROM agent_teams WHERE id = ?", (payload["id"],)):
            raise ExpertConflictError("专家团 ID 已存在")
        scope = ExecutionScope(
            organization_id=payload.get("organization_id", "local-org"),
            workspace_id=payload.get("workspace_id", "default"),
            user_id=payload.get("owner_user_id", "local-user"),
        )
        members = payload.get("members") if isinstance(payload.get("members"), list) else []
        self._validate_team_agents(payload["supervisor_agent_id"], members, scope)
        now = db.utc_now()
        db.execute(
            """
            INSERT INTO agent_teams(
                id, name, description, supervisor_agent_id, aggregation_prompt,
                acceptance_json, budget_json, enabled, organization_id, workspace_id,
                owner_user_id, visibility, permissions_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["id"], payload["name"], payload.get("description", ""),
                payload["supervisor_agent_id"], payload.get("aggregation_prompt", ""),
                db.json_dumps(payload.get("acceptance", [])), db.json_dumps(payload.get("budget", {})),
                1 if payload.get("enabled", True) else 0, scope.organization_id, scope.workspace_id,
                scope.user_id, payload.get("visibility", "organization"),
                db.json_dumps(payload.get("permissions", {})), now, now,
            ),
        )
        try:
            self._replace_members(payload["id"], members)
        except Exception:
            db.execute("DELETE FROM agent_teams WHERE id = ?", (payload["id"],))
            raise
        return self.get_team(payload["id"], scope) or {}

    def update_team(self, team_id: str, scope: ExecutionScope, changes: dict[str, Any]) -> dict[str, Any]:
        row = db.query_one("SELECT * FROM agent_teams WHERE id = ?", (team_id,))
        if not row or not self._visible(row, scope):
            raise ExpertNotFoundError("专家团不存在")
        if not self._owned(row, scope):
            raise ExpertPermissionError("只有专家团所有者可以修改")
        active = db.query_one(
            "SELECT id FROM team_runs WHERE team_id = ? AND status IN ('queued','running','aggregating') LIMIT 1",
            (team_id,),
        )
        if active:
            raise ExpertConflictError("专家团有运行中的任务，暂时不能修改")
        current = self._team_api(row)
        merged = {**current, **changes}
        members = merged.get("members") if isinstance(merged.get("members"), list) else []
        self._validate_team_agents(merged["supervisor_agent_id"], members, scope)
        db.execute(
            """
            UPDATE agent_teams SET name = ?, description = ?, supervisor_agent_id = ?,
                aggregation_prompt = ?, acceptance_json = ?, budget_json = ?, enabled = ?,
                visibility = ?, permissions_json = ?, updated_at = ? WHERE id = ?
            """,
            (
                merged["name"], merged.get("description", ""), merged["supervisor_agent_id"],
                merged.get("aggregation_prompt", ""), db.json_dumps(merged.get("acceptance", [])),
                db.json_dumps(merged.get("budget", {})), 1 if merged.get("enabled", True) else 0,
                merged.get("visibility", "organization"), db.json_dumps(merged.get("permissions", {})),
                db.utc_now(), team_id,
            ),
        )
        if "members" in changes:
            self._replace_members(team_id, members)
        return self.get_team(team_id, scope) or {}

    def delete_team(self, team_id: str, scope: ExecutionScope) -> None:
        row = db.query_one("SELECT * FROM agent_teams WHERE id = ?", (team_id,))
        if not row or not self._visible(row, scope):
            raise ExpertNotFoundError("专家团不存在")
        if not self._owned(row, scope):
            raise ExpertPermissionError("只有专家团所有者可以删除")
        if db.query_one("SELECT id FROM team_runs WHERE team_id = ? LIMIT 1", (team_id,)):
            raise ExpertConflictError("专家团已有运行审计记录，请停用而不是删除")
        db.execute("DELETE FROM agent_team_members WHERE team_id = ?", (team_id,))
        db.execute("DELETE FROM agent_teams WHERE id = ?", (team_id,))

    # -- Team runs -----------------------------------------------------

    @staticmethod
    def _member_run_api(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **{
                key: value
                for key, value in row.items()
                if key not in {"output_json", "error_json", "permissions_json", "input_json"}
            },
            "output": _json(row.get("output_json"), {}),
            "error": _json(row.get("error_json"), {}),
            "permissions": _json(row.get("permissions_json"), {}),
            "input": _json(row.get("input_json"), {}),
        }

    def _run_api(self, row: dict[str, Any], *, include_members: bool = True) -> dict[str, Any]:
        result = {
            **{key: value for key, value in row.items() if key not in {"result_json", "error_json"}},
            "result": _json(row.get("result_json"), {}),
            "error": _json(row.get("error_json"), {}),
        }
        if include_members:
            result["member_runs"] = [
                self._member_run_api(item)
                for item in db.query_all(
                    "SELECT * FROM team_member_runs WHERE team_run_id = ? ORDER BY created_at, attempt",
                    (row["id"],),
                )
            ]
        return result

    def get_team_run(self, team_run_id: str, scope: ExecutionScope) -> dict[str, Any] | None:
        row = db.query_one("SELECT * FROM team_runs WHERE id = ?", (team_run_id,))
        if not row:
            return None
        if (
            row.get("organization_id") != scope.organization_id
            or row.get("workspace_id") != scope.workspace_id
            or row.get("user_id") != scope.user_id
        ):
            return None
        return self._run_api(row)

    def list_team_runs(self, team_id: str, scope: ExecutionScope) -> list[dict[str, Any]]:
        team = self.get_team(team_id, scope)
        if not team:
            raise ExpertNotFoundError("专家团不存在")
        rows = db.query_all(
            """SELECT * FROM team_runs WHERE team_id = ? AND organization_id = ?
               AND workspace_id = ? AND user_id = ? ORDER BY created_at DESC""",
            (team_id, scope.organization_id, scope.workspace_id, scope.user_id),
        )
        return [self._run_api(row) for row in rows]

    def queued_runs_for_recovery(self) -> list[dict[str, str]]:
        """Return durable team submissions that were never picked up.

        Creating the parent task and team-run record happens before the HTTP
        handler schedules background work.  A process exit in that narrow
        window must not leave a task permanently displayed as queued.  Active
        member/supervisor recovery is intentionally handled separately; this
        method only resumes runs that have not started yet.
        """
        return [
            {"id": str(row["id"]), "parent_task_id": str(row["parent_task_id"])}
            for row in db.query_all(
                """SELECT id, parent_task_id FROM team_runs
                   WHERE status = 'queued' ORDER BY created_at, id"""
            )
        ]

    def create_task_and_run(
        self,
        team_id: str,
        scope: ExecutionScope,
        *,
        message: str,
        model_id: str | None = None,
        conversation_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        team = self.get_team(team_id, scope)
        if not team or not team.get("enabled"):
            raise ExpertNotFoundError("专家团不存在、不可见或已停用")
        parent = create_task_record(
            message,
            team["supervisor_agent_id"],
            scope.workspace_id,
            model_id=model_id,
            conversation_id=conversation_id,
            organization_id=scope.organization_id,
            user_id=scope.user_id,
            executor_type="team",
            executor_id=team_id,
        )
        parent_run = self.task_state.create_run(
            parent["id"],
            metadata={"executor_type": "team", "executor_id": team_id, "trigger": "user"},
        )
        team_run = self.create_run_for_task(team_id, parent["id"], scope, parent_run_id=parent_run["id"])
        return parent, parent_run, team_run

    def create_run_for_task(
        self,
        team_id: str,
        parent_task_id: str,
        scope: ExecutionScope,
        *,
        parent_run_id: str = "",
    ) -> dict[str, Any]:
        team = self.get_team(team_id, scope)
        if not team or not team.get("enabled"):
            raise ExpertNotFoundError("专家团不存在、不可见或已停用")
        parent = db.query_one("SELECT * FROM tasks WHERE id = ?", (parent_task_id,))
        if not parent:
            raise ExpertNotFoundError("父任务不存在")
        if str(parent.get("executor_id") or team_id) != team_id:
            raise ExpertValidationError("父任务绑定的专家团与请求不一致")
        team_run_id = _new_id("xrun")
        now = db.utc_now()
        db.execute(
            """
            INSERT INTO team_runs(
                id, team_id, parent_task_id, status, result_json, error_json,
                started_at, finished_at, created_at, updated_at, organization_id,
                workspace_id, user_id, parent_run_id
            ) VALUES (?, ?, ?, 'queued', '{}', '{}', '', '', ?, ?, ?, ?, ?, ?)
            """,
            (
                team_run_id, team_id, parent_task_id, now, now, scope.organization_id,
                scope.workspace_id, scope.user_id, parent_run_id,
            ),
        )
        plan = {
            "goal": str(parent.get("message") or ""),
            "goal_confirmation": {
                "status": "confirmed",
                "label": "已选择专家协作",
                "message": f"由 {team['name']} 的成员独立分析，再由主管汇总验收。",
            },
            "acceptance_criteria": [
                {
                    "id": "team-response",
                    "title": "已生成可交付的最终答复",
                    "status": "pending",
                },
                *[
                    {
                        "id": (
                            str(item.get("id") or "").strip()
                            if isinstance(item, dict)
                            else ""
                        ) or f"team-criterion-{index + 1}",
                        "title": _acceptance_title(item, index),
                        "status": "pending",
                    }
                    for index, item in enumerate(team.get("acceptance") or [])
                ],
            ],
            "nodes": [
                {
                    "id": "understand",
                    "title": "确认目标与专家分工",
                    "status": "pending",
                    "children": [],
                },
                {
                    "id": "execute",
                    "title": "专家并行分析",
                    "status": "pending",
                    "children": [
                        {
                            "id": f"expert:{member['id']}",
                            "title": str(member.get("role") or member.get("agent_id") or "专家"),
                            "kind": "agent",
                            "status": "pending",
                        }
                        for member in team["members"]
                    ],
                },
                {
                    "id": "validate",
                    "title": "主管汇总与验收",
                    "status": "pending",
                    "children": [],
                },
            ],
            "tool_node_id": "execute",
            "executor_type": "team",
            "team_id": team_id,
        }
        emit(
            parent_task_id,
            "plan",
            "专家协作计划",
            f"{len(team['members'])} 位专家并行分析，主管随后汇总。",
            {"plan": plan},
        )
        emit(
            parent_task_id,
            "team_queued",
            "专家团任务已排队",
            f"将由 {len(team['members'])} 位成员并行执行，再由主管汇总。",
            {"team_id": team_id, "team_run_id": team_run_id, "member_count": len(team["members"])},
        )
        return self._run_api(db.query_one("SELECT * FROM team_runs WHERE id = ?", (team_run_id,)) or {})

    @staticmethod
    def _emit_team_progress(
        task_id: str,
        node_id: str,
        status: str,
        content: str,
        *,
        child_id: str = "",
        child_title: str = "",
    ) -> None:
        data: dict[str, Any] = {"node_id": node_id, "status": status}
        if child_id:
            data.update({
                "child_id": child_id,
                "child_title": child_title or child_id,
                "child_kind": "agent",
            })
        emit(task_id, "plan_progress", "专家协作进度", content, data)

    def _scope_for_run(self, run: dict[str, Any]) -> ExecutionScope:
        return ExecutionScope(
            organization_id=run.get("organization_id") or "local-org",
            workspace_id=run.get("workspace_id") or "default",
            user_id=run.get("user_id") or "local-user",
        )

    def _create_member_attempt(
        self,
        team_run: dict[str, Any],
        team: dict[str, Any],
        member: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        parent = db.query_one("SELECT * FROM tasks WHERE id = ?", (team_run["parent_task_id"],)) or {}
        goal_snapshot = str(
            (team_run.get("result") or {}).get("goal_snapshot")
            or parent.get("message")
            or ""
        )
        latest = db.query_one(
            "SELECT COALESCE(MAX(attempt), 0) AS value FROM team_member_runs WHERE team_run_id = ? AND member_id = ?",
            (team_run["id"], member["id"]),
        ) or {"value": 0}
        attempt = int(latest.get("value") or 0) + 1
        conversation_id = f"team:{team_run['id']}:member:{member['id']}"
        role_prompt = str(member.get("member_prompt") or "请从你的专业角色独立分析并给出结论与依据。")
        member_output_chars = _bounded_int(
            (team.get("budget") or {}).get("member_output_chars"), 1600, 500, 8000
        )
        message = (
            "你是专家团中的独立成员。不得假设或引用其他成员的输出。\n"
            f"共同目标：{goal_snapshot}\n"
            f"你的角色：{member.get('role') or 'member'}\n"
            f"你的分工：{role_prompt}\n"
            "只完成你的分工，并给主管提供可核验的结论。"
            f"正文控制在 {member_output_chars} 个中文字符以内，优先保留证据、风险、优先级和验收标准。"
        )
        child = create_task_record(
            message,
            member["agent_id"],
            team_run.get("workspace_id") or "default",
            model_id=parent.get("model_id") or None,
            conversation_id=conversation_id,
            organization_id=team_run.get("organization_id") or "local-org",
            user_id=team_run.get("user_id") or "local-user",
            parent_task_id=team_run["parent_task_id"],
            executor_type="team_member",
            executor_id=member["agent_id"],
        )
        child_run = self.task_state.create_run(
            child["id"],
            metadata={
                "team_run_id": team_run["id"], "member_id": member["id"],
                "member_attempt": attempt, "isolated_context": True,
            },
        )
        permissions = _merge_permissions(
            (self.get_agent(member["agent_id"], self._scope_for_run(team_run)) or {}).get("permissions", {}),
            team.get("permissions") if isinstance(team.get("permissions"), dict) else {},
            member.get("permissions") if isinstance(member.get("permissions"), dict) else {},
        )
        member_run_id = _new_id("xmrun")
        now = db.utc_now()
        db.execute(
            """
            INSERT INTO team_member_runs(
                id, team_run_id, member_id, child_task_id, attempt, status,
                output_json, error_json, started_at, finished_at, created_at, updated_at,
                conversation_id, permissions_json, input_json
            ) VALUES (?, ?, ?, ?, ?, 'queued', '{}', '{}', '', '', ?, ?, ?, ?, ?)
            """,
            (
                member_run_id, team_run["id"], member["id"], child["id"], attempt,
                now, now, conversation_id, db.json_dumps(permissions),
                db.json_dumps({"goal": goal_snapshot, "role": member.get("role", "member"), "member_prompt": role_prompt}),
            ),
        )
        row = db.query_one("SELECT * FROM team_member_runs WHERE id = ?", (member_run_id,)) or {}
        return self._member_run_api(row), child_run["id"]

    @staticmethod
    def _task_output(task_id: str) -> dict[str, Any]:
        child = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,)) or {}
        answer = db.query_one(
            "SELECT content FROM task_events WHERE task_id = ? AND type = 'answer' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ) or {}
        result = _json(child.get("result_json"), {})
        return {
            "task_id": task_id,
            "status": child.get("status", "failed"),
            "summary": str(answer.get("content") or result.get("summary") or ""),
            "result": result,
            "artifacts": _json(child.get("artifacts_json"), []),
        }

    async def _execute_member_attempt(
        self,
        member_run: dict[str, Any],
        child_run_id: str,
        parent_node_id: str = "",
    ) -> dict[str, Any]:
        member_run_id = member_run["id"]
        now = db.utc_now()
        db.execute(
            "UPDATE team_member_runs SET status = 'running', started_at = ?, updated_at = ? WHERE id = ?",
            (now, now, member_run_id),
        )
        if parent_node_id:
            self.task_state.start_node(parent_node_id, metadata={"member_run_id": member_run_id})
        await self.runtime.run_task(member_run["child_task_id"], run_id=child_run_id)
        output = self._task_output(member_run["child_task_id"])
        status = str(output.get("status") or "failed")
        member_status = status if status in {"completed", "failed", "cancelled", "waiting_approval"} else "failed"
        error = {} if member_status == "completed" else {
            "message": str((output.get("result") or {}).get("error") or f"成员任务状态为 {member_status}"),
            "child_task_id": member_run["child_task_id"],
        }
        finished = db.utc_now()
        db.execute(
            """UPDATE team_member_runs SET status = ?, output_json = ?, error_json = ?,
               finished_at = ?, updated_at = ? WHERE id = ?""",
            (member_status, db.json_dumps(output), db.json_dumps(error), finished, finished, member_run_id),
        )
        if parent_node_id:
            if member_status == "completed":
                self.task_state.finish_node(parent_node_id, output={"summary": output.get("summary", ""), "member_run_id": member_run_id})
            else:
                self.task_state.fail_node(parent_node_id, error, metadata={"member_run_id": member_run_id})
        return self._member_run_api(
            db.query_one("SELECT * FROM team_member_runs WHERE id = ?", (member_run_id,)) or {}
        )

    def _latest_member_runs(self, team_run_id: str) -> list[dict[str, Any]]:
        rows = db.query_all(
            "SELECT * FROM team_member_runs WHERE team_run_id = ? ORDER BY member_id, attempt DESC",
            (team_run_id,),
        )
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest.setdefault(str(row["member_id"]), row)
        return [self._member_run_api(row) for row in latest.values()]

    def _begin_parent_run(self, team_run: dict[str, Any], *, trigger: str) -> dict[str, Any]:
        parent_run_id = str(team_run.get("parent_run_id") or "")
        if not parent_run_id:
            created = self.task_state.create_run(
                team_run["parent_task_id"], metadata={"executor_type": "team", "team_run_id": team_run["id"], "trigger": trigger}
            )
            parent_run_id = created["id"]
            db.execute("UPDATE team_runs SET parent_run_id = ?, updated_at = ? WHERE id = ?", (parent_run_id, db.utc_now(), team_run["id"]))
        return self.task_state.begin_run(
            team_run["parent_task_id"], run_id=parent_run_id,
            metadata={"executor_type": "team", "team_run_id": team_run["id"], "trigger": trigger},
        )

    def _finish_parent_failed(self, parent_run_id: str, error: dict[str, Any]) -> None:
        current = self.task_state.get_run(parent_run_id)
        if current and current["status"] not in {"completed", "failed", "cancelled"}:
            self.task_state.finish_run(parent_run_id, status="failed", error=error)

    async def run_team(self, team_run_id: str) -> None:
        row = db.query_one("SELECT * FROM team_runs WHERE id = ?", (team_run_id,))
        if not row or row.get("status") != "queued":
            return
        team_run = self._run_api(row, include_members=False)
        scope = self._scope_for_run(team_run)
        team = self.get_team(team_run["team_id"], scope)
        if not team:
            self._fail_team_run(team_run, "专家团不存在或当前作用域无权访问")
            return
        parent_run: dict[str, Any] | None = None
        try:
            parent_run = self._begin_parent_run(team_run, trigger="team_start")
            parent = db.query_one(
                "SELECT * FROM tasks WHERE id = ?", (team_run["parent_task_id"],)
            ) or {}
            intent = await self.runtime.resolve_task_goal(parent)
            goal_snapshot = str(intent.get("standalone_request") or parent.get("message") or "")
            routing_result = {
                **(team_run.get("result") or {}),
                "goal_snapshot": goal_snapshot,
                "intent_resolution": intent,
            }
            db.execute(
                "UPDATE team_runs SET result_json = ?, updated_at = ? WHERE id = ?",
                (db.json_dumps(routing_result), db.utc_now(), team_run_id),
            )
            team_run["result"] = routing_result
            missing = [
                str(item).strip()
                for item in intent.get("missing_information") or []
                if str(item).strip()
            ]
            if missing:
                clarification = "在安排专家协作前，还需要你补充：" + "；".join(missing[:5]) + "。"
                result = {
                    **routing_result,
                    "summary": clarification,
                    "needs_clarification": True,
                    "missing_information": missing[:20],
                }
                finished = db.utc_now()
                db.execute(
                    """UPDATE team_runs SET status = 'completed', result_json = ?,
                       finished_at = ?, updated_at = ? WHERE id = ?""",
                    (db.json_dumps(result), finished, finished, team_run_id),
                )
                db.update_task_status(team_run["parent_task_id"], "completed", result=result)
                self._emit_team_progress(
                    team_run["parent_task_id"], "understand", "completed",
                    "已确认需要补充关键信息，暂不启动专家成员。",
                )
                emit(
                    team_run["parent_task_id"], "clarification", "需要补充信息",
                    clarification, {"missing_information": missing[:20]},
                )
                emit(team_run["parent_task_id"], "answer", "请补充一下", clarification)
                emit(team_run["parent_task_id"], "done", "等待补充", "补充后可在当前对话重新提交专家任务。")
                current = self.task_state.get_run(parent_run["id"])
                if current and current["status"] not in {"completed", "failed", "cancelled"}:
                    self.task_state.finish_run(parent_run["id"], result=result)
                return
            self._emit_team_progress(
                team_run["parent_task_id"], "understand", "completed",
                "已结合当前对话确认目标，并固定本次专家分工。",
            )
            now = db.utc_now()
            db.execute(
                "UPDATE team_runs SET status = 'running', started_at = ?, updated_at = ? WHERE id = ?",
                (now, now, team_run_id),
            )
            db.update_task_status(team_run["parent_task_id"], "running")
            self._emit_team_progress(
                team_run["parent_task_id"], "execute", "running",
                f"{len(team['members'])} 位专家正在独立并行分析。",
            )
            emit(
                team_run["parent_task_id"], "team_parallel_start", "专家成员并行执行中",
                f"{len(team['members'])} 位成员已同时启动，每位成员使用独立对话上下文。",
                {"team_run_id": team_run_id, "member_ids": [item["id"] for item in team["members"]]},
            )
            attempts: list[tuple[dict[str, Any], str, str]] = []
            for index, member in enumerate(team["members"]):
                member_run, child_run_id = self._create_member_attempt(team_run, team, member)
                self._emit_team_progress(
                    team_run["parent_task_id"], "execute", "running",
                    f"{member.get('role') or '专家'}已开始分析。",
                    child_id=f"expert:{member['id']}",
                    child_title=str(member.get("role") or member["agent_id"]),
                )
                node = self.task_state.create_node(
                    parent_run["id"], f"expert:{member['id']}", f"{member.get('role') or '专家'} · {member['agent_id']}",
                    kind="agent", sequence=index + 1,
                    input_data={"member_id": member["id"], "child_task_id": member_run["child_task_id"]},
                    metadata={"execution_mode": "parallel", "isolated_context": True},
                )
                attempts.append((member_run, child_run_id, node["id"]))
            # Creating every child before scheduling and awaiting one gather is
            # intentional: no member waits for another member to finish.
            await asyncio.gather(
                *(self._execute_member_attempt(member_run, child_run_id, node_id) for member_run, child_run_id, node_id in attempts)
            )
            latest_by_member = {
                item["member_id"]: item for item in self._latest_member_runs(team_run_id)
            }
            for member in team["members"]:
                member_status = str((latest_by_member.get(member["id"]) or {}).get("status") or "failed")
                public_status = "completed" if member_status == "completed" else "failed"
                self._emit_team_progress(
                    team_run["parent_task_id"], "execute", public_status,
                    f"{member.get('role') or '专家'}{'已完成分析' if public_status == 'completed' else '未能完成分析'}。",
                    child_id=f"expert:{member['id']}",
                    child_title=str(member.get("role") or member["agent_id"]),
                )
            await self._complete_or_pause(team_run_id, parent_run["id"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_team_run(team_run, str(exc))
            if parent_run:
                self._finish_parent_failed(parent_run["id"], {"message": str(exc), "error_type": exc.__class__.__name__})

    async def _complete_or_pause(self, team_run_id: str, parent_run_id: str) -> None:
        raw_run = db.query_one("SELECT * FROM team_runs WHERE id = ?", (team_run_id,)) or {}
        team_run = self._run_api(raw_run, include_members=False)
        latest = self._latest_member_runs(team_run_id)
        incomplete = [item for item in latest if item.get("status") != "completed"]
        if incomplete:
            waiting = any(item.get("status") == "waiting_approval" for item in incomplete)
            status = "waiting_approval" if waiting else "partial_failed"
            retryable = [item["id"] for item in incomplete if item.get("status") in {"failed", "cancelled"}]
            result = {
                **(team_run.get("result") or {}),
                "summary": "部分专家成员未完成，可单独重试失败成员。",
                "members": latest,
                "retryable_member_run_ids": retryable,
            }
            now = db.utc_now()
            db.execute(
                "UPDATE team_runs SET status = ?, result_json = ?, updated_at = ? WHERE id = ?",
                (status, db.json_dumps(result), now, team_run_id),
            )
            db.update_task_status(team_run["parent_task_id"], "failed", result=result)
            self._finish_parent_failed(parent_run_id, {"message": result["summary"], "retryable_member_run_ids": retryable})
            emit(
                team_run["parent_task_id"], "team_partial_failed", "专家团部分成员未完成",
                result["summary"], {"team_run_id": team_run_id, "retryable_member_run_ids": retryable},
            )
            self._emit_team_progress(
                team_run["parent_task_id"], "execute", "failed",
                "部分专家未完成，可在专家团运行记录中单独重试。",
            )
            return
        self._emit_team_progress(
            team_run["parent_task_id"], "execute", "completed",
            "全部专家已完成独立分析。",
        )
        await self._aggregate(team_run_id, parent_run_id)

    async def _aggregate(self, team_run_id: str, parent_run_id: str) -> None:
        raw_run = db.query_one("SELECT * FROM team_runs WHERE id = ?", (team_run_id,)) or {}
        team_run = self._run_api(raw_run, include_members=False)
        scope = self._scope_for_run(team_run)
        team = self.get_team(team_run["team_id"], scope)
        parent = db.query_one("SELECT * FROM tasks WHERE id = ?", (team_run["parent_task_id"],)) or {}
        if not team:
            raise ExpertNotFoundError("汇总时专家团不存在")
        goal_snapshot = str(
            (team_run.get("result") or {}).get("goal_snapshot")
            or parent.get("message")
            or ""
        )
        self._emit_team_progress(
            team_run["parent_task_id"], "validate", "running",
            "主管正在整合成员结论并逐项核对验收标准。",
        )
        member_runs = self._latest_member_runs(team_run_id)
        member_sections = []
        for item in member_runs:
            member = next((value for value in team["members"] if value["id"] == item["member_id"]), {})
            member_sections.append(
                f"### {member.get('role') or item['member_id']}\n"
                f"{str((item.get('output') or {}).get('summary') or '')}"
            )
        attempt = int(raw_run.get("aggregation_attempt") or 0) + 1
        supervisor_output_chars = _bounded_int(
            (team.get("budget") or {}).get("supervisor_output_chars"), 2400, 800, 12000
        )
        prompt = (
            "你是专家团主管。请只基于共同目标和下列成员交付进行最终汇总，"
            "明确一致结论、分歧、风险和最终建议，不展示内部思考过程。\n"
            f"共同目标：{goal_snapshot}\n"
            f"主管汇总要求：{team.get('aggregation_prompt') or '整合成员结论并给出可执行的最终答复。'}\n"
            f"验收标准：{db.json_dumps(team.get('acceptance', []))}\n"
            f"最终答复控制在 {supervisor_output_chars} 个中文字符以内，避免重复成员原文。\n\n"
            + "\n\n".join(member_sections)
        )
        supervisor_conversation = f"team:{team_run_id}:supervisor:{attempt}"
        child = create_task_record(
            prompt, team["supervisor_agent_id"], team_run.get("workspace_id") or "default",
            model_id=parent.get("model_id") or None, conversation_id=supervisor_conversation,
            organization_id=team_run.get("organization_id") or "local-org",
            user_id=team_run.get("user_id") or "local-user",
            parent_task_id=team_run["parent_task_id"], executor_type="team_supervisor",
            executor_id=team["supervisor_agent_id"],
        )
        child_run = self.task_state.create_run(
            child["id"], metadata={"team_run_id": team_run_id, "role": "supervisor", "aggregation_attempt": attempt}
        )
        node = self.task_state.create_node(
            parent_run_id, "supervisor:aggregate", "主管汇总与验收", kind="agent",
            input_data={"member_run_ids": [item["id"] for item in member_runs], "child_task_id": child["id"]},
            metadata={"aggregation_attempt": attempt},
        )
        self.task_state.start_node(node["id"])
        now = db.utc_now()
        db.execute(
            """UPDATE team_runs SET status = 'aggregating', supervisor_child_task_id = ?,
               aggregation_attempt = ?, updated_at = ? WHERE id = ?""",
            (child["id"], attempt, now, team_run_id),
        )
        emit(
            team_run["parent_task_id"], "team_aggregating", "主管正在汇总",
            "全部成员已完成，主管正在整合独立结论并执行最终验收。",
            {"team_run_id": team_run_id, "supervisor_task_id": child["id"]},
        )
        await self.runtime.run_task(child["id"], run_id=child_run["id"])
        supervisor = self._task_output(child["id"])
        if supervisor.get("status") != "completed":
            error = {"message": "主管汇总任务失败", "supervisor": supervisor}
            self.task_state.fail_node(node["id"], error)
            self._fail_team_run(team_run, error["message"], error=error)
            self._finish_parent_failed(parent_run_id, error)
            self._emit_team_progress(
                team_run["parent_task_id"], "validate", "failed", error["message"]
            )
            return
        artifacts: list[dict[str, Any]] = []
        seen_artifacts: set[str] = set()
        for output in [*(item.get("output") or {} for item in member_runs), supervisor]:
            for artifact in output.get("artifacts") or []:
                key = str(artifact.get("id") or artifact.get("name") or "")
                if key and key not in seen_artifacts:
                    artifacts.append(artifact)
                    seen_artifacts.add(key)
        result = {
            "summary": supervisor.get("summary", ""),
            "team_run_id": team_run_id,
            "team_id": team_run["team_id"],
            "members": member_runs,
            "supervisor": supervisor,
            "acceptance": team.get("acceptance", []),
            "goal_snapshot": goal_snapshot,
            "intent_resolution": (team_run.get("result") or {}).get("intent_resolution", {}),
        }
        validation = self._validate_team_acceptance(
            list(team.get("acceptance") or []),
            summary=str(result["summary"]),
            artifacts=artifacts,
            member_runs=member_runs,
            goal_snapshot=goal_snapshot,
        )
        result["validation"] = validation
        emit(
            team_run["parent_task_id"],
            "output_check",
            "专家团最终验收",
            validation["message"],
            validation,
        )
        if not validation["passed"]:
            error = {
                "message": validation["message"],
                "error_type": "acceptance_failed",
                "validation": validation,
                "supervisor_task_id": child["id"],
            }
            self.task_state.fail_node(node["id"], error)
            finished = db.utc_now()
            db.execute(
                """UPDATE team_runs SET status = 'failed', result_json = ?, error_json = ?,
                   finished_at = ?, updated_at = ? WHERE id = ?""",
                (
                    db.json_dumps(result), db.json_dumps(error), finished, finished,
                    team_run_id,
                ),
            )
            db.update_task_status(
                team_run["parent_task_id"],
                "failed",
                result={**result, "error": validation["message"]},
                artifacts=artifacts,
            )
            self._emit_team_progress(
                team_run["parent_task_id"], "validate", "failed", validation["message"]
            )
            emit(
                team_run["parent_task_id"],
                "team_acceptance_failed",
                "专家团验收未通过",
                validation["message"],
                {"team_run_id": team_run_id, "validation": validation},
            )
            emit(
                team_run["parent_task_id"],
                "error",
                "专家团验收未通过",
                validation["message"],
                {"team_run_id": team_run_id, "error_type": "acceptance_failed"},
            )
            self._finish_parent_failed(parent_run_id, error)
            return

        self.task_state.finish_node(
            node["id"],
            output={
                "summary": supervisor.get("summary", ""),
                "child_task_id": child["id"],
                "validation": validation,
            },
        )
        finished = db.utc_now()
        db.execute(
            """UPDATE team_runs SET status = 'completed', result_json = ?, error_json = '{}',
               finished_at = ?, updated_at = ? WHERE id = ?""",
            (db.json_dumps(result), finished, finished, team_run_id),
        )
        db.update_task_status(team_run["parent_task_id"], "completed", result=result, artifacts=artifacts)
        self._emit_team_progress(
            team_run["parent_task_id"], "validate", "completed",
            validation["message"],
        )
        emit(team_run["parent_task_id"], "answer", "专家团最终答复", str(result["summary"]), {"team_run_id": team_run_id})
        emit(
            team_run["parent_task_id"], "team_completed", "专家团任务已完成",
            f"{len(member_runs)} 位成员已完成，主管汇总通过。", {"team_run_id": team_run_id},
        )
        current = self.task_state.get_run(parent_run_id)
        if current and current["status"] not in {"completed", "failed", "cancelled"}:
            self.task_state.finish_run(parent_run_id, result=result)

    def _fail_team_run(
        self, team_run: dict[str, Any], message: str, *, error: dict[str, Any] | None = None
    ) -> None:
        payload = error or {"message": message}
        now = db.utc_now()
        db.execute(
            """UPDATE team_runs SET status = 'failed', error_json = ?, finished_at = ?,
               updated_at = ? WHERE id = ?""",
            (db.json_dumps(payload), now, now, team_run["id"]),
        )
        db.update_task_status(team_run["parent_task_id"], "failed", result={"error": message, "team_run_id": team_run["id"]})
        self._emit_team_progress(
            team_run["parent_task_id"], "execute", "failed",
            "专家协作未能完成，请查看错误信息后重试。",
        )
        emit(team_run["parent_task_id"], "error", "专家团任务失败", message, {"team_run_id": team_run["id"]})

    def validate_member_retry(
        self, team_run_id: str, member_run_id: str, scope: ExecutionScope
    ) -> dict[str, Any]:
        raw_team_run = db.query_one("SELECT * FROM team_runs WHERE id = ?", (team_run_id,))
        if not raw_team_run:
            raise ExpertNotFoundError("专家团运行不存在")
        team_run = self._run_api(raw_team_run, include_members=False)
        if self.get_team_run(team_run_id, scope) is None:
            raise ExpertNotFoundError("专家团运行不存在或不属于当前作用域")
        if team_run.get("status") not in {"partial_failed", "failed"}:
            raise ExpertConflictError("只有部分失败或失败状态的专家团运行可以重试成员")
        target = db.query_one(
            "SELECT * FROM team_member_runs WHERE id = ? AND team_run_id = ?",
            (member_run_id, team_run_id),
        )
        if not target:
            raise ExpertNotFoundError("成员运行不存在")
        latest = {item["member_id"]: item for item in self._latest_member_runs(team_run_id)}
        if latest.get(target["member_id"], {}).get("id") != member_run_id:
            raise ExpertConflictError("只能重试该成员最近一次运行")
        if target.get("status") not in {"failed", "cancelled"}:
            raise ExpertConflictError("该成员并非可重试的失败状态")
        return {"team_run": team_run, "target": self._member_run_api(target)}

    async def retry_member(self, team_run_id: str, member_run_id: str, scope: ExecutionScope) -> None:
        validated = self.validate_member_retry(team_run_id, member_run_id, scope)
        team_run = validated["team_run"]
        target = validated["target"]
        team = self.get_team(team_run["team_id"], scope)
        if not team:
            raise ExpertNotFoundError("专家团不存在")
        member = next((item for item in team["members"] if item["id"] == target["member_id"]), None)
        if not member:
            raise ExpertNotFoundError("专家团成员已不存在")
        parent_run = self.task_state.create_run(
            team_run["parent_task_id"],
            metadata={"executor_type": "team", "team_run_id": team_run_id, "trigger": "member_retry", "member_id": member["id"]},
        )
        db.execute(
            """UPDATE team_runs SET status = 'running', parent_run_id = ?, finished_at = '',
               updated_at = ? WHERE id = ?""",
            (parent_run["id"], db.utc_now(), team_run_id),
        )
        db.update_task_status(team_run["parent_task_id"], "queued")
        parent_run = self.task_state.begin_run(team_run["parent_task_id"], run_id=parent_run["id"])
        member_run, child_run_id = self._create_member_attempt(team_run, team, member)
        node = self.task_state.create_node(
            parent_run["id"], f"expert:{member['id']}:retry", f"单独重试 · {member.get('role') or member['agent_id']}",
            kind="agent", input_data={"member_id": member["id"], "previous_member_run_id": member_run_id},
            metadata={"single_member_retry": True},
        )
        emit(
            team_run["parent_task_id"], "team_member_retry", "正在单独重试失败成员",
            f"仅重试 {member.get('role') or member['agent_id']}，不会重跑已成功成员。",
            {"team_run_id": team_run_id, "previous_member_run_id": member_run_id, "new_member_run_id": member_run["id"]},
        )
        try:
            await self._execute_member_attempt(member_run, child_run_id, node["id"])
            await self._complete_or_pause(team_run_id, parent_run["id"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_team_run(team_run, str(exc))
            self._finish_parent_failed(parent_run["id"], {"message": str(exc), "error_type": exc.__class__.__name__})
