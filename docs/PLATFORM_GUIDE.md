# AgentNexus 使用指南

这份指南说明如何启动平台、配置模型、安装 Skill 和 MCP，以及如何通过对话生成可下载文件。文中只描述当前代码已经具备的能力；需要额外组件或部署开关的地方会单独说明。

## 启动平台

### 使用 Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 使用 Docker

```bash
docker compose up --build
```

Compose 默认只监听宿主机 `127.0.0.1:8000`，不会直接暴露到局域网或公网。需要修改监听范围时，应同时配置反向代理、认证和访问控制。

浏览器访问 [http://127.0.0.1:8000/](http://127.0.0.1:8000/)。也可以用下面的地址确认后端是否正常：

```bash
curl http://127.0.0.1:8000/api/health
```

请通过 HTTP 地址进入平台，不要直接打开 `web/index.html`。`file://` 页面没有后端连接，模型保存、对话、上传和文件生成都会失效。

`.env.example` 列出了可用的常见配置，但应用不会替你管理生产密钥。请在启动进程前把需要的值放入进程环境或部署系统；修改后需要重启服务。

平台默认关闭应用级出站网络。在线模型、天气、联网搜索、远程 MCP、HTTP 工具、远程 Policy 和下载链接安装都需要先设置：

```bash
export APP_ALLOW_OUTBOUND_NETWORK=true
```

这是一道总开关，各功能自己的开关、密钥和白名单仍然同时生效。总开关不拦截 stdio MCP 子进程自行联网；严格离线环境还需使用容器网络策略或主机防火墙。

## 第一次使用

建议按这个顺序检查：

1. 打开“模型设置”，添加一个真实模型并测试连接。
2. 返回工作台，选择普通模式和刚才保存的模型。
3. 发送一个简单问题，确认回答和右侧执行过程都会更新。
4. 上传一份文本或 Office 文档，让平台总结内容。
5. 明确要求生成 Word、PDF、Excel、Markdown 或 HTML，确认回答中出现下载链接。
6. 需要工具时，再到“技能中心”和“工具接入”安装并配置对应能力。

内置“离线确定性模型”不需要密钥，主要用于检查页面、事件流和文件链路。它会如实提示自身限制，不会提供真实大模型级别的分析和创作。

## 普通模式和专家模式

模式开关位于对话输入区上方。选择结果会一直保留，直到你再次手动切换。切换模式不会改变一个已经开始运行的任务。

### 普通模式

普通模式由一个智能体完成任务。默认助手会结合当前对话还原目标，从已启用 Skill 中选择合适的流程，并只向模型提供本次计划和权限允许的工具。

适合日常问答、资料整理、单份文档、一次工具查询和大多数分析任务。例如：

```text
根据我上传的会议记录，整理决策、待办、负责人和截止时间，并生成一份 Word 纪要。
```

任务复杂时，平台不会自行切换到专家模式。是否使用专家团始终由你决定。

### 专家模式

专家模式从已经创建且启用的专家团中选择一个团队。选择“自动匹配专家团”时，平台会根据目标与团队职责的文字匹配；也可以在输入区直接指定团队。如果没有明确匹配的团队，平台会请你手工选择，不会随机组团。

团队成员使用独立子任务并行处理，主管根据成员交付汇总，最后再检查团队配置的验收条件。当前只支持无前后依赖的并行成员，不是任意多智能体工作流图。

首次测试前，需要先在“智能体”中准备主管和成员，再在“专家团”中建立至少两名成员的团队。测试问题可以使用：

```text
评审这份上线方案，从产品体验、技术实现和安全风险三个角度给出 P0、P1、P2 建议，最后整理成统一结论。
```

页面会显示选中的团队、成员任务、主管汇总和验收状态。专家模式的成本和耗时通常高于普通模式，是否值得使用取决于任务是否真的需要独立视角和交叉检查。

## 配置模型

当前模型网关支持 `openai` 和 `openai_compatible` 两种配置，二者都使用 OpenAI Chat Completions 兼容协议。Base URL 应指向 API 根路径，例如 `https://api.openai.com/v1`，平台会请求其 `/chat/completions`。这是由后端发起的网络请求，除了开启出站网络，还应通过 `APP_MODEL_HOST_ALLOWLIST` 限制允许访问的模型主机。

原生 Anthropic Messages、Gemini 等不同协议尚未单独适配。某个服务只有在提供 OpenAI-compatible 接口时，才能直接通过现有配置接入。

### 从环境变量读取 API Key

先在平台服务进程的环境中设置 Key：

```bash
export OPENAI_API_KEY='your-api-key'
export APP_ALLOW_OUTBOUND_NETWORK=true
export APP_MODEL_HOST_ALLOWLIST='api.openai.com'
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

模型主机白名单只填写主机名，不含 `https://`、端口或路径；多个主机用逗号分隔。使用其他兼容供应商时，把它的 API 主机加入白名单后再保存 Base URL。

然后在“模型设置”中填写：

- Provider：`openai` 或 `openai_compatible`
- 模型名：供应商实际使用的模型 ID
- Base URL：供应商的兼容接口根地址
- 密钥方式：环境变量
- 变量名：例如 `OPENAI_API_KEY`
- 高级配置：例如 `{"temperature":0.2,"timeout":90}`

页面只保存环境变量名。Key 必须存在于启动后端的进程中；把 Key 设置在另一个终端窗口不会影响已经运行的服务。

### 直接填写 API Key

选择“直接填写 API Key”后输入密钥并保存。后端会使用 Fernet 将密文写入 SQLite，页面只显示“已保存”，不会回显明文。

如果没有配置 `APP_SECRET_KEY`，平台会在 `APP_SECRET_KEY_FILE` 指向的位置生成本机密钥，默认是 `data/.secret_key`。稳定部署应从密钥管理系统注入固定值：

```bash
export APP_SECRET_KEY='a-long-stable-secret'
```

更换或丢失这个值后，已有直接 Key 将无法解密。该设计方便单机使用，不等同于 Vault、KMS、密钥轮换或按用户授权。

### 保存、查看和选择

点击“保存模型”后，成功的配置会出现在左侧“已配置模型”列表，同时进入工作台和智能体的模型选择器。选择该卡片后可以“测试连接”。

如果保存没有成功，编辑区会显示错误。常见原因包括：

- ID、名称或模型名为空。
- 环境变量名不是合法标识符。
- 直接密钥模式没有填写 Key。
- 高级配置不是合法 JSON 对象。
- ID 与已有配置重复。

模型连接测试只证明一次简单请求成功。工具调用还取决于供应商是否正确实现 Chat Completions 的 `tools` 字段，逐段实时输出则需要兼容 SSE。若服务忽略 `stream=true`、直接返回普通 JSON，平台仍会显示完整回答并保留工具调用，但只能一次性输出。因此保存后还应运行一次真实任务确认。

## 上传文件

文件上传默认可用。工作台中当前选择的附件会绑定到下一条任务，正文由服务端读取后加入模型上下文。

| 类型 | 处理方式 |
| --- | --- |
| TXT、MD、CSV、JSON、YAML | 读取 UTF-8 文本 |
| PY、JS、TS、HTML、CSS | 作为文本读取，不执行代码 |
| DOCX | 提取段落和表格文本 |
| XLSX | 提取工作表单元格，公式不会执行 |
| PPTX | 提取页面文字和表格 |
| PDF | 提取可搜索的页面文字，不做 OCR |

其他文件也可以上传，但模型只会收到“当前不支持提取正文”的说明。旧版 `.doc`、`.xls`、`.ppt`，扫描 PDF、加密文件、图片、音频和视频不在默认正文解析范围内。

默认限制如下：

| 限制 | 默认值 |
| --- | --- |
| 上传单文件大小 | 20MB |
| 一个任务处理的附件数 | 10 个 |
| 单文件进入上下文的正文 | 20,000 字符 |
| 所有附件正文合计 | 60,000 字符 |
| XLSX | 前 8 个工作表，每表前 200 行、50 列 |
| PDF | 前 40 页 |
| PPTX | 前 40 页，每页前 200 个对象 |

上传大小可通过 `APP_MAX_UPLOAD_MB` 调整，但正文解析仍有自己的安全上限。超出范围的内容会截断或跳过，不应把“上传成功”理解为文件的每一页都已进入模型上下文。

## 创建和安装 Skill

Skill 是一个可复用的任务说明包，入口文件为 `SKILL.md`。它通常包括适用场景、执行步骤、依赖 MCP 和输出要求。

### 在页面创建

打开“技能中心”，点击“新建技能”。一个简单的入口文件可以写成：

```markdown
---
id: meeting_minutes
name: 会议纪要
description: 从会议记录中整理决策、待办和负责人
category: office
version: 0.1.0
required_mcps: report
---

# 会议纪要

## 使用条件

用户要求整理会议记录或生成会议纪要时使用。

## 执行步骤

1. 区分已确认决策、待确认事项和行动项。
2. 为行动项保留负责人和截止日期；原文没有时标为待补充。
3. 用户要求文件时，调用报告工具生成指定格式。
```

描述应明确写出什么问题需要使用它。过于宽泛的描述会让自动匹配变得不稳定。

### 导入 Skill 包

“技能中心”支持：

- 上传一个 UTF-8 `SKILL.md`。
- 上传包含一个 `SKILL.md` 的 ZIP 包。
- 输入公开 HTTPS 的原始文件或 ZIP 下载地址。
- 通过服务端受控路径安装。

Skill 包总大小不能超过 2MB，单个附属文件不能超过 1MB，最多 200 个文件。ZIP 中的目录穿越、符号链接和多个 Skill 根会被拒绝。

下载链接安装默认关闭。启用时需要同时打开出站总开关，并限制 HTTPS 主机白名单：

```bash
export APP_ALLOW_OUTBOUND_NETWORK=true
export APP_ALLOW_REMOTE_INSTALL=true
export APP_REMOTE_INSTALL_HOST_ALLOWLIST='github.com,raw.githubusercontent.com,gitlab.com'
```

请使用原始 `SKILL.md` 或 ZIP 的直接下载地址。普通仓库网页不是安装包，平台不会抓取整个网页后猜测目录结构。

服务端本地路径安装没有网页入口，需要先限制允许目录：

```bash
export APP_SKILL_LOCAL_ROOTS='/opt/skills:/srv/team-skills'
```

然后调用：

```bash
curl -X POST http://127.0.0.1:8000/api/skills/install/path \
  -H 'Content-Type: application/json' \
  -d '{"path":"/opt/skills/meeting-minutes"}'
```

这里的“本地”指平台服务器，不是浏览器用户的电脑。

### 通过对话安装和查看

普通模式支持明确的管理指令：

```text
安装 Skill https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>/SKILL.md
```

也可以上传 `SKILL.md` 后发送“安装这个技能”，或在消息中粘贴完整内容。发送“查看已安装技能”可以在对话中查看名称、ID 和启用状态，完整清单始终以“技能中心”为准。

某些任务可能触发平台内置 Skill 目录的推荐。此时任务会停在确认卡片；确认后从平台随附内容安装并继续，拒绝后不会在同一任务中反复推荐。这个目录目前只包含少量通用 Skill，不会实时搜索第三方市场。

### Skill 安装后的边界

安装后可以查看和维护 `scripts/`、`references/`、`rules/`、`assets/` 等包文件，也可以导出 ZIP。运行时会读取入口和可读文本作为上下文，但不会自动执行包中的 Python、Shell 或 JavaScript。

专用智能体应明确绑定 Skill ID。`required_mcps` 只是依赖声明；对应服务还要已安装、启用，并且没有被权限或 Policy 拒绝。

## 接入 MCP 和 HTTP 工具

“工具接入”管理四类服务：平台内置工具、本地 stdio MCP、远程 Streamable HTTP MCP 和普通 HTTP API 工具。外部进程及网络工具默认关闭。

### 本地 stdio MCP

在启动平台前设置：

```bash
export APP_ALLOW_STDIO_MCP=true
export APP_STDIO_COMMAND_ALLOWLIST='npx,node,python3,uvx'
```

然后创建“本地 stdio MCP”，配置示例：

```json
{
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/absolute/path/to/workspace"],
  "env": {},
  "timeout": 60
}
```

保存后点击“连接并同步工具”。`timeout` 会被限制在 5 到 300 秒之间。stdio 服务会在平台服务器上启动子进程，具有该服务账号的文件和网络权限；务必配置经过审核的命令白名单，共享部署优先填写可执行程序的绝对路径。空白名单不能替代进程沙箱或操作系统权限控制。

### 远程 Streamable HTTP MCP

```bash
export APP_ALLOW_OUTBOUND_NETWORK=true
export APP_ALLOW_REMOTE_MCP=true
export APP_REMOTE_HOST_ALLOWLIST='mcp.example.com'
export COMPANY_MCP_TOKEN='your-token'
```

配置示例：

```json
{
  "url": "https://mcp.example.com/mcp",
  "headers": {
    "Authorization": "${COMPANY_MCP_TOKEN}"
  }
}
```

`${ENV_NAME}` 会在服务端调用时解析。远程 MCP 要求配置非空的精确主机白名单；白名单只写主机名或 IP，不支持协议、路径和通配符。

### 普通 HTTP 工具

```bash
export APP_ALLOW_OUTBOUND_NETWORK=true
export APP_ALLOW_HTTP_TOOLS=true
export APP_REMOTE_HOST_ALLOWLIST='api.example.com'
export COMPANY_API_TOKEN='your-token'
```

普通 HTTP 服务没有 MCP 工具发现，需要提前声明工具：

```json
{
  "id": "company-api",
  "name": "公司查询服务",
  "kind": "http",
  "enabled": true,
  "config": {
    "base_url": "https://api.example.com",
    "headers": {"Authorization": "${COMPANY_API_TOKEN}"},
    "timeout": 30
  },
  "tools": [
    {
      "name": "query_record",
      "description": "按编号查询记录",
      "method": "GET",
      "path": "/records/query",
      "input_schema": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"]
      },
      "annotations": {"readOnlyHint": true}
    }
  ]
}
```

GET 参数放入 Query String，其他方法发送 JSON Body。“连接并同步工具”只会显示已声明工具，不会读取 OpenAPI 自动生成定义。

### 从文件、链接或对话安装

可以导入单个配置、配置数组或常见的 `mcpServers` 格式：

```json
{
  "mcpServers": {
    "company-mcp": {
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "${COMPANY_MCP_TOKEN}"}
    }
  }
}
```

远程配置文件最大 1MB。链接必须是白名单中的公开 HTTPS JSON 直链。第三方目录只提供一条命令时，需要先把它手工转换成 JSON 配置。

普通模式也可以发送：

```text
安装 MCP https://example.com/mcp.json
```

或上传 JSON 后发送“安装这个 MCP”。“查看已安装 MCP”会列出当前服务。安装只写入平台配置；仍需启用服务、同步工具，并在专用智能体中绑定服务 ID。

### 凭据和调用测试

MCP 公共接口会隐藏 Token、Authorization、Cookie、Password、Secret 等字段，保存页面返回的占位符时会保留原值。不过这只是接口脱敏：MCP 配置 JSON 在 SQLite 中按原值保存，不使用模型 API Key 的 Fernet 加密。优先使用 `${ENV_NAME}` 引用服务进程中的环境变量，并将数据库备份、文件权限和访问范围按敏感数据管理。

页面“调用工具”只允许平台内置只读工具，或明确声明 `annotations.readOnlyHint=true` 的外部工具。写入、破坏性和未标注工具应通过正式任务执行，让 Agent 权限、计划、Policy 和审批共同生效。

## 配置智能体和专家团

“智能体”页面可以设置默认模型、系统提示词、Skill ID、工具服务 ID 和权限 JSON。常用权限包括：

| 字段 | 作用 |
| --- | --- |
| `allowed_tools` / `denied_tools` | 工具允许与拒绝清单 |
| `allowed_mcp_servers` / `denied_mcp_servers` | 服务允许与拒绝清单 |
| `read_only` | 只向模型提供可信只读工具 |
| `max_tool_calls` | 单次运行最多工具调用数 |
| `max_tool_steps` | 模型工具循环的最大步数 |
| `timeout_seconds` | 一次运行的工具总时限 |

拒绝规则优先于允许规则。专家团运行时，团队和成员权限只能进一步收紧 Agent 权限，不能把 Agent 原本无权使用的工具重新开放。

专家模板是可复用的 Agent 定义，安装后会生成普通智能体；真正参与执行的是已安装智能体。建立专家团时需要选择主管、至少两名不同成员、成员职责和主管汇总要求。验收条件可以检查最少字数、必需关键词和要求的文件格式。

Policy 规则可以在目标、计划、工具、审批、产物和输出等阶段拒绝操作、要求审批或追加上下文。当前没有完整的网页 Policy 编辑器，主要通过 `/api/policies` 或 `APP_POLICY_RULES_JSON` 管理。HTTP Policy 默认关闭，不支持把任意脚本当作处理器。

## 任务过程和实时输出

一个普通任务通常会经历：目标理解、准备上下文、执行、可选的文件生成和最终校验。右侧面板采用两层节点：

- 大节点表示任务当前处于哪个阶段。
- 子节点显示本次实际匹配的 Skill、模型、MCP Server 或具体工具。

计划会随任务类型调整，子节点也不是固定清单。页面只显示状态、参数摘要和结果证据，不展示模型的隐藏思维链。

缺少真正必要的参数时，平台会用简短问题请你补充，并停止当前调用。收到补充后可在同一对话继续，但能否正确还原省略信息取决于所选真实模型；离线模型只适合直接、完整的指令。

真实模型从上游 SSE 接收增量，再通过平台事件流更新回答。模型输出前仍要完成目标还原、Skill 匹配和计划建立，因此发送后可能先看到节点进度，稍后才出现正文。短暂断线时页面会按事件游标有限次数重连，避免重复显示已经收到的内容。离线模型使用分片输出模拟相同界面流程。

任务运行中可以追加要求。运行时会停止尚未完成的旧生成，并结合新要求重新组织回答。任务页还提供取消、从头重试和检查点恢复；这些操作会创建或保留不同的运行尝试，不会覆盖历史记录。

某些 Policy 或 Skill 推荐会让任务进入等待审批。批准或拒绝都会写入事件记录。服务重启会标记中断的运行，并尽量从最近的安全检查点创建恢复尝试；外部写操作仍应自行保证幂等，不能只依赖检查点避免重复副作用。

## 生成、预览和下载文件

通过对话明确说明需要的格式，例如：

```text
把前面的结论整理成一份 Word 报告，包含摘要、风险表和行动计划，并提供下载。
```

```text
根据上传的数据生成 Excel，列出原始记录、汇总指标和异常项，并提供下载。
```

当前内置工具可生成：

| 格式 | 额外要求 | 平台预览 |
| --- | --- | --- |
| DOCX | Python 依赖 | 段落和表格 |
| PDF | Python 依赖 | 浏览器内嵌 |
| XLSX | Python 依赖 | 工作表表格 |
| Markdown | 无额外组件 | 渲染后的 Markdown |
| HTML | 无额外组件 | 移除脚本和外部资源后的沙箱预览 |
| PPTX | Node.js 与已配置的 Artifact Tool；未配置时不可用 | 页面文字和结构 |

产物会出现在回答下载区、任务详情和“产物”页面。系统记录所属任务和 Run、版本、大小、MIME、SHA-256，并使用受控 ID 提供预览与下载。文件超过 25MB 时不提供平台内预览，但仍可下载。

PPTX 只有在 Node.js 和 Artifact Tool 都已安装，且入口路径指向有效组件时才可用：

```bash
export APP_NODE_BINARY='node'
export APP_ARTIFACT_TOOL_ENTRYPOINT='/absolute/path/to/artifact_tool.mjs'
export APP_PRESENTATION_TIMEOUT_SECONDS=180
```

项目不会自动下载 Artifact Tool，默认 Docker 镜像也没有安装 Node.js 或该组件。因此，只有完成上述配置的部署才能生成 PPTX；未配置时会明确返回组件不可用，其他格式不受影响。

根据附件生成文件时，运行时会抽取少量来源特征做一致性检查，避免交付与附件完全无关的空泛文档。它只能检查可提取文本和有限样本，不能替代人工校对、事实核验或专业审核。

## 联网搜索

联网搜索默认关闭，需要同时开启出站总开关、搜索能力并配置一个供应商 Key：

```bash
export APP_ALLOW_OUTBOUND_NETWORK=true
export APP_ALLOW_WEB_SEARCH=true
export TAVILY_API_KEY='your-tavily-key'
# 或者
export BRAVE_SEARCH_API_KEY='your-brave-key'
```

重启后可以在“模型设置”的能力状态中查看是否已开启和配置。用户明确提出“联网搜索”“最新资料”“新闻”等目标时，默认助手可调用搜索工具，并把标题、URL 和摘要交给模型。

没有 Key 时不会自动抓取普通搜索网页作为替代。联网回答的准确性仍取决于搜索结果和模型，重要信息应打开来源复核。

内置天气工具会访问 Open-Meteo，不需要搜索供应商 Key，也不受 `APP_ALLOW_WEB_SEARCH` 控制；但它同样受 `APP_ALLOW_OUTBOUND_NETWORK` 总开关限制。总开关关闭时，平台会直接提示管理员开启联网，不会向 Open-Meteo 发起请求。

## 记忆和对话上下文

“记忆”用于保存用户明确指定的长期规则、偏好和稳定事实。可以在页面创建、编辑、停用和删除，也可以在普通对话中使用：

```text
记住：以后评审结果都用中文，并按 P0、P1、P2 排序。
查看记忆
忘记：以后评审结果都用中文，并按 P0、P1、P2 排序。
```

同一对话的历史消息会用于后续目标还原，较早内容可以压缩为可查看的摘要。新建对话后，对话级历史不会继续混入，但用户或工作区级记忆仍可能生效。

知识库能力本阶段暂缓。当前没有文档集合、向量索引、跨文件语义检索、同步连接器、页级引用或增量更新；附件上下文、长期记忆和对话摘要也不等同于知识库。

## 自动化

“自动化”会按触发条件重复创建独立普通任务，不是一个无限自我循环的智能体。支持手工试运行、固定间隔、五字段 Cron、单次时间和签名 Webhook。

创建后建议先试运行一轮，再启动调度。可以设置最大轮数、连续失败阈值、每轮尝试次数和退避时间。同一自动化不会并发重入；等待审批或缺少必要信息时会暂停后续调度。当前通知只显示在站内页面。

Cron 按 UTC 计算。Webhook 使用 HMAC-SHA256，要求 `X-Automation-Timestamp`、`X-Automation-Signature` 和 `Idempotency-Key`；相同幂等键不会重复创建同一事件。

## 数据和安全边界

本地运行会产生：

```text
data/platform.db
data/.secret_key
data/uploads/
data/artifacts/
```

这些文件包含用户内容、任务历史、配置或密钥材料，不应提交到 Git，也不应通过普通静态文件服务公开。公共任务接口会移除服务端路径和内部存储字段，产物下载也限制在受控目录中，但这些措施不能替代身份认证。

当前版本仍有以下边界：

- 没有完整的登录认证、可信用户身份、多租户 RBAC 和数据库行级隔离。
- `organization_id`、`workspace_id` 和 `user_id` 目前主要是逻辑作用域，不是已经认证的主体。
- 没有每任务容器、Worktree、进程沙箱、网络配额或资源配额。
- stdio MCP 使用平台服务账号的本机权限。
- 下载链接安装有 HTTPS、主机和包结构检查，但没有发布者签名、恶意代码扫描、信誉系统或自动升级回滚。
- SQLite 和单进程调度不等同于高可用分布式任务系统。
- 直接 API Key 加密和 MCP 字段脱敏不等同于企业 Secret Vault。
- 附件解析不包含 OCR、图片理解或知识库检索。

因此当前实现更适合本机开发或受控内网。对外网部署前，至少需要补充反向代理 TLS、身份认证、资源所有权校验、Secret Manager、数据库隔离、执行沙箱和基础设施级出站网络策略。

## 常见问题

### 地址打不开

先访问 `/api/health`。如果连接被拒绝，说明服务没有启动或端口不同；如果健康检查正常而页面异常，刷新页面并检查浏览器是否仍停留在 `file://` 地址。

### 发消息后没有回答

确认工作台选择了已启用模型，真实模型已经通过连接测试。再查看任务是否正在等待审批、补充参数，或已经在右侧节点显示错误。使用反向代理时，还要确认 SSE 没有被缓冲，模型供应商也确实支持兼容的流式响应。

### 模型保存后看不到

检查编辑区的保存错误，确认 ID 未重复、模型名不为空、Key 模式填写完整且高级配置是合法 JSON。还要确认页面连接的是同一个后端实例和同一个 `APP_DB_PATH`。

### 回答很慢或看不到实时文字

目标还原和计划会先于正文执行。若长时间停在模型节点，检查供应商响应速度和 `timeout`。反向代理需要关闭事件流缓冲。供应商若忽略 `stream=true`、直接返回合法的普通 Chat Completions JSON，平台仍能完成任务，但正文会一次性显示；既不是有效 SSE、也不是合法 JSON 时才会失败。不能只用“测试连接”结果判断完整兼容性。

### Markdown 显示成源码

工作台回答和 `.md` 产物预览都会渲染 Markdown。代码围栏中的内容按源码显示是正常的；如果整篇都没有渲染，刷新页面确认加载了最新 `web/app.js`，并检查模型是否把全文包进了一个代码围栏。

### Skill 安装了但没有命中

确认 Skill 已启用，名称和描述写清了适用场景，专用智能体已绑定其 ID，`required_mcps` 对应服务可用。自动匹配依赖当前文本，不保证所有第三方 Skill 都能准确命中。

### MCP 安装了但没有调用

依次检查：服务已启用、外部能力开关已打开、工具同步成功、Schema 合法、智能体已绑定服务、当前计划确实需要它、权限和 Policy 没有拒绝。页面对写工具返回 403 是调用测试门禁，不代表服务安装失败。

### PDF 上传后没有正文

常见原因是扫描件、加密文件、文件损坏或超过页数和字符上限。当前没有 OCR，请先转换为可搜索 PDF 或文本。

### 下载链接安装失败

确认使用 HTTPS 直接下载地址，域名在 `APP_REMOTE_INSTALL_HOST_ALLOWLIST` 中，文件没有超过限制。Skill ZIP 只能包含一个 Skill 根；MCP 链接必须返回合法 JSON。

### PPTX 失败而其他文档正常

检查 Node.js、`APP_NODE_BINARY`、`APP_ARTIFACT_TOOL_ENTRYPOINT` 和超时配置。Python 文档依赖安装成功并不能证明 PowerPoint 子进程也已准备好。
