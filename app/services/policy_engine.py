"""Declarative lifecycle policy evaluation.

The policy engine supports two handler types:

* ``builtin_rule`` evaluates data-only match conditions locally.
* ``http`` calls an explicitly enabled and allow-listed policy endpoint.

There is no command, Python callback, template execution, or shell handler.  This
keeps tenant-authored policy documents incapable of executing code on the host.

The engine is persistence agnostic.  API/database code can store rules in any
shape it needs and pass dictionaries to :class:`PolicyEngine`.
"""

from __future__ import annotations

import copy
import fnmatch
import inspect
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

import httpx

from app.services.network_policy import require_outbound_network


SUPPORTED_LIFECYCLE_EVENTS = frozenset(
    {
        "task.created",
        "goal.resolved",
        "plan.created",
        "tool.before",
        "tool.after",
        "tool.failed",
        "approval.requested",
        "artifact.created",
        "output.before",
        "task.completed",
        "task.failed",
    }
)
SUPPORTED_HANDLER_TYPES = frozenset({"builtin_rule", "http"})
SUPPORTED_DECISIONS = frozenset({"allow", "deny", "require_approval", "modify", "add_context"})
SCOPE_ORDER = {"organization": 0, "workspace": 1, "agent": 2, "user": 3}

_SCOPE_ALIASES = {
    "org": "organization",
    "tenant": "organization",
    "project": "workspace",
}
_TERMINAL_DECISIONS = frozenset({"allow", "deny", "require_approval"})
_RESTRICTIVENESS = {"allow": 0, "require_approval": 1, "deny": 2}
_MISSING = object()
_SECRET_KEY_PARTS = ("api_key", "apikey", "authorization", "cookie", "password", "secret", "token")


class PolicyError(RuntimeError):
    """Base class for policy engine errors."""


class PolicyConfigurationError(PolicyError, ValueError):
    """Raised when a rule uses an unsupported or unsafe configuration."""


class PolicyDenied(PolicyError):
    """Raised by :meth:`PolicyEvaluation.raise_for_outcome` for a denial."""

    def __init__(self, evaluation: "PolicyEvaluation") -> None:
        self.evaluation = evaluation
        super().__init__(evaluation.summary)


class PolicyApprovalRequired(PolicyError):
    """Raised when a caller must pause and request user approval."""

    def __init__(self, evaluation: "PolicyEvaluation") -> None:
        self.evaluation = evaluation
        super().__init__(evaluation.summary)


@dataclass(frozen=True)
class PolicyRule:
    """Validated, data-only representation of a policy rule."""

    id: str
    name: str
    events: tuple[str, ...]
    scope: str
    scope_id: str | None
    priority: int
    handler_type: str
    handler: dict[str, Any]
    match: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    @property
    def event(self) -> str:
        """Return the sole event for single-event rules (the common case)."""

        return self.events[0]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PolicyRule":
        if not isinstance(value, Mapping):
            raise PolicyConfigurationError("Policy rule must be an object")

        rule_id = str(value.get("id") or "").strip()
        if not rule_id:
            raise PolicyConfigurationError("Policy rule id is required")

        raw_events = value.get("events", value.get("event"))
        if isinstance(raw_events, str):
            events = (raw_events.strip(),)
        elif isinstance(raw_events, Sequence) and not isinstance(raw_events, (bytes, bytearray)):
            events = tuple(str(item).strip() for item in raw_events if str(item).strip())
        else:
            events = ()
        if not events:
            raise PolicyConfigurationError(f"Rule {rule_id!r} must declare event or events")
        unsupported_events = sorted(set(events) - SUPPORTED_LIFECYCLE_EVENTS)
        if unsupported_events:
            raise PolicyConfigurationError(
                f"Rule {rule_id!r} uses unsupported lifecycle event(s): {', '.join(unsupported_events)}"
            )

        raw_scope = value.get("scope", "organization")
        scope_id = value.get("scope_id")
        if isinstance(raw_scope, Mapping):
            scope_id = raw_scope.get("id", scope_id)
            raw_scope = raw_scope.get("level", raw_scope.get("type", "organization"))
        scope = _SCOPE_ALIASES.get(str(raw_scope).strip().lower(), str(raw_scope).strip().lower())
        if scope not in SCOPE_ORDER:
            raise PolicyConfigurationError(
                f"Rule {rule_id!r} has unsupported scope {scope!r}; expected one of {', '.join(SCOPE_ORDER)}"
            )

        raw_handler = value.get("handler", {})
        if isinstance(raw_handler, str):
            handler = {"type": raw_handler}
        elif isinstance(raw_handler, Mapping):
            handler = copy.deepcopy(dict(raw_handler))
        else:
            raise PolicyConfigurationError(f"Rule {rule_id!r} handler must be an object")
        handler_type = str(handler.get("type") or value.get("handler_type") or "builtin_rule").strip().lower()
        if handler_type not in SUPPORTED_HANDLER_TYPES:
            raise PolicyConfigurationError(
                f"Rule {rule_id!r} handler {handler_type!r} is not supported; arbitrary code and shell handlers are forbidden"
            )
        handler["type"] = handler_type
        if "decision" not in handler and "decision" in value:
            handler["decision"] = value["decision"]
        if "reason" not in handler and "reason" in value:
            handler["reason"] = value["reason"]

        if handler_type == "builtin_rule":
            _validate_decision(handler.get("decision"), rule_id)
        else:
            url = str(handler.get("url") or "").strip()
            if not url:
                raise PolicyConfigurationError(f"HTTP rule {rule_id!r} requires handler.url")
            _validate_http_url(url, rule_id)
            if "on_error" in handler:
                on_error = _validate_decision(handler["on_error"], rule_id)
                if on_error not in {"deny", "require_approval"}:
                    raise PolicyConfigurationError(
                        f"HTTP rule {rule_id!r} on_error must be deny or require_approval to prevent fail-open policy bypass"
                    )

        raw_match = value.get("match", {})
        if raw_match is None:
            raw_match = {}
        if not isinstance(raw_match, Mapping):
            raise PolicyConfigurationError(f"Rule {rule_id!r} match must be an object")

        try:
            priority = int(value.get("priority", 0))
        except (TypeError, ValueError) as exc:
            raise PolicyConfigurationError(f"Rule {rule_id!r} priority must be an integer") from exc

        return cls(
            id=rule_id,
            name=str(value.get("name") or rule_id).strip(),
            events=events,
            scope=scope,
            scope_id=str(scope_id).strip() if scope_id not in (None, "") else None,
            priority=priority,
            handler_type=handler_type,
            handler=handler,
            match=copy.deepcopy(dict(raw_match)),
            enabled=bool(value.get("enabled", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "events": list(self.events),
            "scope": self.scope,
            "scope_id": self.scope_id,
            "priority": self.priority,
            "handler_type": self.handler_type,
            "handler": _redact(copy.deepcopy(self.handler)),
            "match": _redact(copy.deepcopy(self.match)),
            "enabled": self.enabled,
        }


@dataclass
class RuleDecision:
    """One matched rule's auditable result."""

    rule_id: str
    rule_name: str
    scope: str
    scope_id: str | None
    priority: int
    handler_type: str
    decision: str
    reason: str
    match_summary: dict[str, Any]
    modifications: dict[str, Any] = field(default_factory=dict)
    added_context: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    handler_status: str = "success"
    duration_ms: float = 0.0
    effective: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "priority": self.priority,
            "handler_type": self.handler_type,
            "decision": self.decision,
            "reason": self.reason,
            "match": copy.deepcopy(self.match_summary),
            "modifications": _redact(copy.deepcopy(self.modifications)),
            "added_context": _redact(copy.deepcopy(self.added_context)),
            "approval": _redact(copy.deepcopy(self.approval)),
            "metadata": _redact(copy.deepcopy(self.metadata)),
            "handler_status": self.handler_status,
            "duration_ms": round(self.duration_ms, 3),
            "effective": self.effective,
        }


@dataclass
class PolicyEvaluation:
    """Effective decision plus sufficient provenance for an audit event."""

    evaluation_id: str
    event: str
    outcome: str
    decisions: list[RuleDecision]
    modifications: dict[str, Any]
    added_context: dict[str, Any]
    approval_requests: list[dict[str, Any]]
    rules_considered: int
    rules_matched: int
    created_at: str
    duration_ms: float

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"

    @property
    def denied(self) -> bool:
        return self.outcome == "deny"

    @property
    def requires_approval(self) -> bool:
        return self.outcome == "require_approval"

    @property
    def summary(self) -> str:
        effective_reasons = [item.reason for item in self.decisions if item.effective and item.reason]
        if effective_reasons:
            return "; ".join(dict.fromkeys(effective_reasons))
        if self.outcome == "deny":
            return "操作被平台安全策略拒绝"
        if self.outcome == "require_approval":
            return "操作需要用户审批"
        return "策略校验通过"

    def apply(self, context: Mapping[str, Any]) -> dict[str, Any]:
        """Return a patched copy of context without mutating the input.

        Callers should enforce the terminal outcome before consuming the
        returned value. This lets a runtime persist an approval pause before
        continuing with the same evaluated patch.
        """

        if not isinstance(context, Mapping):
            raise PolicyConfigurationError("Policy context must be an object")

        result = copy.deepcopy(dict(context))
        _deep_merge(result, self.modifications)
        _deep_add_missing(result, self.added_context)
        return result

    def raise_for_outcome(self) -> None:
        if self.denied:
            raise PolicyDenied(self)
        if self.requires_approval:
            raise PolicyApprovalRequired(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "event": self.event,
            "outcome": self.outcome,
            "allowed": self.allowed,
            "denied": self.denied,
            "requires_approval": self.requires_approval,
            "summary": self.summary,
            "decisions": [item.to_dict() for item in self.decisions],
            "modifications": _redact(copy.deepcopy(self.modifications)),
            "added_context": _redact(copy.deepcopy(self.added_context)),
            "approval_requests": _redact(copy.deepcopy(self.approval_requests)),
            "rules_considered": self.rules_considered,
            "rules_matched": self.rules_matched,
            "created_at": self.created_at,
            "duration_ms": round(self.duration_ms, 3),
        }


HTTPDispatcher = Callable[
    [str, dict[str, Any], float, Mapping[str, str]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


class PolicyEngine:
    """Evaluate lifecycle events against declarative, hierarchical rules.

    Within one scope, the highest priority terminal decision is effective.  A
    tie is resolved toward the more restrictive outcome.  Across scopes, the
    most restrictive decision wins.  Consequently an organization-level deny
    or approval requirement can never be relaxed by workspace/agent/user rules,
    while a lower scope may still make an organization allow more restrictive.
    """

    def __init__(
        self,
        rules: Iterable[PolicyRule | Mapping[str, Any]] = (),
        *,
        http_enabled: bool = False,
        http_allowlist: Iterable[str] = (),
        http_timeout_seconds: float = 3.0,
        http_max_response_bytes: int = 256 * 1024,
        http_dispatcher: HTTPDispatcher | None = None,
    ) -> None:
        self.http_enabled = bool(http_enabled)
        self.http_allowlist = tuple(str(item).strip() for item in http_allowlist if str(item).strip())
        self.http_timeout_seconds = max(0.1, min(float(http_timeout_seconds), 30.0))
        self.http_max_response_bytes = max(1024, min(int(http_max_response_bytes), 2 * 1024 * 1024))
        self.http_dispatcher = http_dispatcher
        self._rules: list[PolicyRule] = []
        self.set_rules(rules)

    def set_rules(self, rules: Iterable[PolicyRule | Mapping[str, Any]]) -> None:
        validated = [_coerce_rule(item) for item in rules]
        ids = [item.id for item in validated]
        if len(ids) != len(set(ids)):
            raise PolicyConfigurationError("Policy rule ids must be unique")
        self._rules = validated

    def add_rule(self, rule: PolicyRule | Mapping[str, Any]) -> PolicyRule:
        validated = _coerce_rule(rule)
        if any(item.id == validated.id for item in self._rules):
            raise PolicyConfigurationError(f"Policy rule id {validated.id!r} already exists")
        self._rules.append(validated)
        return validated

    def remove_rule(self, rule_id: str) -> bool:
        before = len(self._rules)
        self._rules = [item for item in self._rules if item.id != rule_id]
        return len(self._rules) != before

    def list_rules(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._sorted_rules(self._rules)]

    async def evaluate(
        self,
        event: str,
        context: Mapping[str, Any] | None = None,
        *,
        rules: Iterable[PolicyRule | Mapping[str, Any]] | None = None,
    ) -> PolicyEvaluation:
        """Evaluate an event and return (but do not itself enforce) the result."""

        started = time.perf_counter()
        event = str(event).strip()
        if event not in SUPPORTED_LIFECYCLE_EVENTS:
            raise PolicyConfigurationError(f"Unsupported lifecycle event: {event!r}")
        if context is None:
            context = {}
        if not isinstance(context, Mapping):
            raise PolicyConfigurationError("Policy context must be an object")

        selected_rules = self._rules if rules is None else [_coerce_rule(item) for item in rules]
        eligible = [item for item in selected_rules if item.enabled and event in item.events]
        records: list[RuleDecision] = []
        for rule in self._sorted_rules(eligible):
            if not _scope_matches(rule, context):
                continue
            matched, match_summary = _rule_matches(rule, event, context)
            if not matched:
                continue
            record_started = time.perf_counter()
            try:
                if rule.handler_type == "builtin_rule":
                    raw_result = rule.handler
                else:
                    raw_result = await self._evaluate_http(rule, event, context)
                record = _decision_from_result(rule, raw_result, match_summary)
            except Exception as exc:  # a remote policy outage must fail closed and stay auditable
                fallback = str(rule.handler.get("on_error") or "deny").strip().lower()
                _validate_decision(fallback, rule.id)
                record = _decision_from_result(
                    rule,
                    {
                        "decision": fallback,
                        "reason": f"策略处理器不可用：{_safe_error(exc)}",
                        "metadata": {"error_type": exc.__class__.__name__},
                    },
                    match_summary,
                )
                record.handler_status = "error"
            record.duration_ms = (time.perf_counter() - record_started) * 1000
            records.append(record)

        outcome = _resolve_terminal_outcome(records)
        modifications = _merge_patches(records, "modifications")
        added_context = _merge_patches(records, "added_context")
        approval_requests = []
        for record in records:
            if outcome == "require_approval" and record.effective and record.decision == "require_approval":
                approval_requests.append(
                    {
                        "rule_id": record.rule_id,
                        "rule_name": record.rule_name,
                        "reason": record.reason or "操作需要审批",
                        **copy.deepcopy(record.approval),
                    }
                )

        return PolicyEvaluation(
            evaluation_id=str(uuid.uuid4()),
            event=event,
            outcome=outcome,
            decisions=records,
            modifications=modifications,
            added_context=added_context,
            approval_requests=approval_requests,
            rules_considered=len(eligible),
            rules_matched=len(records),
            created_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    async def _evaluate_http(
        self,
        rule: PolicyRule,
        event: str,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if not self.http_enabled:
            raise PolicyConfigurationError("HTTP policy handlers are disabled")
        url = str(rule.handler["url"]).strip()
        if not _url_is_allowlisted(url, self.http_allowlist):
            raise PolicyConfigurationError("HTTP policy URL is not in the configured allowlist")

        timeout = float(rule.handler.get("timeout_seconds", self.http_timeout_seconds))
        timeout = max(0.1, min(timeout, self.http_timeout_seconds, 30.0))
        headers = {"content-type": "application/json"}
        configured_headers = rule.handler.get("headers", {})
        if configured_headers:
            if not isinstance(configured_headers, Mapping):
                raise PolicyConfigurationError("HTTP policy headers must be an object")
            for key, value in configured_headers.items():
                normalized = str(key).strip().lower()
                if normalized in {"host", "content-length", "transfer-encoding", "connection"}:
                    raise PolicyConfigurationError(f"HTTP policy header {normalized!r} cannot be overridden")
                headers[str(key)] = str(value)

        payload = {
            "event": event,
            "context": _json_safe(_redact(copy.deepcopy(dict(context)))),
            "rule": {
                "id": rule.id,
                "name": rule.name,
                "scope": rule.scope,
                "scope_id": rule.scope_id,
                "priority": rule.priority,
            },
        }
        if self.http_dispatcher:
            result = self.http_dispatcher(url, payload, timeout, headers)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, Mapping):
                raise PolicyError("HTTP policy dispatcher returned a non-object response")
            return result

        require_outbound_network("HTTP Policy 处理器", error_type=PolicyConfigurationError)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            if len(response.content) > self.http_max_response_bytes:
                raise PolicyError("HTTP policy response is too large")
            result = response.json()
        if not isinstance(result, Mapping):
            raise PolicyError("HTTP policy endpoint returned a non-object response")
        return result

    @staticmethod
    def _sorted_rules(rules: Iterable[PolicyRule]) -> list[PolicyRule]:
        return sorted(rules, key=lambda item: (SCOPE_ORDER[item.scope], -item.priority, item.id))


def _coerce_rule(value: PolicyRule | Mapping[str, Any]) -> PolicyRule:
    if isinstance(value, PolicyRule):
        return value
    return PolicyRule.from_dict(value)


def _validate_decision(decision: Any, rule_id: str) -> str:
    normalized = str(decision or "").strip().lower()
    if normalized not in SUPPORTED_DECISIONS:
        raise PolicyConfigurationError(
            f"Rule {rule_id!r} must use one of these decisions: {', '.join(sorted(SUPPORTED_DECISIONS))}"
        )
    return normalized


def _validate_http_url(url: str, rule_id: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PolicyConfigurationError(f"HTTP rule {rule_id!r} must use an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PolicyConfigurationError(
            f"HTTP rule {rule_id!r} URL cannot contain credentials, query parameters, or a fragment"
        )
    try:
        parsed.port
    except ValueError as exc:
        raise PolicyConfigurationError(f"HTTP rule {rule_id!r} URL contains an invalid port") from exc


def _scope_matches(rule: PolicyRule, context: Mapping[str, Any]) -> bool:
    if not rule.scope_id:
        return True
    aliases = {
        "organization": ("organization_id", "org_id", "tenant_id"),
        "workspace": ("workspace_id", "project_id"),
        "agent": ("agent_id",),
        "user": ("user_id",),
    }[rule.scope]
    scope_context = context.get("scope", {})
    if not isinstance(scope_context, Mapping):
        scope_context = {}
    for key in aliases:
        actual = context.get(key, scope_context.get(key, _MISSING))
        if actual is not _MISSING and str(actual) == rule.scope_id:
            return True
    return False


def _rule_matches(
    rule: PolicyRule,
    event: str,
    context: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    match = rule.match
    server, tool, arguments = _tool_context(context)
    summary: dict[str, Any] = {"event": event}
    if server is not _MISSING and server:
        summary["server"] = server
    if tool is not _MISSING and tool:
        summary["tool"] = tool

    server_condition = match.get("server", match.get("server_id", _MISSING))
    if server_condition is not _MISSING and not _condition_matches(server, server_condition):
        return False, summary
    tool_condition = match.get("tool", match.get("tool_name", _MISSING))
    if tool_condition is not _MISSING and not _condition_matches(tool, tool_condition):
        return False, summary

    argument_match = match.get("arguments", match.get("params", _MISSING))
    if argument_match is not _MISSING:
        if not isinstance(argument_match, Mapping):
            raise PolicyConfigurationError(f"Rule {rule.id!r} argument matcher must be an object")
        argument_paths: list[str] = []
        if not _mapping_conditions_match(arguments, argument_match, argument_paths, prefix=""):
            return False, summary
        summary["argument_condition_paths"] = sorted(argument_paths)

    conditions = match.get("conditions", ())
    condition_paths: list[str] = []
    if conditions:
        if isinstance(conditions, Mapping):
            iterable_conditions = [
                {"path": path, **(dict(expected) if isinstance(expected, Mapping) and "op" in expected else {"value": expected})}
                for path, expected in conditions.items()
            ]
        elif isinstance(conditions, Sequence) and not isinstance(conditions, (str, bytes, bytearray)):
            iterable_conditions = list(conditions)
        else:
            raise PolicyConfigurationError(f"Rule {rule.id!r} conditions must be an object or list")
        for condition in iterable_conditions:
            if not isinstance(condition, Mapping) or not str(condition.get("path") or "").strip():
                raise PolicyConfigurationError(f"Rule {rule.id!r} contains an invalid condition")
            path = str(condition["path"]).strip()
            actual = _get_path(context, path)
            expected = {key: value for key, value in condition.items() if key != "path"}
            if "op" not in expected:
                expected = expected.get("value")
            if not _condition_matches(actual, expected):
                return False, summary
            condition_paths.append(path)
        summary["condition_paths"] = sorted(condition_paths)

    reserved = {"server", "server_id", "tool", "tool_name", "arguments", "params", "conditions"}
    generic_match = {key: value for key, value in match.items() if key not in reserved}
    generic_paths: list[str] = []
    if generic_match and not _mapping_conditions_match(context, generic_match, generic_paths, prefix=""):
        return False, summary
    if generic_paths:
        summary["context_condition_paths"] = sorted(generic_paths)
    return True, summary


def _tool_context(context: Mapping[str, Any]) -> tuple[Any, Any, Mapping[str, Any]]:
    tool_value = context.get("tool", {})
    tool_object = tool_value if isinstance(tool_value, Mapping) else {}
    server = context.get(
        "server_id",
        context.get("server", tool_object.get("server_id", tool_object.get("server", _MISSING))),
    )
    name = context.get(
        "tool_name",
        tool_value if isinstance(tool_value, str) else tool_object.get("name", tool_object.get("tool_name", _MISSING)),
    )
    arguments = context.get(
        "arguments",
        context.get("params", tool_object.get("arguments", tool_object.get("params", {}))),
    )
    if not isinstance(arguments, Mapping):
        arguments = {}
    return server, name, arguments


def _mapping_conditions_match(
    actual: Mapping[str, Any],
    expected: Mapping[str, Any],
    paths: list[str],
    *,
    prefix: str,
) -> bool:
    if not isinstance(actual, Mapping):
        return False
    for key, expectation in expected.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        paths.append(path)
        value = actual.get(key, _MISSING)
        if isinstance(expectation, Mapping) and not _is_operator_mapping(expectation):
            if not _mapping_conditions_match(value, expectation, paths, prefix=path):
                return False
        elif not _condition_matches(value, expectation):
            return False
    return True


def _is_operator_mapping(value: Mapping[str, Any]) -> bool:
    operator_keys = {
        "op",
        "eq",
        "equals",
        "ne",
        "not_equals",
        "in",
        "not_in",
        "one_of",
        "contains",
        "not_contains",
        "starts_with",
        "prefix",
        "ends_with",
        "suffix",
        "exists",
        "gt",
        "gte",
        "lt",
        "lte",
        "glob",
    }
    return bool(set(value) & operator_keys)


def _condition_matches(actual: Any, expectation: Any) -> bool:
    if isinstance(expectation, Sequence) and not isinstance(expectation, (str, bytes, bytearray, Mapping)):
        return any(_condition_matches(actual, item) for item in expectation)
    if not isinstance(expectation, Mapping):
        if actual is _MISSING:
            return False
        if isinstance(expectation, str) and any(character in expectation for character in "*?["):
            return fnmatch.fnmatchcase(str(actual), expectation)
        return actual == expectation

    if "op" in expectation:
        op = str(expectation.get("op") or "").strip().lower()
        return _apply_operator(actual, op, expectation.get("value", _MISSING))
    for op, expected in expectation.items():
        normalized = str(op).strip().lower()
        if not _apply_operator(actual, normalized, expected):
            return False
    return True


def _apply_operator(actual: Any, op: str, expected: Any) -> bool:
    aliases = {
        "equals": "eq",
        "not_equals": "ne",
        "one_of": "in",
        "prefix": "starts_with",
        "suffix": "ends_with",
    }
    op = aliases.get(op, op)
    if op == "exists":
        return (actual is not _MISSING) is bool(expected)
    if actual is _MISSING:
        return False
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "in":
        try:
            return actual in expected
        except TypeError:
            return False
    if op == "not_in":
        try:
            return actual not in expected
        except TypeError:
            return False
    if op in {"contains", "not_contains"}:
        try:
            contained = expected in actual
        except TypeError:
            contained = False
        return contained if op == "contains" else not contained
    if op == "starts_with":
        return isinstance(actual, str) and actual.startswith(str(expected))
    if op == "ends_with":
        return isinstance(actual, str) and actual.endswith(str(expected))
    if op == "glob":
        return fnmatch.fnmatchcase(str(actual), str(expected))
    if op in {"gt", "gte", "lt", "lte"}:
        try:
            if op == "gt":
                return actual > expected
            if op == "gte":
                return actual >= expected
            if op == "lt":
                return actual < expected
            return actual <= expected
        except TypeError:
            return False
    raise PolicyConfigurationError(f"Unsupported declarative match operator: {op!r}")


def _decision_from_result(
    rule: PolicyRule,
    result: Mapping[str, Any],
    match_summary: dict[str, Any],
) -> RuleDecision:
    if not isinstance(result, Mapping):
        raise PolicyError("Policy handler result must be an object")
    decision = _validate_decision(result.get("decision"), rule.id)
    modifications = result.get("modifications", result.get("modify", {})) or {}
    added_context = result.get("added_context", result.get("context", {})) or {}
    approval = result.get("approval", {}) or {}
    metadata = result.get("metadata", {}) or {}
    for label, value in (
        ("modifications", modifications),
        ("added_context", added_context),
        ("approval", approval),
        ("metadata", metadata),
    ):
        if not isinstance(value, Mapping):
            raise PolicyError(f"Policy handler {label} must be an object")
    return RuleDecision(
        rule_id=rule.id,
        rule_name=rule.name,
        scope=rule.scope,
        scope_id=rule.scope_id,
        priority=rule.priority,
        handler_type=rule.handler_type,
        decision=decision,
        reason=str(result.get("reason") or "").strip(),
        match_summary=copy.deepcopy(match_summary),
        modifications=copy.deepcopy(dict(modifications)),
        added_context=copy.deepcopy(dict(added_context)),
        approval=copy.deepcopy(dict(approval)),
        metadata=copy.deepcopy(dict(metadata)),
    )


def _resolve_terminal_outcome(records: list[RuleDecision]) -> str:
    by_scope: dict[str, list[RuleDecision]] = {}
    for record in records:
        if record.decision in _TERMINAL_DECISIONS:
            by_scope.setdefault(record.scope, []).append(record)
        else:
            # A modify/add_context decision is effective when its patch is
            # merged, even though it does not influence the terminal outcome.
            record.effective = True

    effective: list[RuleDecision] = []
    for scope_records in by_scope.values():
        highest_priority = max(item.priority for item in scope_records)
        candidates = [item for item in scope_records if item.priority == highest_priority]
        most_restrictive = max(_RESTRICTIVENESS[item.decision] for item in candidates)
        winners = [item for item in candidates if _RESTRICTIVENESS[item.decision] == most_restrictive]
        for item in winners:
            item.effective = True
        effective.extend(winners)

    if not effective:
        return "allow"
    return max(effective, key=lambda item: _RESTRICTIVENESS[item.decision]).decision


def _merge_patches(records: list[RuleDecision], attribute: str) -> dict[str, Any]:
    """Merge low-authority/low-priority first so organization/high wins."""

    ordered = sorted(records, key=lambda item: (-SCOPE_ORDER[item.scope], item.priority, item.rule_id))
    result: dict[str, Any] = {}
    for record in ordered:
        if not record.effective:
            continue
        patch = getattr(record, attribute)
        if patch:
            _deep_merge(result, patch)
    return result


def _deep_merge(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _deep_add_missing(target: dict[str, Any], additions: Mapping[str, Any]) -> None:
    """Add context keys without overwriting original or modified values."""

    for key, value in additions.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(value, Mapping) and isinstance(target[key], dict):
            _deep_add_missing(target[key], value)


def _get_path(value: Any, path: str) -> Any:
    current = value
    for segment in path.split("."):
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _url_is_allowlisted(url: str, allowlist: Sequence[str]) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    if not _url_path_is_safe(parsed.path):
        return False
    candidate_host = parsed.hostname.lower().rstrip(".")
    candidate_port = parsed.port or (443 if parsed.scheme == "https" else 80)

    for entry in allowlist:
        entry = entry.strip()
        if not entry:
            continue
        if "://" not in entry:
            try:
                host_entry = urlsplit("//" + entry)
                entry_host = (host_entry.hostname or "").lower().rstrip(".")
                entry_port = host_entry.port
            except ValueError:
                continue
            if not entry_host:
                continue
            # A hostname-only entry authorizes standard HTTP(S) ports.  A
            # non-standard port must be explicit (host:port or full origin).
            if entry_port is None and candidate_port not in {80, 443}:
                continue
            if entry_port is not None and candidate_port != entry_port:
                continue
            if entry_host.startswith("*."):
                suffix = entry_host[1:]
                if candidate_host.endswith(suffix) and candidate_host != suffix[1:]:
                    return True
            elif candidate_host == entry_host:
                return True
            continue

        allowed = urlsplit(entry)
        if allowed.scheme not in {"http", "https"} or not allowed.hostname:
            continue
        if not _url_path_is_safe(allowed.path):
            continue
        allowed_host = allowed.hostname.lower().rstrip(".")
        allowed_port = allowed.port or (443 if allowed.scheme == "https" else 80)
        if parsed.scheme != allowed.scheme or candidate_host != allowed_host or candidate_port != allowed_port:
            continue
        allowed_path = allowed.path.rstrip("/")
        candidate_path = parsed.path or "/"
        if not allowed_path or allowed_path == "/":
            return True
        if candidate_path == allowed_path or candidate_path.startswith(allowed_path + "/"):
            return True
    return False


def _url_path_is_safe(path: str) -> bool:
    """Reject path forms that different HTTP stacks may normalize differently."""

    decoded = path
    for _ in range(3):
        decoded_once = unquote(decoded)
        if decoded_once == decoded:
            break
        decoded = decoded_once
    if "\\" in decoded:
        return False
    return all(segment not in {".", ".."} for segment in decoded.split("/"))


def _redact(value: Any, key: str = "") -> Any:
    normalized_key = key.lower().replace("-", "_")
    if normalized_key and any(part in normalized_key for part in _SECRET_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): _redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip().replace("\n", " ")
    return text[:240] if text else exc.__class__.__name__


__all__ = [
    "PolicyApprovalRequired",
    "PolicyConfigurationError",
    "PolicyDenied",
    "PolicyEngine",
    "PolicyError",
    "PolicyEvaluation",
    "PolicyRule",
    "RuleDecision",
    "SCOPE_ORDER",
    "SUPPORTED_DECISIONS",
    "SUPPORTED_HANDLER_TYPES",
    "SUPPORTED_LIFECYCLE_EVENTS",
]
