"""L4.7 单帧插图（0.14.0，信息流模式）：从取样包挑"关键时刻" → 抽**全分辨率单帧** → 落
out/<vid>/_meta/figures/NN.png + out/<vid>/_meta/figures.json。

与 [sheet] 读字面板的分工（用户 2026-09-28 定的口径）：

  * **slides 模式的图 = 课件**（完整一页，走 ../slides/NNNN.jpg）；
  * **stream 模式的图 = 插图**（**单帧**，只在关键时刻，走 ../_meta/figures/NN.png）；
  * 面板（12 格缩放拼图）**只是给写手看的材料**，不是交付物里的图。

候选怎么来：取样包 cache/<vid>/sample/index.json 的每个窗口取**画面变化最大的一帧**
（相邻取样帧的归一化平均绝对差最大处；变化落在第 k 帧上就取第 k 帧——它才是"变化之后"的画面）；
窗口内无显著变化（低于 [figures].min_change）就取**窗口中点帧**。上限 [figures].max_per_video。

**跨窗口去重**：同一张静止画面横跨两个窗口时会重复出图（实测 P25：01.png 与 02.png 就是同一页
notebook 的两个中点帧）。每个候选帧算一个 8×8 dHash（64 位，缩略灰度图按行比较相邻像素），
与**已入选**的任一候选 Hamming 距离 <= [figures].dedup_hamming（默认 6）即判为重复、跳过。
被跳过的候选**不静默丢弃**：连 t / window / dHash / hamming / dup_of 一起记进 figures.json 的 skipped，
可审计、可复核；阈值 0 = 不去重。

抽帧**必须全分辨率**：走 M2 已有逻辑 frames.read_frame()（缓存帧已是原生尺寸就直接复用；
缓存帧是缩放图或没有缓存就从整片媒体现抽 PNG 无损单帧）—— 插图是要给读者看的，
拿取样包里那张 720p 缩放 jpg 当插图等于交一张糊图。

产物**不放时间戳**（两次跑逐字节一致，同 [sample] / [sheet] 的约定）；只由显式 bnote figures
或 bnote stream --with-vision 触发 —— [figures].enabled 是给未来自动路径预留的（显式命令不受它拦）。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..tools import figure_names as tool_figure_names
from ..tools import probe_media, sha256_file
from . import frames as frames_layer
from . import media as media_layer

SCHEMA = "bnote-figures/1"
JSON_NAME = "figures.json"
DIRNAME = "figures"


def _params(cfg: dict) -> dict:
    """生效参数（默认值写在这里；config/default.toml 的 [figures] 是同一份口径）。"""
    f = cfg.get("figures") or {}
    size = f.get("diff_size") or (32, 18)
    return {"max": max(0, int(f.get("max_per_video", 4) or 0)),
            "min_change": float(f.get("min_change", 0.02)),
            "diff_size": (int(size[0]), int(size[1])),
            "dedup_hamming": max(0, min(64, int(f.get("dedup_hamming", 6) or 0)))}


def _thumb_gray(path: Path, size) -> bytes | None:
    """缩略灰度图（只用来**比大小**，不读字）：读不出来返回 None。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.convert("L").resize((size[0], size[1])).tobytes()
    except Exception:
        return None


def frame_diff(a, b) -> float:
    """两张缩略灰度图的归一化平均绝对差（0..1）。任一张读不出来按 0 计（当作没变化）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(abs(x - y) for x, y in zip(a, b)) / (255.0 * len(a))


HASH_SIZE = (9, 8)      # dHash 8×8 = 64 位：9 列灰度、按行比较相邻两列


def dhash_gray(thumb) -> int | None:
    """缩略灰度图的 8×8 dHash（64 位整数）：每行比较相邻两列，左 > 右 记 1。

    廉价感知哈希，只用来判"两张候选是不是同一画面"，不读字、不判内容；缩略图尺寸不对返回 None。
    """
    if not thumb or len(thumb) != HASH_SIZE[0] * HASH_SIZE[1]:
        return None
    bits = 0
    for row in range(HASH_SIZE[1]):
        base = row * HASH_SIZE[0]
        for col in range(HASH_SIZE[0] - 1):
            bits = (bits << 1) | (1 if thumb[base + col] > thumb[base + col + 1] else 0)
    return bits


def hamming(a, b) -> int | None:
    """两个 dHash 的 Hamming 距离；任一为空返回 None（无法判定，不去重）。"""
    if a is None or b is None:
        return None
    return bin(int(a) ^ int(b)).count("1")


def pick_candidates(cfg: dict, paths) -> tuple:
    """每个取样窗口一个候选：画面变化最大的一帧；无显著变化取窗口中点帧。

    返回 `(kept, skipped)`：kept 是按窗口顺序、**已跨窗口去重**的候选（含 dHash）；
    skipped 是被判为近重复而跳过的候选（含 dup_of_t / hamming / dHash），供 figures.json 留痕。
    """
    idx = paths.read_json(paths.sample_index, None) or {}
    frames = [f for f in (idx.get("frames") or []) if isinstance(f, dict)]
    if not frames:
        return [], []
    p = _params(cfg)
    by_win: dict = {}
    for f in frames:
        by_win.setdefault(int(f.get("window") or 0), []).append(f)
    kept, skipped = [], []
    for w in sorted(by_win):
        fs = sorted(by_win[w], key=lambda x: float(x.get("t") or 0))
        thumbs = [_thumb_gray(paths.sample_frames / str(f.get("file")), p["diff_size"]) for f in fs]
        diffs = [frame_diff(thumbs[k - 1], thumbs[k]) for k in range(1, len(fs))]
        best = max(diffs) if diffs else 0.0
        if not diffs or best < p["min_change"]:
            k = len(fs) // 2
            why = ("窗口 %d 内无显著画面变化（最大相邻帧差 %.4f < %.4f）→ 取窗口中点帧"
                   % (w, best, p["min_change"]))
        else:
            k = diffs.index(best) + 1        # 变化落入第 k 帧：它才是"变化之后"的画面
            why = "窗口 %d 内画面变化最大的一帧（相邻帧归一化平均差 %.4f）" % (w, best)
        cand = {"t": round(float(fs[k].get("t") or 0), 3), "window": w,
                "frame": str(fs[k].get("file") or ""), "why": why,
                "hash": dhash_gray(_thumb_gray(paths.sample_frames / str(fs[k].get("file")), HASH_SIZE))}
        # 跨窗口去重：与已入选的任一候选比 dHash，近重复就跳过（留痕在 skipped，不静默丢）
        dup = None
        for prev in kept:
            d = hamming(prev.get("hash"), cand["hash"])
            if d is not None and d <= p["dedup_hamming"]:
                dup = (prev, d)
                break
        if dup is not None:
            cand.update({"dup_of_t": dup[0]["t"], "dup_of_window": dup[0]["window"],
                         "hamming": dup[1]})
            skipped.append(cand)
            continue
        kept.append(cand)
    return (kept[:p["max"]] if p["max"] else []), skipped


def _hex(bits) -> str | None:
    """dHash 的 64 位整数 → 16 位十六进制（留痕用）；None 原样保留。"""
    return None if bits is None else "%016x" % int(bits)


def _media_has_video(cfg: dict, paths):
    """媒体在不在、有没有画面：返回 (media, has_video, note)。音频轨占位文件不算画面。"""
    media = media_layer.find_media(paths)
    if media is None:
        return None, False, "没有媒体文件：插图要全分辨率单帧，先跑 bnote stream/fetch 取回媒体"
    try:
        info = probe_media(cfg, media)
    except (Exception, SystemExit):
        return media, None, None      # 探不出来不拦：交给 read_frame 自己报错
    if info.get("has_video") is False:
        return media, False, "媒体只有音频（audio_only 取数）：这集没有画面可作插图"
    return media, True, None


def run(cfg: dict, paths, max_n: int | None = None, force: bool = False) -> dict:
    """生成插图候选：挑时刻 → 抽全分辨率单帧 → 落 figures/NN.png + figures.json。

    重跑会**先清空旧候选图**：图名是序号（01.png…），数量变少时留着旧图就是孤儿文件
    （正文按序号引用，会指到上一轮的图上）。
    """
    p = _params(cfg)
    dest_dir = paths.figures_dir()
    if dest_dir.exists():
        for old in sorted(dest_dir.glob("*.png")):
            old.unlink()
    dest_dir.mkdir(parents=True, exist_ok=True)

    cands, skipped = pick_candidates(cfg, paths)
    if max_n is not None:
        cands = cands[:max(0, int(max_n))]
    figs, notes = [], []
    if not cands:
        notes.append("取样包里没有可用的取样帧（先跑 bnote sample <URL> --page N）")
    media, has_video, note = _media_has_video(cfg, paths)
    if note:
        notes.append(note)
    for c in (cands if has_video else []):
        try:
            fr = frames_layer.read_frame(cfg, paths, c["t"])
        except (Exception, SystemExit) as exc:      # 单张取不到不拦流程，但要留痕
            notes.append("t=%.1fs 取全分辨率单帧失败，跳过：%s" % (c["t"], exc))
            continue
        name = "%02d.png" % (len(figs) + 1)
        dest = dest_dir / name
        shutil.copyfile(fr["path"], dest)
        figs.append({"name": name, "t": round(float(c["t"]), 3), "window": c["window"],
                     "frame": fr.get("frame"), "source": fr.get("source"),
                     "size": list(fr.get("size") or []),
                     "dhash": _hex(c["hash"]),
                     "sha256": sha256_file(dest), "why": c["why"]})
    # 被去重跳过的候选：记 dup_of（入选那张的图名；图名是本轮才分配的，取不到就退回 t）
    name_by_t = {round(float(c["t"]), 3): f["name"] for c, f in zip(cands, figs)}
    skip_doc = [{"t": round(float(s["t"]), 3), "window": s["window"], "frame": s["frame"],
                 "why": s["why"], "dhash": _hex(s.get("hash")), "hamming": s.get("hamming"),
                 "dup_of": name_by_t.get(round(float(s.get("dup_of_t") or 0), 3)),
                 "dup_of_t": s.get("dup_of_t"), "dup_of_window": s.get("dup_of_window")}
                for s in skipped]
    doc = {"schema": SCHEMA, "vid": paths.vid, "basis": "sample",
           "max_per_video": p["max"], "dedup_hamming": p["dedup_hamming"],
           "coverage": (paths.read_json(paths.sample_index, None) or {}).get("coverage") or {},
           "count": len(figs), "figures": figs, "skipped_count": len(skip_doc),
           "skipped": skip_doc, "notes": notes}
    paths.write_json(paths.meta_dir() / JSON_NAME, doc)
    if figs:
        print("[figures] 插图候选 %d 张 → %s（整集上限 %d；跨窗口去重跳过 %d 张，阈值 hamming<=%d）"
              % (len(figs), dest_dir, p["max"], len(skip_doc), p["dedup_hamming"]))
        for f in figs:
            print("  %s  t=%.1fs  %s  %s" % (f["name"], f["t"], "x".join(str(x) for x in f["size"]) or "尺寸未知", f["why"]))
        for s in skip_doc:
            print("  ~~ 跳过 t=%.1fs（窗口 %d）：与 %s 近重复（hamming=%s <= %d）"
                  % (s["t"], s["window"], s["dup_of"] or ("t=%.1fs" % (s["dup_of_t"] or 0)),
                     s["hamming"], p["dedup_hamming"]))
    else:
        print("[figures] 没有插图候选（%s）" % ("；".join(notes) or "取样包为空"))
    return doc


def names(meta_dir) -> set:
    """figures.json 里的插图文件名集合 —— **委托 tools.figure_names**（同一件事只留一个实现，
    manifest 与 text 两条消费路径都指向它）。"""
    return tool_figure_names(meta_dir)
