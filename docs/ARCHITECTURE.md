# AgentNexus 架构说明

AgentNexus（智枢）由一个 FastAPI 服务和一套浏览器界面组成。前端只负责交互和展示，模型调用、任务编排、工具权限、文件处理和持久化都在服务端完成。

## 主要组件

| 组件 | 位置 | 职责 |
| --- | --- | --- |
| Web 工作台 | `web/` | 对话、配置管理、任务进度、文件预览与下载 |
| HTTP API | `app/main.py` | 路由、输入校验、公共响应和 SSE 事件流 |
| Agent Runtime | `app/services/agent_runtime.py` | 上下文还原、Skill 匹配、计划、工具调用和输出校验 |
| Model Gateway | `app/services/model_gateway.py` | 离线适配器及 OpenAI Chat Completions 兼容调用 |
| Skill Registry | `app/services/skill_registry.py` | Skill 包加载、安装、编辑、匹配和导出 |
| MCP Gateway | `app/services/mcp_gateway.py` | 内置工具、stdio MCP、Streamable HTTP MCP 和 HTTP 工具 |
| Task State | `app/services/task_state.py` | 运行尝试、节点、指令和检查点 |
| Expert Team Service | `app/services/expert_team_service.py` | 团队选择、成员并行执行、主管汇总和验收 |
| Context Service | `app/services/context_service.py` | 分层记忆和有效上下文 |
| Loop Scheduler | `app/services/loop_scheduler.py` | 间隔、Cron、单次和 Webhook 自动化 |
| Policy Engine | `app/services/policy_engine.py` | 在任务和工具生命周期应用拒绝、审批或上下文规则 |
| SQLite | `app/db.py` | 配置、任务、事件、记忆和文件索引的单机持久化 |

## 普通任务流程

```text
浏览器提交消息和附件
        ↓
创建 Task 与 Run
        ↓
恢复对话上下文和有效记忆
        ↓
确认当前目标与必要参数
        ↓
匹配 Skill，生成执行计划
        ↓
向模型提供本次允许的 MCP 工具
        ↓
生成回答或文件
        ↓
按计划检查格式、产物和来源一致性
        ↓
保存结果，并通过 SSE 更新页面
```

执行过程采用两层节点。上层是目标理解、准备、执行、文件生成和校验等阶段；下层记录本次实际使用的 Skill、模型、MCP Server 和工具。节点展示的是运行状态和可核验摘要，不包含模型隐藏推理。

每个任务可以有多个 Run。取消、重试、检查点恢复和服务重启恢复都会保留原有运行记录，而不是覆盖历史结果。检查点保存的是 JSON 状态及允许复用的工具结果，不是进程快照。

## 专家任务流程

专家模式不会临时拼装任意成员。运行时从当前作用域内已启用的团队中选择一个团队，或使用用户手工指定的团队。成员分别创建子任务并行执行，完成后由主管读取成员交付并生成汇总结果。

成员上下文相互独立。团队、成员和 Agent 的权限逐层收紧；失败成员可以单独重试。主管汇总完成后还要通过团队配置的结构化验收规则，父任务才会结束。

## Skill 与工具

Skill 是以 `SKILL.md` 为入口的文本流程包。内置 Skill 位于 `app/builtin_skills/`，安装包保存在数据库中。运行时读取 Skill 指令及包内可读文本作为上下文；包中的 Python、Shell 或 JavaScript 文件不会被自动执行。

MCP Gateway 按服务类型分发调用：

- `builtin`：平台维护的天气、搜索、表格和报告工具。
- `mcp_stdio`：通过官方 MCP SDK 连接服务端本地进程。
- `mcp_http`：通过官方 MCP SDK 连接远程 Streamable HTTP 服务。
- `http`：按预先声明的路径和输入 Schema 调用普通 HTTP API。

平台自身的出站请求先受 `APP_ALLOW_OUTBOUND_NETWORK` 总开关控制，再应用模型、搜索、远程 MCP、HTTP 工具、下载链接安装和远程 Policy 的细分开关与白名单。stdio 命令仍应使用白名单限制；其子进程不受 Python 应用总开关约束，需要部署层网络策略。工具在进入模型上下文前还要经过 Agent 权限和当前计划过滤，调用前后可以继续应用 Policy。

## 模型与流式输出

Model Gateway 当前实现 OpenAI Chat Completions 兼容协议。真实模型请求优先使用上游 SSE 流式接口，并把文字增量写入任务事件。如果兼容服务忽略 `stream=true` 并返回普通 Chat Completions JSON，网关会解析该响应并一次性发布文字，同时保留工具调用；这种兼容模式可以完成任务，但没有逐段实时输出。

浏览器保存最后收到的事件游标。连接中断时会有限次数退避重连，服务端从游标之后继续发送事件，避免重复展示；任务结束或进入等待确认状态后关闭连接。

内置离线适配器用于无密钥启动和流程检查，不承担通用内容生成。模型配置保存在 SQLite；环境变量模式只保存变量名，直接密钥模式保存本机加密后的密文。

## 文件存储

默认运行目录如下：

```text
data/platform.db       SQLite 数据库
data/.secret_key       本机密钥（未显式配置 APP_SECRET_KEY 时生成）
data/uploads/          用户上传文件
data/artifacts/        任务生成文件
```

公共任务和事件响应不会返回服务器绝对路径或数据库内部 JSON 字段。产物通过 ID 定位，并由受控接口预览或下载；路径解析会拒绝绝对路径、目录穿越和指向产物目录外部的符号链接。

这些目录仍然是单机共享存储。当前代码没有为不同登录主体提供完整的文件所有权隔离，因此公共响应脱敏不能替代身份认证和访问控制。

## 部署边界

现有架构是单进程、SQLite 的本地平台实现。它没有分布式队列、跨节点锁、每任务容器、CPU/内存配额、可信用户认证或多租户行级隔离。生产化时需要在入口层、数据层和执行层补齐这些能力。

PPTX 生成是一个独立的 Node.js 子进程链路；Python 服务负责准备输入和登记产物。默认 Python Docker 镜像不含该组件，部署时需要额外安装并通过 `APP_ARTIFACT_TOOL_ENTRYPOINT` 指定入口。

附件解析、长期记忆和对话摘要不构成知识库。当前没有向量索引、跨文件语义检索、文档同步连接器或页级引用系统。
