"""L17 跨讲层：把多集的钩子/结构汇成一张"综合工作表"，供 agent 写跨讲索引。

设计：工具只做**汇总与取证**（哪一集、哪个节点、什么时间、原文钩子、结构统计），
不做语义判断 —— "A 集的问题由 B 集回答" 是 agent 的判断，写在综合工作表旁边的索引文件里。
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _episode(paths_root: Path, vid: str, cache_root: Path | None = None) -> dict | None:
    out = paths_root / vid
    if not out.exists():
        return None
    man = None
    mp = out / "chapters" / "manifest.json"
    if mp.exists():
        try:
            man = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            man = None
    hooks = None
    hp = out / "_meta" / "note_hooks.json"
    if hp.exists():
        try:
            hooks = json.loads(hp.read_text(encoding="utf-8"))
        except Exception:
            hooks = None
    val = None
    vp = out / "_meta" / "validation.json"
    if vp.exists():
        try:
            val = json.loads(vp.read_text(encoding="utf-8"))
        except Exception:
            val = None
    lecture = out / "lecture.md"
    note = out / "note.md"
    # 信息流模式没有 manifest：标题/时长从 cache/<vid>/meta.json 兜底，结构改看 paragraphs.json
    meta, paras = None, None
    if cache_root:
        mp = cache_root / vid / "meta.json"
        if mp.exists():
            try: meta = json.loads(mp.read_text(encoding="utf-8"))
            except Exception: meta = None
    pp = out / "_meta" / "paragraphs.json"
    if pp.exists():
        try: paras = json.loads(pp.read_text(encoding="utf-8")).get("paragraphs") or []
        except Exception: paras = None
    return {
        "vid": vid,
        "title": ((man or {}).get("video") or {}).get("title") or (meta or {}).get("part") or (meta or {}).get("title") or "",
        "duration": ((man or {}).get("video") or {}).get("duration") or (meta or {}).get("duration") or 0,
        "paragraphs": len(paras or []),
        "chapters": (man or {}).get("chapters") or [],
        "hooks": (hooks or {}).get("hooks") or [],
        "errors": len((val or {}).get("errors") or []),
        "warns": len((val or {}).get("warnings") or []),
        "lecture_chars": len("".join(lecture.read_text(encoding="utf-8").split())) if lecture.exists() else 0,
        "note_chars": len("".join(note.read_text(encoding="utf-8").split())) if note.exists() else 0,
    }


def build(cfg, out_root: Path, bvid: str, page_from: int, page_to: int) -> dict:
    eps = []
    for p in range(page_from, page_to + 1):
        cr = Path(cfg["paths"]["cache_root"]) if cfg and cfg.get("paths", {}).get("cache_root") else None
        e = _episode(out_root, "%s_p%d" % (bvid, p), cr)
        if e:
            eps.append(e)
    if not eps:
        raise SystemExit("这一段里没有已产出的集（%s p%d-p%d）" % (bvid, page_from, page_to))

    dest_dir = out_root / "_xref"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("%s_p%d-p%d_worksheet.md" % (bvid, page_from, page_to))

    L = []
    total_hooks = sum(len(e["hooks"]) for e in eps)
    L.append("# 跨讲综合工作表：%s p%d–p%d" % (bvid, page_from, page_to))
    L.append("")
    L.append("- 集数：%d ｜ 钩子合计：%d 条 ｜ 生成时间：%s"
             % (len(eps), total_hooks, time.strftime("%Y-%m-%d %H:%M:%S")))
    L.append("- 本表只做汇总与取证；**跨讲结论由 agent 判断并写进同目录的索引文件**。")
    L.append("")
    L.append("## 一、各集结构一览")
    L.append("")
    L.append("| 集 | 标题 | 时长 | 结构 | 讲义字数 | 笔记字数 | 结构校验 | 钩子 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for e in eps:
        shape = ("章 %d" % len(e["chapters"])) if e["chapters"] else ("段落 %d" % e.get("paragraphs", 0))
        L.append("| p%s | %s | %s | %s | %d | %s | %s | %d |" % (
            e["vid"].split("_p")[-1], e["title"][:40],
            "%d:%02d" % (int(e["duration"]) // 60, int(e["duration"]) % 60),
            shape, e["lecture_chars"],
            e["note_chars"] or "-",
            "0 错 0 警" if not e["errors"] and not e["warns"] else "%d 错 %d 警" % (e["errors"], e["warns"]),
            len(e["hooks"])))
    L.append("")
    L.append("## 二、各集结构骨架")
    L.append("")
    for e in eps:
        L.append("### p%s %s" % (e["vid"].split("_p")[-1], e["title"][:60]))
        L.append("")
        if not e["chapters"]:
            L.append("- 信息流模式（无幻灯片、无章节）：机械分段 %d 段，段落锚点见该集 `index.md`" % e.get("paragraphs", 0))
            L.append("")
            continue
        for c in e["chapters"]:
            r = c.get("range") or ["", ""]
            L.append("- [%s] %s-%s ｜ %s（slide %s）" % (
                c.get("id"), r[0][3:], r[1][3:], c.get("title"),
                ", ".join("%04d" % x for x in (c.get("slides") or []))))
        L.append("")
    L.append("## 三、全部钩子（跨讲线索的原始素材）")
    L.append("")
    for e in eps:
        if not e["hooks"]:
            continue
        L.append("### p%s（%d 条）" % (e["vid"].split("_p")[-1], len(e["hooks"])))
        L.append("")
        for h in e["hooks"]:
            L.append("- **[%s] %s** ｜ %s" % (h.get("time", ""), h.get("node", ""), h.get("hook", "")))
        L.append("")
    dest.write_text("\n".join(L) + "\n", encoding="utf-8")
    return {"path": str(dest), "episodes": len(eps), "hooks": total_hooks,
            "missing_note": [e["vid"] for e in eps if not e["note_chars"]]}
