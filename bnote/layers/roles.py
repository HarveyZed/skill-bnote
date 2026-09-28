"""L4.7 帧角色层（M4）：判断**已抽出的帧**是什么角色。

为什么需要它（§3.5 冻结契约）
------------------------------
M3 之前"段内挑哪一帧"只看 OCR 字数 + 墨迹 + 清晰度，于是一段**出镜画面**（讲者照片 / 现场
镜头）或一页**放大截图**只要字多、清楚，就会被选成该页的终态图，讲义里就出现一张不是幻灯片
的"主图"。实测靶子：p20 356.9~417.5 s 整段都是演讲现场配图，旧口径把它当成一页。
M4 的角色层把每张候选帧判成五个角色之一，切片层据此实现"**整页优先**"（段内只要存在
full_page 候选，终态就必须选整页）—— 判据要**便宜、可解释、只用已有输入**。

输入只有三样（**不新增解码**）
-----------------------------
1. **已抽出的帧**（cache/<vid>/frames/，bnote slides 的产物）—— 只降采样读取，不解码媒体；
2. cache/<vid>/overlay.json（可选）—— 字幕条等遮挡区从统计里排除，避免"字幕条很黑"把
   整页判成暗场；
3. 本批帧自己算出的**笔画尺度中位数**（见下）—— zoom_detail 需要"比本集整页的字更大"
   这个相对量，不需要 measure.json。

六个角色与判据（判据名即 role_evidence.criterion，全部落进 signals）
------------------------------------------------------------------------
| 角色 | 判据名 | 成立条件（都要成立） |
|---|---|---|
| blank | uniform_frame | 灰度标准差 <= blank_std_max，或近全白，或均值极暗（黑场） |
| insert | letterbox | 黑边总宽度**明显大于本集常态**（>= max(letterbox_min, 本集中位数 + letterbox_delta)） |
| presenter | face_dominant | 肤色像素占比 >= presenter_skin_min，且有足量深像素（照片/现场镜头） |
| zoom_detail | stroke_scale | 笔画尺度 >= zoom_stroke_multiple × 本批中位数，且内容铺满整幅、仍是浅底深字 |
| app_screen | ui_dense_small_text | 整屏应用/录屏：前景占比小 + 行带多 + 前景笔画很短（UI 字小） |
| full_page | layout_grid | 底色成片（模式无关明暗）+ 有前景 + 有边 + 有版面 + 无大面积肤色 |
| 兜底 | layout_weak | 都不成立时给**低置信**（0.30）的 full_page |

**app_screen 的语义（M4b）**：IDE / 浏览器 / 终端 / 桌面录屏 —— **仍是整屏内容**，所以它是
PAGE_ROLES 之一（整页优先照旧可以选它当主图、check 也认它）；唯一的差别是**不走手写涂白**
（那里没有手写，UI 的彩色像素被当成彩色笔画涂白只会伤 OCR 与帧差）。判据与实测表见
segmenters/framesig.py 的 APP_DEFAULTS。

**occlusion 不是角色**，是给切片层用的标量：最大连通前景块占整幅的比例（见 _occlusion）。
切片层在"信息量接近"的同页候选之间挑**遮挡最少**的那张（M4b-3：人/桌面挡住页面时换更完整的一张）。

**为什么这样判**（每条都对着实测样本，不凭想象）：
* 出镜/现场照片（p20 356.9~417.5 s，6 张候选帧）实测 skin_frac 0.103~0.104，而同集幻灯片
  最高只有 0.036 —— 肤色是这条判据里唯一分得开的**正面证据**，所以 presenter 只认它。
  肤色规则另加饱和度上/下限：幻灯片里的橙/朱红填充块能骗过经典 RGB 规则，但饱和度接近 0.9。
* insert 的黑边判据必须**相对本集**：p21/p22 整集都带 4:3 上下黑边（每边 4%~5%），用绝对阈值
  会把所有幻灯片判成插播。一幅画面的 w/h 在同一集里是常数，只能记进 signals，不能当判据。
* zoom_detail 同理用**本批笔画中位数**（不是绝对像素数）；笔画量测的深色阈值还要按本帧背景
  下移——灰底/深底幻灯片的背景亮度本身就在 0.5~0.6，固定阈值会把整块背景算成笔画，实测
  有一帧的游程中位数因此爆到 31 px（本集中位数 2 px）。
* blank 先判，因为纯色帧会让后面所有相对量（边、墨迹、笔画）都退化成噪声。
* 兜底不判 insert 而判 full_page：本层只认正面证据，判不出时选**代价小**的那条（见下）。

判不出怎么办
------------
**宁可判不出（confidence 低）也不要硬判**：兜底分支的 confidence 固定 0.30，并在 signals
里写明是哪条弱证据让它拿到这个角色。角色值本身**必须**是五值之一（契约要求可枚举），
"不确定"只能体现在置信与判据名上。

层与层的关系（P1）
----------------
本模块是**纯函数库**：输入是帧文件与遮罩框，输出是判定结果，不写任何文件、不 import 别的层。
切片层（segmenters/stable.py）**不 import 它**——它通过 role_fn 参数注入（见
layers/segment.py 的接线），这样 L5 不反向依赖 L4。
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from ..segmenters.framesig import app_params, app_screen_metrics

SCHEMA = "bnote-roles/1"
# 六值枚举（§3.5-1 的五值 + M4b 的 app_screen）
ROLES = ("full_page", "app_screen", "zoom_detail", "presenter", "insert", "blank")
# "可以当主图"的角色：整页优先挑的就是它们。app_screen **仍是整屏内容**（IDE / 浏览器 /
# 终端 / 桌面录屏），语义上等价于一页，所以它和 full_page 一样有资格当主图；差别只有一条：
# app_screen 帧**不走手写涂白**（那里没有手写，见 framesig.APP_DEFAULTS 的说明）。
PAGE_ROLES = ("full_page", "app_screen")


# ---------------------------------------------------------------- 参数

def params(cfg: dict) -> dict:
    """生效参数（[roles] 段；默认值写在 config/default.toml，代码里只留同值兜底）。"""
    r = (cfg or {}).get("roles") or {}
    return {
        "enabled": bool(r.get("enabled", True)),
        "analyze_width": int(r.get("analyze_width", 160)),
        "stroke_width": int(r.get("stroke_width", 320)),
        "blank_std_max": float(r.get("blank_std_max", 0.02)),
        "blank_white_min": float(r.get("blank_white_min", 0.995)),
        "blank_fg_max": float(r.get("blank_fg_max", 0.004)),
        "letterbox_dark_max": float(r.get("letterbox_dark_max", 0.12)),
        "letterbox_line_frac": float(r.get("letterbox_line_frac", 0.97)),
        "letterbox_min": float(r.get("letterbox_min", 0.10)),
        "letterbox_delta": float(r.get("letterbox_delta", 0.30)),
        "letterbox_window_min": float(r.get("letterbox_window_min", 0.25)),
        "presenter_skin_min": float(r.get("presenter_skin_min", 0.05)),
        "presenter_mode_max": float(r.get("presenter_mode_max", 0.35)),
        # 8.0 是**保守到在本语料上不触发**的取值：现有 5 集 + BV1CC 里没有"真·页内放大截图"
        # 样本，而字号偏大的幻灯片（p21 000472/002325）实测是 6.0×本集中位数，取 8.0 正好把它们
        # 挡在门外，落回 full_page（低置信）。宁可判不出，也不要靠一个没有真样本的阈值去换主图。
        "zoom_stroke_multiple": float(r.get("zoom_stroke_multiple", 8.0)),
        "zoom_mode_min": float(r.get("zoom_mode_min", 0.40)),
        "zoom_content_min": float(r.get("zoom_content_min", 0.55)),
        "zoom_fg_min": float(r.get("zoom_fg_min", 0.05)),
        "zoom_border_min": float(r.get("zoom_border_min", 0.25)),
        "zoom_border_band": float(r.get("zoom_border_band", 0.04)),
        "fullpage_mode_min": float(r.get("fullpage_mode_min", 0.45)),
        "fullpage_fg_min": float(r.get("fullpage_fg_min", 0.01)),
        "fullpage_edge_min": float(r.get("fullpage_edge_min", 0.004)),
        "fg_gap": float(r.get("fg_gap", 0.20)),
        # light_frac 只作记录（审计用），不参与任何判据——判据一律与明暗主题无关
        "light_threshold": float(r.get("light_threshold", 0.70)),
        "stroke_gap": float(r.get("stroke_gap", 0.15)),
        "ink_dark_max": float(r.get("ink_dark_max", 0.62)),
        "edge_eps": float(r.get("edge_eps", 0.08)),
        "skin_sat_min": float(r.get("skin_sat_min", 0.15)),
        "skin_sat_max": float(r.get("skin_sat_max", 0.68)),
        # 整屏应用/录屏判据（app_screen）：唯一真源在 segmenters/framesig.py 的 APP_DEFAULTS，
        # 这里只把 [roles] 的覆盖项接进来（手写守卫读同一份参数）
        **app_params(r),
    }


# ---------------------------------------------------------------- 一张帧的特征

def _resize_gray(im_gray: Image.Image, width: int) -> np.ndarray:
    w, h = im_gray.size
    hh = max(2, int(round(width * h / float(w))))
    return np.asarray(im_gray.resize((width, hh), Image.BILINEAR), dtype=np.float32) / 255.0


def _resize_rgb(im: Image.Image, width: int) -> np.ndarray:
    w, h = im.size
    hh = max(2, int(round(width * h / float(w))))
    return np.asarray(im.resize((width, hh), Image.BILINEAR), dtype=np.float32) / 255.0


def _keep_grid(masks, w: int, h: int):
    """把相对坐标的遮罩框落成布尔网格（框中心落在格里即遮住那一格）；没有遮罩返回 None。"""
    if not masks:
        return None
    yy, xx = np.mgrid[0:h, 0:w]
    cy = (yy + 0.5) / h
    cx = (xx + 0.5) / w
    keep = np.ones((h, w), dtype=bool)
    for box in masks:
        l, t, r, b = (float(x) for x in box)
        keep &= ~((cx >= l) & (cx <= r) & (cy >= t) & (cy <= b))
    return keep if keep.any() else None


def _frac(mask: np.ndarray, keep) -> float:
    if keep is None:
        return float(mask.mean())
    if not keep.any():
        return 0.0
    return float((mask & keep).sum() / keep.sum())


def _skin_mask(rgb: np.ndarray, p: dict) -> np.ndarray:
    """廉价肤色判据（RGB 规则 + 饱和度上下限）。

    为什么加饱和度上下限：课程幻灯片里大量**橙色/朱红填充块**（p22 的流程图）能骗过经典
    RGB 肤色规则（r>g>b），但它们的饱和度接近 0.9，而人脸/手的肤色多在 0.2~0.5；
    上限 skin_sat_max 是这条判据不误伤彩色幻灯片的关键。
    """
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    m = ((r * 255 > 95) & (g * 255 > 40) & (b * 255 > 20)
         & ((mx - mn) * 255 > 15) & (np.abs(r - g) * 255 > 15) & (r > g) & (r > b))
    return m & (sat >= p["skin_sat_min"]) & (sat <= p["skin_sat_max"])


def _edge_mask(gray: np.ndarray, eps: float) -> np.ndarray:
    gx = np.zeros_like(gray, dtype=bool)
    gy = np.zeros_like(gray, dtype=bool)
    gx[:, :-1] = np.abs(np.diff(gray, axis=1)) > eps
    gy[:-1, :] = np.abs(np.diff(gray, axis=0)) > eps
    return gx | gy


def _occlusion(gray: np.ndarray, gap: float, cells_w: int = 32, cells_h: int = 18,
               cell_frac: float = 0.25) -> float:
    """遮挡程度：**最大连通前景块**占整幅的比例（M4b「遮挡最少」的判据数字）。

    为什么粗网格 + 连通域：人/手/桌面挡在页面上时是**一整块**连成片的前景，而正文是散落的
    小块。把 320 宽的前景掩膜降到 32x18 的格子（每格 >= cell_frac 前景算"占住"），再取最大
    4 连通块 —— 大块占比高就是被挡得多。
    只用于**同页候选之间**的相对比较（同一页的背景与版式相同，所以遮挡差异会直接体现出来），
    不跨页比、也不当角色判据。
    """
    fh, fw = gray.shape
    m = (np.abs(gray - float(np.median(gray))) > gap)
    h, w = fh // cells_h, fw // cells_w
    if h < 1 or w < 1:
        return 0.0
    grid = m[:h * cells_h, :w * cells_w].reshape(cells_h, h, cells_w, w).mean(axis=(1, 3))
    occ = grid >= cell_frac
    if not occ.any():
        return 0.0
    seen = np.zeros_like(occ, dtype=bool)
    best = 0
    for sy in range(cells_h):
        for sx in range(cells_w):
            if not occ[sy, sx] or seen[sy, sx]:
                continue
            stack = [(sy, sx)]
            seen[sy, sx] = True
            n = 0
            while stack:
                y, x = stack.pop()
                n += 1
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < cells_h and 0 <= xx < cells_w and occ[yy, xx] and not seen[yy, xx]:
                        seen[yy, xx] = True
                        stack.append((yy, xx))
            best = max(best, n)
    return round(best / float(cells_h * cells_w), 4)


def _stroke_px(gray_hi: np.ndarray, dark_max: float, max_run: int = 32) -> float:
    """笔画尺度：深色像素**水平游程长度**的中位数。

    为什么用游程中位数：字号/笔画宽度没有便宜的"直接读数"，但"深色像素连续多长"会随字号
    单调变化——整页小字的中位数在 1~2 像素，页内放大截图会到 4~8 像素。只在固定分析宽度上
    量，且只用 <= max_run 的游程（长游程是版面线条/涂色块，不是笔画）。
    """
    dark = gray_hi < dark_max
    lens = []
    row_step = max(1, dark.shape[0] // 90)
    for y in range(0, dark.shape[0], row_step):
        row = dark[y].astype(np.int8)
        if not row.any():
            continue
        d = np.diff(np.concatenate(([0], row, [0])))
        starts = np.flatnonzero(d == 1)
        ends = np.flatnonzero(d == -1)
        if starts.size:
            lens.append(ends - starts)
    if not lens:
        return 0.0
    L = np.concatenate(lens)
    L = L[L <= max_run]
    if L.size == 0:
        return 0.0
    return float(np.median(L))


def _bars(gray: np.ndarray, dark_max: float, line_frac: float) -> tuple:
    """上下左右成片纯黑边的宽度占比（letterbox）+ 内容窗口的均值亮度。

    只统计**首尾连续段**，中间的黑条不算边。窗口（整幅减去黑边的那些行列）的亮度是用来区分
    "插屏黑边"与"深色主题自身的黑"的：真正的插屏黑边之外是另一幅画面（多为亮底），而黑底课件
    （p46/BV1CC）整幅都黑，窗口也黑。实测 p46 的"insert"帧窗口均值远低于 0.25。
    """
    dark = gray < dark_max
    rows = dark.mean(axis=1)
    cols = dark.mean(axis=0)

    def head(v):
        n = 0
        for x in v:
            if x >= line_frac:
                n += 1
            else:
                break
        return n
    fh, fw = gray.shape[0], gray.shape[1]
    nt, nb = head(rows), head(rows[::-1])
    nl, nr = head(cols), head(cols[::-1])
    win = np.ones((fh, fw), dtype=bool)
    if nt:
        win[:nt, :] = False
    if nb:
        win[fh - nb:, :] = False
    if nl:
        win[:, :nl] = False
    if nr:
        win[:, fw - nr:] = False
    wmean = float(gray[win].mean()) if win.any() else 0.0
    return (nt / float(fh), nb / float(fh), nl / float(fw), nr / float(fw), wmean)


def features(cfg: dict, path, masks=None, p: dict | None = None) -> dict:
    """算一张帧的全部判据标量（**一次读图**，三个分辨率各取所需，不重复解码）。"""
    p = p or params(cfg)
    with Image.open(path) as im0:
        im = im0.convert("RGB")
        im_gray = im.convert("L")
        rgb = _resize_rgb(im, p["analyze_width"])
        gray = _resize_gray(im_gray, p["analyze_width"])
        gray_hi = _resize_gray(im_gray, p["stroke_width"])
        rgb_hi = _resize_rgb(im, p["stroke_width"])
        size = [int(im.width), int(im.height)]
    fh, fw = gray.shape
    keep = _keep_grid(masks, fw, fh)
    med = float(np.median(gray))
    # 笔画量测的深色阈值按**本帧背景**下移：灰底/深底幻灯片（p21/p22 大量存在）背景亮度本身
    # 就在 0.5~0.6，用固定 0.62 会把整块背景算成"笔画"，游程中位数直接爆到 31 px。
    stroke_thr = min(p["ink_dark_max"], med - p["stroke_gap"])
    hist, _ = np.histogram(gray, bins=20, range=(0.0, 1.0))
    mode_frac = float(hist.max()) / float(gray.size)
    ink = gray < p["ink_dark_max"]
    edge = _edge_mask(gray, p["edge_eps"])
    skin = _skin_mask(rgb, p)
    white = gray > 0.90
    light = gray > p["light_threshold"]        # 浅底占比（只作记录：判据一律用与主题无关的量）
    # 底色 = 直方图峰所在档的中心；前景 = 与底色反差够大的像素。
    # **主题无关**是这里的硬要求：BV1CCtz6WEvF_p1 是深色主题（底色 0.03、亮字），
    # 用"暗于 0.62 = 墨迹"会把整幅算成内容、把深色页判成黑场（实测 mean=0.061）。
    bg = float((int(np.argmax(hist)) + 0.5) * 0.05)
    content = np.abs(gray - bg) > p["fg_gap"]
    # 黑边（letterbox）先从"内容"里去掉：它是视频取景带来的整行/整列纯黑，不是页面版式。
    # 留着会把 content_frac 拉到 1.00、bands 虚高（p21/p22 整集都带 4:3 上下黑边），
    # 于是"内容是否铺满整幅"这条 zoom 判据永远成立。
    dark_frame = gray < p["letterbox_dark_max"]
    bars_mask = ((dark_frame.mean(axis=1) >= p["letterbox_line_frac"])[:, None]
                 | (dark_frame.mean(axis=0) >= p["letterbox_line_frac"])[None, :])
    content = content & ~bars_mask
    sat_mx = rgb.max(axis=2)
    sat_mn = rgb.min(axis=2)
    saturated = ((sat_mx > 1e-6) & ((sat_mx - sat_mn) / np.maximum(sat_mx, 1e-6) > 0.35)
                 & (sat_mx > 0.35))
    con_keep = content if keep is None else (content & keep)
    ys, xs = np.where(con_keep)
    if ys.size:
        content_frac = ((ys.max() - ys.min() + 1) * (xs.max() - xs.min() + 1)) / float(fh * fw)
    else:
        content_frac = 0.0
    prof = con_keep.any(axis=1)
    bands = int((np.count_nonzero(prof[1:] != prof[:-1]) + (1 if prof[:1].any() else 0)) // 2)
    # 内容是否顶到画面边缘：页内放大截图是把底页的一块放大到满屏，内容会压到边框；
    # 正常幻灯片有页边距。这是 zoom_detail 与"字号本来就大的幻灯片"之间唯一的正面区分。
    band = max(1, int(round(fh * p["zoom_border_band"])))
    bcols = max(1, int(round(fw * p["zoom_border_band"])))
    border_cells = np.zeros_like(con_keep)
    border_cells[:band, :] = True
    border_cells[-band:, :] = True
    border_cells[:, :bcols] = True
    border_cells[:, -bcols:] = True
    border_fg_frac = _frac(con_keep & border_cells, keep) / max(1e-6, _frac(border_cells, keep))
    bt, bb, bl, br, bar_window_mean = _bars(gray, p["letterbox_dark_max"], p["letterbox_line_frac"])
    app = app_screen_metrics(gray_hi, rgb_hi, {k: float(v) for k, v in p.items() if k.startswith("app_")})
    return {
        "std": float(gray.std()),
        "mean": float(gray.mean()),
        "white_frac": _frac(white, keep),
        "light_frac": _frac(light, keep),
        "bg": round(bg, 4),
        "fg_frac": _frac(content, keep),
        "dark_frac": _frac(ink, keep),
        "edge_frac": _frac(edge, keep),
        "skin_frac": _frac(skin, keep),
        "sat_frac": _frac(saturated, keep),
        "median": round(med, 4),
        "mode_frac": round(mode_frac, 4),
        # 整屏应用/录屏的四个标量（320 宽尺度；判据与阈值见 framesig.APP_DEFAULTS）
        "app": app,
        "fg_run_px": app["run_px"],
        # M4b「遮挡最少」的判据数字：最大连通前景块占比（只作同页候选之间的相对比较）
        "occlusion": _occlusion(gray_hi, p["fg_gap"]),
        "stroke_thr": round(stroke_thr, 4),
        "stroke_px": _stroke_px(gray_hi, stroke_thr),
        "content_frac": round(content_frac, 4),
        "border_fg_frac": round(border_fg_frac, 4),
        "bands": bands,
        "bar_top": round(bt, 4), "bar_bottom": round(bb, 4),
        "bar_left": round(bl, 4), "bar_right": round(br, 4),
        "bar_window_mean": round(bar_window_mean, 4),
        "size": size,
    }


# ---------------------------------------------------------------- 判据

def _signals(feat: dict, criterion: str, conf: float, **extra) -> dict:
    """统一的 evidence：判据名 + 置信 + 关键数字（契约 §3.5-1 要求三者都在）。"""
    keys = ("white_frac", "light_frac", "fg_frac", "bg", "dark_frac", "edge_frac", "skin_frac",
            "sat_frac", "std", "mean", "median", "mode_frac", "stroke_px", "stroke_thr",
            "content_frac", "border_fg_frac", "bands", "fg_run_px", "bar_window_mean",
            "bar_top", "bar_bottom", "bar_left", "bar_right")
    sig = {k: round(float(feat[k]), 4) for k in keys}
    sig.update(extra)
    return {"criterion": criterion, "confidence": round(float(conf), 2), "signals": sig}


def classify(feat: dict, p: dict, stroke_med: float | None = None,
             bar_base: float = 0.0) -> tuple:
    """按固定顺序判角色；返回 (role, role_evidence)。

    顺序是有意的：blank 先判（纯色帧会让其它相对量都退化），insert 再判（letterbox 是硬几何
    特征），presenter / zoom_detail 都是"比整页更极端"的形态，最后才落到 full_page 与低置信
    兜底——**不让兜底抢在强判据前面**。

    两条判据是**相对**的（都相对本集）：笔画尺度相对本集候选帧中位数（stroke_med），
    黑边宽度相对本集黑边中位数（bar_base）。理由：同一集的取景与字号是常数，绝对值判不了
    "这一帧与别的不一样"；p21/p22 整集都带 4:3 上下黑边（每边约 4%~5%），用绝对阈值会把它们
    全判成插播。

    兜底给 full_page（低置信）：本判据只认**正面证据**（纯色 / 黑边 / 肤色 / 笔画尺度），
    认不出就别硬说它是插播——把幻灯片判成插播会连带触发 check 的"整段无整页候选"警告，
    而把插播判成整页的代价只是"它继续留在原位"。这就是所谓判错方向要选便宜的那条。
    """
    # 1) blank：近纯色（前景像素几乎没有）/ 近全白 / 近乎无结构
    if (feat["fg_frac"] <= p["blank_fg_max"] or feat["white_frac"] >= p["blank_white_min"]
            or feat["std"] <= p["blank_std_max"]):
        conf = 0.75 if feat["std"] <= p["blank_std_max"] else 0.6
        return "blank", _signals(feat, "uniform_frame", conf,
                                 fg_max=p["blank_fg_max"])
    # 2) insert：黑边显著**多于本集常态**
    bars = feat["bar_top"] + feat["bar_bottom"] + feat["bar_left"] + feat["bar_right"]
    need = max(p["letterbox_min"], float(bar_base) + p["letterbox_delta"])
    # 窗口还要够亮：否则那是深色主题自身的黑底，不是插屏黑边（实测 p46 的黑底课件页
    # bars_total 能到 1.5，但窗口均值 < 0.25）
    if bars >= need and feat["bar_window_mean"] >= p["letterbox_window_min"]:
        conf = min(0.9, 0.5 + 0.5 * (bars - need) + 0.5 * bars)
        return "insert", _signals(feat, "letterbox", conf, bars_total=round(bars, 4),
                                  bars_need=round(need, 4), bars_base=round(float(bar_base), 4),
                                  window_min=p["letterbox_window_min"])
    # 3) presenter：肤色聚集 **且整幅没有成片的单一底色**（照片/现场镜头）
    #    mode_frac 是这条判据的关键：真人照片的亮度分布没有"一片占 35% 以上的底色"
    #    （实测讲者照片 0.28），而幻灯片无论白底/灰底/深底都有一大片底色（实测 0.38~0.87）。
    #    只看肤色会把幻灯片里的粉/橙填充块与红笔笔迹误判成出镜（实测 p21/p22/BV1CC 共 6 例）。
    if (feat["skin_frac"] >= p["presenter_skin_min"]
            and feat["mode_frac"] <= p["presenter_mode_max"]):
        conf = min(0.85, 0.45 + 2.0 * feat["skin_frac"])
        return "presenter", _signals(feat, "face_dominant", conf,
                                     skin_min=p["presenter_skin_min"],
                                     mode_max=p["presenter_mode_max"])
    # 4) zoom_detail：笔画尺度远大于本批中位数，且内容铺满整幅（真的被放大到满屏）
    if (stroke_med and stroke_med > 0
            and feat["stroke_px"] >= p["zoom_stroke_multiple"] * stroke_med
            and feat["mode_frac"] >= p["zoom_mode_min"]
            and feat["fg_frac"] >= p["zoom_fg_min"]
            and feat["content_frac"] >= p["zoom_content_min"]
            and feat["border_fg_frac"] >= p["zoom_border_min"]):
        ratio = feat["stroke_px"] / stroke_med
        conf = min(0.85, 0.4 + 0.25 * (ratio - p["zoom_stroke_multiple"] + 1.0))
        return "zoom_detail", _signals(feat, "stroke_scale", conf,
                                       stroke_median=round(float(stroke_med), 3),
                                       ratio=round(ratio, 3),
                                       multiple=p["zoom_stroke_multiple"])
    # 4.5) app_screen：整屏应用/录屏（IDE / 浏览器 / 终端 / 桌面）。四个标量一起判，
    #      阈值与实测依据见 framesig.APP_DEFAULTS；命中意味着这一帧**不走手写涂白**。
    if feat["app"]["hit"]:
        a = feat["app"]
        conf = min(0.85, 0.45 + 0.2 * (feat["bands"] - p["app_bands_min"])
                   + 5.0 * a["border_fg"])
        return "app_screen", _signals(feat, "ui_dense_small_text", conf,
                                      app=dict(a, hit=None),
                                      thresholds={"fg_max": p["app_fg_max"], "bands_min": p["app_bands_min"],
                                                  "border_min": p["app_border_min"],
                                                  "run_max": p["app_run_max"]})
    # 5) full_page：**底色成片**（mode_frac，与明暗无关）+ 有前景 + 有边 + 有版面 + 无大面积肤色
    if (feat["mode_frac"] >= p["fullpage_mode_min"] and feat["fg_frac"] >= p["fullpage_fg_min"]
            and feat["edge_frac"] >= p["fullpage_edge_min"] and feat["bands"] >= 1
            and feat["skin_frac"] < p["presenter_skin_min"]):
        conf = min(0.85, 0.40 + 0.4 * feat["mode_frac"] + 5.0 * feat["edge_frac"])
        return "full_page", _signals(feat, "layout_grid", conf)
    # 6) 兜底：判不出就写低置信，不硬判
    return "full_page", _signals(feat, "layout_weak", 0.30)


def classify_set(cfg: dict, paths, files, masks=None) -> dict:
    """一次判完一批帧：{文件名: {"role","evidence","features"}}（同一文件只读一次）。

    切片层在"候选都选完之后"调用一次（传全集的候选文件名），这样笔画中位数是**本集口径**，
    而不是某一页的口径。
    """
    p = params(cfg)
    uniq = []
    for name in files:
        n = str(name or "")
        if n and n not in uniq:
            uniq.append(n)
    if not p["enabled"] or not uniq:
        return {name: None for name in uniq}
    feats = {}
    for name in uniq:
        try:
            feats[name] = features(cfg, paths.frames / name, masks=masks, p=p)
        except Exception as exc:
            print("[roles] 跳过读不出来的帧 %s（%s）" % (name, exc))
            feats[name] = None
    vals = [f["stroke_px"] for f in feats.values() if f and f["stroke_px"] > 0]
    med = float(np.median(vals)) if vals else 0.0
    bars = [f["bar_top"] + f["bar_bottom"] + f["bar_left"] + f["bar_right"]
            for f in feats.values() if f]
    bar_base = float(np.median(bars)) if bars else 0.0
    out = {}
    for name in uniq:
        f = feats.get(name)
        if not f:
            out[name] = None
            continue
        role, ev = classify(f, p, med, bar_base)
        out[name] = {"role": role, "evidence": ev, "features": f}
    if out:
        print(summary_line(out))
    return out


def stroke_median_of(results: dict) -> float:
    """从 classify_set 的结果里取本集笔画中位数（只用于打印摘要）。"""
    vals = [v["features"]["stroke_px"] for v in results.values()
            if v and v["features"]["stroke_px"] > 0]
    return float(np.median(vals)) if vals else 0.0


def summary_line(results: dict) -> str:
    """一行摘要：各角色计数 + 笔画中位数（跑 slides 时打印，便于一眼看出判成了什么）。"""
    from collections import Counter
    c = Counter(v["role"] for v in results.values() if v)
    return ("[roles] 判定 %d 张候选帧：%s ｜ 笔画中位数 %.2f px"
            % (sum(c.values()),
               "、".join("%s=%d" % (k, c[k]) for k in ROLES if c[k]) or "无",
               stroke_median_of(results)))

