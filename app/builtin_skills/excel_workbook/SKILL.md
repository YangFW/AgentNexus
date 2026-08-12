---
id: excel_workbook
name: Excel 工作簿生成 Skill
description: 将结构化数据、清单、计划和分析结果生成可下载的 XLSX 工作簿。
category: document
version: 1.0.0
required_mcps: spreadsheet
---

# Excel 工作簿生成

当用户要求 Excel、XLSX、电子表格或可下载数据表时使用。

1. 将内容整理为字段稳定的行数据。
2. 调用 `spreadsheet.create_excel` 生成 `xlsx` 文件。
3. 校验表头、行数、文件扩展名和下载地址。
4. 数据不足时保留“待补充”字段，不编造数据。
