"""L4.6 遮挡层（M3）：从**已抽出的帧**里认出「不是幻灯片内容」的那几块像素。

产物 ``cache/<vid>/overlay.json``（**接口冻结**，键序即结构：
schema / vid / algo / source / params / regions / applicability / stats）。
消费方：``segmenters/stable.py``（决定挖掉哪些像素再算帧差 / 墨迹 / 清晰度 / 送 OCR 的图）
与 ``layers/measure.py``（决定 motion / freeze 用哪些像素）。两者都**只读这个文件**，
不 import 本模块（P1：层与层只通过文件通信）。

为什么需要它（实测靶子，不是设想）
--------------------------------
* p20 第 5 页：底部有烧录字幕（旧判据恰好触发，rb≈0.1507，阈值 0.15），同时**左下角有一个
  标注工具条**（激光笔 / 笔 / 荧光笔 / 橡皮擦 / 色板）。工具条在字幕带**之外**，它的文字
  进了 chosen 帧的 OCR 文本；而 OCR 文本参与「同页判定」（4-gram 包含度）→ 外物污染判定链。
  p21 第 23 页、p22 第 9/16 页、BV1CCtz6WEvF_p1 第 10 页同样。
* 未遮罩时 ``freezedetect`` 在 BV1CCtz6WEvF_p1 上给出 1670 段 / 覆盖 96.5% 时长——烧录字幕
  每秒都在变，可以把任何一段稳定画面切成很多小段。所以 freeze / motion 必须按遮罩算。

两条判据（都写进 regions[].evidence；判不了就写 applicability=insufficient，**不许静默返回空**）
----------------------------------------------------------------------------------
1. ``band_change_rate``：底部字幕条。逐行算「相邻两帧这一行的平均绝对差 > 0.6×diff_threshold」
   的帧对占比，得到**逐行变化剖面**；在画面下方 ``caption_band_top_limit`` 以内取峰值行并
   向上/向下生长出**拟合带**，再按带整体算变化率 ``rb``。同时把旧实现
   （``stable._detect_caption_strip``：48×27 签名、固定底部 ``caption_strip_ratio`` 带）
   **逐字搬过来**算一遍 ``rb_legacy`` 作为对照与审计 —— 两条都过就合并成一个区域。
   拟合带比固定 14% 带贴合得多（固定带被同一带里的静止行稀释），这是 BV1CCtz6WEvF_p1 从
   ``rb≈0.1375`` 不触发翻成触发的机械原因。

2. ``corner_static_glyphs``：角状外物（标注工具条 / 水印 / 进度条 / 鼠标）。三条同时成立：
   ① **位置固定在边角**（四个角各一个 ``widget_zone`` 边长的搜索窗）；
   ② **帧间几乎不变**（窗内逐像素「变过的帧对比例」均值 < ``widget_changed_max``）；
   ③ **有细小字符或色块** —— 在**全分辨率**的若干帧上量两条：梯度密度 ``edge_frac`` 与
      显著色块色调数 ``color_patches``（饱和度 > 0.35、亮度 > 0.35 的像素按 30° 分成 12 档，
      每档像素数达窗内 0.4% 才计一档）。

   **为什么 ③ 必须回到全分辨率**：工具条的文字行在 160 宽的缩略图上只剩 1~2 像素，梯度被
   抹平（实测 p20 工具条在时间均值图上的 edge≈0），而幻灯片里表格的梯度反而更高 ——
   低分辨率分不开这两者。真正分得开的是**色块色调数**：实测标注工具条 6~7 档（激光笔/笔/
   荧光笔/橡皮擦 + 一排色板），而幻灯片内容（表格、代码、手写）只有 1~3 档。
   两条都要求（AND）是有意的：幻灯片表格的梯度比工具条更高，只用梯度会把表格挖掉。

   收紧方框用**同内容帧组的交集**：整集均匀取若干帧，以「最像外物」的那帧为样板，挑出与样板
   内容几乎相同的帧（同内容帧组），组内各帧的「梯度或显著色块」掩膜求交 —— 工具条的
   文字与色板帧帧相同（留得下），幻灯片内容逐页重画（被交掉）；组里只有 1 帧就说明这东西
   只出现过一次，按幻灯片内容处理。

M4 新增判据 3：color_stroke（手写笔迹）
----------------------------------------
彩色细笔画（红/橙笔，hue 窗口见 params 的 handwriting_hue_*；蓝窗默认关）识别为
kind=handwriting 的区域。它**只用于把笔迹从 OCR 与帧差/墨迹里排除，交付图照旧保留**，
且**消费方按帧用、不按框挖**（理由见 handwriting_regions 的 docstring）：所以 masks() 会
跳过 handwriting，消费方要用 strokes() 拿判据参数逐帧重算。
判据实现只有一份，在 segmenters/framesig.py 的 stroke_mask（叶子工具，两个消费方共用）。

坐标系（**写错就是掩码错位**）
--------------------------
``box`` 是相对坐标 ``[l, t, r, b]``，坐标空间是**抽帧后的画面**（``[frames].crop`` 之后、
``[frames].scale_height`` 之后），与 ``ocr.text(region=)`` 同一坐标系。
``[frames].crop`` 非空时，量测层解码的是**媒体原图**，两者不是一个空间 ——
``measure.py`` 用 ``tools.crop_box_to_media`` 换算。

不落的东西：**不写逐帧掩码**（只写区域 + 判据 + 置信），**不写任何时间戳**
（两次跑必须逐字节一致）。
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PIL import Image

# 彩色细笔画（手写笔迹）判据的实现放在 segmenters/framesig.py（叶子工具）：它同时被本模块
# （产出手写区域）与 stable.py 的签名（把笔迹从帧差里挖掉）用。一份实现两处用，避免漂移。
from ..segmenters.framesig import (STROKE_DEFAULTS, app_params, app_screen_metrics,
                                   stroke_mask, stroke_params)

from ..tools import check_basis_dest

SCHEMA = "bnote-overlay/1"
ALGO = "bnote-overlay/1"
# source.basis：帧从哪来。**整片模式的值不变**（旧产物可直接比对）；取样模式写取样包
# 索引的 relpath（§3.6-3）。--basis 的解析在 tools.resolve_basis（full → None = 沿用原路径）。
BASIS = "cache/frames/index.json"

# kind 枚举（取值**只能**来自这里）。handwriting 是 M4 新增：彩色细笔画 = 手写笔迹，
# **只用于把它从 OCR 与帧差/墨迹里排除，交付图里照旧保留**。
KINDS = ("caption_strip", "overlay_widget", "watermark", "progress_bar", "cursor", "handwriting")

PROFILE_W = 160                 # 低分辨率统计宽度（高度按源帧长宽比换算）
LEGACY_W, LEGACY_H = 48, 27     # 旧判据（stable._detect_caption_strip）的签名分辨率，逐字沿用
STATIC_EPS = 0.04               # 逐像素「变了」的判定：平均绝对差超过它才算变
EDGE_EPS = 0.08                 # 全分辨率梯度「有边」的判定（灰度 0..1）
SAT_MIN, VAL_MIN = 0.35, 0.35   # 显著色块：饱和度与亮度下限
HUE_BINS = 12                   # 色调按 30° 分档
SAME_EPS = 0.03                 # 两帧同一区域"内容相同"的判定（平均绝对差上限）

OK = "ok"
INSUFFICIENT = "insufficient"


# ---------------------------------------------------------------- 参数

def params(cfg: dict) -> dict:
    """生效参数（键序固定，写进 overlay.json 的 params）。

    前三条**沿用 ``[segment]`` 的同名键**：字幕带判据只有一份语义，也就只能有一个真源；
    在 ``[overlay]`` 里再抄一遍默认值必然漂移。``[overlay]`` 只放 M3 新增的判据参数。
    """
    seg = cfg.get("segment") or {}
    ov = cfg.get("overlay") or {}
    return {
        # —— 冻结的 params 三条（取自 [segment]，语义与旧实现一致）——
        "caption_strip_ratio": float(seg.get("caption_strip_ratio", 0.14)),
        "caption_band_multiple": float(seg.get("caption_band_multiple", 2.5)),
        "diff_threshold": float(seg.get("diff_threshold", 0.035)),
        # —— M3 新增（[overlay]）——
        "caption_band_top_limit": float(ov.get("caption_band_top_limit", 0.45)),
        "caption_rate_min": float(ov.get("caption_rate_min", 0.15)),
        "caption_min_rows": int(ov.get("caption_min_rows", 2)),
        "caption_row_multiple": float(ov.get("caption_row_multiple", 2.5)),
        "caption_row_gap": int(ov.get("caption_row_gap", 0)),
        "widget_enabled": bool(ov.get("widget_enabled", False)),
        "widget_zone": float(ov.get("widget_zone", 0.30)),
        "widget_changed_max": float(ov.get("widget_changed_max", 0.05)),
        "widget_samples": int(ov.get("widget_samples", 40)),
        "widget_min_area": float(ov.get("widget_min_area", 0.004)),
        "widget_max_area": float(ov.get("widget_max_area", 0.09)),
        "widget_edge_min": float(ov.get("widget_edge_min", 0.03)),
        "widget_color_min": int(ov.get("widget_color_min", 6)),
        "widget_pad": float(ov.get("widget_pad", 0.008)),
        # —— M4 新增：手写笔迹（判据实现在 segmenters/framesig.py）——
        "handwriting_enabled": bool(ov.get("handwriting_enabled", True)),
        "handwriting_stride": int(ov.get("handwriting_stride", 2)),
        "handwriting_min_frac": float(ov.get("handwriting_min_frac", 0.001)),
        "handwriting_min_frames": int(ov.get("handwriting_min_frames", 5)),
        "handwriting_pad": float(ov.get("handwriting_pad", 0.01)),
        **{k: float(ov.get(k, v)) for k, v in STROKE_DEFAULTS.items()},
    }


def _enabled(cfg: dict) -> bool:
    ov = cfg.get("overlay") or {}
    return bool(ov.get("enabled", True)) and bool((cfg.get("segment") or {}).get("auto_caption_strip", True))


def rel_zones(zone: float):
    """四个角的相对坐标搜索窗（与剖面网格无关，直接是画面相对坐标）。"""
    z = float(zone)
    return [("top-left", (0.0, 0.0, z, z)), ("top-right", (1.0 - z, 0.0, 1.0, z)),
            ("bottom-left", (0.0, 1.0 - z, z, 1.0)), ("bottom-right", (1.0 - z, 1.0 - z, 1.0, 1.0))]


# ---------------------------------------------------------------- 一遍扫帧
def scan(frames_dir: Path, files: list, p: dict, app: dict | None = None) -> dict:
    """流式扫一遍已抽出的帧，攒出统计量（不落逐帧中间产物，内存只留剖面与各角最好的一帧）。

    返回 dict：
      R     逐帧对的**逐行**平均绝对差，形状 (pairs, FH)
      mean  逐像素时间均值（灰度，FH×FW）
      chg   逐像素「变过的帧对比例」（FH×FW）
      widget 每个角「最像外物」的一帧 {hue, edge, crop, static}；heard 是各角过关的帧数
      legacy 旧判据的两条变化率 {rb, rm, n, ...}（48×27 口径，逐字搬过来）

    **成本**：角状外物的证据必须**逐帧、近全分辨率**地量（工具条是闪现的，缩略图又数不出色板
    的色调档数——见 widget_regions 的说明），所以这一遍会比只看剖面贵一截；实测 p20（1547 帧）
    从 12 s 涨到约 45 s，BV1CCtz6WEvF_p1（5624 帧）从 45 s 涨到约 2.5 min。这笔钱换的是
    「chosen 帧的 OCR 文本里不再有工具条的字」；将来若要省，方向是先用更便宜的信号圈出候选帧
    再做全分辨率复核（实测「低分辨率梯度」「低分辨率饱和度」都圈不准，这两条已试过并否掉）。
    """
    prev_fine = None
    prev_legacy = None
    rows = []
    sum_g = chg = None
    n = 0
    size = None
    zones = rel_zones(float(p["widget_zone"])) if p["widget_enabled"] else []
    wbest = {name: None for name, _ in zones}
    wheard = {name: 0 for name, _ in zones}
    # M4 手写笔迹：逐**采样**帧算一次彩色细笔画掩膜（判据在 framesig.stroke_mask）。
    # stride 是为了控成本：笔迹是"这一段有没有"的量，不需要每帧都量；而它的两个消费方
    # （stable 的签名、ocr 的送识别图）都是**按帧现算**的，不依赖这里扫了几帧。
    hw = [] if p["handwriting_enabled"] else None
    hsum = None
    hfrac_max = 0.0
    hframes = 0
    app_skipped = 0            # 因"整屏应用/录屏"而跳过手写判定的采样帧数（写进 evidence）

    thr = float(p["diff_threshold"]) * 0.6     # 旧实现的门槛：带内平均差 > 0.6×diff_threshold
    lb = int(LEGACY_H * (1.0 - float(p["caption_strip_ratio"])))   # 固定底部带（旧口径）
    lt = max(2, int(LEGACY_H * 0.35))                              # 旧口径的「标题区」对照
    l_hit_b = l_hit_m = l_n = 0

    for fi, name in enumerate(files):
        path = frames_dir / name
        try:
            with Image.open(path) as im0:
                rgbim = im0.convert("RGB")          # 帧只解码一次，灰度与角上裁片都由它来
                w, h = rgbim.size
                fh = max(2, int(round(PROFILE_W * h / float(w))))
                gray = rgbim.convert("L")
                fine = np.asarray(gray.resize((PROFILE_W, fh), Image.BILINEAR),
                                  dtype=np.float32) / 255.0
                legacy = np.asarray(gray.resize((LEGACY_W, LEGACY_H), Image.BILINEAR),
                                    dtype=np.float32) / 255.0
                # 角上的证据只裁四小块再转 numpy：整帧 asarray（2.7M 个 float）是这条链最贵的一步，
                # 而四块加起来只有 4 个 384x216；实测这一改把 BV1CCtz6WEvF_p1 的 overlay 从 311 s 降到约 150 s。
                crops = {}
                for corner, (zl, zt, zr, zb) in zones:
                    x0, y0 = int(zl * w), int(zt * h)
                    x1, y1 = max(int(zr * w), x0 + 1), max(int(zb * h), y0 + 1)
                    crops[corner] = np.asarray(rgbim.crop((x0, y0, x1, y1)),
                                               dtype=np.float32) / 255.0
        except Exception as exc:
            print("[overlay] 跳过读不出来的帧 %s（%s）" % (name, exc))
            continue
        if size is None:
            size = (w, h)
        if sum_g is None:
            sum_g = np.zeros_like(fine)
            chg = np.zeros_like(fine)
        if fine.shape != sum_g.shape:
            print("[overlay] 帧尺寸不一致（%s）→ 停止统计" % name)
            break
        sum_g += fine
        for corner, (zl, zt, zr, zb) in zones:
            sub = crops[corner]
            if sub.size < 64:
                continue
            hue, edge = _hue_patches(sub, p), _edge_frac(sub)
            if hue >= int(p["widget_color_min"]) and edge >= float(p["widget_edge_min"]):
                wheard[corner] += 1
            cur = wbest[corner]
            if cur is None or (hue, edge) > (cur["hue"], cur["edge"]):
                wbest[corner] = {"hue": hue, "edge": edge, "crop": sub,
                                 "zone": (zl, zt, zr, zb)}
        if hw is not None and (fi % max(1, int(p["handwriting_stride"]))) == 0:
            small = np.asarray(rgbim.resize(
                (PROFILE_W * 2, max(2, int(round(PROFILE_W * 2 * h / float(w))))),
                Image.BILINEAR), dtype=np.float32) / 255.0
            # M4b 守卫：整屏应用/录屏（IDE/浏览器/终端）**不参与手写判定** —— 那里的彩色像素是
            # UI，不是手写；不排掉会让 P51/P52 这种录屏集凭空产出一条 handwriting 区域，
            # 进而把整集的帧差/OCR 都涂白（实测 chosen 漂移 10/27 与 3/14 段）。
            if app_screen_metrics(small.mean(axis=2), small, app or app_params(None))["hit"]:
                app_skipped += 1
                sm = np.zeros((1, 1), dtype=bool)      # 空掩膜：本帧不计入统计
            else:
                sm = stroke_mask(small, stroke_params(p))
            if sm.size > 1:
                frac = float(sm.mean())
                if hsum is None or hsum.shape != sm.shape:
                    hsum = np.zeros(sm.shape, dtype=np.float32)
                hsum += sm.astype(np.float32)
                hframes += 1
                if frac > hfrac_max:
                    hfrac_max = frac
                if frac >= float(p["handwriting_min_frac"]):
                    hw.append((name, frac))
        if prev_fine is not None:
            d = np.abs(fine - prev_fine)
            rows.append(d.mean(axis=1))
            chg += (d > STATIC_EPS)
        if prev_legacy is not None:
            a, b = prev_legacy, legacy
            if float(np.abs(a[lb:LEGACY_H] - b[lb:LEGACY_H]).mean()) > thr:
                l_hit_b += 1
            if float(np.abs(a[0:lt] - b[0:lt]).mean()) > thr:
                l_hit_m += 1
            l_n += 1
        prev_fine, prev_legacy = fine, legacy
        n += 1

    if sum_g is None or not rows:
        return {"R": None, "mean": None, "chg": None, "widget": {}, "heard": {},
                "pairs": 0, "size": size, "legacy": None,
                "stroke_sum": None, "stroke_frames": 0, "stroke_frac_max": 0.0,
                "stroke_hits": [], "app_skipped": app_skipped}
    return {
        "stroke_sum": hsum, "stroke_frames": hframes, "stroke_frac_max": hfrac_max,
        "stroke_hits": hw, "app_skipped": app_skipped,
        "R": np.array(rows, dtype=np.float32),
        "mean": sum_g / n,
        "chg": chg / max(1, len(rows)),
        "widget": wbest,
        "heard": wheard,
        "pairs": len(rows),
        "size": size,
        "legacy": {"rb": (l_hit_b / l_n) if l_n else 0.0,
                   "rm": (l_hit_m / l_n) if l_n else 0.0,
                   "n": l_n, "band_rows": [lb, LEGACY_H], "title_rows": [0, lt],
                   "threshold": thr, "multiple": float(p["caption_band_multiple"])},
    }


# ---------------------------------------------------------------- 判据 1：字幕带
def _band_rate(R: np.ndarray, y0: int, y1: int, thr: float) -> float:
    """带整体变化率：带内逐行平均后仍超过 thr 的帧对占比。"""
    return float((R[:, y0:y1 + 1].mean(axis=1) > thr).mean())


def fit_caption_band(R: np.ndarray, p: dict) -> dict:
    """按逐行变化剖面拟合字幕带。

    把画面下方 ``caption_band_top_limit`` 以上的行当参考区，取搜索窗内变化率最高的行作锚点，
    再向上/向下生长（允许 ``caption_row_gap`` 个低行，给多行字幕留缝）。
    """
    fh = int(R.shape[1])
    lo = int(round(fh * (1.0 - float(p["caption_band_top_limit"]))))
    lo = max(1, min(lo, fh - 1))
    thr = float(p["diff_threshold"]) * 0.6
    rate = (R > thr).mean(axis=0)
    ref = float(rate[:lo].mean())
    gate = max(ref * float(p["caption_row_multiple"]), ref + 0.08)
    peak = lo + int(np.argmax(rate[lo:]))
    y0 = y1 = peak
    gap = 0
    while y0 - 1 >= lo:
        if rate[y0 - 1] >= gate:
            y0 -= 1
            gap = 0
        elif gap < int(p["caption_row_gap"]):
            y0 -= 1
            gap += 1
        else:
            break
    gap = 0
    while y1 + 1 < fh:
        if rate[y1 + 1] >= gate:
            y1 += 1
            gap = 0
        elif gap < int(p["caption_row_gap"]):
            y1 += 1
            gap += 1
        else:
            break
    return {"y0": int(y0), "y1": int(y1), "rb": _band_rate(R, y0, y1, thr),
            "ref": ref, "gate": gate, "thr": thr, "top": lo, "fh": fh,
            "content_rate": _band_rate(R, 0, max(1, lo - 1), thr)}


def caption_region(R: np.ndarray, p: dict, legacy: dict):
    """字幕带判据的汇总：候选带（拟合带 + 旧固定带），任一条过就产出一条 ``caption_strip``。"""
    mult = float(p["caption_band_multiple"])
    lo_gate = float(p["caption_rate_min"])
    fit = fit_caption_band(R, p)
    fh = fit["fh"]

    cands = []
    fit_gate = max(fit["content_rate"] * float(p["caption_row_multiple"]),
                   fit["content_rate"] + 0.08)
    if (fit["rb"] >= lo_gate and fit["rb"] > fit_gate
            and (fit["y1"] - fit["y0"] + 1) >= int(p["caption_min_rows"])):
        cands.append({"name": "band_change_rate_fit", "y0": fit["y0"], "y1": fit["y1"],
                      "rb": fit["rb"], "gate": fit_gate})
    if legacy and legacy["n"]:
        lgate = max(legacy["rm"] * mult, legacy["rm"] + 0.08)
        if legacy["rb"] > lo_gate and legacy["rb"] > lgate:
            y0 = int(round(fh * legacy["band_rows"][0] / float(LEGACY_H)))
            cands.append({"name": "band_change_rate_fixed", "y0": y0, "y1": fh - 1,
                          "rb": legacy["rb"], "gate": lgate})
    if not cands:
        return None

    y0 = max(0, min(min(c["y0"] for c in cands), fh - 1))
    y1 = max(y0, min(max(c["y1"] for c in cands), fh - 1))
    box = [0.0, round(y0 / float(fh), 4), 1.0, 1.0]
    rb = max(c["rb"] for c in cands)
    margin = min(rb / max(lo_gate, 1e-6), rb / max(min(c["gate"] for c in cands), 1e-6))
    conf = float(min(0.95, max(0.5, 0.45 + 0.2 * margin)))
    ev = {"criterion": "band_change_rate",
          "rb": round(rb, 4), "rm": round(float(legacy["rm"]), 4) if legacy else None,
          "content_rate": round(fit["content_rate"], 4),
          "threshold": lo_gate,
          "multiple": mult,
          "band_rows": [y0, y1],
          "band_rows_total": fh,
          "row_gate": round(min(c["gate"] for c in cands), 4),
          "candidates": [{"criterion": c["name"], "rb": round(c["rb"], 4),
                          "rows": [c["y0"], c["y1"]], "gate": round(c["gate"], 4)} for c in cands],
          "threshold_basis": "0.6×diff_threshold（沿用旧实现）"}
    if legacy and legacy["n"]:
        ev["legacy_fixed_band"] = {"rb": round(legacy["rb"], 4), "rm": round(legacy["rm"], 4),
                                   "rows": legacy["band_rows"], "rows_total": LEGACY_H,
                                   "ratio": float(p["caption_strip_ratio"]),
                                   "criteria_name": "band_change_rate_fixed"}
    return {"kind": "caption_strip", "box": box, "confidence": round(conf, 2),
            "evidence": ev, "applicability": OK}


# ---------------------------------------------------------------- 判据 2：角状外物
def _hue_patches(arr: np.ndarray, p: dict) -> int:
    """显著色块色调数：饱和度/亮度都够的像素按 30° 分 12 档，每档像素达标才计一档。"""
    mx = arr.max(axis=2)
    mn = arr.min(axis=2)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    m = (sat > SAT_MIN) & (mx > VAL_MIN)
    if not m.any():
        return 0
    d = np.maximum(mx - mn, 1e-6)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    hue = np.where(mx == r, ((g - b) / d) % 6.0,
                   np.where(mx == g, (b - r) / d + 2.0, (r - g) / d + 4.0)) * 60.0
    bins = np.clip((hue / (360.0 / HUE_BINS)).astype(np.int32), 0, HUE_BINS - 1)[m]
    cnt = np.bincount(bins, minlength=HUE_BINS)
    need = max(20, int(0.004 * m.size))
    return int((cnt >= need).sum())


def _edge_map(arr: np.ndarray) -> np.ndarray:
    """有边掩膜（灰度梯度 > EDGE_EPS），形状与 arr 的 H/W 对齐（末尾列/行取邻值）。

    输入可以是 RGB（按通道取均值当灰度）或已经是灰度/单通道（预筛阶段传的就是 2-D 灰度）。
    """
    g = arr.mean(axis=2) if arr.ndim == 3 else arr
    gx = np.zeros_like(g, dtype=bool)
    gy = np.zeros_like(g, dtype=bool)
    gx[:, :-1] = np.abs(np.diff(g, axis=1)) > EDGE_EPS
    gx[:, -1] = gx[:, -2] if g.shape[1] > 1 else False
    gy[:-1, :] = np.abs(np.diff(g, axis=0)) > EDGE_EPS
    gy[-1, :] = gy[-2, :] if g.shape[0] > 1 else False
    return gx | gy


def _sat_map(arr: np.ndarray) -> np.ndarray:
    mx = arr.max(axis=2)
    mn = arr.min(axis=2)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    return (sat > SAT_MIN) & (mx > VAL_MIN)


def _edge_frac(arr: np.ndarray) -> float:
    return float(_edge_map(arr).mean())


def widget_regions(paths, files: list, scan_out: dict, p: dict) -> list:
    """角状外物：位置固定 + 帧间几乎不变 + 有细小字符或色块（三条同时成立才采信）。

    三条怎么量（每条都落进 evidence）：
      ① 位置固定：四个角各一个 widget_zone 边长的搜索窗；
      ② 帧间几乎不变：窗内逐像素「变过的帧对比例」均值 < widget_changed_max；
      ③ 有细小字符或色块：scan 在**每一帧、近全分辨率**上算的显著色块色调档数 color_patches
         （饱和度与亮度都够的像素按 30 度分 12 档，每档像素数达窗内 0.4% 才计一档）与梯度密度
         edge_frac，**两条都要满足**。

    为什么要逐帧：标注工具条是**闪现**的——p20 整集 1547 帧里只有 3 帧的角上同时有色块与文字
    （t 约 255~258 s，正好覆盖第 5 页的 chosen 帧），p21 只有 8 帧。等间隔抽样必然漏掉它，
    而漏掉的后果正是「工具条文字进了 chosen 帧的 OCR 文本」。
    为什么不能用缩略图：缩到 480 宽时一排色板被双线性插值糊成一团，实测同一个工具条再也数不出
    5 档色调（全分辨率下是 6~7 档），这也是「先低分辨率预筛再复核」这条捷径在本样本上失败的原因。
    为什么两条都要：幻灯片表格的梯度比工具条更高（实测 p22 表格 0.16 vs 工具条 0.09），
    只用梯度会把表格挖掉；工具条的显著色调档数 6~7，幻灯片内容（表格 / 代码 / 手写）只有 1~3。

    **代价（写进证据，不藏）**：区域是全局的（冻结契约不放时间戳），所以工具条只出现几帧时，
    其余帧里同一角落的幻灯片内容也会被一起挖掉；evidence 里的 present_frames / frame_count
    让复核者一眼看出这条区域是常驻还是闪现，从而判断该不该采信。
    """
    chg = scan_out.get("chg")
    if chg is None or not files:
        return []
    fh, fw = chg.shape
    unchanging = 1.0 - chg
    gate = 1.0 - float(p["widget_changed_max"])
    bestmap = scan_out.get("widget") or {}
    heard = scan_out.get("heard") or {}
    regions = []
    for corner, (zl, zt, zr, zb) in rel_zones(float(p["widget_zone"])):
        best = bestmap.get(corner)
        if not best:
            continue
        if best["hue"] < int(p["widget_color_min"]) or best["edge"] < float(p["widget_edge_min"]):
            continue
        crop = best["crop"]
        zh_px, zw_px = int(crop.shape[0]), int(crop.shape[1])
        mask = _edge_map(crop) | _sat_map(crop)
        if not mask.any():
            continue
        ys, xs = np.where(mask)
        bx0, bx1 = int(xs.min()), int(xs.max()) + 1
        by0, by1 = int(ys.min()), int(ys.max()) + 1
        pad_x = int(round(zw_px * float(p["widget_pad"])))
        pad_y = int(round(zh_px * float(p["widget_pad"])))
        bx0, by0 = max(0, bx0 - pad_x), max(0, by0 - pad_y)
        bx1, by1 = min(zw_px, bx1 + pad_x), min(zh_px, by1 + pad_y)
        w_rel, h_rel = zr - zl, zb - zt
        box = [round(zl + (bx0 / float(zw_px)) * w_rel, 4),
               round(zt + (by0 / float(zh_px)) * h_rel, 4),
               round(zl + (bx1 / float(zw_px)) * w_rel, 4),
               round(zt + (by1 / float(zh_px)) * h_rel, 4)]
        area = (box[2] - box[0]) * (box[3] - box[1])
        if area < float(p["widget_min_area"]) or area > float(p["widget_max_area"]):
            continue
        ly0, ly1 = int(round(zt * fh)), max(int(round(zb * fh)), int(round(zt * fh)) + 1)
        lx0, lx1 = int(round(zl * fw)), max(int(round(zr * fw)), int(round(zl * fw)) + 1)
        static = float(unchanging[ly0:ly1, lx0:lx1].mean())
        if static < gate:
            continue
        ty0, ty1 = int(round(box[1] * fh)), max(int(round(box[3] * fh)), int(round(box[1] * fh)) + 1)
        tx0, tx1 = int(round(box[0] * fw)), max(int(round(box[2] * fw)), int(round(box[0] * fw)) + 1)
        static_t = float(unchanging[ty0:ty1, tx0:tx1].mean())
        edge_t, hue_t = best["edge"], best["hue"]
        w_r, h_r = box[2] - box[0], box[3] - box[1]
        if h_r <= 0.035 and w_r >= 0.35 and (box[1] <= 0.08 or box[3] >= 0.92):
            kind = "progress_bar"
        elif box[3] <= 0.35 and hue_t < int(p["widget_color_min"]):
            kind = "watermark"
        else:
            kind = "overlay_widget"
        present = int(heard.get(corner, 0))
        conf = float(min(0.9, max(0.3, 0.25 + 0.5 * (hue_t / 8.0) + 1.0 * edge_t)))
        regions.append({"kind": kind, "box": box, "confidence": round(conf, 2),
                        "evidence": {"criterion": "corner_static_glyphs",
                                     "corner": corner,
                                     "static_frac": round(static_t, 4),
                                     "changed_max": float(p["widget_changed_max"]),
                                     "edge_frac": round(edge_t, 4),
                                     "color_patches": int(hue_t),
                                     "area_ratio": round(area, 4),
                                     "present_frames": present,
                                     "frame_count": len(files),
                                     "note": "位置固定 + 帧间几乎不变 + 有细小字符或色块"
                                             "（色块按显著色调分档计数；区域是全局的，闪现的"
                                             "工具条会连带遮住其余帧同一角的内容）"},
                        "applicability": OK})
    return regions


# ---------------------------------------------------------------- 判据 3：手写笔迹
def handwriting_regions(files: list, scan_out: dict, p: dict) -> tuple:
    """手写笔迹：彩色细笔画（红/橙笔；蓝窗默认关）→ 一条 handwriting 区域。

    与另两条判据最大的不同：**消费方要按帧用，不能按框挖**。
    笔迹在画面上是移动、累积的（p22 一页上越写越多），一个全局框会把其余帧同位置的正文
    一起挖掉（这正是 M3 角状外物那条判据被默认关掉的原因）。所以这里的 box 只是**审计与
    检索范围**：告诉人"这一集的笔迹大致落在哪"，而 stable / ocr 拿到 overlay.json 后按
    params 里的 handwriting_* 判据**逐帧**重算笔画掩膜。契约里 kind=handwriting 的语义
    （"只用于把它从 OCR 与帧差里排除，交付图照旧保留"）就是靠这条实现的。

    产出条件（任一条不成立就**不落区域**，并返回一句理由由 analyze 写进文档级 applicability）：
      ① 采样帧里至少 handwriting_min_frames 帧的笔画占比 >= handwriting_min_frac；
      ② 这些帧的笔画像素并集能给出一个非空包围盒。
    返回 (regions, reason)：regions 为空时 reason 非空。
    """
    apply_note = ("按帧应用：消费方（stable/ocr）用 params 里的 handwriting_* 判据逐帧重算笔画"
                  "掩膜再涂白，**不是**把这条 box 整块挖掉（box 只是审计范围）")
    hits = scan_out.get("stroke_hits") or []
    hsum = scan_out.get("stroke_sum")
    sampled = int(scan_out.get("stroke_frames") or 0)
    frac_max = float(scan_out.get("stroke_frac_max") or 0.0)
    # 判不出**不落区域**（返回空列表 + 一句理由，由 analyze 写进文档级 applicability 的 note）。
    # 为什么不落成"box=None 的 insufficient 区域"：契约要求每条 region 带 box，消费方（切片 /
    # 量测 / 复核脚本）都是按"有 box 才能用"写的；落一条没有 box 的区域只会到处埋雷。
    # §3.4-3 的"不许静默返回空"由**文档级 note 里的这句理由**满足，不是靠塞一条残疾区域。
    if not p["handwriting_enabled"]:
        return [], "手写笔迹判据按配置关闭（[overlay].handwriting_enabled=false）"
    if hsum is None or not hits:
        return [], ("手写笔迹判据不成立（采样 %d 帧，笔画像素占比最高 %.5f，低于下限 %.5f；%s）"
                    % (sampled, frac_max, float(p["handwriting_min_frac"]), apply_note))
    if len(hits) < int(p["handwriting_min_frames"]):
        return [], ("手写笔迹判据不成立（命中 %d 帧 < 门槛 %d 帧；%s）"
                    % (len(hits), int(p["handwriting_min_frames"]), apply_note))
    keep = hsum >= int(p["handwriting_min_frames"])
    ys, xs = np.where(keep)
    if ys.size == 0:
        return [], ("手写笔迹判据不成立（没有重复出现的笔画像素；%s）" % apply_note)
    h, w = hsum.shape
    pad = float(p["handwriting_pad"])
    box = [round(max(0.0, float(xs.min()) / w - pad), 4),
           round(max(0.0, float(ys.min()) / h - pad), 4),
           round(min(1.0, (float(xs.max()) + 1) / w + pad), 4),
           round(min(1.0, (float(ys.max()) + 1) / h + pad), 4)]
    area = (box[2] - box[0]) * (box[3] - box[1])
    conf = float(min(0.8, 0.35 + 0.3 * min(1.0, len(hits) / max(1.0, sampled) * 4.0)
                     + 0.3 * min(1.0, frac_max / 0.01)))
    return [{"kind": "handwriting", "box": box, "confidence": round(conf, 2),
             "evidence": {"criterion": "color_stroke",   # 成功路径：box 一定是四元组（判不出则不落区域）
                          "stroke_frames": len(hits), "frames_sampled": sampled,
                          "app_skipped_frames": int(scan_out.get("app_skipped") or 0),
                          "stride": int(p["handwriting_stride"]),
                          "stroke_frac_max": round(frac_max, 5),
                          "min_frac": float(p["handwriting_min_frac"]),
                          "min_frames": int(p["handwriting_min_frames"]),
                          "area_ratio": round(area, 4),
                          "hue_windows": [[0.0, float(p["handwriting_hue_max"])],
                                          [float(p["handwriting_hue_min"]), 360.0],
                                          [float(p["handwriting_blue_min"]),
                                           float(p["handwriting_blue_max"])],
                                          ],
                          "sat_min": float(p["handwriting_sat_min"]),
                          "val_min": float(p["handwriting_val_min"]),
                          "bg_light_min": float(p["handwriting_light_min"]),
                          # 判据是"笔画 vs 实心色块"，按**厚度**分（见 framesig._block_core）：
                          # 阈值一起落盘，复核者不用翻代码就能重算这条区域
                          "block_erode": int(p["handwriting_block_erode"]),
                          "block_pad": int(p["handwriting_block_pad"]),
                          "ink_max": float(p["handwriting_ink_max"]),
                          "criterion_note": "厚度 >= 2*block_erode+1 的连通域算实心色块，整块不涂白"
                                            "（红底白字条/填充框上的字得以保留）；只有笔画涂白，"
                                            "且最后一圈膨胀不长进深色印刷字",
                          "guard_note": "M4b：整屏应用/录屏（app_screen）帧既不参与判定也不涂白"
                                        "（见 app_skipped_frames 与 framesig.APP_DEFAULTS）",
                          "note": apply_note},
             "applicability": OK}], None


# ---------------------------------------------------------------- 主流程
def _params_doc(p: dict) -> dict:
    return {k: p[k] for k in (
        "caption_strip_ratio", "caption_band_multiple", "diff_threshold",
        "caption_band_top_limit", "caption_rate_min", "caption_min_rows",
        "caption_row_multiple", "caption_row_gap",
        "widget_enabled", "widget_zone", "widget_changed_max", "widget_samples",
        "widget_min_area", "widget_max_area", "widget_edge_min", "widget_color_min",
        "widget_pad", "handwriting_enabled", "handwriting_stride", "handwriting_min_frac",
        "handwriting_min_frames", "handwriting_pad", *STROKE_DEFAULTS.keys())}


def analyze(cfg: dict, paths, frames=None, basis=None) -> dict:
    """跑一遍并组出 overlay 文档（**不落盘**，便于单独测试与复算）。

    basis=None = 整片模式（读 cache/frames/index.json，与 M3 逐字节相同）；
    basis（来自 tools.resolve_basis("sample")）= 取样包：读取样索引、帧根目录换成取样帧目录。
    """
    p = params(cfg)
    root = (basis or {}).get("root") or paths.frames
    idx = (basis or {}).get("index") or (paths.read_json(paths.frames / "index.json") or {})
    if frames is None:
        frames = idx.get("frames") or []
    files = [str(f.get("file")) for f in frames if f.get("file")]
    fps = float(idx["fps"]) if idx.get("fps") else None
    frames_min = int((cfg.get("overlay") or {}).get("frames_min", 24))

    regions = []
    applies = []
    size = None
    if len(files) < frames_min:
        applies.append("帧数 %d < 门槛 %d，统计量不足" % (len(files), frames_min))
    else:
        out = scan(root, files, p, app_params((cfg.get("roles") or {})))
        size = out["size"]
        if out["R"] is None or out["pairs"] < 8:
            applies.append("可用帧对只有 %d，变化率判据不成立" % out["pairs"])
        else:
            cap = caption_region(out["R"], p, out["legacy"])
            if cap:
                regions.append(cap)
            else:
                applies.append("底部带变化率未过阈值（字幕条判据不成立）")
            if p["widget_enabled"]:
                regions.extend(widget_regions(paths, files, out, p))
            if p["handwriting_enabled"]:
                hw_regs, hw_reason = handwriting_regions(files, out, p)
                regions.extend(hw_regs)
                if hw_reason:
                    applies.append(hw_reason)
            if not regions:
                applies.append("角状外物判据也不成立（没有同时满足「位置固定 + 帧间几乎不变 "
                               "+ 有细小字符或色块」的边角区域）"
                               if p["widget_enabled"] else
                               "角状外物判据按配置关闭（[overlay].widget_enabled=false，"
                               "默认值，理由见 config/default.toml 的注释）")

    # handwriting 区域**不进遮罩面积**：它的 box 只是审计范围（按帧应用，见 handwriting_regions），
    # 把它算进 masked_area_ratio 会给出"遮掉了 90% 画面"这种误导性数字。
    hw = [r for r in regions if r.get("kind") == "handwriting" and r.get("applicability") == OK]
    boxed = [r for r in regions if r.get("kind") != "handwriting"]
    masked = bool(boxed)
    note = ("已按遮罩算：stable 挖掉这些区域后再算帧差/墨迹/清晰度/送 OCR，"
            "measure 用 drawbox 在同一遍解码里挖掉同样的像素" if masked
            else "未产出可整块挖的遮罩（%s）；消费方退化为全画面，与 M3 之前一致"
                 % "；".join(applies))
    if hw:
        note += ("；另有 %d 条 handwriting 区域（手写笔迹）：**不整块挖、不计入遮罩面积**，"
                 "消费方按 params 的 handwriting_* 判据逐帧重算笔画掩膜" % len(hw))
    area = sum(max(0.0, (r["box"][2] - r["box"][0]) * (r["box"][3] - r["box"][1]))
               for r in boxed)
    return {
        "schema": SCHEMA,
        "vid": paths.vid,
        "algo": ALGO,
        "source": {"frames": len(files), "fps": fps, "size": list(size) if size else None,
                   "basis": (basis or {}).get("relpath") or BASIS},
        "params": _params_doc(p),
        "regions": regions,
        "applicability": {"masked": masked,
                          "frames_min_ok": len(files) >= frames_min,
                          "sampling_fps": fps,
                          "note": note},
        "stats": {"region_count": len(regions),
                  "boxed_region_count": len(boxed),
                  "handwriting_region_count": len(hw),
                  "masked_area_ratio": round(min(1.0, area), 4)},
    }


def _rel(paths, path) -> str:
    try:
        return str(Path(path).relative_to(paths.root))
    except ValueError:
        return path.name


def masks(doc: dict) -> list:
    """从 overlay 文档里取出**可采信**的区域框（消费方也可自行读文件，文件即接口）。

    **不含 handwriting**：手写笔迹的 box 只是审计范围，整块挖会把其余帧同位置的正文一起挖掉；
    消费方应当读 strokes() 拿到判据参数后**逐帧**重算笔画掩膜。
    """
    return [list(r["box"]) for r in (doc.get("regions") or [])
            if r.get("applicability") == OK and r.get("kind") != "handwriting"
            and isinstance(r.get("box"), list) and len(r["box"]) == 4]


def strokes(doc: dict) -> dict | None:
    """overlay 文档里的**手写笔迹判据参数**（overlay.json 的 params，含 handwriting_* 键）。

    有可采信的 handwriting 区域才返回，否则返回 None（消费方据此决定要不要逐帧挖笔迹）。
    返回的是 params 整份，framesig.stroke_params() 会挑出自己要的键——单一真源在那边。
    """
    for r in (doc.get("regions") or []):
        if r.get("kind") == "handwriting" and r.get("applicability") == OK:
            return dict(doc.get("params") or {})
    return None


def run(cfg: dict, paths, force: bool = False, basis: dict | None = None) -> dict:
    """``bnote overlay`` 的入口：已存在且非 --force 就跳过；跑完打印一行摘要。

    basis=None = 整片（cache/frames/index.json → cache/<vid>/overlay.json，行为不变）；
    basis=取样包 → 取样帧 + cache/<vid>/sample/overlay.json（**不碰顶层 overlay.json**）。
    """
    dest = paths.sample_overlay if basis else paths.overlay
    if not _enabled(cfg):
        print("[overlay] 已按配置关闭（[overlay].enabled 或 [segment].auto_caption_strip=false），跳过")
        return {}
    if dest.exists() and not force:
        doc = paths.read_json(dest) or {}
        print("[overlay] 已存在，跳过（--force 重跑）：%s" % _rel(paths, dest))
        return doc
    if basis:
        frames = basis.get("frames") or []
    else:
        frames = (paths.read_json(paths.frames / "index.json") or {}).get("frames") or []
    if not frames:
        print("[overlay] 没有 %s（%s）→ 不产出 %s"
              % ((basis or {}).get("relpath") or "cache/frames/index.json",
                 "先跑 bnote sample" if basis else "先跑 bnote slides 抽帧", _rel(paths, dest)))
        return {}
    t0 = time.monotonic()
    doc = analyze(cfg, paths, frames, basis=basis)
    check_basis_dest(dest, basis, "overlay.json", "overlay.json",
                     paths.cache, paths.sample)      # **同名**：必须连目录一起比
    dest.parent.mkdir(parents=True, exist_ok=True)     # 只建自己要写的那一层
    paths.write_json(dest, doc)
    print(summary_line(doc, time.monotonic() - t0, _rel(paths, dest)))
    return doc


def summary_line(doc: dict, elapsed: float, dest: str) -> str:
    """一行摘要：帧数/帧对数/区域数/遮罩面积/各区域与判据/耗时。"""
    src = doc.get("source") or {}
    st = doc.get("stats") or {}
    ap = doc.get("applicability") or {}
    # box 一律按"四元组或没有"处理：即使将来有人落了一条缺 box 的区域，这里也只报名字不炸
    kinds = "、".join("%s@[%s]" % (r.get("kind"),
                                   ",".join("%.3f" % x for x in r["box"]))
                      for r in (doc.get("regions") or [])
                      if isinstance(r.get("box"), list) and len(r["box"]) == 4) or "无"
    return ("[overlay] %s 帧 / fps=%s / %s | 区域 %d（%s）| 遮罩面积 %.1f%% | masked=%s | %.1fs → %s"
            % (src.get("frames"), src.get("fps"), src.get("size"), st.get("region_count"), kinds,
               100.0 * float(st.get("masked_area_ratio") or 0.0), ap.get("masked"), elapsed, dest))
