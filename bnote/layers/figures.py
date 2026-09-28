"""L4.7 单帧插图（0.14.0，信息流模式）：从取样包挑"关键时刻" → 抽**全分辨率单帧** → 落
out/<vid>/_meta/figures/NN.png + out/<vid>/_meta/figures.json。

与 [sheet] 读字面板的分工（用户 2026-09-28 定的口径）：

  * **slides 模式的图 = 课件**（完整一页，走 ../slides/NNNN.jpg）；
  * **stream 模式的图 = 插图**（**单帧**，只在关键时刻，走 ../_meta/figures/NN.png）；
  * 面板（12 格缩放拼图）**只是给写手看的材料**，不是交付物里的图。

候选怎么来：取样包 cache/<vid>/sample/index.json 的每个窗口取**画面变化最大的一帧**
（相邻取样帧的归一化平均绝对差最大处；变化落在第 k 帧上就取第 k 帧——它才是"变化之后"的画面）；
窗口内无显著变化（低于 [figures].min_change）就取**窗口中点帧**。上限 [figures].max_per_video。

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
            "diff_size": (int(size[0]), int(size[1]))}


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


def pick_candidates(cfg: dict, paths) -> list:
    """每个取样窗口一个候选：画面变化最大的一帧；无显著变化取窗口中点帧。"""
    idx = paths.read_json(paths.sample_index, None) or {}
    frames = [f for f in (idx.get("frames") or []) if isinstance(f, dict)]
    if not frames:
        return []
    p = _params(cfg)
    by_win: dict = {}
    for f in frames:
        by_win.setdefault(int(f.get("window") or 0), []).append(f)
    out = []
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
        out.append({"t": round(float(fs[k].get("t") or 0), 3), "window": w,
                    "frame": str(fs[k].get("file") or ""), "why": why})
    return out[:p["max"]] if p["max"] else []


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

    cands = pick_candidates(cfg, paths)
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
                     "sha256": sha256_file(dest), "why": c["why"]})
    doc = {"schema": SCHEMA, "vid": paths.vid, "basis": "sample",
           "max_per_video": p["max"],
           "coverage": (paths.read_json(paths.sample_index, None) or {}).get("coverage") or {},
           "count": len(figs), "figures": figs, "notes": notes}
    paths.write_json(paths.meta_dir() / JSON_NAME, doc)
    if figs:
        print("[figures] 插图候选 %d 张 → %s（整集上限 %d）" % (len(figs), dest_dir, p["max"]))
        for f in figs:
            print("  %s  t=%.1fs  %s  %s" % (f["name"], f["t"], "x".join(str(x) for x in f["size"]) or "尺寸未知", f["why"]))
    else:
        print("[figures] 没有插图候选（%s）" % ("；".join(notes) or "取样包为空"))
    return doc


def names(meta_dir) -> set:
    """figures.json 里的插图文件名集合 —— **委托 tools.figure_names**（同一件事只留一个实现，
    manifest 与 text 两条消费路径都指向它）。"""
    return tool_figure_names(meta_dir)
