from __future__ import annotations

from typing import Any


BUILTIN_SKILL_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "mermaid_diagram",
        "name": "Mermaid 图表设计 Skill",
        "description": "生成流程图、时序图、架构图和状态图对应的 Mermaid 源码，便于复制到支持 Mermaid 的编辑器中使用。",
        "keywords": ("mermaid", "流程图", "时序图", "架构图", "状态图"),
        "source_label": "智枢内置目录",
        "content": """---
id: mermaid_diagram
name: Mermaid 图表设计 Skill
description: 生成流程图、时序图、架构图和状态图对应的 Mermaid 源码。
category: recommended
version: 1.0.0
---
# Mermaid 图表设计

当用户要求流程图、时序图、架构图或状态图时使用。

1. 先确定最适合的图类型和节点关系。
2. 节点文字保持简短，包含标点时使用引号。
3. 输出完整的 Mermaid fenced code block，并附一段阅读说明。平台只生成源码，不承诺在当前页面中渲染成图。
4. 复杂图拆成两张，避免单图节点过多。
""",
    },
    {
        "id": "product_requirement_document",
        "name": "产品需求文档 PRD Skill",
        "description": "将产品想法整理为背景、目标、用户故事、功能范围、验收标准和风险清单。",
        "keywords": ("prd", "产品需求文档", "用户故事", "验收标准"),
        "source_label": "智枢内置目录",
        "content": """---
id: product_requirement_document
name: 产品需求文档 PRD Skill
description: 将产品想法整理为目标、用户故事、功能范围、验收标准和风险清单。
category: recommended
version: 1.0.0
required_mcps: report
---
# 产品需求文档 PRD

1. 明确背景、目标用户、问题和成功指标。
2. 使用用户故事描述核心场景。
3. 区分本期范围、非本期范围和依赖项。
4. 每项功能提供可验证的验收标准。
5. 用户要求下载时调用报告工具生成 Word、PDF 或 Markdown。
""",
    },
)


def recommend_builtin_skill(
    message: str, installed_ids: set[str]
) -> dict[str, Any] | None:
    lowered = message.lower()
    for item in BUILTIN_SKILL_CATALOG:
        if item["id"] in installed_ids:
            continue
        if any(keyword.lower() in lowered for keyword in item["keywords"]):
            return item
    return None


def get_builtin_skill(skill_id: str) -> dict[str, Any] | None:
    return next((item for item in BUILTIN_SKILL_CATALOG if item["id"] == skill_id), None)
