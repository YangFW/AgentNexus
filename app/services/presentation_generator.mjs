#!/usr/bin/env node

import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

const SLIDE_SIZE = { width: 1280, height: 720 };
const PAGE = { left: 42, top: 36, width: 1196, height: 594 };
const COLORS = {
  canvas: "#FFFFFF",
  ink: "#111111",
  muted: "#59616E",
  panel: "#EDEDED",
  rule: "#B8BCC4",
  accent: "#6DCBF4",
  accentStrong: "#3D8DFF",
};
const TYPEFACE = "Helvetica Neue";
const MAX_CONTENT_SLIDES = 8;
const META_HEADING = /^(?:视觉(?:元素)?建议|制作建议|设计说明|版式建议|排版建议|配色建议|图表建议|图片建议|演讲者备注|讲者备注|备注|visual direction|design notes?|production notes?|speaker notes?)(?:\s*[:：—-].*)?\s*$/i;

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || !value) {
      throw new Error("Expected --artifact-tool <path> --output <path>");
    }
    result[key.slice(2)] = value;
  }
  return result;
}

async function readStdin() {
  let value = "";
  for await (const chunk of process.stdin) value += chunk;
  return value;
}

function plainMarkdown(value) {
  return String(value || "")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/<[^>]+>/g, "")
    .replace(/[*_`~]+/g, "")
    .replace(/([A-Za-z])、(?=[A-Za-z])/g, "$1, ")
    .replace(/\s+/g, " ")
    .trim();
}

function summarizeRepeatedText(value, limit) {
  const text = plainMarkdown(value);
  if (text.length <= limit) return text;
  const slice = text.slice(0, Math.max(1, limit - 1));
  const boundary = Math.max(slice.lastIndexOf("。"), slice.lastIndexOf("；"), slice.lastIndexOf("，"), slice.lastIndexOf(" "));
  return `${(boundary > limit * 0.55 ? slice.slice(0, boundary + 1) : slice).trim()}…`;
}

function requireTextWithinLimit(value, limit, label) {
  const text = plainMarkdown(value);
  if (text.length > limit) {
    throw new Error(
      `PowerPoint 内容超出安全版式限制：${label}为 ${text.length} 字，最多 ${limit} 字。请缩短或拆分后重试。`,
    );
  }
  return text;
}

function chunks(values, size) {
  const result = [];
  for (let index = 0; index < values.length; index += size) {
    result.push(values.slice(index, index + size));
  }
  return result;
}

function sectionSlides(section) {
  return chunks(section.points, 6).map((points) => ({
    title: section.title,
    points,
  }));
}

function fitSectionsToSlides(sections) {
  const expanded = sections.flatMap(sectionSlides);
  if (expanded.length <= MAX_CONTENT_SLIDES) return expanded;

  // Prefer dedicated chapter slides. If they exceed the page budget, later
  // chapters can share slides only when every chapter title and point remains
  // visible in full.
  for (let prefixCount = sections.length - 1; prefixCount >= 0; prefixCount -= 1) {
    const dedicated = sections.slice(0, prefixCount).flatMap(sectionSlides);
    if (dedicated.length >= MAX_CONTENT_SLIDES) continue;

    const combinedPoints = [];
    let canCombine = true;
    for (const section of sections.slice(prefixCount)) {
      for (const point of section.points) {
        try {
          combinedPoints.push(requireTextWithinLimit(
            `${section.title}：${point}`,
            180,
            `章节“${section.title}”的合并要点`,
          ));
        } catch {
          canCombine = false;
          break;
        }
      }
      if (!canCombine) break;
    }
    if (!canCombine) continue;

    const availableSlides = MAX_CONTENT_SLIDES - dedicated.length;
    if (combinedPoints.length > availableSlides * 6) continue;
    const combinedGroups = chunks(combinedPoints, 6);
    const combined = combinedGroups.map((points, index) => ({
      title: combinedGroups.length > 1 ? `补充要点 ${index + 1}` : "补充要点",
      points,
    }));
    return [...dedicated, ...combined];
  }

  throw new Error(
    `PowerPoint 内容无法在 ${MAX_CONTENT_SLIDES} 个内容页内无损排版：共 ${sections.length} 个章节、`
      + `${sections.reduce((total, section) => total + section.points.length, 0)} 个要点。请减少内容或拆分为多份演示文稿。`,
  );
}

function filterProducerNotes(content) {
  const output = [];
  let skippingMetaSection = false;
  for (const raw of String(content || "").split(/\r?\n/)) {
    const trimmed = raw.trim();
    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const headingText = plainMarkdown(heading[2]);
      skippingMetaSection = META_HEADING.test(headingText);
      if (!skippingMetaSection) output.push(raw);
      continue;
    }
    if (skippingMetaSection) continue;
    const withoutListMarker = trimmed.replace(/^(?:[-*+]\s+|\d+[.)、]\s*)/, "");
    if (META_HEADING.test(plainMarkdown(withoutListMarker))) continue;
    output.push(raw);
  }
  return output.join("\n");
}

function parseSections(content, fallbackTitle) {
  const cleaned = filterProducerNotes(content);
  const sections = [];
  let current = { title: "核心内容", points: [] };
  let paragraph = [];

  const flushParagraph = () => {
    const value = requireTextWithinLimit(
      paragraph.join(" "),
      180,
      `章节“${current.title}”的段落`,
    );
    if (value) current.points.push(value);
    paragraph = [];
  };
  const flushSection = () => {
    flushParagraph();
    if (current.points.length) {
      sections.push(current);
    }
  };

  for (const raw of cleaned.split(/\r?\n/)) {
    const trimmed = raw.trim();
    if (!trimmed) {
      flushParagraph();
      continue;
    }
    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const nextTitle = requireTextWithinLimit(heading[2], 46, "章节标题");
      if (!nextTitle || nextTitle === plainMarkdown(fallbackTitle)) continue;
      flushSection();
      current = { title: nextTitle, points: [] };
      continue;
    }
    if (/^\|?.+\|.+\|?$/.test(trimmed)) {
      if (/^\|?[\s:|-]+\|[\s:|-]+\|?$/.test(trimmed)) continue;
      flushParagraph();
      const cells = trimmed
        .replace(/^\||\|$/g, "")
        .split("|")
        .map((cell) => plainMarkdown(cell))
        .filter(Boolean);
      if (cells.length) {
        current.points.push(requireTextWithinLimit(
          cells.join(" · "),
          180,
          `章节“${current.title}”的表格行`,
        ));
      }
      continue;
    }
    const item = trimmed.match(/^(?:[-*+]\s+|\d+[.)、]\s*)(.+)$/);
    if (item) {
      flushParagraph();
      const point = requireTextWithinLimit(
        item[1],
        180,
        `章节“${current.title}”的要点`,
      );
      if (point) current.points.push(point);
      continue;
    }
    paragraph.push(trimmed);
  }
  flushSection();

  if (!sections.length) {
    return [{
      title: "核心内容",
      points: [requireTextWithinLimit(fallbackTitle, 180, "核心内容")],
    }];
  }
  return fitSectionsToSlides(sections);
}

function addText(slide, name, text, position, style = {}) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    name,
    position,
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text = String(text || "");
  shape.text.style = {
    typeface: TYPEFACE,
    color: COLORS.ink,
    fontSize: 24,
    ...style,
  };
  return shape;
}

function addRect(slide, name, position, fill, line = "none") {
  return slide.shapes.add({
    geometry: "rect",
    name,
    position,
    fill,
    line: line === "none"
      ? { style: "solid", fill: "none", width: 0 }
      : { style: "solid", fill: line, width: 1 },
  });
}

function addFooter(slide, pageNumber) {
  addText(slide, `footer-${pageNumber}`, String(pageNumber).padStart(2, "0"), {
    left: 1184,
    top: 658,
    width: 54,
    height: 24,
  }, { fontSize: 14, color: COLORS.muted, alignment: "right" });
}

function addCover(presentation, title, sections) {
  const slide = presentation.slides.add();
  slide.background.fill = COLORS.canvas;
  addRect(slide, "cover-accent", { left: 42, top: 147, width: 12, height: 274 }, COLORS.accentStrong);
  addText(slide, "cover-kicker", "演示文稿", { left: 42, top: 42, width: 480, height: 38 }, {
    fontSize: 24,
    bold: true,
    color: COLORS.muted,
  });
  addText(slide, "cover-title", title, { left: 84, top: 146, width: 1040, height: 282 }, {
    fontSize: title.length > 42 ? 68 : 80,
    bold: true,
    verticalAlignment: "middle",
  });
  const subtitle = sections[0]?.points[0] || sections[0]?.title || "围绕关键内容形成清晰结论";
  addText(slide, "cover-subtitle", summarizeRepeatedText(subtitle, 86), { left: 84, top: 468, width: 850, height: 100 }, {
    fontSize: 28,
    color: COLORS.muted,
  });
  addRect(slide, "cover-rule", { left: 84, top: 607, width: 1154, height: 2 }, COLORS.rule);
  return slide;
}

function splitPoint(point) {
  const match = String(point).match(/^(.{2,24}?)[：:](.+)$/);
  if (!match) return { heading: "", body: point };
  return {
    heading: match[1],
    body: requireTextWithinLimit(match[2], 150, `要点“${match[1]}”的正文`),
  };
}

function addPoint(slide, point, index, position, emphasis = false) {
  const compactColumn = position.width < 400;
  addRect(slide, `point-rule-${index}`, {
    left: position.left,
    top: position.top,
    width: emphasis ? 62 : 42,
    height: 5,
  }, index % 2 === 0 ? COLORS.accentStrong : COLORS.accent);
  addText(slide, `point-number-${index}`, String(index + 1).padStart(2, "0"), {
    left: position.left,
    top: position.top + 20,
    width: 62,
    height: 42,
  }, { fontSize: 24, bold: true, color: COLORS.muted });
  const { heading, body } = splitPoint(point);
  const textTop = position.top + 66;
  if (heading) {
    addText(slide, `point-heading-${index}`, heading, {
      left: position.left,
      top: textTop,
      width: position.width,
      height: 44,
    }, { fontSize: 32, bold: true });
    addText(slide, `point-body-${index}`, body, {
      left: position.left,
      top: textTop + 53,
      width: position.width,
      height: position.height - 119,
    }, { fontSize: compactColumn ? 22 : emphasis ? 26 : 23, color: COLORS.muted, verticalAlignment: "top" });
  } else {
    addText(slide, `point-body-${index}`, body, {
      left: position.left,
      top: textTop,
      width: position.width,
      height: position.height - 66,
    }, { fontSize: compactColumn ? 22 : emphasis ? 29 : 25, bold: emphasis, color: emphasis ? COLORS.ink : COLORS.muted, verticalAlignment: "top" });
  }
}

function contentFrames(pointCount) {
  if (pointCount <= 2) {
    const width = pointCount === 1 ? 820 : 560;
    const gap = 48;
    return Array.from({ length: pointCount }, (_, index) => ({
      left: 42 + index * (width + gap), top: 220, width, height: 360,
    }));
  }
  if (pointCount === 3) {
    return Array.from({ length: 3 }, (_, index) => ({
      left: 42 + index * 411, top: 292, width: 374, height: 290,
    }));
  }
  if (pointCount === 4) {
    return Array.from({ length: 4 }, (_, index) => ({
      left: 42 + (index % 2) * 615,
      top: 194 + Math.floor(index / 2) * 220,
      width: 560,
      height: 190,
    }));
  }
  return Array.from({ length: pointCount }, (_, index) => ({
    left: 42 + (index % 3) * 411,
    top: 204 + Math.floor(index / 3) * 224,
    width: 374,
    height: 196,
  }));
}

function addContentSlide(presentation, section, pageNumber) {
  const slide = presentation.slides.add();
  slide.background.fill = COLORS.canvas;
  addText(slide, `section-title-${pageNumber}`, section.title, {
    left: PAGE.left,
    top: PAGE.top,
    width: PAGE.width,
    height: 112,
  }, { fontSize: 48, bold: true, verticalAlignment: "top" });
  addRect(slide, `section-rule-${pageNumber}`, { left: 42, top: 148, width: 1196, height: 2 }, COLORS.rule);

  if (section.points.length > 6) {
    throw new Error(`PowerPoint 内部排版错误：章节“${section.title}”超过 6 个要点`);
  }
  const points = section.points;
  const frames = contentFrames(points.length);
  points.forEach((point, index) => addPoint(slide, point, index, frames[index], points.length <= 2));
  addFooter(slide, pageNumber);
  return slide;
}

function addSynthesis(presentation, sections, pageNumber) {
  const takeaways = sections.length >= 3
    ? sections.slice(0, 3).map((section) => summarizeRepeatedText(`${section.title}：${section.points[0] || ""}`, 120))
    : sections.flatMap((section) =>
      section.points.map((point) => summarizeRepeatedText(`${section.title}：${point}`, 120)),
    ).slice(0, 3);
  const slide = presentation.slides.add();
  slide.background.fill = COLORS.canvas;
  addText(slide, "synthesis-kicker", "总结", { left: 42, top: 42, width: 320, height: 40 }, {
    fontSize: 24,
    bold: true,
    color: COLORS.muted,
  });
  addText(slide, "synthesis-title", "关键结论", { left: 42, top: 150, width: 992, height: 120 }, {
    fontSize: 72,
    bold: true,
  });
  addText(slide, "synthesis-points", takeaways.map((value, index) => `${index + 1}. ${value}`).join("\n\n"), {
    left: 42,
    top: 346,
    width: 1000,
    height: 240,
  }, { fontSize: 28, color: COLORS.muted });
  addRect(slide, "synthesis-accent", { left: 1102, top: 150, width: 136, height: 436 }, COLORS.accent);
  addFooter(slide, pageNumber);
  return slide;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const artifactToolPath = path.resolve(args["artifact-tool"] || "");
  const outputPath = path.resolve(args.output || "");
  if (!artifactToolPath || !outputPath) throw new Error("Missing generation path");
  const payload = JSON.parse(await readStdin());
  const title = requireTextWithinLimit(payload.title || "演示文稿", 72, "演示文稿标题");
  const sections = parseSections(payload.content || "", title);
  const { Presentation, PresentationFile } = await import(pathToFileURL(artifactToolPath).href);

  const presentation = Presentation.create({ slideSize: SLIDE_SIZE });
  addCover(presentation, title, sections);
  sections.forEach((section, index) => addContentSlide(presentation, section, index + 2));
  const pointCount = sections.reduce((total, section) => total + section.points.length, 0);
  if (sections.length >= 2 || pointCount >= 4) {
    addSynthesis(presentation, sections, presentation.slides.items.length + 1);
  }

  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  const pptx = await PresentationFile.exportPptx(presentation);
  await pptx.save(outputPath);
  process.stdout.write(`${JSON.stringify({
    slideCount: presentation.slides.items.length,
    output: outputPath,
    contentTruncated: false,
  })}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error?.stack || error}\n`);
  process.exitCode = 1;
});
