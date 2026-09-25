"""L9 学习笔记层：为"全局视角的学习笔记（note.md）"准备素材并做校验。

为什么要独立于讲义：
  * 讲义（lecture.md）= 视频里讲了什么（内容完整、可读，不含工程碎片）；
  * 笔记（note.md）   = 学完之后要记住什么（课程地图、核心概念、跨章关联、思考题、行动项）。
  两者读者与写作要求不同：讲义可以分章并行写，笔记必须**读完全部章节后**由单次全局写作产出，
  否则做不出跨章衔接（前置知识、前后呼应、易混点对照）。

本层做两件事：
  1. 汇总各章 front matter 里的 keypoints / questions，生成 _meta/note_brief.md（全局写作的输入）；
  2. 校验 note.md 是否符合契约（缺章节、含工程词、缺知识地图等一律报错）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import manifest as manifest_layer
from . import prompt as prompt_layer

FENCE = chr(96) * 3
NL = chr(10)
SENT_TS = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")


def _chapter_files(paths):
    return sorted(paths.chapters().glob("0*.md"))



def _hms(sec) -> str:
    sec = int(sec or 0)
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


_HEAD_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
_SEC_TIME_RE = re.compile(r"^\*(\d{1,2}:\d{2}(?::\d{2})?)[–\-—~]\d{1,2}:\d{2}(?::\d{2})?\s*[|｜]\s*slide")
_TR_TIME_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.+)$")


def _hms_short(sec: int) -> str:
    if sec < 3600:
        return "%02d:%02d" % (sec // 60, sec % 60)
    return _hms(sec)


def anchor_candidates(paths, limit: int = 40, preview_chars: int = 30) -> list[tuple]:
    """「小节锚点候选」：讲义里的 时间 → 小节标题 → 那一刻的字幕首句。

    为什么给材料、而不是事后判错：结构校验只能保证锚点落在范围内且单调递增，
    证明不了它指对位置（契约里已写明）；而笔记标题是写手改写过的、与讲义标题几乎对不上，
    自动比对在 5 篇真实笔记（70 个节点）上误报约 8%。把工具已经知道的时间点与原文列出来让写手挑，
    比事后猜稳。
    """
    lec = paths.out / "lecture.md"
    if not lec.exists():
        return []
    sections, title = [], None
    for line in lec.read_text(encoding="utf-8").splitlines():
        m = _HEAD_RE.match(line)
        if m:
            title = (len(m.group(1)), m.group(2))
            continue
        m = _SEC_TIME_RE.match(line.strip())
        if m and title:
            parts = [int(x) for x in m.group(1).split(":")]
            sec = parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]
            sections.append((sec, title[0], title[1]))
            title = None
    if not sections:
        return []
    deep = [s for s in sections if s[1] >= 3]        # 优先小节（###）；没有就用章（##）
    use = deep or sections
    rows = []
    tr = paths.out / "transcript.md"
    if tr.exists():
        for line in tr.read_text(encoding="utf-8").splitlines():
            m = _TR_TIME_RE.match(line.strip())
            if m:
                p = [int(x) for x in m.group(1).split(":")]
                rows.append((p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1],
                             m.group(2).strip()))
    out = []
    for sec, _lvl, t in use:
        # 取该时刻起 18 秒内**第一句像话**的字幕（跳过「好」「那么好」这类口水句），没有就取最近的一句
        near = [r for r in rows if sec - 3 <= r[0] <= sec + 18]
        pv = next((x[1] for x in near if len(x[1]) >= 6), "")
        if not pv:
            later = [r for r in rows if r[0] >= sec]
            pv = near[0][1] if near else (later[0][1] if later else "")
        pv = re.sub(r"[*_`]", "", pv)[:preview_chars]
        out.append((sec, t, pv))
    if len(out) > limit:                              # 太长就均匀抽样（保留首尾）
        step = len(out) / float(limit)
        out = [out[int(i * step)] for i in range(limit)]
    return out


def build_brief(cfg, paths, meta: dict, seg_data: dict, transcript: dict | None = None) -> Path:
    text_mode = seg_data.get("mode") == "text"   # 信息流模式：没有章节，按段落锚点组织
    nc = cfg.get("note", {})
    lines = ["# 全局笔记写作输入（note_brief）", "",
             ("本文件由 bnote note 生成，给**单次全局写作**用：读它 + `lecture.md`（整理稿）与上面的段落锚点表，产出 note.md。"
              if text_mode else
              "本文件由 bnote note 生成，给**单次全局写作**用：读它 + 各章 chapters/*.md，产出 note.md。"),
             "不要在 note.md 里出现 bnote 相关的工程词（校验会拦）。", "",
             "## 课程元信息", "",
             "- 标题: %s" % (meta.get("part") or meta.get("title")),
             "- 合集: %s" % meta.get("title"),
             "- UP: %s ｜ 时长: %ss ｜ 分P: P%s / %s" % (
                 meta.get("owner"), meta.get("duration"), meta.get("page"), meta.get("page_count")),
             "- 源: %s" % meta.get("url"),
             ("- 段落数: %d ｜ 字幕段数: %s" if text_mode else "- slide 页数: %d ｜ 字幕段数: %s") % (
                 len(seg_data.get("segments", [])),
                 len((transcript or {}).get("segments", []))),
             "", "## 视频背景（来自视频页元信息：可用作判断，不许当课程内容写进笔记）", "",
             prompt_layer.video_meta_block(cfg, meta), "",
             ("## 段落锚点（信息流模式：没有章节，按段落组织；node 锚点请用这些起点）" if text_mode
              else "## 各章要点（供合成知识地图 / 分章要点）"), ""]
    total_kp = 0
    man = manifest_layer.load(paths) or {}
    rows = man.get("chapters") or []
    if not text_mode:
        # 锚点候选表：把工具已知的「时间 + 那一刻的字幕原文」摊给写手挑，别让它凭印象回忆时间
        cand = anchor_candidates(paths,
                                  limit=int(nc.get("anchor_candidate_limit", 40)),
                                  preview_chars=int(nc.get("anchor_preview_chars", 30)))
        if cand:
            total_sec = len(cand)
            lines += ["## 小节锚点候选（**node 的锚点请从这里挑**：小节起点 + 那一刻的字幕首句）", "",
                      "- 这张表由工具从 `lecture.md` 的小节时间行与 `transcript.md` 生成，是事实材料；",
                      "- 锚点必须单调递增、且是字幕里真实出现过的时间（校验会拦自编时间）；",
                      "- 表里没有、但字幕里确实更合适的时刻，可以用（写清楚你选它的理由）。", ""]
            lines += ["- `[%s]` %s ｜ %s" % (_hms_short(s), t, pv) for s, t, pv in cand]
            lines.append("")
    if text_mode:
        for p in (seg_data.get("segments") or [])[:int(nc.get("paragraph_anchor_limit", 400))]:
            preview = " ".join((p.get("text") or "").split())[:int(nc.get("paragraph_preview_chars", 36))]
            lines.append("- [%s] %s" % (_hms(p.get("t_start")), preview))
        lines.append("")
    for fm in rows:
        kps = fm.get("keypoints") or []
        qs = fm.get("questions") or []
        total_kp += len(kps)
        rng = fm.get("range") or ["", ""]
        lines += ["### %s %s（%s-%s）" % (fm.get("id", ""), fm.get("title", ""),
                                          rng[0] if len(rng) > 0 else "", rng[1] if len(rng) > 1 else ""), ""]
        if kps:
            lines += ["- " + str(k) for k in kps]
        else:
            lines.append("- （本章 front matter 未提供 keypoints，请从正文自行提炼）")
        if qs:
            lines += ["", "自测题："] + ["- " + str(q) for q in qs]
        lines.append("")
    # ---- 契约要点（内联，免得写手自己去翻契约模板/源码；结构校验会按这些拦）----
    lec = paths.out / "lecture.md"
    l_chars = len("".join(lec.read_text(encoding="utf-8").split())) if lec.exists() else 0
    ratio = float(nc.get("max_ratio", 0.2))
    budget = int(l_chars * ratio) if l_chars else 0
    heads = "、".join(nc.get("head_sections") or [])
    tails = "、".join(nc.get("tail_sections") or [])
    labels = " / ".join(nc.get("field_labels") or [])
    need = "、".join(nc.get("require_per_node") or ["提炼"])
    use_terms = []
    gp = Path(cfg["paths"]["state_root"]) / "glossary" / ("%s.json" % paths.vid)
    if gp.exists():
        try:
            gd = json.loads(gp.read_text(encoding="utf-8"))
            if gd.get("confirmed"):
                use_terms = list(gd.get("terms_use") or [])[:int(nc.get("glossary_terms_limit", 20))]
        except Exception:
            use_terms = []
    lines += ["## 契约要点（照这些写；结构校验会拦）", "",
              "- 头部小节（顺序照写）：%s" % (heads or "（未配置）"),
              "- 尾部小节：%s" % (tails or "（未配置）"),
              "- 正文：沿原视频时间轴的 **%s–%s 个节点**，每个节点一个 `## [MM:SS] 节点标题`（锚点必须单调递增）"
              % (nc.get("min_nodes", 6), nc.get("max_nodes", 30)),
              "- 每个节点内至少一条 **%s**；可选注释类型只有：%s" % (need, labels or "（未配置）"),
              "- 注释写法：`- 提炼：…` / `- 联想：…`（行首短横线 + 类型名 + 全角冒号）",
              "- 篇幅：正文 <= 讲义正文的 %.0f%%%s（超了只会多一条提示，**不能靠删事实来压**）"
              % (ratio * 100, ("；当前讲义 %d 字 → 约 %d 字" % (l_chars, budget)) if budget else ""),
              "- 术语：%s" % ("、".join(use_terms) + "（已确认白名单）" if use_terms else
                            "未确认 → 以字幕上下文用字为准，拿不准就地括注，不要猜"),
              "- 不要在 note.md 里出现 bnote 相关的工程词（校验会拦）。", ""]
    lines += ["## 统计", "", "- 章节数: %d" % len(_chapter_files(paths)),
              "- keypoints 合计: %d" % total_kp, ""]
    p = paths.out / "_meta" / "note_brief.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(NL.join(lines), encoding="utf-8")
    print("[note] 全局写作输入 → %s（keypoints %d 条）" % (p, total_kp))
    return p


NODE_RE = re.compile(r"^##\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.+?)\s*$")
FIELD_RE = re.compile(r"^\s*[-*]\s*(\S+?)\s*[：:]")


def _sec3(t: str) -> int:
    parts = [int(x) for x in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_nodes(text: str):
    """解析 note.md 里  ## [MM:SS] 标题  形式的关键节点"""
    nodes, cur = [], None
    for line in text.splitlines():
        m = NODE_RE.match(line)
        if m:
            cur = {"time": m.group(1), "title": m.group(2), "fields": {}, "lines": []}
            nodes.append(cur)
            continue
        if re.match(r"^##\s", line):
            # 任何其它二级标题（如尾部小节 带走三件事/未解之问/自测/行动）都结束当前节点，
            # 否则尾部的列表项会被误判成最后一个节点的注释类型
            cur = None
            continue
        if cur is not None:
            fm = FIELD_RE.match(line)
            if fm:
                cur["fields"].setdefault(fm.group(1), []).append(line.strip())
            cur["lines"].append(line)
    return nodes


def export_hooks(cfg, paths, note_text: str, nodes: list[dict]):
    """把每个节点的"钩子"抽成机器可读文件，供未来跨 lecture 串联使用"""
    hooks = []
    for n in nodes:
        for h in n["fields"].get("钩子", []):
            body = re.sub(r"^\s*[-*]\s*钩子\s*[：:]\s*", "", h).strip()
            hooks.append({"lecture": paths.vid, "time": n["time"], "node": n["title"], "hook": body})
    p = paths.out / "_meta" / "note_hooks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"lecture": paths.vid, "count": len(hooks), "hooks": hooks},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return hooks


def derive_index(cfg, paths, note_text: str) -> str:
    """节点索引是**派生数据**：由节点标题机械生成，不让 agent 手写，也不计入内容预算。"""
    nodes = parse_nodes(note_text)
    if not nodes:
        return note_text
    lines = ["## 节点索引", ""] + ["- [%s] %s" % (n["time"], n["title"]) for n in nodes] + [""]
    block = "\n".join(lines)
    pat = re.compile(r"^##\s*节点索引\s*$.*?(?=^##\s)", re.M | re.S)
    if pat.search(note_text):
        return pat.sub(block + "\n", note_text, count=1)
    # 没有就插在第一个节点之前
    m = re.search(r"^##\s*\[", note_text, re.M)
    if m:
        return note_text[:m.start()] + block + "\n" + note_text[m.start():]
    return note_text


def content_chars(cfg, note_text: str) -> int:
    """内容字数：排除派生的节点索引（导航不算内容）"""
    t = re.sub(r"^##\s*节点索引\s*$.*?(?=^##\s)", "", note_text, flags=re.M | re.S)
    return len("".join(t.split()))


def validate_note(cfg, paths) -> tuple[list[str], list[str]]:
    nc = cfg.get("note", {})
    errors, warns = [], []
    note = paths.out / nc.get("filename", "note.md")
    if not note.exists():
        return ["还没有 %s（由全局写作 agent 产出）" % note.name], warns
    text = note.read_text(encoding="utf-8")

    if str(nc.get("mode", "timeline-nodes")) != "timeline-nodes":
        return errors, warns

    for s in nc.get("head_sections") or []:
        if not re.search(r"^##\s+%s" % re.escape(s), text, re.M):
            errors.append("%s: 缺少头部小节「## %s」" % (note.name, s))
    for s in nc.get("tail_sections") or []:
        if not re.search(r"^##\s+%s" % re.escape(s), text, re.M):
            warns.append("%s: 缺少尾部小节「## %s」（建议补齐）" % (note.name, s))

    nodes = parse_nodes(text)
    lo, hi = int(nc.get("min_nodes", 6)), int(nc.get("max_nodes", 30))
    if not nodes:
        errors.append("%s: 没有解析到关键节点（格式应为 ## [MM:SS] 节点标题）" % note.name)
        return errors, warns
    if len(nodes) < lo:
        warns.append("%s: 关键节点 %d 个，少于建议下限 %d" % (note.name, len(nodes), lo))
    if len(nodes) > hi:
        errors.append("%s: 关键节点 %d 个，超过上限 %d（等于抄讲义）" % (note.name, len(nodes), hi))

    allowed = set(nc.get("field_labels") or [])
    required = list(nc.get("require_per_node") or [])
    last = -1
    for n in nodes:
        t = _sec3(n["time"])
        if t < last:
            errors.append("%s: 节点 [%s] %s 的时间早于上一节点，时间轴顺序被打乱"
                          % (note.name, n["time"], n["title"]))
        last = max(last, t)
        for r in required:
            if r not in n["fields"]:
                errors.append("%s: 节点 [%s] %s 缺少「%s」注释" % (note.name, n["time"], n["title"], r))
        for label in n["fields"]:
            if allowed and label not in allowed:
                errors.append("%s: 节点 [%s] 出现未约定的注释类型「%s」（允许：%s）"
                              % (note.name, n["time"], label, "/".join(sorted(allowed))))

    ratio = float(nc.get("max_ratio", 0.2))
    lec = paths.out / "lecture.md"
    if lec.exists() and ratio > 0:
        n_chars = content_chars(cfg, text)
        l_chars = len("".join(lec.read_text(encoding="utf-8").split()))
        if l_chars and n_chars > l_chars * ratio:
            warns.append("%s: 正文 %d 字，是讲义的 %.0f%%（预算 %.0f%%）—— 只留关键节点，压缩到预算内"
                         % (note.name, n_chars, 100.0 * n_chars / l_chars, ratio * 100))
    if len(text.strip()) < 400:
        warns.append("%s: 全文不足 400 字，像是没写完" % note.name)
    return errors, warns


def build(cfg, paths, meta: dict, seg_data: dict, transcript: dict | None = None) -> dict:
    if not cfg.get("note", {}).get("enabled", True):
        return {}
    brief = build_brief(cfg, paths, meta, seg_data, transcript)
    note_path = paths.out / cfg.get("note", {}).get("filename", "note.md")
    if cfg.get("note", {}).get("derive_index", True) and note_path.exists():
        before = note_path.read_text(encoding="utf-8")
        after = derive_index(cfg, paths, before)
        if after != before:
            note_path.write_text(after, encoding="utf-8")
            print("[note] 节点索引已按节点自动重建（%d 行）" % len(parse_nodes(after)))
    errors, warns = validate_note(cfg, paths)
    if cfg.get("note", {}).get("export_hooks", True) and (paths.out / cfg.get("note", {}).get("filename", "note.md")).exists():
        try:
            nodes = parse_nodes((paths.out / cfg.get("note", {}).get("filename", "note.md")).read_text(encoding="utf-8"))
            hooks = export_hooks(cfg, paths, "", nodes)
            print("[note] 钩子 → _meta/note_hooks.json（%d 条，供跨 lecture 串联）" % len(hooks))
        except Exception as exc:
            print("[note] 钩子抽取失败：%s" % exc)
    note = paths.out / cfg.get("note", {}).get("filename", "note.md")
    if note.exists():
        if errors:
            print("[note] 校验未通过：%d 个错误" % len(errors))
            for e in errors[:8]:
                print("   ✗ %s" % e)
        else:
            print("[note] 校验通过（%d 个警告）→ %s" % (len(warns), note))
    else:
        print("[note] 还没有 %s —— 请用全局写作 agent 读 %s 后产出" % (note.name, brief.name))
    report = ["# note.md 校验报告", "",
              "- 结果：%s" % ("未生成" if not note.exists() else ("通过" if not errors else "未通过")),
              "- 错误：%d ｜ 警告：%d" % (len(errors), len(warns)), ""]
    if errors:
        report += ["## 错误", ""] + ["- " + e for e in errors] + [""]
    if warns:
        report += ["## 警告", ""] + ["- " + w for w in warns] + [""]
    (paths.out / "_meta" / "note_validation.md").write_text(NL.join(report), encoding="utf-8")
    return {"brief": str(brief), "note": str(note) if note.exists() else None,
            "errors": errors, "warns": warns}