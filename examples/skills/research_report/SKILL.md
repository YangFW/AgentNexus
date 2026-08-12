---
id: research_report
name: 联网研究报告 Skill
description: 围绕指定主题开展联网检索、交叉核验来源并生成带链接的研究报告。
category: research
version: 1.0.0
required_mcps: web-search,report
---

# 联网研究报告

## 触发条件

用户要求搜索最新信息、竞品调研、行业研究或带来源报告时使用。

## 执行流程

1. 将主题拆成 3 至 5 个检索问题。
2. 调用 `web-search.search` 获取候选来源。
3. 对关键结论至少使用两个独立来源交叉核验。
4. 明确区分事实、推断和建议，并保留来源 URL 与访问日期。
5. 按用户要求调用 `report.generate_document` 输出报告。

## 质量要求

- 不伪造来源，不输出无法追溯的数字。
- 时效性信息注明日期。
- 搜索不可用时明确说明，不用模型记忆冒充实时结果。
