"""L15 正文结构层：**小节时间行**的校验与生成（工具锁结构，agent 只写内容）。

背景（真实缺陷）：章级 time range 受校验，但正文里写手自填的斜体时间行是**第二套时间数据**、无人校验 ——
实测 p18 第 8 章越出章界、p19/p20 各有一处相邻小节时间重叠、p20 前 5 章带秒后 4 章不带。
根因是"派生数据由 agent 手写"。v0.7.0 起：

  写手只写 `*slide 0006*` / `*slides 0014, 0015, 0016*`（本节依据哪几页幻灯片）
  → `bnote retime` 用 slides.json 的 t_start/t_end 展开成 `*起始–结束 ｜ slide NNNN*`
  → `bnote check` 按展开式校验（格式统一 / 单调 / 不重叠 / 不越章界 / slide 归属 / 多图每图有时间戳）

时间全部来自 slides.json（单调且互不重叠），所以 retime 是幂等的，且一次消除上述三类问题。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*[–\-—~]\s*(\d{1,2}):(\d{2})(?::(\d{2}))?")
SLIDES_RE = re.compile(r"slide[s]?\s*[:：]?\s*((?:\d{3,4})(?:\s*[,，]\s*\d{3,4})*)", re.I)
ITALIC_RE = re.compile(r"^\s*\*[^*].*\*\s*$")
IMG_RE = re.compile(r"!\[[^\]]*\]\((?:\.\./)?slides/(\d{3,4})\.jpg\)")
HEAD_RE = re.compile(r"^(##)\s+(.+?)\s*$")


def hms_to_sec(s: str) -> int:
    p = [int(x) for x in str(s).split(":")]
    while len(p) < 3:
        p.insert(0, 0)
    return p[0] * 3600 + p[1] * 60 + p[2]


def sec_to_hms(sec: int) -> str:
    sec = max(0, int(sec))
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def _time_from_line(line: str):
    m = TIME_RE.search(line)
    if not m:
        return None
    g = m.groups()
    a = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2] or 0) if g[2] is not None else int(g[0]) * 60 + int(g[1])
    b = int(g[3]) * 3600 + int(g[4]) * 60 + int(g[5] or 0) if g[5] is not None else int(g[3]) * 60 + int(g[4])
    return a, b


def _slides_from_line(line: str):
    m = SLIDES_RE.search(line)
    if not m:
        return []
    return [int(x) for x in re.split(r"[,，]", m.group(1)) if x.strip()]


def parse_sections(text: str) -> list[dict]:
    """把一章正文切成小节：标题 + 时间行 + 图 + 行号区间。"""
    lines = text.splitlines()
    idxs = [i for i, l in enumerate(lines) if HEAD_RE.match(l)]
    sections = []
    for k, i in enumerate(idxs):
        end = idxs[k + 1] if k + 1 < len(idxs) else len(lines)
        head = HEAD_RE.match(lines[i]).group(2)
        body = lines[i + 1:end]
        tline = tline_idx = None
        for j, l in enumerate(body):
            if ITALIC_RE.match(l):
                tline, tline_idx = l, i + 1 + j
                break
            if l.strip():
                break
        imgs = [(i + 1 + j, int(m.group(1)))
                for j, l in enumerate(body) for m in [IMG_RE.search(l)] if m]
        sections.append({
            "title": head,
            "head_idx": i,
            "end_idx": end,
            "time_line": tline,
            "time_idx": tline_idx,
            "raw_time": _time_from_line(tline or ""),
            "raw_slides": _slides_from_line(tline or ""),
            "images": imgs,
        })
    return sections


def _chapter_span(ch: dict) -> tuple[int, int]:
    r = ch.get("range") or ["00:00:00", "00:00:00"]
    return hms_to_sec(r[0]), hms_to_sec(r[1])


def _load_slides(paths) -> dict:
    p = paths.out / "slides.json"
    if not p.exists():
        return {}
    doc = json.loads(p.read_text(encoding="utf-8"))
    return {int(s["id"]): s for s in doc.get("slides", [])}


def validate_bodies(man: dict, paths, cfg: dict, only: list[str] | None = None) -> tuple[list, list]:
    """正文小节结构校验。only = 只查这些章 id（写作 agent 自检用）。"""
    errors, warns = [], []
    tol = int((cfg.get("manifest") or {}).get("time_tolerance_sec", 1))
    slides = _load_slides(paths)
    multi_ts = bool(cfg.get("manifest", {}).get("require_per_image_time", True))

    def err(cid, msg, hint=""):
        errors.append({"message": "chapters/%s: %s" % (cid, msg), "owner": "chapter:%s" % cid,
                       "fix_hint": hint})

    def warn(cid, msg, hint=""):
        warns.append({"message": "chapters/%s: %s" % (cid, msg), "owner": "chapter:%s" % cid,
                      "fix_hint": hint})

    for ch in man.get("chapters", []):
        cid = str(ch.get("id"))
        if only and cid not in only:
            continue
        fp = paths.chapters() / str(ch.get("body") or "")
        if not (ch.get("body") and fp.exists()):
            err(cid, "正文文件不存在：%s" % ch.get("body"), "让该章写手产出文件")
            continue
        text = fp.read_text(encoding="utf-8")
        c_start, c_end = _chapter_span(ch)
        ch_slides = {int(x) for x in (ch.get("slides") or [])}
        secs = parse_sections(text)

        if not secs:
            err(cid, "没有解析到小节标题（应为 ## 级）", "按契约用 ## 写小节")
            continue

        prev_end = None
        for i, s in enumerate(secs):
            tag = "小节「%s」" % s["title"][:24]
            # 1) 时间行必须存在，且必须是展开式（HH:MM:SS + 时间范围）
            if not s["time_line"]:
                err(cid, "%s 缺少时间行" % tag,
                    "在标题下写 *slide NNNN*，再由编排者跑 bnote retime 展开")
                continue
            if s["raw_time"] is None:
                err(cid, "%s 的时间行还没展开（只有 slide 号）：%s" % (tag, s["time_line"]),
                    "跑 bnote retime（时间由工具生成，不要手写）")
                continue
            if re.search(r"\d{1,2}:\d{2}\s*[–\-—~]\s*\d{1,2}:\d{2}(?!:)", s["time_line"]):
                err(cid, "%s 的时间不是 HH:MM:SS 格式：%s" % (tag, s["time_line"]),
                    "跑 bnote retime 统一格式")
            a, b = s["raw_time"]
            if b <= a:
                err(cid, "%s 结束时间不大于开始：%s" % (tag, s["time_line"]), "跑 bnote retime")
            if a < c_start - tol or b > c_end + tol:
                err(cid, "%s 时间越出本章范围 %s-%s：%s"
                    % (tag, ch["range"][0], ch["range"][1], s["time_line"]), "跑 bnote retime")
            if prev_end is not None and a < prev_end - tol:
                err(cid, "%s 与上一小节时间重叠（%s < %s）"
                    % (tag, sec_to_hms(a), sec_to_hms(prev_end)), "跑 bnote retime")
            prev_end = b

            # 2) slide 归属
            for sid in s["raw_slides"]:
                if slides and sid not in slides:
                    err(cid, "%s 引用了不存在的 slide %04d" % (tag, sid), "核对 manifest 的 slides")
                elif ch_slides and sid not in ch_slides:
                    err(cid, "%s 引用了不属于本章的 slide %04d（本章：%s）"
                        % (tag, sid, sorted(ch_slides)), "改引本页或调整 manifest 的 slides")

            # 3) 图片：文件存在、属于本章
            for line_no, sid in s["images"]:
                if not (paths.slides() / ("%04d.jpg" % sid)).exists():
                    err(cid, "第 %d 行引用的图片不存在：slides/%04d.jpg" % (line_no + 1, sid),
                        "跑 bnote bundle 或 bnote remap")
                if ch_slides and sid not in ch_slides:
                    err(cid, "第 %d 行的图 %04d 不属于本章（本章：%s）"
                        % (line_no + 1, sid, sorted(ch_slides)), "调整 manifest 的 slides 或换图")

            # 4) 多图小节：每张图必须有自己的时间行（紧跟图上方）
            if multi_ts and len(s["images"]) >= 2:
                lines = text.splitlines()
                for line_no, sid in s["images"]:
                    prev = line_no - 1
                    while prev > s["head_idx"] and not lines[prev].strip():
                        prev -= 1
                    pl = lines[prev] if prev > s["head_idx"] else ""
                    got = _slides_from_line(pl) if ITALIC_RE.match(pl or "") else []
                    if not got or got[0] != sid:
                        err(cid, "%s 内多图（%d 张）但 slide %04d 上方没有自己的时间行"
                            % (tag, len(s["images"]), sid),
                            "跑 bnote retime 自动补每图时间行")

        # 5) manifest 字段
        for c in (ch.get("corrections") or []):
            if not isinstance(c, dict) or not (c.get("wrong") and c.get("right") and c.get("evidence")):
                err(cid, "corrections 有一条缺字段（需 wrong/right/evidence）：%r" % (c,),
                    "补齐三个字段，evidence 指向 slide 编号或时间码")
        for sm in (ch.get("stage_merges") or []):
            ids = [int(x) for x in (sm.get("slides") or [])]
            if not ids or any(x not in ch_slides for x in ids):
                err(cid, "stage_merges 的 slides %s 不属于本章 %s" % (ids, sorted(ch_slides)),
                    "改成本章的 slide 编号")
            elif sm.get("kept") is not None and int(sm["kept"]) not in ids:
                err(cid, "stage_merges 的 kept=%s 不在 slides %s 里" % (sm.get("kept"), ids), "修正 kept")
        if not (ch.get("keypoints") or []):
            warn(cid, "还没有 keypoints（写手未回填补丁？）", "写 _meta/patch/%s.json 后跑 bnote collect" % cid)
    return errors, warns


def retime(man: dict, paths, cfg: dict, only: list[str] | None = None) -> list[str]:
    """按 slides.json 重写小节时间行（幂等）。返回变更摘要。"""
    slides = _load_slides(paths)
    if not slides:
        raise SystemExit("没有 out/<vid>/slides.json，无法生成时间行")
    changed = []
    for ch in man.get("chapters", []):
        cid = str(ch.get("id"))
        if only and cid not in only:
            continue
        fp = paths.chapters() / str(ch.get("body") or "")
        if not (ch.get("body") and fp.exists()):
            continue
        text = fp.read_text(encoding="utf-8")
        secs = parse_sections(text)
        if not secs:
            changed.append("%s: 无小节，跳过" % cid)
            continue
        c_start, c_end = _chapter_span(ch)

        # 1) 计算每节的 [start, end)：连续、不重叠、不越界
        #
        # 分三级依据，前一级给不出边界就退到下一级：
        #   a) 本节首张 slide 的 t_start（slides.json，权威且单调）—— 决定"从哪开始"；
        #   b) 下一节的首张 slide t_start —— 决定"到哪结束"；
        #   c) a/b 都定不出边界（相邻多节引用同一张 slide，或某节没写 slide）：
        #      · 这一组小节原本带合法时间 → 保留写手从字幕读出的边界，只做去重叠与夹紧；
        #      · 否则按各节正文字数**比例分摊**该组可用区间，并在报告里标为估算（不自作聪明静默处理）。
        blines = text.splitlines()
        anchor_start = []
        for s in secs:
            ids = [i for i in s["raw_slides"] if i in slides]
            anchor_start.append(slides[min(ids)]["t_start"] if ids else None)

        n = len(secs)
        groups, cur = [], [0]
        for k in range(1, n):
            a_prev, a_k = anchor_start[k - 1], anchor_start[k]
            # 锚点严格递增（且真的隔了 1 秒以上）才算进入新的一组
            if a_k is not None and (a_prev is None or int(a_k) > int(a_prev) + 1):
                groups.append(cur)
                cur = [k]
            else:
                cur.append(k)
        groups.append(cur)

        def _chars_of(s):
            seg = blines[s["head_idx"] + 1:s["end_idx"]]
            return sum(len(l) for l in seg
                       if l.strip() and not IMG_RE.search(l) and not ITALIC_RE.match(l))

        spans = [None] * n
        estimated = []
        prev_end = c_start
        for gi, g in enumerate(groups):
            a0 = anchor_start[g[0]]
            g_start = int(a0) if (a0 is not None and int(a0) >= prev_end) else int(prev_end)
            g_end = None
            for gj in range(gi + 1, len(groups)):
                na = anchor_start[groups[gj][0]]
                if na is not None and int(na) > g_start + 1:
                    g_end = int(na)
                    break
            if g_end is None:
                g_end = int(c_end)
            g_end = min(g_end, int(c_end))
            if g_end <= g_start:
                g_end = g_start + 1
            if len(g) == 1:
                spans[g[0]] = (g_start, g_end)
                prev_end = g_end
                continue
            old = [secs[k].get("raw_time") for k in g]
            if all(o and o[1] > o[0] for o in old):
                cs = g_start
                for idx, k in enumerate(g):
                    s0 = max(int(old[idx][0]), cs)
                    e0 = min(int(old[idx][1]), g_end)
                    if e0 <= s0:
                        e0 = min(s0 + 1, g_end)
                    spans[k] = (s0, e0)
                    cs = e0
                prev_end = spans[g[-1]][1]
            else:
                weights = [max(_chars_of(secs[k]), 30) for k in g]
                total = sum(weights)
                span_len = g_end - g_start
                cs = g_start
                for idx, k in enumerate(g):
                    last = (idx == len(g) - 1)
                    e0 = g_end if last else cs + max(1, int(round(span_len * weights[idx] / total)))
                    if e0 <= cs:
                        e0 = cs + 1
                    if e0 > g_end:
                        e0 = g_end
                    spans[k] = (cs, e0)
                    cs = e0
                    estimated.append("%s 小节「%s」" % (cid, secs[k]["title"][:16]))
                prev_end = g_end

        # 2) 重建正文
        lines = text.splitlines()
        drop = set()
        for s in secs:
            for j in range(s["head_idx"] + 1, s["end_idx"]):
                if ITALIC_RE.match(lines[j]) and (_slides_from_line(lines[j]) or _time_from_line(lines[j])):
                    drop.add(j)
        out_lines = []
        sec_of = {}
        for i, s in enumerate(secs):
            for j in range(s["head_idx"], s["end_idx"]):
                sec_of[j] = i
        skip_blanks = False
        for j, line in enumerate(lines):
            if j in drop:
                continue
            if skip_blanks and not line.strip():
                continue
            skip_blanks = False
            if j in sec_of:
                s = secs[sec_of[j]]
                i = sec_of[j]
                is_head = (j == s["head_idx"])
                is_img = any(j == ln for ln, _ in s["images"])
                if is_head:
                    out_lines.append(line)
                    # 标题后：空行 + 时间行 + 空行（规范化，重复跑不变）
                    while out_lines and out_lines[-1].strip() == "":
                        out_lines.pop()
                    a, b = spans[i]
                    ids = [x for x in s["raw_slides"]] or [None]
                    label = "slide" if len([x for x in ids if x]) <= 1 else "slides"
                    idtxt = ", ".join("%04d" % x for x in ids if x)
                    out_lines.append("")
                    out_lines.append("*%s–%s ｜ %s %s*" % (sec_to_hms(a), sec_to_hms(b), label, idtxt)
                                     if idtxt else "*%s–%s*" % (sec_to_hms(a), sec_to_hms(b)))
                    out_lines.append("")
                    skip_blanks = True
                    continue
                if is_img and len(s["images"]) >= 2:
                    sid = [x for ln, x in s["images"] if ln == j][0]
                    if sid in slides:
                        while out_lines and out_lines[-1].strip() == "":
                            out_lines.pop()
                        out_lines.append("")
                        out_lines.append("*%s ｜ slide %04d*" % (sec_to_hms(int(slides[sid]["t_start"])), sid))
                        out_lines.append("")
                    out_lines.append(line)
                    continue
            out_lines.append(line)
        new = "\n".join(out_lines).rstrip() + "\n"
        note = ""
        if estimated:
            note = "；其中 %d 个小节因相邻多节共用同一张 slide，边界按字数比例估算（%s）" % (
                len(estimated), "、".join(estimated))
            changed.append("%s: 估算边界 → %s" % (cid, "、".join(estimated)))
        if new != text:
            fp.write_text(new, encoding="utf-8")
            changed.append("%s: %d 个小节重写%s" % (cid, len(secs), note))
        else:
            changed.append("%s: 无变化" % cid)
    return changed
