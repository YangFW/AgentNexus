---
id: word_document
name: Word 文档生成 Skill
description: 将对话结果、报告、方案或会议内容整理为可下载的 DOCX 文档。
category: document
version: 1.0.0
required_mcps: report
---

# Word 文档生成

当用户明确要求 Word、DOCX 或可下载文档时使用。

1. 先生成结构完整的 Markdown 正文，包含标题、段落、列表和必要表格。
2. 调用 `report.generate_document`，格式必须为 `docx`。
3. 工具返回产物后，确认扩展名和下载地址。
4. 不得只输出代码或让用户自行安装依赖。
