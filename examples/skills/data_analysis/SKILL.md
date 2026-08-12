---
id: data_analysis
name: 表格数据分析 Skill
description: 分析 CSV、Excel 或结构化数据，检查质量、汇总指标、识别异常并输出 Excel 结果。
category: data
version: 1.0.0
required_mcps: spreadsheet,report
---

# 表格数据分析

## 触发条件

用户上传 CSV、Excel 或提供结构化数据并要求统计、清洗、找异常、生成结果表时使用。

## 执行流程

1. 检查字段、类型、缺失值、重复值和异常范围。
2. 说明统计口径，再计算核心指标。
3. 将原始事实、计算结果和业务建议分开。
4. 调用 `spreadsheet.create_excel` 输出可下载结果；需要说明文档时调用 `report.generate_document`。

## 质量要求

- 不静默丢弃异常数据。
- 计算结果需说明单位和口径。
- 数据不足时标记限制，不编造结论。
