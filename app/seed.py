from __future__ import annotations

from app import db


DEFAULT_AGENTS = (
    {
        "id": "general-agent",
        "name": "智枢助手",
        "description": "面向分析、总结、规划、文档生成和工具协作等常见任务的默认助手。",
        "model": "deterministic",
        "system_prompt": (
            "准确理解用户目标，优先选择合适的 Skill 和 MCP 工具执行，"
            "并以清晰、可核验的结果完成交付。"
        ),
        "skills": ["general_task", "report_generation"],
        "mcp_servers": ["spreadsheet", "report", "web-search", "weather"],
        "permissions": {"filesystem": "workspace_only"},
    },
)


def seed_agents() -> None:
    """Create the built-in assistant without overwriting user customizations."""

    now = db.utc_now()
    for agent in DEFAULT_AGENTS:
        if db.query_one("SELECT id FROM agents WHERE id = ?", (agent["id"],)):
            continue
        db.execute(
            """
            INSERT INTO agents(
                id, name, description, model, system_prompt, skills_json,
                mcp_servers_json, permissions_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent["id"],
                agent["name"],
                agent["description"],
                agent["model"],
                agent["system_prompt"],
                db.json_dumps(agent["skills"]),
                db.json_dumps(agent["mcp_servers"]),
                db.json_dumps(agent["permissions"]),
                now,
                now,
            ),
        )
