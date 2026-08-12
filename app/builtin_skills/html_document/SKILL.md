---
id: html_document
name: HTML 文档生成 Skill
description: 将报告、说明和结构化内容生成可下载、可在浏览器打开的 HTML 文件。
category: document
version: 1.0.0
required_mcps: report
---

# HTML 文档生成

当用户要求 HTML、网页文档或可离线打开的页面文件时使用。

1. 先整理语义清晰的标题、段落和列表。
2. 调用 `report.generate_document`，格式必须为 `html`。
3. 输出必须包含 UTF-8、响应式 viewport 和基础可读样式。
4. 校验文件扩展名和下载地址。
