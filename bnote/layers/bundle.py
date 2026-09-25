"""L7 交付层：把中间产物整理成给人和 agent 用的 Markdown 包。

产出（out/<vid>/）：
  transcript.md   完整字幕（带时间戳，默认不截断）
  slides/NNNN.jpg 每页终态图
  slides.json     每页的机器可读清单
  index.md        标题 + mermaid 大纲骨架 + 章节建议 + 写作约定
  chapters/       由 agent（多模态）按章写入讲义
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

FENCE = chr(96) * 3


def _ts(sec: float) -> str:
    sec = int(sec)
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def _top_comment_line(cfg: dict, meta: dict) -> str:
    """index.md 里的置顶评论一节：同时给出作者与原文。

    三种情况要分清：取到了 / 作者没置顶 / 这次没取（开关关了或接口被风控）——后两种不能混为一谈。
    """
    toc = meta.get("top_comment")
    if isinstance(toc, dict):
        text = (toc.get("text") or "").strip()
        if text:
            who = (toc.get("uname") or "").strip()
            head = "作者 %s ｜ 赞 %s\n\n" % (who, toc.get("like")) if who else ""
            return head + text
    if not ((cfg or {}).get("meta") or {}).get("top_comment", True):
        return "（已关闭置顶评论抓取：配置里 meta.top_comment = false）"
    if meta.get("aid") is None:
        return "（旧缓存没有 aid，未能抓取；重跑取数即可补上）"
    return "（这条视频没有置顶评论，或本次未能取到——上游接口变化或平台风控时取数日志里会有一行提示）"


def _propose_chapters(cfg, segments: list[dict]) -> list[dict]:
    gap = float(cfg["bundle"]["chapter_gap_sec"])
    chapters, cur = [], []
    for seg in segments:
        if cur and seg["t_start"] - cur[-1]["t_end"] > gap:
            chapters.append(cur)
            cur = []
        cur.append(seg)
    if cur:
        chapters.append(cur)
    out = []
    for i, segs in enumerate(chapters, start=1):
        out.append({"id": i, "t_start": segs[0]["t_start"], "t_end": segs[-1]["t_end"],
                    "slide_ids": [s["id"] for s in segs], "n_slides": len(segs)})
    return out


def snapshot_chapters(paths) -> str | None:
    """覆盖 chapters/ 前先做一次快照，避免 agent 写好的讲义被无声覆盖。

    快照位置：out/<vid>/_history/<时间戳>/chapters/
    """
    src = paths.chapters()
    if not src.exists() or not any(src.glob("*.md")):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = paths.out / "_history" / stamp / "chapters"
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.md"):
        shutil.copyfile(f, dst / f.name)
    return str(dst)


def build(cfg: dict, paths, meta: dict, transcript: dict, seg_data: dict) -> dict:
    out = paths.out
    snap = snapshot_chapters(paths)
    if snap:
        print("[bundle] 已快照旧讲义 → %s" % snap)
    slides_dir = paths.slides()
    slides_dir.mkdir(parents=True, exist_ok=True)
    for old in slides_dir.glob("*.jpg"):
        old.unlink()

    segments = seg_data["segments"]
    slides = []
    for seg in segments:
        src = paths.frames / seg["chosen"]["file"]
        dst = slides_dir / ("%04d.jpg" % seg["id"])
        if src.exists():
            shutil.copyfile(src, dst)
        slides.append({
            "id": seg["id"],
            "t_start": seg["t_start"],
            "t_end": seg["t_end"],
            "time": _ts(seg["chosen"]["t"]),
            "jpg": str(dst.relative_to(out)),
            "ocr_chars": seg["chosen"].get("ocr_chars", 0),
            "ocr_text": seg["chosen"].get("ocr_text", ""),
            "merged_from": seg.get("merged_from", []),
            "boundary_evidence": seg.get("boundary_evidence"),
        })
    # 重切片会改变页序号，而正文用 ../slides/NNNN.jpg 按序号引用：
    # 写新清单前先把旧的快照留一份，`bnote remap --from` 才有可用的对照源。
    old_json = out / "slides.json"
    if old_json.exists():
        try:
            old_doc = json.loads(old_json.read_text(encoding="utf-8"))
        except Exception:
            old_doc = None
        new_doc = {"vid": paths.vid, "strategy": seg_data.get("strategy"),
                   "count": len(slides), "slides": slides}
        # 无条件保留上一版（不比较差异）——比较逻辑一旦有边角情况就会"该快照时没快照"，
        # 而 remap 恰恰只在页序变了的时候才需要它。宁可多留一份，也不要丢对照源。
        if old_doc:
            snap = paths.cache / ("_prev_slides_%s.json" % paths.vid.split("_p")[-1])
            snap.write_text(json.dumps(old_doc, ensure_ascii=False, indent=2), encoding="utf-8")
            old_sig = [(s.get("id"), round(float(s.get("t_start", 0)), 1)) for s in old_doc.get("slides", [])]
            new_sig = [(s.get("id"), round(float(s.get("t_start", 0)), 1)) for s in slides]
            print("[bundle] 上一版 slides.json 已快照 → %s%s"
                  % (snap, "（页序有变，bnote remap --from 用它）" if old_sig != new_sig else ""))
    paths.write_json(out / "slides.json", {
        "vid": paths.vid, "strategy": seg_data.get("strategy"),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(slides), "slides": slides,
    })

    lines = ["# 完整字幕 · %s" % (meta.get("part") or meta.get("title")),
             "",
             "- 视频: %s" % meta.get("url"),
             "- 后端: %s (%s)，共 %d 段" % (transcript.get("backend"),
                                            transcript.get("source"), len(transcript.get("segments", []))),
             ""]
    limit = int(cfg["bundle"].get("transcript_max_chars") or 0)
    used = 0
    for s in transcript.get("segments", []):
        line = "[%s] %s" % (_ts(s["from"]), s["text"])
        lines.append(line)
        used += len(line)
        if limit and used > limit:
            lines.append("...（已按配置截断）")
            break
    (out / "transcript.md").write_text("\n".join(lines), encoding="utf-8")

    chapters = _propose_chapters(cfg, segments) if cfg["bundle"].get("propose_chapters") else []
    flow_lines = ["flowchart LR"]
    for ch in chapters:
        label = "第%d章 (%s-%s)" % (ch["id"], _ts(ch["t_start"]), _ts(ch["t_end"]))
        flow_lines.append('  C%d["%s"]' % (ch["id"], label))
    for i in range(1, len(chapters)):
        flow_lines.append("  C%d --> C%d" % (i, i + 1))
    if len(chapters) == 1:
        flow_lines.append("  C1 --> TODO[章节要点待 agent 补齐]")

    idx = [
        "# %s" % (meta.get("part") or meta.get("title")),
        "",
        "- 合集: %s" % meta.get("title"),
        "- 分P: P%s / 共 %s P" % (meta.get("page"), meta.get("page_count")),
        "- 时长: %s" % _ts(meta.get("duration") or 0),
        "- UP: %s" % meta.get("owner"),
        "- 源: %s" % meta.get("url"),
        "- 生成: %s（bnote，切片策略=%s，共 %d 页）"
        % (time.strftime("%Y-%m-%d %H:%M:%S"), seg_data.get("strategy"), len(segments)),
        "- 字幕来源: %s" % transcript.get("source"),
        "- 分区: %s ｜ 标签: %s" % (
            meta.get("tname") or ("tid=%s" % meta.get("tid") if meta.get("tid") else "-"),
            ", ".join(meta.get("tags") or []) or "-"),
        "",
        "## 视频简介（原文）",
        "",
        (meta.get("desc") or "（这条视频没有简介）"),
        "",
        "## UP 主置顶评论（原文）",
        "",
        _top_comment_line(cfg, meta),
        "",
        "## 大纲",
        "",
        FENCE + "mermaid",
        "\n".join(flow_lines),
        FENCE,
        "",
        "## 章节建议（按 slide 间隔自动分段，agent 可合并/重命名）",
        "",
        "| 章 | 起止 | slide 数 | 页号 | 讲义文件 |",
        "|---|---|---|---|---|",
    ]
    for ch in chapters:
        idx.append("| %d | %s - %s | %d | %s | chapters/%02d-*.md |"
                   % (ch["id"], _ts(ch["t_start"]), _ts(ch["t_end"]), ch["n_slides"],
                      ",".join(str(s) for s in ch["slide_ids"]), ch["id"]))
    idx += [
        "",
        "## 页面清单",
        "",
        "| # | 时间 | 时长 | 图 | OCR 字数 | 合并自 |",
        "|---|---|---|---|---|---|",
    ]
    for s in slides:
        idx.append("| %d | %s | %.1fs | %s | %d | %s |"
                   % (s["id"], s["time"], s["t_end"] - s["t_start"], s["jpg"],
                      s["ocr_chars"], s["merged_from"] or "-"))

    idx += [
        "",
        "## 写作约定（给 agent）",
        "",
        "1. 讲义必须覆盖 transcript.md 的**完整**字幕，逐段推进，不做摘要式压缩；",
        "2. 每段配时间戳锚点，格式 [HH:MM:SS]；",
        "3. slide 图片挂在它被讲解的位置（相对路径 ../slides/NNNN.jpg）；",
        "4. 字幕为 ASR 时会有同音错字与术语错认 —— 用 slide 上的文字校正术语；",
        "5. 若某页图与字幕明显不符（截到过渡画面），在文末「已知问题」里记一笔，不要硬编；",
        "6. 每章一个文件 chapters/NN-slug.md，开头写本章时间范围与对应 slide 编号。",
        "",
    ]
    (out / "index.md").write_text("\n".join(idx), encoding="utf-8")
    print("[bundle] 交付物 → %s" % out)
    print("[bundle]   index.md / transcript.md / slides.json / slides(%d 张) / chapters/" % len(slides))
    return {"slides": len(slides), "chapters": len(chapters)}