from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, StringConstraints, field_validator


ResourceId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=2,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]+$",
    ),
]
ResourceName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
]
McpKind = Literal["builtin", "http", "mcp_stdio", "stdio", "mcp_http", "streamable_http"]
ModelProvider = Literal["openai", "openai_compatible"]
ApiKeyMode = Literal["env", "direct"]


def _validate_http_base_url(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Base URL 必须是有效的 HTTP(S) 地址")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Base URL 不能包含账号、密码或 URL 片段")
    return value.rstrip("/")


class SkillCreate(BaseModel):
    id: ResourceId
    name: ResourceName
    description: str = Field(default="", max_length=2_000)
    category: str = Field(default="custom", min_length=1, max_length=80)
    version: str = Field(default="0.1.0", min_length=1, max_length=40)
    content: str = Field(..., max_length=2_000_000)
    enabled: bool = True
    required_mcps: list[ResourceId] = Field(default_factory=list, max_length=100)


class SkillUpdate(BaseModel):
    name: ResourceName | None = None
    description: str | None = Field(default=None, max_length=2_000)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    version: str | None = Field(default=None, min_length=1, max_length=40)
    content: str | None = Field(default=None, max_length=2_000_000)
    enabled: bool | None = None
    required_mcps: list[ResourceId] | None = Field(default=None, max_length=100)


class SkillFileUpdate(BaseModel):
    content: str


class AgentCreate(BaseModel):
    id: ResourceId
    name: ResourceName
    description: str = Field(default="", max_length=2_000)
    model: ResourceId = "deterministic"
    system_prompt: str = Field(default="", max_length=50_000)
    skills: list[ResourceId] = Field(default_factory=list, max_length=100)
    mcp_servers: list[ResourceId] = Field(default_factory=list, max_length=100)
    permissions: dict[str, Any] = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    name: ResourceName | None = None
    description: str | None = Field(default=None, max_length=2_000)
    model: ResourceId | None = None
    system_prompt: str | None = Field(default=None, max_length=50_000)
    skills: list[ResourceId] | None = Field(default=None, max_length=100)
    mcp_servers: list[ResourceId] | None = Field(default=None, max_length=100)
    permissions: dict[str, Any] | None = None


class ExpertTemplateCreate(BaseModel):
    id: str = Field(..., min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)
    version: str = Field(default="0.1.0", min_length=1, max_length=40)
    source: str = Field(default="local", min_length=1, max_length=200)
    manifest: dict[str, Any] = Field(default_factory=dict)
    organization_id: str = Field(default="local-org", min_length=1, max_length=128)
    workspace_id: str = Field(default="default", min_length=1, max_length=128)
    owner_user_id: str = Field(default="local-user", min_length=1, max_length=128)
    visibility: str = Field(default="organization", pattern="^(private|workspace|organization|public)$")
    permissions: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ExpertTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2_000)
    version: str | None = Field(default=None, min_length=1, max_length=40)
    source: str | None = Field(default=None, min_length=1, max_length=200)
    manifest: dict[str, Any] | None = None
    visibility: str | None = Field(default=None, pattern="^(private|workspace|organization|public)$")
    permissions: dict[str, Any] | None = None
    enabled: bool | None = None


class ExpertInstallRequest(BaseModel):
    installation_id: str | None = Field(default=None, min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    agent_id: str | None = Field(default=None, min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    organization_id: str = Field(default="local-org", min_length=1, max_length=128)
    workspace_id: str = Field(default="default", min_length=1, max_length=128)
    user_id: str = Field(default="local-user", min_length=1, max_length=128)
    visibility: str = Field(default="private", pattern="^(private|workspace|organization)$")
    permissions: dict[str, Any] = Field(default_factory=dict)
    overrides: dict[str, Any] = Field(default_factory=dict)


class ExpertTeamMemberCreate(BaseModel):
    id: str | None = Field(default=None, min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    agent_id: str = Field(..., min_length=2, max_length=80)
    role: str = Field(default="member", min_length=1, max_length=80)
    execution_mode: str = Field(default="parallel", pattern="^(parallel)$")
    depends_on: list[str] = Field(default_factory=list)
    member_prompt: str = Field(default="", max_length=8_000)
    position: int = Field(default=0, ge=0, le=10_000)
    permissions: dict[str, Any] = Field(default_factory=dict)


class ExpertTeamCreate(BaseModel):
    id: str = Field(..., min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)
    supervisor_agent_id: str = Field(..., min_length=2, max_length=80)
    aggregation_prompt: str = Field(default="", max_length=8_000)
    acceptance: list[dict[str, Any] | str] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    members: list[ExpertTeamMemberCreate] = Field(..., min_length=2, max_length=20)
    organization_id: str = Field(default="local-org", min_length=1, max_length=128)
    workspace_id: str = Field(default="default", min_length=1, max_length=128)
    owner_user_id: str = Field(default="local-user", min_length=1, max_length=128)
    visibility: str = Field(default="organization", pattern="^(private|workspace|organization)$")
    permissions: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ExpertTeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2_000)
    supervisor_agent_id: str | None = Field(default=None, min_length=2, max_length=80)
    aggregation_prompt: str | None = Field(default=None, max_length=8_000)
    acceptance: list[dict[str, Any] | str] | None = None
    budget: dict[str, Any] | None = None
    members: list[ExpertTeamMemberCreate] | None = Field(default=None, min_length=2, max_length=20)
    visibility: str | None = Field(default=None, pattern="^(private|workspace|organization)$")
    permissions: dict[str, Any] | None = None
    enabled: bool | None = None


class ExpertTeamRunCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=100_000)
    model_id: str | None = None
    conversation_id: str | None = None
    organization_id: str = Field(default="local-org", min_length=1, max_length=128)
    workspace_id: str = Field(default="default", min_length=1, max_length=128)
    user_id: str = Field(default="local-user", min_length=1, max_length=128)


class ExpertMemberRetryRequest(BaseModel):
    note: str = Field(default="", max_length=1_000)


class McpServerCreate(BaseModel):
    id: ResourceId
    name: ResourceName
    kind: McpKind = "http"
    description: str = Field(default="", max_length=2_000)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list)


class McpServerUpdate(BaseModel):
    name: ResourceName | None = None
    kind: McpKind | None = None
    description: str | None = Field(default=None, max_length=2_000)
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None


class SkillPathInstall(BaseModel):
    path: str
    enabled: bool = True


class RemoteInstall(BaseModel):
    url: str


class ModelConfigCreate(BaseModel):
    id: ResourceId
    name: ResourceName
    provider: ModelProvider = "openai_compatible"
    model: str = Field(..., min_length=1, max_length=200)
    base_url: str = Field(default="", max_length=2_000)
    api_key_env: str = Field(default="", max_length=200)
    api_key: str | None = None
    api_key_mode: ApiKeyMode = "env"
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)

    _normalise_base_url = field_validator("base_url")(_validate_http_base_url)


class ModelConfigUpdate(BaseModel):
    name: ResourceName | None = None
    provider: ModelProvider | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=2_000)
    api_key_env: str | None = Field(default=None, max_length=200)
    api_key: str | None = None
    api_key_mode: ApiKeyMode | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None

    @field_validator("base_url")
    @classmethod
    def normalise_base_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_base_url(value)


class TaskCreate(BaseModel):
    message: str = Field(..., min_length=1)
    agent_id: ResourceId = "general-agent"
    model_id: ResourceId | None = None
    conversation_id: str | None = None
    workspace: str = "default"
    organization_id: str = Field(default="local-org", min_length=1, max_length=128)
    user_id: str = Field(default="local-user", min_length=1, max_length=128)
    parent_task_id: str | None = None
    executor_type: str = Field(default="agent", pattern="^(agent|team)$")
    executor_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list)


class ToolInvokeRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None


class ApprovalRequest(BaseModel):
    approved: bool
    note: str = ""


class TaskCommandRequest(BaseModel):
    type: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskResumeRequest(BaseModel):
    checkpoint_id: str | None = None


class CheckpointRestoreRequest(BaseModel):
    note: str = ""


class PolicyRuleCreate(BaseModel):
    id: str = Field(..., min_length=2)
    name: str = ""
    event: str | None = None
    events: list[str] | None = None
    scope: str = "organization"
    scope_id: str | None = None
    priority: int = 0
    enabled: bool = True
    match: dict[str, Any] = Field(default_factory=dict)
    handler: dict[str, Any] = Field(default_factory=dict)


class PolicyRuleUpdate(BaseModel):
    name: str | None = None
    event: str | None = None
    events: list[str] | None = None
    scope: str | None = None
    scope_id: str | None = None
    priority: int | None = None
    enabled: bool | None = None
    match: dict[str, Any] | None = None
    handler: dict[str, Any] | None = None


class MemoryCreate(BaseModel):
    organization_id: str = Field(default="local-org", min_length=1, max_length=128)
    workspace_id: str = Field(default="default", min_length=1, max_length=128)
    user_id: str = Field(default="local-user", min_length=1, max_length=128)
    agent_id: str = ""
    conversation_id: str = ""
    scope_type: str = Field(default="user", pattern="^(organization|workspace|user|agent|conversation)$")
    kind: str = Field(default="preference", min_length=1, max_length=64)
    title: str = Field(default="", max_length=200)
    content: str = Field(..., min_length=1, max_length=20_000)
    tags: list[str] = Field(default_factory=list)
    source_type: str = Field(default="user_explicit", max_length=64)
    source_ref: str = Field(default="", max_length=500)
    trust_level: int = Field(default=80, ge=0, le=100)
    enabled: bool = True
    expires_at: str | None = None


class MemoryUpdate(BaseModel):
    kind: str | None = Field(default=None, min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=20_000)
    tags: list[str] | None = None
    source_type: str | None = Field(default=None, max_length=64)
    source_ref: str | None = Field(default=None, max_length=500)
    trust_level: int | None = Field(default=None, ge=0, le=100)
    enabled: bool | None = None
    expires_at: str | None = None
    reason: str = Field(default="updated", max_length=300)


class ConversationSummaryUpdate(BaseModel):
    summary: str = Field(..., min_length=1, max_length=50_000)
    preserved_constraints: list[str] = Field(default_factory=list, max_length=50)
    through_task_id: str = Field(default="", max_length=128)
    model_id: str = Field(default="manual-editor", max_length=128)


class LoopCreate(BaseModel):
    id: str | None = None
    name: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    agent_id: str = "general-agent"
    model_id: str = "deterministic"
    trigger_type: str = Field(default="interval", pattern="^(interval|cron|once|webhook)$")
    interval_seconds: int = Field(default=3600, ge=5, le=31_536_000)
    cron_expression: str = Field(default="", max_length=120)
    once_at: str = Field(default="", max_length=80)
    webhook_secret: str | None = Field(default=None, min_length=16, max_length=500)
    webhook_tolerance_seconds: int = Field(default=300, ge=30, le=3600)
    organization_id: str = Field(default="local-org", min_length=1, max_length=128)
    workspace_id: str = Field(default="default", min_length=1, max_length=128)
    user_id: str = Field(default="local-user", min_length=1, max_length=128)
    max_runs: int = Field(default=10, ge=1, le=10_000)
    max_failures: int = Field(default=3, ge=1, le=100)
    max_attempts: int = Field(default=1, ge=1, le=10)
    retry_backoff_seconds: int = Field(default=0, ge=0, le=3600)
    initial_state: dict[str, Any] = Field(default_factory=dict)
    auto_start: bool = False


class LoopUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    prompt: str | None = Field(default=None, min_length=1)
    agent_id: str | None = None
    model_id: str | None = None
    trigger_type: str | None = Field(default=None, pattern="^(interval|cron|once|webhook)$")
    interval_seconds: int | None = Field(default=None, ge=5, le=31_536_000)
    cron_expression: str | None = Field(default=None, max_length=120)
    once_at: str | None = Field(default=None, max_length=80)
    webhook_secret: str | None = Field(default=None, min_length=16, max_length=500)
    webhook_tolerance_seconds: int | None = Field(default=None, ge=30, le=3600)
    max_runs: int | None = Field(default=None, ge=1, le=10_000)
    max_failures: int | None = Field(default=None, ge=1, le=100)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    retry_backoff_seconds: int | None = Field(default=None, ge=0, le=3600)
    state: dict[str, Any] | None = None
