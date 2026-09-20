"""scene 策略（基线对照）：取"画面突变瞬间"的帧。

这正是 Quark/多数现有工具的失败模式：PPT 动画每次元素飞入都是一次"场景变化"，
于是截到的是加载到一半的页面。保留此实现仅用于与 stable 策略做 A/B 对比。
"""
from __future__ import annotations

from .framesig import frame_diff, hamming, ink_ratio, sharpness, signature


def segment(cfg, paths, frames, transcript, ocr):
    region = tuple(cfg["frames"].get("region") or (0, 0, 1, 1))
    fps = float(cfg["frames"]["fps"])
    th = float(cfg["segment"].get("scene_threshold", 0.04))
    gap = float(cfg["segment"].get("merge_max_gap_sec", 8.0))

    sigs, cheap = [], []
    for f in frames:
        s = signature(paths.frames / f["file"], region)
        sigs.append(s)
        cheap.append({"ink": ink_ratio(s[0]), "sharp": sharpness(s[0])})

    picks = [0]
    for i in range(1, len(frames)):
        d = frame_diff(sigs[i - 1], sigs[i], 0.5)
        if d > th and (frames[i]["t"] - frames[picks[-1]]["t"]) >= gap:
            picks.append(i)

    segments = []
    for gi, idx in enumerate(picks, start=1):
        start = frames[idx]["t"]
        end = frames[picks[gi]]["t"] if gi < len(picks) else round(frames[-1]["t"] + 1.0 / fps, 3)
        info = ocr.text(paths.frames / frames[idx]["file"]) if ocr else {"text": "", "chars": 0}
        segments.append({
            "id": gi, "t_start": start, "t_end": end, "n_frames": 1,
            "stable_sec": 0.0,
            "chosen": {"t": start, "file": frames[idx]["file"], "score": 0.0,
                       "ocr_chars": info["chars"], "ocr_text": info.get("text", ""),
                       "ink": round(cheap[idx]["ink"], 4), "sharp": round(cheap[idx]["sharp"], 5),
                       "dhash": sigs[idx][1]},
            "candidates": [], "merged_from": [],
        })
    return segments
