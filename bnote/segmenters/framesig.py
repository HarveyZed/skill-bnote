"""帧签名与廉价打分：所有 segmenter 共用的底层工具。

只用 Pillow + numpy（不依赖 opencv），保证在最小依赖下可运行。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

SMALL_W, SMALL_H = 48, 27


def signature(path: Path, region=(0.0, 0.0, 1.0, 1.0)):
    """返回 (small_gray_float32[27,48], dhash_uint64)"""
    im = Image.open(path).convert("L")
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
