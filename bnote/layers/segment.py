"""L5 切片层：把帧序列切成"一页幻灯片"。

策略可插拔（config [segment].strategy）：
  stable  —— 默认。稳定态窗口 + 段内取最完整帧（解决"截到动画加载一半"）
  scene   —— 基线。取画面突变瞬间的帧（多数现有工具的做法，用于 A/B 对照）
"""
from __future__ import annotations

import json

from ..segmenters import scene, stable

STRATEGIES = {"stable": stable.segment, "scene": scene.segment}


def build(cfg: dict, paths, frames: list[dict], transcript: dict | None, ocr, force: bool = False) -> dict:
    if paths.segments.exists() and not force:
        data = json.loads(paths.segments.read_text(encoding="utf-8"))
        print("[segment] 已有 segments.json（%d 页，策略=%s），跳过加 --force 可重算"
              % (len(data.get("segments", [])), data.get("strategy")))
        return data

    name = cfg["segment"]["strategy"]
    fn = STRATEGIES.get(name)
    if not fn:
        raise ValueError("未知切片策略: %s（可选 %s）" % (name, list(STRATEGIES)))

    print("[segment] 策略=%s，帧数=%d，OCR=%s" % (name, len(frames), "on" if ocr.available else "off"))
    segments = fn(cfg, paths, frames, transcript, ocr)
    data = {"strategy": name, "frame_count": len(frames), "video": paths.vid,
            "segments": segments}
    paths.write_json(paths.segments, data)
    print("[segment] 得到 %d 页 → %s" % (len(segments), paths.segments))
    return data
