"""L13 脚手架层：按切片结果自动生成章节结构（manifest.json），供人工/编排者微调。

背景：此前每次都要手写 manifest 的章名/时间范围/slide 映射，多集时成本高且易错。
做法：封面 + 目录算第 1 章，其后**一页一章**（薄页已被切片阶段吸收）；章名从该页 OCR 文本里
取标题行并清理水印噪声；时间范围取 [本页起点, 下一页起点)，末章到视频结束。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

_TITLE_MAX = 24          # 自动分章标题的最大字数（--auto 观察用；正式分章由 --groups 给定）

# 从 OCR 文本里取标题时要剔除的噪声（正则列表）由 config 提供：scaffold.noise_patterns。
# 某门课专有的水印/页眉请写进 config/local.toml，不要写进 default.toml。


def _clean_title(text: str, fallback: str, noise_patterns=None) -> str:
    t = (text or "").strip().replace("\n", " ")
    pats = [str(p) for p in (noise_patterns or []) if str(p).strip()]
    if pats:
        t = re.compile("|".join(pats), re.I).sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" ·-—|")
    # 取前 24 个字符作为标题（OCR 文本常把整页内容串起来）
    return (t[:_TITLE_MAX] or fallback)


def _hms(sec: float) -> str:
    sec = int(sec)
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def build(cfg, paths, meta: dict, leading_merge: bool = True, groups_path: str | None = None) -> dict:
    noise = list((cfg.get("scaffold") or {}).get("noise_patterns") or [])
    slides = json.loads((paths.out / "slides.json").read_text(encoding="utf-8"))["slides"]
    if not slides:
        raise SystemExit("没有 slides.json，请先跑 bnote bundle")
    chapters = []
    start_i = 0
    if groups_path:
        spec = json.loads(Path(groups_path).read_text(encoding="utf-8"))
        by_id = {s["id"]: s for s in slides}
        for g in spec:
            ids = [int(x) for x in g["slides"]]
            ids = [i for i in ids if i in by_id]
            if not ids:
                continue
            chapters.append({"ids": ids, "title": g["title"], "start": by_id[ids[0]]["t_start"]})
        start_i = None
    if start_i is not None and leading_merge and len(slides) > 1 and slides[1]["t_start"] - slides[0]["t_end"] < 60:
        # 封面 + 目录 合并为第 1 章
        chapters.append({"ids": [slides[0]["id"], slides[1]["id"]],
                         "title": "开场与课程地图", "start": slides[0]["t_start"]})
        start_i = 2
    if start_i is not None:
        for s in slides[start_i:]:
            chapters.append({"ids": [s["id"]],
                             "title": _clean_title(s.get("ocr_text", ""), "第 %d 页" % s["id"], noise),
                             "start": s["t_start"]})
    dur = float(meta.get("duration") or slides[-1]["t_end"])
    rows = []
    for i, ch in enumerate(chapters):
        end = chapters[i + 1]["start"] if i + 1 < len(chapters) else dur
        rows.append({
            "id": "%02d" % (i + 1),
            "title": ch["title"],
            "range": [_hms(ch["start"]), _hms(end)],
            "slides": ch["ids"],
            "body": "%02d-%s.md" % (i + 1, re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", ch["title"])[:28].strip("-") or "chapter"),
            "keypoints": [], "questions": [],
        })
    man = {
        "schema": "bnote-chapters/1",
        "video": {"vid": paths.vid, "bvid": meta.get("bvid"), "page": meta.get("page"),
                  "title": meta.get("part") or meta.get("title"), "duration": int(dur)},
        "slide_count": len(slides),
        "scaffolded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chapters": rows,
    }
    paths.chapters().mkdir(parents=True, exist_ok=True)
    (paths.chapters() / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")

    plan = ["# %s 章节划分（scaffold 自动生成，可人工微调）" % (meta.get("part") or paths.vid), "",
            "| 章 | 标题 | 时间范围 | slide | 正文文件 |", "|---|---|---|---|---|"]
    for r in rows:
        plan.append("| %s | %s | %s-%s | %s | %s |" % (
            r["id"], r["title"], r["range"][0], r["range"][1],
            ", ".join("%04d" % n for n in r["slides"]), r["body"]))
    plan += ["", "> 章标题取自该页 OCR 首段（已去水印），**请人工核对**；正文由写作 agent 写。" ]
    (paths.chapters() / "_plan.md").write_text("\n".join(plan), encoding="utf-8")
    print("[scaffold] %d 章 / %d 页 → %s" % (len(rows), len(slides), paths.chapters() / "manifest.json"))
    for r in rows:
        print("  %s %-30s %s-%s slide %s" % (r["id"], r["title"], r["range"][0][:5], r["range"][1][:5],
                                             ",".join("%04d" % n for n in r["slides"])))
    return man
