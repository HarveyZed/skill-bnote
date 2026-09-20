"""L8 合并层（v4）：结构来自 manifest.json，正文是纯 Markdown，合成前只做结构校验。

变化（相对 v3）：
  * 章节结构（编号/标题/时间范围/slide/要点/问题/校正/复核备注）全部在 chapters/manifest.json，
    由 manifest.validate 做严格结构校验；正文文件是纯正文，不再有 front matter 与 meta 块；
  * 移除了用词黑名单：不想让 agent 写进正文的内容写进它的写作契约（prompt / SKILL.md），
    而不是事后 grep —— 事后用词检查既拦不住换词，又会误杀真实内容（校验 / 复核都踩过）。
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

from . import manifest as manifest_layer

FENCE = chr(96) * 3
NL = chr(10)
IMG_ANY = re.compile(r"!\[([^\]]*)\]\((?:\.\./)?slides/([^)]+)\)")
HEADING = re.compile(r"^#{1,5}\s")


def _demote(text: str) -> str:
    out, in_fence = [], False
    for line in text.splitlines():
        if line.startswith(FENCE):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and HEADING.match(line):
            out.append("#" + line)
            continue
        out.append(line)
    return NL.join(out)


def _mmss(t: str) -> str:
    h, m, s = (int(x) for x in str(t).split(":"))
    return ("%02d:%02d:%02d" % (h, m, s)) if h else ("%02d:%02d" % (m, s))


def _outline(man: dict) -> list[str]:
    lines = ["## 目录", ""]
    for i, ch in enumerate(man["chapters"], start=1):
        a, b = ch["range"]
        sl = ", ".join("%04d" % n for n in ch.get("slides") or [])
        lines.append("%d. **%s** — %s–%s%s" % (i, ch["title"], _mmss(a), _mmss(b),
                                               (" ｜ slide %s" % sl) if sl else ""))
    return lines + [""]


def _table(man: dict) -> list[str]:
    lines = ["## 章节一览", "", "| # | 章节 | 时间 | slide |", "|---|---|---|---|"]
    for i, ch in enumerate(man["chapters"], start=1):
        a, b = ch["range"]
        sl = ", ".join("%04d" % n for n in ch.get("slides") or [])
        lines.append("| %d | %s | %s–%s | %s |" % (i, ch["title"], _mmss(a), _mmss(b), sl or "-"))
    return lines + [""]


def _timeline(man: dict) -> list[str]:
    lines = ["## 时间线", "", FENCE + "mermaid", "timeline", "    title 课程时间线"]
    for ch in man["chapters"]:
        lines.append("    %s : %s" % (_mmss(ch["range"][0]).replace(":", "："),
                                      str(ch["title"]).replace(":", "：")))
    return lines + [FENCE, ""]


def _collect_review(man: dict) -> str:
    out = ["# 复核信息（来自 manifest.json，不进讲义）", ""]
    for ch in man["chapters"]:
        blocks = []
        for c in ch.get("corrections") or []:
            if isinstance(c, dict):
                blocks.append("- 校正：%s → %s（依据：%s）" % (c.get("wrong"), c.get("right"), c.get("evidence", "-")))
            else:
                blocks.append("- 校正：%s" % c)
        for f in ch.get("review_flags") or []:
            blocks.append("- 警告：%s" % f)
        if ch.get("coverage_notes"):
            blocks.append("- 覆盖说明：%s" % ch["coverage_notes"])
        if ch.get("review_notes"):
            blocks.append(NL + ch["review_notes"])
        if blocks:
            out += ["## %s %s" % (ch["id"], ch["title"]), ""] + blocks + [""]
    return NL.join(out)


def build(cfg, paths, meta: dict, seg_data: dict, transcript: dict | None = None) -> dict:
    mc = cfg.get("merge", {})
    out = paths.out
    man = manifest_layer.load(paths)
    if man is None:
        legacy = sorted(paths.chapters().glob("0*.md"))
        if legacy:
            print("[merge] 未找到 manifest.json，检测到 v3 章节 → 自动迁移（原文件备份在 chapters/_v3_backup/）")
            man = manifest_layer.migrate(paths, meta)
    if man is None:
        print("[merge] 还没有章节结构，跳过合并")
        return {}

    errors, warns = manifest_layer.validate_all(cfg, paths, meta, transcript)[1:]
    meta_dir = out / "_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "corrections.md").write_text(_collect_review(man), encoding="utf-8")

    def _line(e):
        bits = ["[%s]" % e.get("owner", "?")]
        if e.get("chapter"):
            bits.append("章 %s" % e["chapter"])
        bits.append(e.get("message", ""))
        if e.get("fix_hint"):
            bits.append("→ %s" % e["fix_hint"])
        return "- " + " ".join(bits)

    routing = {}
    for e in list(errors) + list(warns):
        routing.setdefault(e.get("owner", "?"), []).append(e.get("message", ""))

    report = ["# lecture.md 结构校验报告", "",
              "- 结果：%s" % ("通过" if not errors else "未通过（未覆盖 lecture.md）"),
              "- 错误：%d ｜ 警告：%d" % (len(errors), len(warns)),
              "- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
              "- 章节数：%d ｜ slide 数：%s" % (len(man["chapters"]), man.get("slide_count")), ""]
    if routing:
        report += ["## 归属（谁该修）", ""] + ["- %s：%d 条" % (k, len(v)) for k, v in sorted(routing.items())] + [""]
    if errors:
        report += ["## 错误", ""] + [_line(e) for e in errors] + [""]
    if warns:
        report += ["## 警告", ""] + [_line(e) for e in warns] + [""]
    if not errors and not warns:
        report += ["结构校验通过：manifest schema、字段类型、正文文件存在且为纯正文、"
                   "时间范围连续且覆盖全片、slide 编号合法、每章 keypoints/questions 达标。", ""]
    (meta_dir / "validation.md").write_text(NL.join(report), encoding="utf-8")
    (meta_dir / "validation.json").write_text(
        json.dumps({"result": "pass" if not errors else "fail",
                    "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "errors": errors, "warnings": warns,
                    "routing": {k: v for k, v in sorted(routing.items())}},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    if errors:
        print("[merge] 结构校验未通过：%d 错 / %d 警 → %s" % (len(errors), len(warns), meta_dir / "validation.md"))
        for e in errors[:8]:
            print("   ✗ [%s] %s" % (e.get("owner"), e.get("message")))
        print("[merge] 归属汇总（详见 _meta/validation.json，按 owner 派修）：")
        for k, v in sorted(routing.items()):
            who = {"manifest": "编排者", "pipeline": "工具/流水线"}.get(k, "该章写作 agent 交回")
            print("   %-14s %d 条 → %s" % (k, len(v), who))
        print("[merge] 已保留原有 lecture.md（未覆盖）")
        return {"ok": False, "errors": errors, "warns": warns, "routing": routing}
    if warns:
        print("[merge] 结构校验通过（%d 警告，见 %s）" % (len(warns), meta_dir / "validation.md"))

    video = man.get("video") or {}
    dur = int(video.get("duration") or meta.get("duration") or 0)
    head = ["# %s" % (video.get("title") or meta.get("part") or paths.vid), "",
            "%s ｜ 时长 %02d:%02d:%02d ｜ %d 章 ｜ [完整字幕](transcript.md)" % (
                meta.get("owner") or "-", dur // 3600, (dur % 3600) // 60, dur % 60, len(man["chapters"])), ""]
    style = str(mc.get("outline", "list+table")).lower()
    head += _outline(man)
    if "table" in style:
        head += _table(man)
    if "timeline" in style:
        head += _timeline(man)
    head += ["---", ""]

    body = []
    for i, ch in enumerate(man["chapters"], start=1):
        a, b = ch["range"]
        sl = ", ".join("%04d" % n for n in ch.get("slides") or []) or "-"
        body += ["## %d. %s" % (i, ch["title"]), "", "*%s–%s ｜ slide %s*" % (_mmss(a), _mmss(b), sl), ""]
        body.append(_demote((paths.chapters() / ch["body"]).read_text(encoding="utf-8").strip()))
        body.append(NL + "---" + NL)
    merged = IMG_ANY.sub(lambda m: "![%s](slides/%s)" % (m.group(1), m.group(2)), NL.join(head) + NL.join(body))

    p1 = out / mc.get("filename", "lecture.md")
    p1.write_text(merged, encoding="utf-8")
    result = {"ok": True, "merged": str(p1), "merged_kb": round(p1.stat().st_size / 1024, 1),
              "errors": [], "warns": warns}
    print("[merge] 纯讲义 → %s (%.0f KB)" % (p1, p1.stat().st_size / 1024))
    if mc.get("standalone", True):
        def embed(m):
            p = out / "slides" / m.group(2)
            if not p.exists():
                return m.group(0)
            return "![%s](data:image/jpeg;base64,%s)" % (m.group(1), base64.b64encode(p.read_bytes()).decode("ascii"))
        p2 = out / mc.get("standalone_filename", "lecture.standalone.md")
        p2.write_text(IMG_ANY.sub(embed, merged), encoding="utf-8")
        result["standalone"] = str(p2)
        print("[merge] 自包含版 → %s (%.0f KB)" % (p2, p2.stat().st_size / 1024))
    return result
