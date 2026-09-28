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
    "handwriting_light_min": 0.80,    # 笔迹邻近背景的亮度下限：笔写在**浅底**（幻灯片白底/浅灰底）上。
                                      #   M4b 实测从 0.55/0.65 收到 0.80：桌面录屏（壁纸+图标）
                                      #   0.0049→0.00009、浏览器/控制台页与 IDE 主题的彩色元素
                                      #   基本清零，而幻灯片上的红笔只掉 30%~80%（001421
                                      #   0.00275→0.00141、000607 0.0174→0.0137），仍远高于
                                      #   handwriting_min_frac；OCR 对照结论不变（0.67 保住、
                                      #   一大/粗筛仍清）。代价：灰底很深的课件上笔迹覆盖变少
                                      #   （p21 000466 0.0056→0.00022）。
    "handwriting_block_erode": 3,     # 判"实心色块"的腐蚀次数：>=3 次还活着 = 厚度 >=7 px
                                      #   （320 宽分析尺度）→ 红底白字条/填充框；笔画 1 次就没了
    "handwriting_block_pad": 3,       # 色块核之外再多保护几圈（见 _block_core 的实测说明）
    "handwriting_ink_max": 0.62,      # 深色印刷字阈值（沿用 segment 的墨迹口径）：最后一圈膨胀
                                      #   不许长进这些像素，免得把被笔划过的数字啃掉
    "handwriting_bg_window": 15,      # 判"邻近背景亮不亮"的滑窗边长
}


# 整屏应用/录屏（IDE / 浏览器 / 终端 / 桌面）判据的**唯一真源**：默认值在这里，
# [roles] 段可以覆盖，判定数字逐帧写进 role_evidence。阈值在 **320 宽**的分析尺度上标定。
# 实测（P51/P52/P53 的 VS Code 画面 vs 稀疏底课件）—— 四个标量要一起用：
#   ① fg_frac：IDE 大半个屏是 UI 背景，前景只占 1.3%~2.2%；但稀疏课件也能低到 1.8%~2.9%，
#      所以它只能当"上限"，单独用会误判（p49/p50 的深底课件）。
#   ② bands：行带数（IDE 一行行的代码/文件树/终端很多，实测 8~14；课件 5~8）。
#   ③ border_fg：画面四周 4% 边带里的前景占比 —— IDE 有侧栏/标签栏/状态栏、浏览器整屏没有
#      页边距，实测 0.02~0.04；幻灯片的页边距让边带干净（0.00~0.01）。
#   ④ run_px：前景水平游程中位数（UI 字更小，实测 1~2 px；幻灯片正文 >=3 px）。
# 命中样例：P51 002264、P52 000430、P53 000212/001250、p20 001427/001472（浏览器里的页面）。
# 实测（320 宽尺度，16 张标注帧：8 张 IDE/浏览器 + 8 张课件）：
#   | 组 | fg_frac | bands | run_px |
#   | APP  | 0.018~0.028 | 13~23 | 1~2 |
#   | 课件 | 0.017~0.279 |  4~9  | 1~4 |
# 三条一起用（fg + bands + run）全部 16 张判对；border_fg 不够可靠（浏览器页的页边距也很干净：
# 实测 0.007~0.017，而带红笔的课件反而有 0.39~0.53），所以它只作**记录值**，不参与判定。
APP_DEFAULTS = {
    "app_fg_max": 0.032,       # ① 前景像素占比上限（UI 大半个屏是背景）
    "app_bands_min": 12,       # ② 行带数下限（IDE/终端的代码、文件树、终端行很多）
    "app_border_min": 0.0,     # ③ 边带前景占比下限：默认关闭（0），只记录数字（见上表）
    "app_run_max": 2.0,        # ④ 前景水平游程中位数上限（px，320 宽尺度；UI 字更小）
    "app_gap": 0.20,           # 与底色差多少算前景
    "app_border_band": 0.04,   # 边带宽度（占画面比例；只用于 ③ 的记录值）
}


def app_params(raw: dict | None) -> dict:
    """从 [roles] 配置（或任意字典）里取"整屏应用"判据参数；缺项用默认值。"""
    src = raw or {}
    return {k: float(src.get(k, v)) for k, v in APP_DEFAULTS.items()}


def gray_at(im_gray, width: int) -> np.ndarray:
    """把灰度图缩到 width 宽取浮点数组（0..1）。"""
    w, h = im_gray.size
    hh = max(2, int(round(width * h / float(w))))
    return np.asarray(im_gray.resize((width, hh), Image.BILINEAR), dtype=np.float32) / 255.0


def app_screen_metrics(gray: np.ndarray, rgb: np.ndarray, ap: dict) -> dict:
    """整屏应用/录屏的四个标量 + 命中与否（gray/rgb 都必须是 **320 宽**尺度）。"""
    hist, _ = np.histogram(gray, bins=20, range=(0.0, 1.0))
    mode_frac = float(hist.max()) / float(gray.size)
    bg = float((int(np.argmax(hist)) + 0.5) * 0.05)
    fg = np.abs(gray - bg) > float(ap["app_gap"])
    fh, fw = gray.shape
    fg_frac = float(fg.mean())
    prof = fg.any(axis=1)
    bands = int((np.count_nonzero(prof[1:] != prof[:-1]) + (1 if prof[:1].any() else 0)) // 2)
    band = max(1, int(round(fh * float(ap["app_border_band"]))))
    bcols = max(1, int(round(fw * float(ap["app_border_band"]))))
    cells = np.zeros_like(fg)
    cells[:band, :] = True
    cells[-band:, :] = True
    cells[:, :bcols] = True
    cells[:, -bcols:] = True
    border = float((fg & cells).sum() / max(1, int(cells.sum())))
    runs = []
    for y in range(0, fh, 2):
        row = fg[y].astype(np.int8)
        if not row.any():
            continue
        d = np.diff(np.concatenate(([0], row, [0])))
        s = np.flatnonzero(d == 1)
        e = np.flatnonzero(d == -1)
        runs.append(e - s)
    L = np.concatenate(runs) if runs else np.array([0])
    L = L[L <= 40]
    run_px = float(np.median(L)) if L.size else 0.0
    hit = bool(fg_frac <= float(ap["app_fg_max"]) and bands >= int(ap["app_bands_min"])
               and border >= float(ap["app_border_min"])
               and 0.0 < run_px <= float(ap["app_run_max"]))
    # ③ 的默认下限是 0（即不参与判定）：border 命中与否只体现在 border >= 0 上，数字照记
    return {"fg_frac": round(fg_frac, 4), "bands": bands, "border_fg": round(border, 4),
            "run_px": round(run_px, 2), "mode_frac": round(mode_frac, 4), "bg": round(bg, 4),
            "hit": hit}


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


def _block_core(m: np.ndarray, k: int, pad: int = 0) -> np.ndarray:
    """实心色块的核 + 把它膨胀回整块（**按笔画收紧**的关键一步）。

    做法：对饱和掩膜做 k 次 4 邻域腐蚀，还活着的像素说明它所在的连通域**两个方向都至少有
    2k+1 像素厚**；把核再膨胀 k+1 圈，就把整块（含外围）圈住。

    为什么不是"滑窗密度 >= 0.95"（上一版的做法，已废弃）：红底白字条里的**白字把颜色挖空**，
    字周围的红色像素滑窗密度不够、于是没被算成块、被当成"笔画"涂白 → 白字变白底白字，OCR
    直接丢字（实测 p22 的"平滑指数：10.00"、表格里被红笔圈住的 0.67→0.07 就是这么来的）。
    按厚度判就没有这个问题：白字只是把小块挖了几个洞，整块照样够厚。

    实测（320 宽分析尺度）：红底白字条厚 5 px（腐蚀 3 次还剩 31 px），"Rerank" 填充块同样活到
    腐蚀 3 次；而红笔笔画腐蚀 1 次就没了（p22 两个样本的腐蚀残点全部落在色块内）。所以
    k = handwriting_block_erode = 3 分得开"笔画"与"实心色块"。

    pad（handwriting_block_pad）是核之外**多保护几圈**：白字周围的色块像素本来就该保住，核按
    腐蚀次数膨胀（k+1）还不够盖住色块两端那几列 —— 实测 pad=1 时"平滑指数：10.00"的尾字仍被
    啃掉（读成"10."），pad=3 才完整读回；而 pad=3 时红笔伪文字（一大/粗筛）照样清得掉。
    """
    if k <= 0:
        return np.zeros_like(m)
    core = m
    for _ in range(k):
        core = (core & _shift(core, 1, 0) & _shift(core, -1, 0)
                & _shift(core, 0, 1) & _shift(core, 0, -1))
        if not core.any():
            return core
    return _dilate(core, k + int(pad))


def stroke_mask(rgb: np.ndarray, sp: dict) -> np.ndarray:
    """彩色**细**笔画掩膜（手写笔迹）：饱和色 + 落在笔色窗口 + **不是实心色块** +
    邻近背景够亮 + 只按笔画涂白。

    为什么这几条（每条都对着实测）：
    * 饱和色 + 笔色窗口：p22 整集的红笔是 h 约 0~15 的纯红；幻灯片自身的蓝标题、青色块若一起
      挖掉会伤正文，所以蓝窗默认关（handwriting_blue_min/max = 0，实测据见 overlay.py 文档）。
    * **按厚度排除实心色块**（_block_core）：红底白字条、填充的流程框都不是笔画；它们的字是
      "被挖空的"，按滑窗密度判会把字周围的色块当成笔画涂掉，屏幕上的字就没了。
    * 邻近背景要亮：笔是写在浅底上的；这条把暗色主题（BV1CCtz6WEvF_p1）里的彩色元素排除在外。
    * 最后一圈膨胀**不许长进深色印刷字**：笔划过数字时这一圈会把数字的笔画啃掉（p22 的 0.67
      被读成 0.07）。宁可留一点红边，也不吃掉屏幕上本来就有的字。
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
    block = _block_core(m, int(sp["handwriting_block_erode"]), int(sp["handwriting_block_pad"]))
    gray = rgb.mean(axis=2)
    bg = _box_mean(gray, int(sp["handwriting_bg_window"]))
    thin = m & ~block & (bg >= sp["handwriting_light_min"])
    if not thin.any():
        return thin
    # 向外扩一圈补笔画自己的抗锯齿边，但不许长进深色印刷字（见 docstring 最后一条）
    dark = _dilate(gray < float(sp["handwriting_ink_max"]), 1)
    return (_dilate(thin, 1) & ~dark) | thin


def signature(path: Path, region=(0.0, 0.0, 1.0, 1.0), masks=None, strokes=None,
              stroke_width: int = 320, app=None):
    """返回 (small_gray_float32[27,48], dhash_uint64)。

    传入 masks（overlay.json 的 regions 框）时**先把它们涂成同一片白**再算签名。涂成同一个
    常数有两个好处：帧差里这些像素恒为 0（不再污染稳定性判定），dHash 在这些格子上恒等
    （不再污染汉明距离）。也就是说遮罩同时作用于**帧差的两项**，而不只是像素差那一项。

    传入 strokes（overlay.json 的 params，含 handwriting_* 键）时，再按 stroke_mask **逐帧**
    把彩色细笔画涂白（M4 的"笔迹不进帧差/墨迹"）。为什么按帧而不是按区域：笔迹在画面上是
    移动、累积的（p22 一页上越写越多），一个全局框会把其余帧同位置的正文也挖掉；而"这一帧
    哪里是笔迹"在帧上是能直接看出来的。判据参数来自 overlay.json，所以文件仍然是接口。

    传入 app（[roles] 段的 app_* 参数）时，先判"这一帧是不是整屏应用/录屏"（app_screen）：
    是就**整帧跳过涂白** —— UI 的彩色元素不是手写笔迹，挖掉只会伤 OCR 与帧差（M4b）。
    """
    with Image.open(path) as im0:
        im_gray = im0.convert("L")
        im = im_gray
        if strokes:
            sp = stroke_params(strokes)
            rgb = rgb_at(im0, stroke_width, im.size)
            # **整屏应用守卫**（M4b）：IDE/浏览器/终端录屏里没有手写，把它们的彩色 UI 当成
            # 彩色笔画涂白只会有害（P51/P52 实测 chosen 漂移 10/27 与 3/14 段）。判据与阈值
            # 见 APP_DEFAULTS：命中就整帧跳过涂白（不调阈值去兼容两种画面）。
            skip = False
            if app:
                skip = app_screen_metrics(gray_at(im_gray, stroke_width), rgb,
                                          app_params(app))["hit"]
            if not skip:
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
