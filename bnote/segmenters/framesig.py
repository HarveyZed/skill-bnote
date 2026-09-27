"""帧签名与廉价打分：所有 segmenter 共用的底层工具。

只用 Pillow + numpy（不依赖 opencv），保证在最小依赖下可运行。

本模块是**叶子工具**（不 import 任何流水线层）：M4 的"彩色细笔画（手写笔迹）"判据放在这里，
是因为它有两个消费方——layers/overlay.py（认出手写区域并落盘成 overlay.json 的
handwriting 区域）与 segmenters/stable.py 的签名（把笔迹从帧差与 dHash 里挖掉）。
一份实现两处用，避免两份判据迟早漂移（P1 的"中性工具"那一条）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

SMALL_W, SMALL_H = 48, 27

# 彩色细笔画（手写笔迹）判据的**唯一真源**：默认值在这里，overlay.json 的 params 里落一份，
# 消费方（stable / ocr）从 overlay.json 读回来。键名固定，不要在各处另起名字。
STROKE_DEFAULTS = {
    "handwriting_sat_min": 0.45,      # 饱和度下限（笔画是饱和色，印刷灰字不是）
    "handwriting_val_min": 0.35,      # 亮度下限（排除暗色块）
    "handwriting_hue_max": 45.0,      # 暖色窗下界：h <= 此值（红/橙笔）
    "handwriting_hue_min": 320.0,     # 暖色窗上界：h >= 此值（红笔的另一侧）
    "handwriting_blue_min": 0.0,      # 蓝窗（0/0 = 关闭：实测幻灯片自身的蓝色标题字会被误挖）
    "handwriting_blue_max": 0.0,
    "handwriting_light_min": 0.55,    # 笔迹邻近背景的亮度下限（写在浅底上）
    "handwriting_block_window": 5,    # 判"实心色块"的滑窗边长（分析尺度上的像素）
    "handwriting_block_dens": 0.95,   # 窗内饱和像素占比达到它就算色块内部，不算笔画
    "handwriting_bg_window": 15,      # 判"邻近背景亮不亮"的滑窗边长
}


def stroke_params(raw: dict | None) -> dict:
    """从 overlay.json 的 params（或任意字典）里取出笔画判据参数；缺项用默认值。"""
    src = raw or {}
    return {k: float(src.get(k, v)) for k, v in STROKE_DEFAULTS.items()}


def _shift(a: np.ndarray, dy: int, dx: int, fill=False):
    out = np.full_like(a, fill)
    ys = slice(max(0, dy), a.shape[0] + min(0, dy))
    xs = slice(max(0, dx), a.shape[1] + min(0, dx))
    yd = slice(max(0, -dy), a.shape[0] + min(0, -dy))
    xd = slice(max(0, -dx), a.shape[1] + min(0, -dx))
    out[yd, xd] = a[ys, xs]
    return out


def _dilate(m: np.ndarray, times: int = 1) -> np.ndarray:
    for _ in range(times):
        m = m | _shift(m, 1, 0) | _shift(m, -1, 0) | _shift(m, 0, 1) | _shift(m, 0, -1)
    return m


def _box_mean(a: np.ndarray, k: int) -> np.ndarray:
    """k×k 滑窗均值（积分图实现，O(1)/像素）。用来判"这附近是不是一片实心色块 / 背景亮不亮"。"""
    c = np.pad(np.cumsum(np.cumsum(a, axis=0), axis=1), ((1, 0), (1, 0)))
    h, w = a.shape
    ys = np.clip(np.arange(h) - k // 2, 0, h)
    ye = np.clip(np.arange(h) + k // 2 + 1, 0, h)
    xs = np.clip(np.arange(w) - k // 2, 0, w)
    xe = np.clip(np.arange(w) + k // 2 + 1, 0, w)
    s = c[np.ix_(ye, xe)] - c[np.ix_(ys, xe)] - c[np.ix_(ye, xs)] + c[np.ix_(ys, xs)]
    n = (ye - ys)[:, None] * (xe - xs)[None, :]
    return s / np.maximum(n, 1)


def stroke_mask(rgb: np.ndarray, sp: dict) -> np.ndarray:
    """彩色**细**笔画掩膜（手写笔迹）：饱和色 + 落在笔色窗口 + 不是大色块的内部/边缘 +
    邻近背景够亮。

    为什么这四条（每条都对着实测）：
    * 饱和色 + 笔色窗口：p22 整集的红笔是 h 约 0~15 的纯红；幻灯片自身的蓝标题、青色块若一起
      挖掉会伤正文，所以蓝窗默认关（handwriting_blue_min/max = 0，实测据见 overlay.py 文档）。
    * 排除大色块内部**并向外膨胀两格**：幻灯片里的红/橙填充块（p22 流程图）边缘只有 1~2 像素
      过渡，只去内部会把整块的外圈留成"笔画"。
    * 邻近背景要亮：笔是写在浅底上的；这条把暗色主题（BV1CCtz6WEvF_p1）里的彩色元素排除在外。
    """
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    d = np.maximum(mx - mn, 1e-6)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    hue = np.where(mx == r, ((g - b) / d) % 6.0,
                   np.where(mx == g, (b - r) / d + 2.0, (r - g) / d + 4.0)) * 60.0
    warm = (hue <= sp["handwriting_hue_max"]) | (hue >= sp["handwriting_hue_min"])
    if sp["handwriting_blue_max"] > sp["handwriting_blue_min"]:
        warm = warm | ((hue >= sp["handwriting_blue_min"]) & (hue <= sp["handwriting_blue_max"]))
    m = (mx - mn) / np.maximum(mx, 1e-6) >= sp["handwriting_sat_min"]
    m &= (mx >= sp["handwriting_val_min"]) & warm
    if not m.any():
        return m
    # "是不是一片实心色块"用滑窗密度判，而不是形态学腐蚀：笔画本身只有 2~4 像素宽
    # （320 宽的分析尺度上），3x3 腐蚀会把笔画自己吃干净——实测那样只剩零星几点。
    dens = _box_mean(m.astype(np.float32), int(sp["handwriting_block_window"]))
    block = _dilate(dens >= float(sp["handwriting_block_dens"]), 1)
    gray = rgb.mean(axis=2)
    bg = _box_mean(gray, int(sp["handwriting_bg_window"]))
    thin = m & ~block & (bg >= sp["handwriting_light_min"])
    if not thin.any():
        return thin
    # 向外扩一圈：笔画的抗锯齿边缘饱和度低，不补这一圈会把红笔的淡边留给 OCR 与帧差。
    return _dilate(thin, 1)


def signature(path: Path, region=(0.0, 0.0, 1.0, 1.0), masks=None, strokes=None,
              stroke_width: int = 320):
    """返回 (small_gray_float32[27,48], dhash_uint64)。

    传入 masks（overlay.json 的 regions 框）时**先把它们涂成同一片白**再算签名。涂成同一个
    常数有两个好处：帧差里这些像素恒为 0（不再污染稳定性判定），dHash 在这些格子上恒等
    （不再污染汉明距离）。也就是说遮罩同时作用于**帧差的两项**，而不只是像素差那一项。

    传入 strokes（overlay.json 的 params，含 handwriting_* 键）时，再按 stroke_mask **逐帧**
    把彩色细笔画涂白（M4 的"笔迹不进帧差/墨迹"）。为什么按帧而不是按区域：笔迹在画面上是
    移动、累积的（p22 一页上越写越多），一个全局框会把其余帧同位置的正文也挖掉；而"这一帧
    哪里是笔迹"在帧上是能直接看出来的。判据参数来自 overlay.json，所以文件仍然是接口。
    """
    with Image.open(path) as im0:
        im = im0.convert("L")
        if strokes:
            sp = stroke_params(strokes)
            rgb = rgb_at(im0, stroke_width, im.size)
            m = stroke_mask(rgb, sp)
            if m.any():
                mi = Image.fromarray((m * 255).astype(np.uint8), mode="L").resize(im.size, Image.NEAREST)
                im = Image.composite(Image.new("L", im.size, 255), im, mi)
    if masks:
        from PIL import ImageDraw
        w0, h0 = im.size
        d = ImageDraw.Draw(im)
        for (l, t, r, b) in masks:
            x0, y0 = int(l * w0), int(t * h0)
            x1, y1 = max(int(round(r * w0)) - 1, x0), max(int(round(b * h0)) - 1, y0)
            d.rectangle([x0, y0, x1, y1], fill=255)
    w, h = im.size
    l, t, r, b = region
    if (l, t, r, b) != (0.0, 0.0, 1.0, 1.0):
        im = im.crop((int(l * w), int(t * h), max(int(r * w), 1), max(int(b * h), 1)))
    small = np.asarray(im.resize((SMALL_W, SMALL_H), Image.BILINEAR), dtype=np.float32) / 255.0
    hash_img = np.asarray(im.resize((9, 8), Image.BILINEAR), dtype=np.int16)
    bits = (hash_img[:, 1:] > hash_img[:, :-1]).flatten()
    dhash = 0
    for bit in bits:
        dhash = (dhash << 1) | int(bit)
    return small, dhash


def rgb_at(im0: Image.Image, width: int, full_size) -> np.ndarray:
    """把帧缩到 width 宽取 RGB 浮点数组（0..1），供笔画判据用（比全分辨率便宜得多）。"""
    w, h = full_size
    hh = max(2, int(round(width * h / float(w))))
    return np.asarray(im0.convert("RGB").resize((width, hh), Image.BILINEAR),
                      dtype=np.float32) / 255.0


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def ink_ratio(small: np.ndarray) -> float:
    """墨迹占比：暗像素比例，粗略代表"这一页画了多少东西" """
    return float((small < 0.62).mean())


def sharpness(small: np.ndarray) -> float:
    """梯度方差：模糊/运动过渡帧明显偏低"""
    gx = np.diff(small, axis=1)
    gy = np.diff(small, axis=0)
    return float(gx.var() + gy.var())


def frame_diff(a, b, alpha: float) -> float:
    ham = hamming(a[1], b[1]) / 64.0
    pix = float(np.abs(a[0] - b[0]).mean())
    return alpha * ham + (1.0 - alpha) * pix
