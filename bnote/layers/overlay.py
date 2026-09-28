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

2. ``transient_overlay``：**临时出现**的遮挡（闪现的标注工具条 / 菜单 / 进度条 / 鼠标），
   判据按**瞬态性**判、不按种类（见 framesig.TRANSIENT_DEFAULTS 的完整说明与全部实测数字）：

   * **一筛**（搭在下面 scan 已有的逐帧解码上，增量≈0）：每帧每角算三个数 —— 角窗对
     **时域中位**的平均绝对残差 ``resid``、同一中位的**整帧**残差 ``frame_resid``（换页帧的
     守卫）与角窗的梯度密度 ``edge``（纹理守卫）；三个门槛都过才是候选帧。
   * **二筛**（只对候选帧回 ~480 宽复核）：角窗里"显著色块色调档数" ``color_patches`` >=
     ``transient_color_min``。这一条是**区分工具条与幻灯片自己内容**的那把刀：实测工具条
     7~10 档（笔色 + 一排色板），幻灯片上的文字框 / 逐条动画元素只有 0~2 档。
   * **瞬态门槛**：命中帧占整集的比例 <= ``transient_max_ratio``（默认 0.2）才产出区域。
     **两条"常驻"日志必须分开读**（2026-09-29 校准 §4.3-1）：``reason_kind="ratio_over"`` 是
     **有检出但出现太频繁**（半常驻，切掉会伤内容）；``reason_kind="resident_detect"`` 是
     **本角没有通过瞬态判据的帧**，只报数——"跨页不变量 + 纹理"占比高**既可能是常驻水印/固定版式，
     也可能什么都没有**，不进瞬态门槛、不动像素。

   **旧判据 ``corner_static_glyphs`` 整族已删**（连同它的 8 个 ``widget_*`` 参数）：它要求
   "帧间几乎不变 + 色调档数 >= 6"，而 p20/p21/p22/P48 全帧四角的色调档数**上限是 3** ——
   门槛不可达，判据在真实样本上永远产不出区域（实测 p20 1547 / p21 3244 / p22 2802 /
   P48 1385 帧四角命中全 0）。修正后的判据在同样这 4 集上给出 p20=3、p21=8、p22=11 帧
   （与逐帧目视过的真值逐个相同），P48–P53 干净集只有 p51 的 7 帧真例（桌面右键菜单）。

M4 新增判据 3：color_stroke（手写笔迹）
----------------------------------------
彩色细笔画（红/橙笔，hue 窗口见 params 的 handwriting_hue_*；蓝窗默认关）识别为
kind=handwriting 的区域。它**只用于把笔迹从 OCR 与帧差/墨迹里排除，交付图照旧保留**，
且**消费方按帧用、不按框挖**（理由见 handwriting_regions 的 docstring）：所以 masks() 会
跳过 handwriting，消费方要用 strokes() 拿判据参数逐帧重算。
判据实现只有一份，在 segmenters/framesig.py 的 stroke_mask（叶子工具，两个消费方共用）。

判据 4（M6）：pip_desync_motion（画中画讲师小窗）
------------------------------------------------
画中画讲师小窗（P33 的右上角真人、P25 的右下角卡通讲师）是**第三类固定区域**：它既不是
烧录字幕（判据 1），也不是闪现的角状外物（判据 2）—— 它是**全程常驻**的独立画面。

三条判据（**全部同时成立**才产出一条 kind=pip_window 的区域；每条的数字都进 evidence）：
  ① **位置固定 / 面积稳定 / 长时间常驻**：小窗在整集里是同一个矩形。实现上把整幅按
     ``pip_grid_cols`` 列等分成**跨帧固定的网格**，逐帧对在低分辨率剖面上量「每格的平均绝对差」，
     得到每格的**活跃率** ``active_ratio``（有多少比例的帧对里这一格动了 > ``pip_cell_eps``）。
     常驻 = 活跃率高（默认 ≥ ``pip_active_min`` = 0.5，即大部分时长都在动）。
  ② **内容持续小幅变化（不是每帧全换）**：格的**典型变化幅度** ``amp_median`` 要落在
     [``pip_amp_min``, ``pip_amp_max``] 区间里 —— 下界滤掉 JPEG 噪声级别的不动，
     上界滤掉「整块换内容」（幻灯片换页、文档滚动）；同时把 **90 分位** ``amp_p90`` 也落盘，
     复核者一眼能看出这条区域是"稳定小幅"还是"偶尔炸一下"。
  ③ **与全帧变化事件不同步（关键的一条）**：把每帧对的**全帧平均绝对差**排成一条序列，
     逐格算它与它的**皮尔逊相关** ``sync_corr``；小窗的帧间游走与"整屏滚动/换页"发生的时间
     无关，所以 |corr| 小（默认 ≤ ``pip_sync_max`` = 0.6）；而**滚动文档自己的格子**在滚动帧对上
     必然同步变化，corr ≈ 1（P33 取样包实测：小窗 4 格 |corr| ≤ 0.01，文档格 0.6~1.0）——
     这一条就是把"独立运动的人脸窗"与"整屏滚动的文档"分开的那把刀。

**面积上限**（``pip_max_area``，默认 0.25）：防止把「幻灯片里嵌入的视频」当成讲师小窗整块挖掉。
超过上限就当判据不成立（写理由，不裁剪成一条含糊的区域）。

**与已采信区域不重叠**：字幕带拟合出来的那几行（以及任何已采信区域的方框）里的格子**不参与**
本判据 —— 烧录字幕同样是"位置固定 + 一直小幅变化"，但它已经被判据 1 认走了（P25 取样包的
实测：不排掉会把字幕条当成 pip_window）。

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
from ..segmenters.framesig import (STROKE_DEFAULTS, TRANSIENT_DEFAULTS,
                                   TRANSIENT_SMALL, TRANSIENT_TEX,
                                   app_params, app_screen_metrics, color_patch_bins, corner_rect,
                                   corner_zones, edge_density, stroke_mask, stroke_params,
                                   transient_overlay_corners, transient_params)

from ..tools import check_basis_dest

SCHEMA = "bnote-overlay/1"
ALGO = "bnote-overlay/1"
# source.basis：帧从哪来。**整片模式的值不变**（旧产物可直接比对）；取样模式写取样包
# 索引的 relpath（§3.6-3）。--basis 的解析在 tools.resolve_basis（full → None = 沿用原路径）。
BASIS = "cache/frames/index.json"

# kind 枚举（取值**只能**来自这里）。handwriting 是 M4 新增：彩色细笔画 = 手写笔迹，
# **只用于把它从 OCR 与帧差/墨迹里排除，交付图里照旧保留**。
# pip_window 是 M6 新增：画中画讲师小窗（位置固定 + 长时间常驻 + 内容持续小幅变化 +
# **与全帧变化事件不同步**）。它与另两类一样**整块挖**（不是按帧应用）。
KINDS = ("caption_strip", "transient_overlay", "handwriting", "pip_window")

PROFILE_W = 160                 # 低分辨率统计宽度（高度按源帧长宽比换算）
LEGACY_W, LEGACY_H = 48, 27     # 旧判据（stable._detect_caption_strip）的签名分辨率，逐字沿用
STATIC_EPS = 0.04               # 逐像素「变了」的判定：平均绝对差超过它才算变

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
        # —— 临时遮挡（判据实现在 segmenters/framesig.transient_overlay_corners）——
        # 键名与默认值的**唯一真源**在 framesig.TRANSIENT_DEFAULTS；这里只是把 [overlay] 段的
        # 覆盖值搬进产物 params（消费方从 overlay.json 读，文件即接口）。
        **transient_params(ov),
        # —— M6 新增：画中画讲师小窗（判据实现在本模块 pip_window_regions）——
        # 每一条的语义与实测依据写在模块头「判据 4」；默认值的取舍写在 config/default.toml。
        "pip_enabled": bool(ov.get("pip_enabled", True)),
        "pip_grid_cols": int(ov.get("pip_grid_cols", 20)),
        "pip_cell_eps": float(ov.get("pip_cell_eps", 0.004)),
        "pip_active_min": float(ov.get("pip_active_min", 0.5)),
        "pip_amp_min": float(ov.get("pip_amp_min", 0.0015)),
        "pip_amp_max": float(ov.get("pip_amp_max", 0.12)),
        "pip_sync_max": float(ov.get("pip_sync_max", 0.6)),
        "pip_max_area": float(ov.get("pip_max_area", 0.25)),
        "pip_min_cells": int(ov.get("pip_min_cells", 2)),
        "pip_pairs_max": int(ov.get("pip_pairs_max", 40000)),
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


# ---------------------------------------------------------------- 画中画小窗的统计网格
def cell_grid(fh: int, fw: int, cols: int):
    """画中画判据的统计网格：整幅等分成 cols × rows 个格子（**跨帧固定**）。

    为什么用格子而不是滑动窗：判据要落一条**全局矩形**区域（契约不落时间戳），格子能把
    "哪一块在动"直接变成"哪些格子同时满足三条判据"，再做 4 连通分组取最大块。
    cols 夹到不超过 fw —— 超过就会出现宽度为 0 的空片（那一格没有像素）。

    返回 (label, ys, xs, rows, cols, counts)：label 是 (fh, fw) 的格子编号（bincount 用）、
    ys/xs 是行/列边界（相对坐标换算要用）、counts 是每格的像素数（用来排除空片）。
    """
    cols = max(2, min(int(cols), int(fw)))
    rows = max(2, int(round(cols * fh / float(fw))))
    ys = np.linspace(0, fh, rows + 1).astype(np.int32)
    xs = np.linspace(0, fw, cols + 1).astype(np.int32)
    lab = np.zeros((fh, fw), dtype=np.int32)
    for r in range(rows):
        for c in range(cols):
            lab[ys[r]:ys[r + 1], xs[c]:xs[c + 1]] = r * cols + c
    counts = np.bincount(lab.ravel(), minlength=rows * cols).astype(np.float32)
    return lab, ys, xs, rows, cols, counts


# ---------------------------------------------------------------- 一遍扫帧
def scan(frames_dir: Path, files: list, p: dict, app: dict | None = None) -> dict:
    """流式扫一遍已抽出的帧，攒出统计量（不落逐帧中间产物，内存只留剖面与各角最好的一帧）。

    返回 dict：
      R     逐帧对的**逐行**平均绝对差，形状 (pairs, FH)
      mean  逐像素时间均值（灰度，FH×FW）
      chg   逐像素「变过的帧对比例」（FH×FW）
      small 逐帧 **48x27 灰度剖面**（(n,27,48) float32）—— 临时遮挡判据的"时域中位"就是拿它算的；
            tex 是逐帧 **96x54 灰度剖面**（纹理守卫用）。两者都**搭在已经在解码的那一帧**上算，
            不多解一次码（48x27 这一份就是 legacy 剖面，原本就已经在算）
      legacy 旧判据的两条变化率 {rb, rm, n, ...}（48×27 口径，逐字搬过来）
      pipD  逐帧对的**每格**平均绝对差（形状 帧对 × 格子数），pipG 是逐帧对的**全帧**平均绝对差；
            pip_grid 是 (rows, cols, ys, xs, fh, fw, counts)。判据 4 只用这三条序列 ——
            判据 1/2/3 一行都不读它们，所以**不改**既有判据的结果。

    **成本**：临时遮挡的**指纹**（48x27 + 96x54 两个剖面）搭在本函数已有的逐帧解码上，增量≈0；
    贵的那一步（480 宽的色块色调档数）**只对一筛候选帧跑**（每集通常 0~40 帧），所以整集的
    overlay 与只看剖面同量级。旧判据 corner_static_glyphs（已删）是**逐帧近全分辨率**量四角，
    实测 p20 从 12 s 涨到约 45 s、BV1CCtz6WEvF_p1 从 45 s 涨到约 2.5 min —— 那笔钱换不到
    任何区域（它的门槛在这些集上不可达），所以整族退役。
    """
    prev_fine = None
    prev_legacy = None
    rows = []
    sum_g = chg = None
    n = 0
    size = None
    # 临时遮挡：逐帧剖面（48x27 的 legacy + 96x54 的纹理尺度）搭在本帧已解码的灰度图上，增量≈0。
    # 它们给的是 framesig.transient_overlay_corners 的一筛输入；二筛（480 宽的色块档数）在对
    # 候选帧的第二次小扫描里做（见 transient_overlay_regions）。
    tr_on = bool(p["transient_enabled"])
    small_list, tex_list = [], []
    # M4 手写笔迹：逐**采样**帧算一次彩色细笔画掩膜（判据在 framesig.stroke_mask）。
    # stride 是为了控成本：笔迹是"这一段有没有"的量，不需要每帧都量；而它的两个消费方
    # （stable 的签名、ocr 的送识别图）都是**按帧现算**的，不依赖这里扫了几帧。
    hw = [] if p["handwriting_enabled"] else None
    hsum = None
    hfrac_max = 0.0
    hframes = 0
    # M6 画中画小窗：只收两条序列（每格平均绝对差 + 全帧平均绝对差），判据全部由它们算出来；
    # pip_pairs_max 是内存上界（格子数 × 帧对 × 4 B）：超了就**不再收**并让判据报 insufficient，
    # 不静默用半截序列判。
    pd = [] if p["pip_enabled"] else None
    pg = [] if p["pip_enabled"] else None
    pip_grid = None
    pip_lab = pip_cnt = None
    pip_pairs = 0
    pip_dropped = 0
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
                # 临时遮挡的纹理尺度剖面：96x54 是**固定的**（不按长宽比缩高）—— 标定数字
                # （见 framesig.TRANSIENT_DEFAULTS）都是在这个尺度上量的，换尺度就换标定。
                tex = (np.asarray(gray.resize((TRANSIENT_TEX[0], TRANSIENT_TEX[1]), Image.BILINEAR),
                                  dtype=np.float32) / 255.0) if tr_on else None
        except Exception as exc:
            print("[overlay] 跳过读不出来的帧 %s（%s）" % (name, exc))
            continue
        if size is None:
            size = (w, h)
        if sum_g is None:
            sum_g = np.zeros_like(fine)
            chg = np.zeros_like(fine)
            if pd is not None:
                pip_lab, _pys, _pxs, _prows, _pcols, pip_cnt = cell_grid(fh, PROFILE_W,
                                                                       int(p["pip_grid_cols"]))
                pip_grid = (_prows, _pcols, _pys, _pxs, fh, PROFILE_W, pip_cnt)
        if fine.shape != sum_g.shape:
            print("[overlay] 帧尺寸不一致（%s）→ 停止统计" % name)
            break
        sum_g += fine
        if tr_on:
            small_list.append(legacy)
            tex_list.append(tex)
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
            if pd is not None:
                if pip_pairs < int(p["pip_pairs_max"]):
                    # 每格平均绝对差：一次 bincount 把 (fh×fw) 的差分图按格子号求和（比逐格切片快）
                    s = np.bincount(pip_lab.ravel(), weights=d.ravel().astype(np.float64),
                                    minlength=pip_cnt.size).astype(np.float32)
                    pd.append(s / np.maximum(pip_cnt, 1.0))
                    pg.append(float(d.mean()))
                    pip_pairs += 1
                else:
                    pip_dropped += 1
        if prev_legacy is not None:
            a, b = prev_legacy, legacy
            if float(np.abs(a[lb:LEGACY_H] - b[lb:LEGACY_H]).mean()) > thr:
                l_hit_b += 1
            if float(np.abs(a[0:lt] - b[0:lt]).mean()) > thr:
                l_hit_m += 1
            l_n += 1
        prev_fine, prev_legacy = fine, legacy
        n += 1

    empty_pip = {"pipD": None, "pipG": None, "pip_grid": None, "pip_dropped": pip_dropped}
    if sum_g is None or not rows:
        out = {"R": None, "mean": None, "chg": None, "small": None, "tex": None,
               "pairs": 0, "size": size, "legacy": None,
               "stroke_sum": None, "stroke_frames": 0, "stroke_frac_max": 0.0,
               "stroke_hits": [], "app_skipped": app_skipped}
        out.update(empty_pip)
        return out
    return {
        "pipD": np.array(pd, dtype=np.float32) if pd else None,
        "pipG": np.array(pg, dtype=np.float32) if pg else None,
        "pip_grid": pip_grid, "pip_dropped": pip_dropped,
        "stroke_sum": hsum, "stroke_frames": hframes, "stroke_frac_max": hfrac_max,
        "stroke_hits": hw, "app_skipped": app_skipped,
        "R": np.array(rows, dtype=np.float32),
        "mean": sum_g / n,
        "chg": chg / max(1, len(rows)),
        "small": np.stack(small_list) if small_list else None,
        "tex": np.stack(tex_list) if tex_list else None,
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


def caption_region(R: np.ndarray, p: dict, legacy: dict, fit: dict | None = None):
    """字幕带判据的汇总：候选带（拟合带 + 旧固定带），任一条过就产出一条 ``caption_strip``。

    ``fit`` 只允许由调用方传``fit_caption_band(R, p)`` 的结果（判据 4 也要用同一条拟合带做
    排除，重算一遍纯属浪费；**语义不变**，不传就照旧自己算）。
    """
    mult = float(p["caption_band_multiple"])
    lo_gate = float(p["caption_rate_min"])
    if fit is None:
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


# ---------------------------------------------------------------- 判据 4：画中画小窗
def _series_corr(A: np.ndarray, g: np.ndarray) -> np.ndarray:
    """A（帧对 × 若干列）的每一列与 g（帧对）的皮尔逊相关。

    判据 ③ 的实现：g 是**全帧平均绝对差**（"整屏这一对帧变了多少"），一列是某一格/某个框的
    平均绝对差。滚动文档自己的格子与 g 同步（corr ≈ 1 —— 它本来就是 g 的主要成分），
    独立运动的人脸窗与 g 无关（corr ≈ 0）。**这条就是把两者分开的那把刀。**
    """
    if A.ndim == 1:
        A = A.reshape(-1, 1)
    n = int(A.shape[0])
    if n < 3:
        return np.zeros(A.shape[1], dtype=np.float32)
    gs = g - float(g.mean())
    ng = float(np.sqrt(float(gs @ gs)))
    if ng <= 1e-12:                      # 全帧一直不动：没有"变化事件"可比，判不下来
        return np.zeros(A.shape[1], dtype=np.float32)
    Ac = A - A.mean(axis=0, keepdims=True)
    na = np.sqrt((Ac * Ac).sum(axis=0))
    den = na * ng
    return np.where(den > 1e-12, (Ac * gs[:, None]).sum(axis=0) / np.maximum(den, 1e-12), 0.0)


def _cell_components(mask: np.ndarray, rows: int, cols: int) -> list:
    """格子的 4 连通分组（画中画在网格上是一个矩形块，允许中间被排除掉几格）。"""
    seen = np.zeros(mask.size, dtype=bool)
    groups = []
    for start in np.flatnonzero(mask):
        start = int(start)
        if seen[start]:
            continue
        seen[start] = True
        stack = [start]
        cur = []
        while stack:
            i = stack.pop()
            cur.append(i)
            r, c = divmod(i, cols)
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                rr, cc = r + dr, c + dc
                if 0 <= rr < rows and 0 <= cc < cols:
                    j = rr * cols + cc
                    if mask[j] and not seen[j]:
                        seen[j] = True
                        stack.append(j)
        groups.append(cur)
    return groups


def _pip_blocked(rows: int, cols: int, ys, xs, fh: int, fw: int,
                 regions: list, fit: dict | None) -> np.ndarray:
    """判据 4 **不参与**的格子：任何已采信区域的方框内 + 字幕带拟合出来的那几行。

    为什么必须排除（不是洁癖，是 P25 取样包实测踩出来的）：烧录字幕同样满足「位置固定 +
    长时间常驻 + 内容持续小幅变化 + 与全帧变化不同步」—— 不排掉就会先产出一条 caption_strip、
    再产出一条盖在它上面的 pip_window。字幕带那几行的行号由判据 1 的**拟合带**给出
    （不管它最后有没有过区域门槛，那是"这一带就是字幕"的最好现成证据）。
    """
    blocked = np.zeros(rows * cols, dtype=bool)
    boxes = [r["box"] for r in (regions or [])
             if r.get("applicability") == OK and isinstance(r.get("box"), list)
             and len(r["box"]) == 4]
    for r in range(rows):
        cy = (float(ys[r]) + float(ys[r + 1])) / 2.0 / float(fh)
        for c in range(cols):
            cx = (float(xs[c]) + float(xs[c + 1])) / 2.0 / float(fw)
            if any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes):
                blocked[r * cols + c] = True
    if fit:
        y0, y1 = int(fit["y0"]), int(fit["y1"])
        for r in range(rows):
            cy = (float(ys[r]) + float(ys[r + 1])) / 2.0
            if y0 <= cy <= y1:
                blocked[r * cols:(r + 1) * cols] = True
    return blocked


def pip_window_regions(scan_out: dict, p: dict, fit: dict | None, regions: list) -> tuple:
    """画中画讲师小窗（判据 4）：位置固定 + 长时间常驻 + 内容持续小幅变化 + **与全帧变化事件不同步**。

    三条判据的数字全进 evidence（活跃率 / 幅度中位与 90 分位 / 与全帧变化的相关 / 面积占比 /
    全帧变化事件数），复核者不用翻代码就能重算这条区域。判不出**不落区域**，返回一句带数字的
    理由由 analyze 写进文档级 applicability（§3.4-3：不许静默返回空）。

    坐标与置信：box 与另三类同空间（抽帧后画面的相对坐标）；置信由"活跃率离下限多远 +
    |相关| 离上限多远"两个余量线性给出，范围 0.5~0.95。
    """
    apply_note = "整块挖（与字幕条一致）：帧差 / 墨迹 / 送 OCR 的图都不再读它"
    if not p["pip_enabled"]:
        return [], ("画中画小窗判据按配置关闭（[overlay].pip_enabled=false）；%s" % apply_note)
    D = scan_out.get("pipD")
    G = scan_out.get("pipG")
    grid = scan_out.get("pip_grid")
    dropped = int(scan_out.get("pip_dropped") or 0)
    if dropped and D is None:
        return [], ("画中画小窗判据不成立（帧对数超过 pip_pairs_max=%d，有 %d 对没统计；%s）"
                    % (int(p["pip_pairs_max"]), dropped, apply_note))
    pairs = 0 if D is None else int(D.shape[0])
    if D is None or G is None or grid is None or pairs < 8:
        return [], "画中画小窗判据不成立（可用帧对只有 %d，少于 8）" % pairs

    rows, cols, ys, xs, fh, fw, cnt = grid
    amin, amax = float(p["pip_amp_min"]), float(p["pip_amp_max"])
    act_min, smax = float(p["pip_active_min"]), float(p["pip_sync_max"])
    eps = float(p["pip_cell_eps"])
    blocked = _pip_blocked(rows, cols, ys, xs, fh, fw, regions, fit)
    valid = (cnt > 0) & (~blocked)
    act = (D > eps).mean(axis=0)
    amp = np.median(D, axis=0)
    amp90 = np.percentile(D, 90, axis=0)
    cc = _series_corr(D, G)
    mask = (valid & (act >= act_min) & (amp >= amin) & (amp <= amax)
            & (np.abs(cc) <= smax))
    groups = [g for g in _cell_components(mask, rows, cols) if len(g) >= int(p["pip_min_cells"])]
    if not groups:
        return [], ("画中画小窗判据不成立（%d 个格子里只有 %d 格长时间在动，没有同时满足"
                    "「活跃率 ≥ %.2f + 幅度中位在 [%.4f, %.4f] + |与全帧变化的相关| ≤ %.2f」的连通块；%s）"
                    % (int(valid.sum()), int((valid & (act >= act_min)).sum()), act_min,
                       amin, amax, smax, apply_note))

    gi = max(groups, key=len)
    rr = [i // cols for i in gi]
    ccs = [i % cols for i in gi]
    r0, r1, c0, c1 = min(rr), max(rr), min(ccs), max(ccs)
    box = [round(float(xs[c0]) / float(fw), 4), round(float(ys[r0]) / float(fh), 4),
           round(float(xs[c1 + 1]) / float(fw), 4), round(float(ys[r1 + 1]) / float(fh), 4)]
    area = (box[2] - box[0]) * (box[3] - box[1])
    if area > float(p["pip_max_area"]):
        return [], ("画中画小窗判据不成立（候选区域 %s 面积 %.4f 超过上限 %.2f —— 面积这么大的"
                    "「独立运动块」更像幻灯片里嵌入的视频或整屏动画，整块挖会伤正文；%s）"
                    % (box, area, float(p["pip_max_area"]), apply_note))

    sel = np.array(gi, dtype=np.int64)
    sub = D[:, sel].mean(axis=1)
    act_b = float((sub > eps).mean())
    amp_b = float(np.median(sub))
    amp90_b = float(np.percentile(sub, 90))
    cc_b = float(_series_corr(sub, G)[0])
    ghi = G >= np.percentile(G, 75)
    share = 0.0
    if float(G[ghi].mean()) > 1e-9:
        share = float(sub[ghi].mean() / float(G[ghi].mean())) * area
    if act_b < act_min or not (amin <= amp_b <= amax) or abs(cc_b) > smax:
        return [], ("画中画小窗判据不成立（候选框 %s 复核不过：活跃率 %.3f（下限 %.2f）/ "
                    "幅度中位 %.5f（区间 [%.4f, %.4f]）/ |与全帧变化的相关| %.3f（上限 %.2f）；%s）"
                    % (box, act_b, act_min, amp_b, amin, amax, abs(cc_b), smax, apply_note))

    m_act = min(1.0, max(0.0, (act_b - act_min) / max(1e-6, 1.0 - act_min)))
    m_sync = min(1.0, max(0.0, (smax - abs(cc_b)) / max(1e-6, smax)))
    conf = float(min(0.95, max(0.5, 0.5 + 0.25 * m_act + 0.25 * m_sync)))
    ev = {"criterion": "pip_desync_motion",
          "cells": len(gi), "cells_valid": int(valid.sum()), "grid": [rows, cols],
          "active_ratio": round(act_b, 4), "active_min": act_min,
          "amp_median": round(amp_b, 5), "amp_p90": round(amp90_b, 5),
          "amp_min": amin, "amp_max": amax,
          "sync_corr": round(cc_b, 4), "sync_max": smax,
          "cell_eps": eps, "area_ratio": round(area, 4),
          "change_share": round(share, 4),
          "global_change_median": round(float(np.median(G)), 5),
          "global_events": int(ghi.sum()), "pairs": pairs,
          "blocked_cells": int(blocked.sum()),
          "note": "位置固定 + 长时间常驻 + 内容持续小幅变化 + 与全帧变化事件不同步（三条同时成立）；"
                  "change_share = 高变化帧对上这条区域占全帧变化量的份额（越小越「独立」）"}
    return [{"kind": "pip_window", "box": box, "confidence": round(conf, 2),
             "evidence": ev, "applicability": OK}], None


# ---------------------------------------------------------------- 判据 2：临时遮挡
def _rgb_corner_loader(paths, files: list, p: dict, cache: dict):
    """返回 (frame_idx, corner) -> 该角在 ~480 宽尺度上的 RGB（float32 0..1）。

    **只对一筛候选帧调用**（每集通常 0~40 帧）：同一帧的四个角共用一次解码 + 一次 resize，
    结果按帧号缓存。二筛为什么要回到 ~480 宽：工具条那排色板在 320 宽就被插值糊成一团，
    同一个工具条只数得出 3 档（480 宽 7~10 档），而假例在任何尺度都只有 0~2 档。
    """
    def load(i: int, corner: str) -> np.ndarray:
        arr = cache.get(i)
        if arr is None:
            w = max(64, int(p["transient_color_width"]))
            with Image.open(paths.frames / files[i]) as im0:
                im = im0.convert("RGB")
                h = max(2, int(round(w * im.size[1] / float(im.size[0]))))
                im = im.resize((w, h), Image.BILINEAR)
            arr = np.asarray(im, dtype=np.float32) / 255.0
            cache[i] = arr
        # RGB 是 (H, W, 3)：角窗几何走 corner_rect（不能拿末两维当 H/W）
        y0, y1, x0, x1 = corner_rect(arr.shape[0], arr.shape[1], corner,
                                     float(p["transient_zone"]))
        return arr[y0:y1, x0:x1]
    return load


def transient_overlay_regions(paths, files: list, scan_out: dict, p: dict) -> tuple:
    """临时遮挡：一筛指纹 + 二筛色块复核 + 瞬态门槛 → kind="transient_overlay" 的区域 + 日志。

    判据本体（含全部实测数字）在 segmenters/framesig.transient_overlay_corners；本函数只做三件事：
      1) 把 scan 存下来的逐帧剖面交给叶子判据；
      2) 给叶子判据提供一个"按帧取角窗 RGB"的加载器（**只有候选帧会被真的解码**）；
      3) 把 verdict == "transient" 的角落成区域、把 verdict == "resident" 的角**打印出来**。

    **区域语义（与另几类不同，消费方必须照做）**：这是**按帧**生效的区域 —— 只有 evidence.frames
    里列出的那几帧在那个角上有遮挡物；其余帧同一角是**正常画面**，整块挖掉就是伤正文。
    所以 masks() 会跳过它，消费方（composite）只对"终态帧正好在 frames 里"的页做镶嵌。
    """
    if not p["transient_enabled"]:
        return [], "临时遮挡判据按配置关闭（[overlay].transient_enabled=false）"
    small, tex = scan_out.get("small"), scan_out.get("tex")
    if small is None or tex is None or not files:
        msg = "临时遮挡判据不成立（没有逐帧剖面）"
        print("[overlay] " + msg)
        return [], msg
    # 剖面是 (n, H, W)：TRANSIENT_SMALL/TEX 按惯例写成 (宽, 高)，比的时候要反过来
    want_small = (TRANSIENT_SMALL[1], TRANSIENT_SMALL[0])
    want_tex = (TRANSIENT_TEX[1], TRANSIENT_TEX[0])
    if tuple(small.shape[1:]) != want_small or tuple(tex.shape[1:]) != want_tex:
        msg = ("临时遮挡判据不成立（剖面尺度 %s / %s 与标定尺度 %s / %s 不一致）"
               % (tuple(small.shape[1:]), tuple(tex.shape[1:]), want_small, want_tex))
        print("[overlay] " + msg)
        return [], msg
    cache = {}
    loader = _rgb_corner_loader(paths, files, p, cache)
    t0 = time.monotonic()
    recs = transient_overlay_corners(small, tex, p, loader)
    print("[overlay] 临时遮挡判据耗时 %.1fs（一筛指纹搭在已解码的帧上、增量≈0；"
          "二筛只解码了 %d 帧做 480 宽色块复核）" % (time.monotonic() - t0, len(cache)))
    regions, notes = [], []
    for r in recs:
        if r["verdict"] == "resident":
            line = "[overlay] 角 %s：%s" % (r["corner"], r["reason"])
            print(line)
            notes.append(line[len("[overlay] "):])
            continue
        if r["verdict"] != "transient":
            continue
        frames = [files[i] for i in r["hits"]]
        ev = {"criterion": "transient_overlay",
              "corner": r["corner"],
              "frames_hit": len(frames), "frame_count": len(files),
              "hit_ratio": r["hit_ratio"], "max_ratio": float(p["transient_max_ratio"]),
              "min_frames": int(p["transient_min_frames"]),
              "resid_hits": r["resid_hits"], "edge_hits": r["edge_hits"],
              "color_hits": r["color_hits"],
              "resid_min": float(p["transient_resid_min"]),
              "frame_resid_max": float(p["transient_frame_resid_max"]),
              "frame_share_min": float(p["transient_frame_share_min"]),
              "edge_min": float(p["transient_edge_min"]),
              "color_min": int(p["transient_color_min"]),
              "color_width": int(p["transient_color_width"]),
              "zone": float(p["transient_zone"]),
              "window": int(p["transient_window"]),
              "candidate_frames": len(r["candidates"]),
              "resident_ratio": r["resident_ratio"], "resident_votes_max": r["votes_max"],
              # 命中的**具体帧**：按帧镶嵌只认这个清单（不是时间戳，跑两次逐字节一致）
              "frames": frames,
              "note": ("角窗对时域中位的残差 >= resid_min 且整帧残差守卫通过、角窗纹理 >= edge_min、"
                       "480 宽尺度色块色调档数 >= color_min；命中帧占比 <= max_ratio（否则按常驻处理）。"
                       "**按帧生效**：只有 frames 里那几帧的该角有遮挡物，其余帧是正常画面，"
                       "所以整块挖会伤正文；消费方（composite）按帧用。")}
        conf = float(min(0.95, 0.5 + 0.4 * (len(frames) / max(1, len(r["candidates"])))
                         + 0.1 * min(1.0, max(r["color_hits"] or [0]) / 12.0)))
        regions.append({"kind": "transient_overlay", "box": r["box"],
                        "confidence": round(conf, 2), "evidence": ev, "applicability": OK})
        print("[overlay] 临时遮挡：角 %s 命中 %d/%d 帧（%.2f%%）→ 框 %s，交付期按帧从同页干净帧补回"
              % (r["corner"], len(frames), len(files), 100 * r["hit_ratio"],
                 ",".join("%.3f" % x for x in r["box"])))
    return regions, "；".join(notes)


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
        *TRANSIENT_DEFAULTS.keys(),
        "pip_enabled", "pip_grid_cols", "pip_cell_eps", "pip_active_min", "pip_amp_min",
        "pip_amp_max", "pip_sync_max", "pip_max_area", "pip_min_cells", "pip_pairs_max",
        "handwriting_enabled", "handwriting_stride", "handwriting_min_frac",
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
    pip_regs = []
    applies = []
    size = None
    if len(files) < frames_min:
        applies.append("帧数 %d < 门槛 %d，统计量不足" % (len(files), frames_min))
    else:
        out = scan(root, files, p, app_params((cfg.get("roles") or {})))
        size = out["size"]
        # 临时遮挡先跑：它只依赖逐帧剖面（不依赖帧对），所以帧对数不足时也照跑
        tr_regs, tr_note = transient_overlay_regions(paths, files, out, p)
        regions.extend(tr_regs)
        if tr_note:
            applies.append(tr_note)
        if out["R"] is None or out["pairs"] < 8:
            applies.append("可用帧对只有 %d，变化率判据不成立" % out["pairs"])
        else:
            # 字幕带的**拟合带**只算一遍：判据 1 用它产区域，判据 4 用它排除字幕那几行
            fit = fit_caption_band(out["R"], p)
            cap = caption_region(out["R"], p, out["legacy"], fit=fit)
            if cap:
                regions.append(cap)
            else:
                applies.append("底部带变化率未过阈值（字幕条判据不成立）")
            if p["pip_enabled"]:
                pip_regs, pip_reason = pip_window_regions(out, p, fit, regions)
                regions.extend(pip_regs)
                if pip_reason:
                    applies.append(pip_reason)
            if p["handwriting_enabled"]:
                hw_regs, hw_reason = handwriting_regions(files, out, p)
                regions.extend(hw_regs)
                if hw_reason:
                    applies.append(hw_reason)

    # handwriting 与 transient_overlay **都不进遮罩面积**：
    #   * handwriting 的 box 只是审计范围（消费方按 params 里的判据逐帧重算笔画掩膜）；
    #   * transient_overlay 的 box 只对 evidence.frames 里那几帧成立（整块挖会伤其余帧的正文）。
    # 把它们算进 masked_area_ratio 会给出"遮掉了 90% 画面"这种误导性数字。
    hw = [r for r in regions if r.get("kind") == "handwriting" and r.get("applicability") == OK]
    tr = [r for r in regions if r.get("kind") == "transient_overlay"
          and r.get("applicability") == OK]
    per_frame_kinds = ("handwriting", "transient_overlay")
    boxed = [r for r in regions if r.get("kind") not in per_frame_kinds]
    masked = bool(boxed)
    note = ("已按遮罩算：stable 挖掉这些区域后再算帧差/墨迹/清晰度/送 OCR，"
            "measure 用 drawbox 在同一遍解码里挖掉同样的像素" if masked
            else "未产出可整块挖的遮罩（%s）；消费方退化为全画面，与 M3 之前一致"
                 % "；".join(applies))
    if hw:
        note += ("；另有 %d 条 handwriting 区域（手写笔迹）：**不整块挖、不计入遮罩面积**，"
                 "消费方按 params 的 handwriting_* 判据逐帧重算笔画掩膜" % len(hw))
    if tr:
        n_frame = sum(int(r["evidence"].get("frames_hit") or 0) for r in tr)
        note += ("；另有 %d 条 transient_overlay 区域（临时出现的遮挡：标注工具条 / 菜单 / 提示条）："
                 "**不整块挖、不计入遮罩面积**，命中帧共 %d 帧；消费方（composite）只对"
                 "「终态帧正好在 evidence.frames 里」的交付页做**同页无遮挡镶嵌**"
                 "（框内取同页干净帧、框外取终态帧）" % (len(tr), n_frame))
    if pip_regs:
        note += ("；另有 %d 条 pip_window 区域（画中画讲师小窗）：**整块挖**（与字幕条一致），"
                 "判据与数字见该区域的 evidence（位置固定 + 长时间常驻 + 内容持续小幅变化 + "
                 "与全帧变化事件不同步）" % len(pip_regs))
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
                  "transient_overlay_region_count": len(tr),
                  "masked_area_ratio": round(min(1.0, area), 4)},
    }


def _rel(paths, path) -> str:
    try:
        return str(Path(path).relative_to(paths.root))
    except ValueError:
        return path.name


def masks(doc: dict) -> list:
    """从 overlay 文档里取出**可采信**的区域框（消费方也可自行读文件，文件即接口）。

    **不含 handwriting 与 transient_overlay**：这两类的 box 都只对"某些帧"成立，整块挖会把
    其余帧同位置的正文一起挖掉 —— 手写笔迹读 strokes() 拿判据参数逐帧重算笔画掩膜；临时遮挡
    读 transient() 拿命中帧清单，只对那几帧做镶嵌。
    """
    return [list(r["box"]) for r in (doc.get("regions") or [])
            if r.get("applicability") == OK
            and r.get("kind") not in ("handwriting", "transient_overlay")
            and isinstance(r.get("box"), list) and len(r["box"]) == 4]


def transient(doc: dict) -> list:
    """overlay 文档里的**临时遮挡**区域（按帧生效）：返回 [{box, frames, evidence}, ...]。

    消费方（layers/composite.py）只用它判断"这一页的终态帧是不是被临时遮挡了"：
    是 -> 拿同页干净帧把框内补回来；不是 -> 原样交付。**不整块挖**（见 masks 的说明）。
    没有区域时返回空列表（调用方退化为原样交付）。
    """
    out = []
    for r in (doc.get("regions") or []):
        if r.get("kind") != "transient_overlay" or r.get("applicability") != OK:
            continue
        box = r.get("box")
        if not (isinstance(box, list) and len(box) == 4):
            continue
        ev = r.get("evidence") or {}
        out.append({"box": [float(x) for x in box],
                    "frames": [str(x) for x in (ev.get("frames") or [])],
                    "evidence": ev})
    return out


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
