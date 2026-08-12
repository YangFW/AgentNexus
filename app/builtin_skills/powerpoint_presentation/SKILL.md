---
id: powerpoint_presentation
name: PowerPoint 演示文稿生成 Skill
description: 将方案、介绍、汇报和研究结果生成可直接下载的 PPTX 演示文稿。
category: document
version: 1.0.0
required_mcps: report
---

# PowerPoint 演示文稿生成

当用户要求 PPT、PPTX、PowerPoint、幻灯片或演示文稿时使用。

1. 先组织封面、章节和每页要点，单页避免堆积长段落。
2. 正文使用 Markdown 标题划分幻灯片，列表表示页面要点。
3. 调用 `report.generate_document`，格式必须为 `pptx`。
4. 校验文件真实存在且能通过平台下载，不能只返回 PPT 脚本。
