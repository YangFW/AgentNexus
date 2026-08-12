---
id: markdown_document
name: Markdown 文档生成 Skill
description: 生成结构化 Markdown 正文或可下载的 MD 文件。
category: document
version: 1.0.0
required_mcps: report
---

# Markdown 文档生成

当用户明确要求 Markdown、MD 文件或 Markdown 报告时使用。

1. 使用规范标题、列表、表格、引用和代码块。
2. 只要求页面展示时直接流式输出 Markdown。
3. 要求下载文件时调用 `report.generate_document`，格式为 `md`。
4. 校验页面渲染和下载内容一致。
