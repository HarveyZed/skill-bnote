"""重切片后的引用重映射：bnote remap

问题：章节正文用 ../slides/NNNN.jpg 按**页序号**引用图片；一旦重新切片（换策略、调阈值、
改候选帧算法），页数或页序会变，旧引用就会指向错误的图。
做法：拿上一次的 slides.json 快照与当前 segments.json 按**时间轴重叠**做匹配，批量改写引用。

用法：bnote remap <BV|URL> --from cache/_prev_slides.json
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

IMG = re.compile(r"\.\./slides/(\d{4})\.jpg")


def _pages_from_slides_json(path: Path):
    d = json.loads(path.read_text(encoding="utf-8"))
    items = d.get("slides") or d.get("segments") or []
    out = []
    for s in items:
        c = s.get("chosen") or {}
        out.append({"id": int(s["id"]), "t_start": float(s["t_start"]), "t_end": float(s["t_end"]),
                    "chars": c.get("ocr_chars", s.get("ocr_chars", 0)),
                    "text": (c.get("ocr_text") or s.get("ocr_text") or "")[:40]})
    return out


def _pages_from_segments(path: Path):
    d = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for s in d["segments"]:
        out.append({"id": int(s["id"]), "t_start": float(s["t_start"]), "t_end": float(s["t_end"]),
                    "chars": s["chosen"].get("ocr_chars", 0),
                    "text": s["chosen"].get("ocr_text", "")[:40]})
    return out


def _best_match(old, news):
    best, best_score = None, -1.0
    for n in news:
        overlap = min(old["t_end"], n["t_end"]) - max(old["t_start"], n["t_start"])
        if overlap > best_score:
            best, best_score = n, overlap
    if best_score <= 0:  # 没有重叠 -> 取起点最近的一页
        best = min(news, key=lambda n: abs(n["t_start"] - old["t_start"]))
    return best


def run(cfg, paths, prev_slides: Path, dry: bool = False, force: bool = False) -> dict:
    # 幂等保护：同一份 prev 快照只允许应用一次，避免重复映射把页码越推越偏
    stamp_path = paths.out / "_meta" / "remap.json"
    key = "%s:%d" % (prev_slides.name, prev_slides.stat().st_mtime_ns)
    if stamp_path.exists() and not force:
        try:
            prev = json.loads(stamp_path.read_text(encoding="utf-8"))
            if prev.get("applied_key") == key:
                print("[remap] 这份快照已应用过（key=%s），跳过；如需强制重跑加 force=True" % key)
                return {"skipped": True, "mapping": prev.get("mapping", {})}
        except Exception:
            pass
    old = _pages_from_slides_json(prev_slides)
    new = _pages_from_segments(paths.segments)
    mapping = {}
    for o in old:
        mapping[o["id"]] = _best_match(o, new)["id"]

    print("[remap] 旧 %d 页 -> 新 %d 页，映射变化：%s" % (
        len(old), len(new),
        ", ".join("%04d→%04d" % (k, v) for k, v in sorted(mapping.items()) if k != v) or "无"))

    changed = {}
    for f in sorted(paths.chapters().glob("0*.md")):
        text = f.read_text(encoding="utf-8")
        hits = []

        def sub(m):
            old_id = int(m.group(1))
            new_id = mapping.get(old_id)
            if new_id is None or new_id == old_id:
                return m.group(0)
            hits.append((old_id, new_id))
            return "../slides/%04d.jpg" % new_id

        new_text = IMG.sub(sub, text)
        if hits:
            changed[f.name] = hits
            if not dry:
                f.write_text(new_text, encoding="utf-8")
    for name, hits in changed.items():
        print("[remap]   %s: %s" % (name, ", ".join("%04d→%04d" % h for h in hits)))

    # 同步 manifest.json 里的 slides 页码（结构文件同样会因重切片而错位）
    mp = paths.chapters() / "manifest.json"
    if mp.exists():
        man = json.loads(mp.read_text(encoding="utf-8"))
        n_fix = 0
        for ch in man.get("chapters", []):
            new_slides = []
            for n in ch.get("slides") or []:
                m2 = mapping.get(int(n))
                new_slides.append(m2 if m2 else int(n))
                if m2 and m2 != int(n):
                    n_fix += 1
            if new_slides and new_slides != ch.get("slides"):
                ch["slides"] = new_slides
        if n_fix and not dry:
            mp.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
        if n_fix:
            print("[remap] manifest.json 同步 %d 个 slide 页码" % n_fix)

    # 覆盖性提示
    used = set()
    for f in sorted(paths.chapters().glob("0*.md")):
        for m in IMG.finditer(f.read_text(encoding="utf-8")):
            used.add(int(m.group(1)))
    all_ids = {n["id"] for n in new}
    print("[remap] 未被引用：%s ｜ 引用但不存在：%s" % (
        sorted(all_ids - used) or "无", sorted(i for i in used if i not in all_ids) or "无"))
    if not dry:
        (paths.out / "_meta").mkdir(parents=True, exist_ok=True)
        (paths.out / "_meta" / "remap.json").write_text(
            json.dumps({"from": str(prev_slides), "applied_key": key, "mapping": mapping,
                        "changed": changed, "applied_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    return {"mapping": mapping, "changed": changed}