# AgentNexus（智枢）

AgentNexus（中文名“智枢”）是一个通过浏览器使用的智能体工作平台。模型、智能体、Skill、MCP 工具、任务记录和生成文件都由平台统一管理，使用者可以直接发起对话，也可以按工作场景配置自己的处理流程。

目前提供的主要能力包括：

- 普通模式：由一个智能体理解上下文，并匹配已启用的 Skill 和 MCP 工具。
- 专家模式：从已配置的专家团中选择团队，成员并行处理后由主管汇总。
- 模型管理：支持 OpenAI 和 OpenAI-compatible 接口，密钥可以来自环境变量，也可以在页面直接填写。
- Skill 管理：创建、编辑、导入、导出和通过 HTTPS 下载链接安装 Skill 包。
- 工具接入：支持本地 stdio MCP、远程 Streamable HTTP MCP 和普通 HTTP 工具。
- 文件处理：可读取常见文本、Office 文档和 PDF 的可提取正文，并生成可下载的 Word、PDF、Excel、Markdown 或 HTML 文件；PowerPoint 仅在 Node.js 和生成组件配置完成后可用。
- 任务过程：通过事件流显示计划、Skill、模型、工具调用、文件生成和最终校验状态。
- 记忆与自动化：保存明确的长期偏好，或按时间和 Webhook 触发重复任务。

内置离线模型不需要 API Key，适合检查页面、任务和文件流程。它不是通用语言模型；需要内容创作、复杂分析或工具规划时，请先配置真实模型。

## 运行环境

- Python 3.12
- Node.js 与 Artifact Tool（仅生成 PPTX 时需要）
- SQLite（由 Python 自带，无需单独安装）

## 本地启动

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

浏览器打开 [http://127.0.0.1:8000/](http://127.0.0.1:8000/)。健康检查地址是 [http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health)。

不要直接用 `file://` 打开 `web/index.html`。静态页面无法保存模型、上传文件或运行任务。

## Docker 启动

```bash
docker compose up --build
```

当前 Dockerfile 只包含 Python 环境，因此容器内默认不能生成 PPTX。需要 PowerPoint 输出时，请在自定义镜像中安装 Node.js 和相应生成组件，或在本地 Python 环境运行。

Compose 默认只把服务绑定到宿主机 `127.0.0.1:8000`，并关闭应用级出站网络。需要在线模型或外部工具时，先设置 `APP_ALLOW_OUTBOUND_NETWORK=true`，再开启对应的细分能力；修改后重新创建容器。

## 配置模型

打开“模型设置”，点击“添加模型”，填写模型名、Base URL 和密钥方式。当前支持：

- `openai`：OpenAI Chat Completions 兼容入口。
- `openai_compatible`：提供 `/v1/chat/completions` 兼容接口的服务。

使用环境变量时，先在启动平台的进程环境中设置 Key：

```bash
export OPENAI_API_KEY='your-api-key'
export APP_ALLOW_OUTBOUND_NETWORK=true
export APP_MODEL_HOST_ALLOWLIST='api.openai.com'
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

然后在页面选择“环境变量”，填写 `OPENAI_API_KEY`。Base URL 应填写供应商的 OpenAI-compatible API 根地址，例如 `https://api.openai.com/v1`；平台会在其后请求 `/chat/completions`。`APP_MODEL_HOST_ALLOWLIST` 填写允许访问的模型主机名，多个主机用逗号分隔，不含协议和路径。

也可以选择“直接填写 API Key”；密钥会使用本机密钥加密后保存在 SQLite 中，页面不会回显明文。这是单机存储方案，不替代生产环境的 Secret Manager 或 KMS。

保存后，模型会出现在“已配置模型”和工作台的模型选择器中。建议先点击“测试连接”，再用它运行任务。

## 第一次使用

1. 在“模型设置”中配置并测试一个真实模型。
2. 回到工作台，在输入框上方选择普通模式或专家模式。
3. 选择本次任务使用的模型；普通模式还可以选择智能体。
4. 如有资料，先添加附件。
5. 输入目标并发送，在右侧查看执行进度和实际调用的 Skill、模型及工具。
6. 生成的文件可以在回答中下载，也会保留在“产物”页面。

模式切换会持续生效，直到再次手动切换。普通模式不会因为任务复杂而自动变成专家模式；专家模式需要先在“专家团”页面创建并启用团队。

可以从下面两个任务开始：

```text
根据我上传的资料，提炼主要结论、风险和下一步行动，并生成一份 Word 报告供我下载。
```

```text
评审这份上线方案，从产品、技术和安全三个角度列出优先级明确的改进建议。
```

## Skill 和 MCP

Skill 用来描述适用场景、执行步骤、依赖工具和输出要求。在“技能中心”可以新建 Skill，也可以导入 `SKILL.md`、ZIP 包或公开 HTTPS 下载链接。安装完成后，可在技能列表中查看、停用、编辑或导出。

MCP 在“工具接入”中管理。平台可导入常见 `mcpServers` JSON，也可以手工配置本地 stdio、远程 Streamable HTTP 或普通 HTTP 工具。外部进程和网络工具默认关闭，需要部署者通过环境变量明确开启并设置白名单。安装服务记录后，还需要把服务 ID 绑定到相应智能体。

本地 stdio MCP 至少需要开启 `APP_ALLOW_STDIO_MCP`，并在 `APP_STDIO_COMMAND_ALLOWLIST` 中只列出经过审核的可执行程序；共享部署建议使用绝对路径。远程 MCP 或普通 HTTP 工具还需要开启出站总开关及对应功能，并用 `APP_REMOTE_HOST_ALLOWLIST` 限制目标主机。白名单只写主机名，多个值用逗号分隔。

MCP 页面会隐藏常见密钥字段，但服务配置仍以 JSON 保存在 SQLite 中，并未使用模型 Key 的加密存储。不要把 Token 直接写进配置；优先使用 `${ENV_NAME}` 引用服务进程中的环境变量，并把数据库当作敏感运行数据保护。

更完整的安装方式、配置样例和安全限制见 [平台使用指南](docs/PLATFORM_GUIDE.md)。

## 文件与产物

附件上传默认可用。TXT、Markdown、CSV、JSON、YAML、常见代码文件、DOCX、XLSX、PPTX 和 PDF 的可提取正文可以进入任务上下文。扫描版 PDF 没有 OCR，旧版 Office 文件也不在默认解析范围内。

平台内置 DOCX、PDF、XLSX、Markdown 和 HTML 生成能力。PPTX 只有在 Node.js 和 Artifact Tool 均已安装，并且入口文件配置有效时才可用：

```bash
export APP_NODE_BINARY='node'
export APP_ARTIFACT_TOOL_ENTRYPOINT='/absolute/path/to/artifact_tool.mjs'
```

项目不会自动安装 Artifact Tool。未配置或路径无效时，PPTX 任务会明确报错，不影响其他文档格式。

文件保存在 `data/` 下的运行目录中，并通过受控的预览和下载接口访问。数据库、上传文件、生成文件以及本机密钥都属于运行数据，不应提交到版本库。

## 重要边界

当前实现适合本机开发或受控内网使用，尚未包含完整的登录认证、多租户 RBAC、资源行级隔离、分布式任务队列和进程沙箱。不要未经加固直接暴露到不可信公网。

安装 Skill 不会自动执行包内脚本。本地 stdio MCP 会在平台服务器上启动进程，而不是在浏览器用户的电脑上运行。启用这类能力前，请限制命令、远程主机和工具权限。

`APP_ALLOW_OUTBOUND_NETWORK` 默认关闭，是在线模型、天气、联网搜索、远程 MCP、HTTP 工具、远程 Policy 和下载链接安装的总开关。总开关不会限制 stdio 子进程自身的网络访问；严格离线部署仍应在容器、主机防火墙或网络策略中禁止出站连接。

## 项目结构

```text
app/
  main.py                 FastAPI 入口和 HTTP/SSE 接口
  db.py                   SQLite 存储
  builtin_skill_catalog.py 对话中可确认安装的少量内置 Skill
  builtin_skills/         随平台加载的 Skill
  services/               任务、模型、Skill、MCP、专家团、记忆和产物服务
web/                      浏览器界面
examples/                 可手工安装的配置示例
docs/                     使用和架构说明
data/                     本地运行数据（不提交）
```

实现结构见 [架构说明](docs/ARCHITECTURE.md)。可用环境变量以 [.env.example](.env.example) 为准；修改环境变量后需要重启平台进程。
