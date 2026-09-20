"""L18 文本层：信息流 / 口播类视频（无幻灯片）的纯文本流水线。

与幻灯片流程的分工：
  * 不抽帧、不切片、不 OCR、不分章：没有 manifest / scaffold / body / merge；
  * 输入只有字幕（transcript.json），交付两份：
      transcript.md   原样字幕（带时间戳）
      lecture.md      整理稿（补标点、按术语表校正、逐段覆盖不摘要）
  * 写作采用渐进式披露：工具把字幕切成 N 块（每块 <= text.chunk_chars 字），
    每块一个素材文件 + 一个产出文件；writer 一块块读、一块块写，最后 assemble 拼接。

派生数据由工具生成：段落划分、段落锚点、导航锚点、index.md（从正文标题派生）。
writer 只写正文与措辞，不许自编时间。
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

NL = "\n"
ANCHOR_RE = re.compile(r"^##\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.+)$", re.M)


def _ts(sec) -> str:
    sec = int(sec or 0)
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def _sec(text: str) -> int:
    parts = [int(x) for x in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _chars(s: str) -> int:
    return len("".join((s or "").split()))


def load_transcript(paths) -> dict:
    p = paths.subtitle / "transcript.json"
    if not p.exists():
        raise SystemExit(
            "还没有字幕：%s\n  先跑 bnote stream <URL> --page N 取一次（取不到字幕时的三条路见 SKILL.md）" % p)
    return json.loads(p.read_text(encoding="utf-8"))


def write_transcript_md(paths, transcript: dict) -> Path:
    lines = []
    for s in transcript.get("segments") or []:
        lines.append("[%s] %s" % (_ts(s.get("from")), (s.get("text") or "").strip()))
    p = paths.out / "transcript.md"
    p.write_text(NL.join(lines), encoding="utf-8")
    return p


def build_paragraphs(cfg, paths) -> list:
    """按字幕停顿/时长机械分段 —— 不切在半句，不做语义判断。"""
    tc = cfg.get("text", {})
    gap = float(tc.get("para_gap_sec", 1.2))
    max_sec = float(tc.get("max_para_sec", 120.0))
    segs = [s for s in (load_transcript(paths).get("segments") or []) if (s.get("text") or "").strip()]
    paras, cur = [], None
    for idx, s in enumerate(segs, 1):
        a, b = int(s.get("from") or 0), int(s.get("to") or 0)
        if cur is None:
            cur = {"t_start": a, "t_end": b, "seg_from": idx, "seg_to": idx, "texts": [s["text"].strip()]}
            continue
        too_long = (b - cur["t_start"]) > max_sec
        pause = (a - cur["t_end"]) >= gap
        if too_long or pause:
            paras.append(cur)
            cur = {"t_start": a, "t_end": b, "seg_from": idx, "seg_to": idx, "texts": [s["text"].strip()]}
        else:
            cur["t_end"] = b
            cur["seg_to"] = idx
            cur["texts"].append(s["text"].strip())
    if cur:
        paras.append(cur)
    merged = []
    for p in paras:
        if merged and _chars(NL.join(p["texts"])) < 30:
            prev = merged[-1]
            prev["t_end"] = p["t_end"]
            prev["seg_to"] = p["seg_to"]
            prev["texts"] += p["texts"]
        else:
            merged.append(p)
    out = []
    for i, p in enumerate(merged, 1):
        out.append({
            "id": i,
            "t_start": p["t_start"],
            "t_end": p["t_end"],
            "seg_from": p["seg_from"],
            "seg_to": p["seg_to"],
            "chars": _chars(NL.join(p["texts"])),
            "text": NL.join(p["texts"]),
        })
    paths.write_json(paths.meta_dir() / "paragraphs.json",
                     {"vid": paths.vid, "count": len(out), "paragraphs": out})
    print("[text] 机械分段：%d 段（停顿阈值 %.1fs，单段上限 %.0fs）" % (len(out), gap, max_sec))
    return out


def _anchor_marks(cfg, paragraphs: list) -> list:
    every = float(cfg.get("text", {}).get("anchor_every_sec", 0) or 0)
    if every <= 0:
        return []
    marks, nxt = [], every
    for p in paragraphs:
        if p["t_start"] >= nxt:
            marks.append(p)
            nxt = p["t_start"] + every
    return marks


def build_chunks(cfg, paths, paragraphs: list, meta: dict) -> list:
    """渐进式披露：把段落切成 N 块，每块一个素材文件（writer 一块块读）。"""
    cap = int(cfg.get("text", {}).get("chunk_chars", 6000))
    groups, cur, cur_chars = [], [], 0
    for p in paragraphs:
        if cur and cur_chars + p["chars"] > cap and cur_chars >= cap * 0.5:
            groups.append(cur)
            cur, cur_chars = [], 0
        cur.append(p)
        cur_chars += p["chars"]
    if cur:
        groups.append(cur)
    cdir = paths.meta_dir() / "text_chunks"
    cdir.mkdir(parents=True, exist_ok=True)
    tdir = paths.out / "text"
    tdir.mkdir(parents=True, exist_ok=True)
    title = meta.get("part") or meta.get("title") or paths.vid
    chunks = []
    for i, g in enumerate(groups, 1):
        f = cdir / ("%02d.md" % i)
        body = [
            "# 第 %d/%d 块 ｜ %s-%s ｜ 段落 %d-%d ｜ %d 字" % (
                i, len(groups), _ts(g[0]["t_start"]), _ts(g[-1]["t_end"]),
                g[0]["id"], g[-1]["id"], sum(p["chars"] for p in g)),
            "",
            "> 视频：%s ｜ 本文件由 bnote 生成（bnote stream 的产物，勿手改）" % title,
            "",
            "## 这一块你要做什么",
            "",
            "- 只处理本块的字幕；产出写到 %s（相对工作目录；纯正文 Markdown，不写前言、不写元信息）" % (Path("text") / ("%02d.md" % i)),
            "- 逐段覆盖、不摘要、不编造；讲师没说的不写，你的推断要标明（推断）",
            "- 段落锚点用下面给出的（工具已打好，不许自编时间）；可以合并相邻段落，合并时必须用【最早】那一段的锚点",
            "- 每段自己拟一个小标题，写成：## [00:12:30] 小标题",
            "- 字幕无标点、可能有 ASR 错字：按上下文补标点，按术语表校正",
            "",
            "## 段落与字幕原文",
            "",
        ]
        for p in g:
            body += ["### [%s] 段落 %d（%d 段字幕，%d 字）" % (_ts(p["t_start"]), p["id"], p["seg_to"] - p["seg_from"] + 1, p["chars"]), "",
                     p["text"], ""]
        f.write_text(NL.join(body), encoding="utf-8")
        # 清单里一律写**相对 out 目录**的路径：数据根整体搬走后仍然指得对（不写绝对路径）
        chunks.append({"no": i, "file": str(Path("_meta") / "text_chunks" / f.name),
                       "out": str(Path("text") / ("%02d.md" % i)),
                       "para_from": g[0]["id"], "para_to": g[-1]["id"],
                       "t_start": g[0]["t_start"], "t_end": g[-1]["t_end"],
                       "chars": sum(p["chars"] for p in g)})
    paths.write_json(paths.meta_dir() / "text_chunks.json",
                     {"vid": paths.vid, "chunk_chars": cap, "count": len(chunks), "chunks": chunks})
    print("[text] 分块：%d 块（每块上限 %d 字）→ %s" % (len(chunks), cap, cdir))
    return chunks


def render_brief(cfg, paths, meta: dict, paragraphs: list, chunks: list) -> Path:
    from . import glossary as glossary_layer
    from . import profile as profile_layer
    from . import prompt as prompt_layer
    tpl = Path(cfg["paths"]["contracts_dir"]) / "text_writer.md"
    text = tpl.read_text(encoding="utf-8") if tpl.exists() else "# 派单：信息流整理稿\n"
    prof = profile_layer.build(cfg, paths, meta, load_transcript(paths), None)
    glossary_layer.propose(cfg, paths, prof)
    use, avoid, confirmed = glossary_layer.effective(cfg, paths, prof)
    rows = []   # 表头在契约模板里，这里只出数据行（曾经两边都写，渲染成两份表头）
    for c in chunks:
        rows.append("| %d/%d | %s-%s | %d-%d | %d | %s | %s |" % (
            c["no"], len(chunks), _ts(c["t_start"]), _ts(c["t_end"]),
            c["para_from"], c["para_to"], c["chars"], c["file"], c["out"]))
    page = paths.vid.split("_p")[-1]
    ctx = {
        "OUT_DIR": str(paths.out),
        "TITLE": str(meta.get("part") or meta.get("title") or ""),
        "DURATION": _ts(meta.get("duration")),
        "CHUNK_COUNT": str(len(chunks)),
        "PARA_COUNT": str(len(paragraphs)),
        "CHUNK_TABLE": NL.join(rows),
        "CHECK_CMD": "BNOTE_ROOT=%s bnote stream %s --page %s --assemble   # 拼接并校验（带数据根，换根也能照抄）"
                     % (cfg["paths"]["root"], meta.get("url") or "<URL>", page),
        "VIDEO_META": prompt_layer.video_meta_block(cfg, meta),
        "EVIDENCE_SOURCES": "`transcript.md`（本地 ASR 或平台字幕，一手；本集没有幻灯片，没有任何图可看）",
        "TERM_POLICY": profile_layer.term_policy(prof, reviewed=use, confirmed=confirmed, mode="text"),
        "FORMAT_RULES": profile_layer.format_rules(prof, mode="text"),
    }
    for k, v in ctx.items():
        text = text.replace("{{%s}}" % k, str(v))
    p = paths.meta_dir() / "prompt_text.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    print("[text] 派单任务书 → %s（%d 块）" % (p, len(chunks)))
    return p


def _resolve_out(paths, chunk: dict) -> Path:
    """块的产出路径：**只认相对 out 目录的路径**（清单里不再写绝对路径，数据根整体搬走也指得对）。"""
    raw = str(chunk.get("out") or "")
    name = Path(raw).name or "01.md"
    return paths.out / (raw if raw and not Path(raw).is_absolute() else str(Path("text") / name))


def _load_paragraphs(paths) -> list:
    p = paths.meta_dir() / "paragraphs.json"
    return (json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}).get("paragraphs") or []


def assemble(cfg, paths) -> Path:
    """把各块产出拼成 lecture.md，并从正文标题派生 index.md。"""
    cp = paths.meta_dir() / "text_chunks.json"
    chunks = (json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {}).get("chunks") or []
    if not chunks:
        raise SystemExit("还没有分块：先跑 bnote stream <URL> --page N")
    parts, missing = [], []
    for c in chunks:
        f = _resolve_out(paths, c)
        if not f.exists() or _chars(f.read_text(encoding="utf-8")) < 50:
            missing.append("%02d" % c["no"])
            continue
        parts.append(f.read_text(encoding="utf-8").strip())
    if missing:
        raise SystemExit("这些块还没写（或几乎为空）：%s\n  素材在 %s"
                         % (", ".join(missing), paths.meta_dir() / "text_chunks"))
    meta = json.loads(paths.meta.read_text(encoding="utf-8")) if paths.meta.exists() else {}
    head = ["# %s" % (meta.get("part") or paths.vid), "",
            "- 合集: %s ｜ UP: %s" % (meta.get("title") or "", meta.get("owner") or ""),
            "- 时长: %s ｜ 源: %s" % (_ts(meta.get("duration")), meta.get("url") or ""),
            "- 本稿是信息流（无幻灯片）模式的整理稿：按时间轴逐段覆盖字幕，未做摘要压缩。", ""]
    lec = paths.out / "lecture.md"
    lec.write_text(NL.join(head) + NL + NL.join(parts) + NL, encoding="utf-8")   # 元信息块后留空行
    md = lec.read_text(encoding="utf-8")
    anchors = ANCHOR_RE.findall(md)
    idx = ["# %s（导航）" % (meta.get("part") or paths.vid), "",
           "- 段落锚点: %d ｜ 正文 %d 字 ｜ 源: %s" % (len(anchors), _chars(md), meta.get("url") or ""),
           "", "## 锚点", ""]
    for t, title in anchors:
        idx.append("- [%s] %s" % (t, title.strip()))
    (paths.out / "index.md").write_text(NL.join(idx) + NL, encoding="utf-8")
    print("[text] 拼接 %d 块 → %s（%d 个段落锚点）" % (len(parts), lec, len(anchors)))
    return lec


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s or "")


def _sections(md: str) -> list:
    """把整理稿切成 [(锚点秒, 正文)] —— 只认工具给过的 ## [HH:MM:SS] 形态。"""
    out = []
    cur = None
    for line in md.splitlines():
        m = ANCHOR_RE.match(line)
        if m:
            if cur:
                out.append(cur)
            cur = (_sec(m.group(1)), [])
        elif cur is not None:
            cur[1].append(line)
    if cur:
        out.append(cur)
    return [(a, NL.join(b)) for a, b in out]


# 命中还须落在"更早那段的靠前处"：m.a 是匹配在小节首句里的起点，超过这个值就不是"首句来自更早段落"
_DRIFT_MATCH_HEAD = 8


def _anchor_drift_warns(cfg, paths, paras: list, md: str) -> list:
    """锚点位置启发式：小节首句若能在**更早**的段落里找到，说明合并时用了靠后的锚点。

    机器只能验「锚点是不是工具给的段落起点」，验不出「指得对不对」——
    这条用「首句来自哪一段」做旁证，只提示、不判错。
    """
    tcfg = cfg.get("text", {})
    head_chars = int(tcfg.get("anchor_drift_head_chars", 40))
    min_chars = int(tcfg.get("anchor_drift_min_chars", 12))
    window = int(tcfg.get("anchor_drift_window", 3))
    warns = []
    idx_of = {_ts(p["t_start"]): i for i, p in enumerate(paras)}
    for a_sec, body in _sections(md):
        i = idx_of.get(_ts(a_sec))
        if i is None or i == 0:
            continue
        head = _norm(body)[:head_chars]
        if len(head) < min_chars:
            continue
        for j in range(max(0, i - window), i):
            prev = _norm(paras[j].get("text") or "")[:400]
            if not prev:
                continue
            m = difflib.SequenceMatcher(None, head, prev, autojunk=False).find_longest_match(0, len(head), 0, len(prev))
            # 判据收紧：重合 >=12 字 **且**落在更早段落靠前处（<=40 字）才提示。
            # 8 字门槛会误报：实测 P3 命中「gent的开发和」、P8 命中单词「ontology」，两处锚点本来就是对的。
            if m.size >= min_chars and m.a <= _DRIFT_MATCH_HEAD and m.b <= head_chars:
                warns.append({"level": "warn", "owner": "text",
                              "message": "小节 %s 的首句像是来自更早的段落 %s（%s）——合并相邻段落时请用**最早**那段的锚点"
                                         % (_ts(a_sec), paras[j]["id"], _ts(paras[j]["t_start"]))})
                break
    return warns


def validate(cfg, paths) -> tuple:
    """结构校验：只校能不能定位，不判内容好坏（内容看契约）。"""
    errors, warns = [], []
    paras = _load_paragraphs(paths)
    if not paras:
        return ["还没有 paragraphs.json：先跑 bnote stream <URL> --page N"], warns
    for i, p in enumerate(paras[1:], 1):
        prev = paras[i - 1]
        # 覆盖靠字幕段号保证（字幕之间有静音间隔，时间不要求严丝合缝）
        if p["seg_from"] != prev["seg_to"] + 1:
            errors.append({"level": "error", "owner": "pipeline",
                           "message": "段落 %d 的字幕覆盖不连续（上一段到第 %d 段，本段从第 %d 段起）" % (
                               p["id"], prev["seg_to"], p["seg_from"])})
        if p["t_start"] < prev["t_end"]:
            errors.append({"level": "error", "owner": "pipeline",
                           "message": "段落 %d 的时间与前一段重叠或逆序（%s < %s）" % (
                               p["id"], _ts(p["t_start"]), _ts(prev["t_end"]))})
    lec = paths.out / "lecture.md"
    if not lec.exists():
        errors.append({"level": "error", "owner": "pipeline", "message": "还没有 lecture.md（先 --assemble）"})
    else:
        md = lec.read_text(encoding="utf-8")
        allowed = {_ts(p["t_start"]) for p in paras}
        found = ANCHOR_RE.findall(md)
        bad = [t for t, _ in found if _ts(_sec(t)) not in allowed]
        if bad:
            errors.append({"level": "error", "owner": "text",
                           "message": "正文里有 %d 个锚点不是工具给的段落起点（不许自编时间）：%s" % (len(bad), ", ".join(bad[:5]))})
        secs = [_sec(t) for t, _ in found]
        if secs != sorted(secs):
            errors.append({"level": "error", "owner": "text", "message": "段落锚点不是单调递增"})
        ratio = float(cfg.get("text", {}).get("min_cover_ratio", 0.98))
        # 分母用字幕**正文**（不含 transcript.md 里的时间戳，否则会把基准抬高）
        src = sum(p["chars"] for p in paras)
        # 分子只算**正文**：元信息块（标题/时长/源那条）有 200 来字，算进去会让超短集永远达标（实测 P11 因此漏检）
        got = _chars(NL.join(b for _, b in _sections(md)))
        if src and got < src * ratio:
            errors.append({"level": "error", "owner": "text",
                           "message": "正文 %d 字 < 字幕正文 %d 字 × %.2f —— 像是做了摘要压缩（元信息块不计入）" % (got, src, ratio)})
        meta_p = paths.meta
        if meta_p.exists():
            dur = float(json.loads(meta_p.read_text(encoding="utf-8")).get("duration") or 0)
            last_end = max((p["t_end"] for p in paras), default=0)
            if dur and last_end > dur * 1.05:
                warns.append({"level": "warn", "owner": "pipeline",
                              "message": "段落时间轴到 %ds，超出片长 %ds —— 字幕轨道可能属于整段视频或串了别的分 P"
                                         % (last_end, dur)})
        tp = paths.subtitle / "transcript.json"
        if tp.exists():
            td = json.loads(tp.read_text(encoding="utf-8"))
            cov = td.get("coverage")
            if td.get("partial") or (cov is not None and cov < 0.8):
                warns.append({"level": "warn", "owner": "pipeline",
                              "message": "字幕是残轨（覆盖度 %s）——这份稿子只覆盖了一小段片长，别当完整讲义"
                                         % (("%.1f%%" % (cov * 100)) if cov is not None else "未知")})
        if found and len(found) < len(paras) * 0.5:
            warns.append({"level": "warn", "owner": "text",
                          "message": "段落锚点只有 %d 个（工具给了 %d 段）—— 合并得有点狠，确认没有漏讲" % (len(found), len(paras))})
        warns += _anchor_drift_warns(cfg, paths, paras, md)
    return errors, warns


def write_validation(paths, errors: list, warns: list) -> Path:
    doc = {"vid": paths.vid, "mode": "text", "errors": errors, "warnings": warns}
    p = paths.write_json(paths.meta_dir() / "validation.json", doc)
    lines = ["# 结构校验（信息流模式）", "", "- 结果：%s" % ("通过" if not errors else "不通过"),
             "- 错误：%d ｜ 警告：%d" % (len(errors), len(warns)), ""]
    for e in errors:
        lines.append("- ✗ [%s] %s" % (e.get("owner"), e.get("message")))
    for w in warns:
        lines.append("- ⚠ [%s] %s" % (w.get("owner"), w.get("message")))
    (paths.meta_dir() / "validation.md").write_text(NL.join(lines) + NL, encoding="utf-8")
    return p


def build(cfg, paths, meta: dict, transcript: dict) -> dict:
    paths.ensure()
    write_transcript_md(paths, transcript)
    paras = build_paragraphs(cfg, paths)
    chunks = build_chunks(cfg, paths, paras, meta)
    render_brief(cfg, paths, meta, paras, chunks)
    return {"paragraphs": len(paras), "chunks": len(chunks)}
