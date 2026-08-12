---
id: report_generation
name: 报告生成 Skill
description: 用于生成 Markdown、Word、PDF、PowerPoint、Excel、HTML 等报告、汇报材料和结果文件。
category: builtin
version: 0.1.0
required_mcps: report,spreadsheet
---

# 报告生成 Skill

## 使用条件

当用户需要生成报告、总结、汇报、Excel 或其他可下载文件时，使用本 Skill。

## 推荐流程

1. 明确报告对象、使用人和输出格式。
2. 收集任务摘要、关键数据、风险点和结论。
3. 如需 XLSX 或 CSV，调用 `spreadsheet.create_excel` 并使用对应文件扩展名。
4. 如需 Word、PDF、PowerPoint、Markdown 或 HTML，调用 `report.generate_document` 并明确指定格式。
5. 返回下载链接和简短摘要。

## 质量要求

- 报告中要区分事实、模型结果和建议。
- 不确定的信息应明确标注来源和限制，不能写成确定事实。
- 涉及发布、覆盖或写回外部系统时，应遵守当前权限和审批要求。
