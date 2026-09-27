"""L4.6 读字面板（M2）：把若干帧拼成一张**只烧序号**的索引图。

产物（**接口冻结**，键序即结构：schema / vid / algo / params / sheets / tiles / applicability）：

  out/<vid>/_meta/sheets/<name>.png   面板图（**只放这里，绝不进 slides/** —— 页号空间要干净）
  out/<vid>/_meta/sheet.json          权威映射：tiles[{index,sheet,row,col,t,frame,sha256}]

三条硬约束（§3.3）：
  * 每格**只烧序号**（1..N），**不烧时间码**；`行列 → 帧 → t` 只在 sheet.json 里；
  * **面板里的字一律不采信**：面板是缩放拼图，要读字必须 `bnote frames --read` 取全分辨率单帧
    （「图像文字的可信度由分辨率决定」）；
  * 产物**不放时间戳**：同一份输入两次跑逐字节可比。

采样与拼版：语料是 cache/frames/index.json（`--from/--to` 限时间区间、`--max` 给张数上限，
不指定就按全区间均匀取样）；每张面板吃 cols×rows 个候选，**空白格按标准差跳过**（不编号、
不补位，记进该面板的 skipped_blank）。预设 `500x140 / 620x170 / 760x210` 是**单格包围盒**，
实际格宽按源帧长宽比反推（1280x720 对 620x170 得 302x170）。拼版只用已有 Pillow + numpy：
不引 ImageMagick、不装新包；序号用内置 5x7 点阵烧，避免依赖系统字体（环境不是契约）。

`[sheet].enabled / .inline` 只约束「自动生成 / 写手引用」（M2 里没有这类自动路径）：
显式敲 `bnote sheet` 就是「要它跑」，不受 enabled 拦。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw
import numpy as np

from . import frames as frames_layer
from . import slideset as slideset_layer

SCHEMA = "bnote-sheet/1"
ALGO = "bnote-sheet/1"
SHEETS_DIRNAME = "sheets"
SHEET_JSON_NAME = "sheet.json"
APPLICABILITY_NOTE = "面板是缩放拼图：里面的字只当索引，读字请用 frames --read"
APPLICABILITY_MASKED = False

# 三档预设：**单格包围盒**（宽×高的上限），格宽按源帧长宽比反推
PRESETS = (("500x140", (500, 140)), ("620x170", (620, 170)), ("760x210", (760, 210)))
PRESET_NAMES = tuple(name for name, _ in PRESETS)
_INDEX_FACTOR = 0.15          # 序号数字高度占格高的比例（决定点阵放大倍数）

# 5x7 点阵数字：只烧序号用，避免依赖系统字体
_DIGITS = {
    "0": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}


def fit_cell(size, box) -> tuple[int, int]:
    """按源帧长宽比把预设包围盒反推成**实际格尺寸**（不拉伸、不留黑边）。

    「格宽按面板像素反推」就是这一步：预设 620x170 对 1280x720 的帧得 302x170，
    面板宽 = 列数 × 302。预设是上限框，不是硬塞进画面的格宽。
    """
    w, h = int(size[0]), int(size[1])
    bw, bh = int(box[0]), int(box[1])
    if w <= 0 or h <= 0:
        return bw, bh
    scale = min(bw / float(w), bh / float(h))
    return max(1, int(round(w * scale))), max(1, int(round(h * scale)))


def sample(cand: list, want: int) -> list:
    """从候选里均匀取 want 个（含首尾、不重复、确定性）。"""
    n = len(cand)
    if want >= n:
        return list(cand)
    if want <= 1:
        return [cand[n // 2]] if n else []
    step = (n - 1) / float(want - 1)
    out, seen = [], set()
    for i in range(want):
        k = int(round(i * step))
        while k in seen and k + 1 < n:
            k += 1
        seen.add(k)
        out.append(cand[k])
    return out


def burn_index(panel: Image.Image, n: int, scale: int, x0: int, y0: int) -> None:
    """把序号烧进格子左上角（白底黑框黑字）。**只烧序号**：面板里不放时间码 ——
    时间只在 sheet.json 里，面板是索引不是证据。"""
    s = str(n)
    scale = max(1, int(scale))
    d = ImageDraw.Draw(panel)
    pad = 2 * scale
    w = (len(s) * 6 - 1) * scale + 2 * pad
    h = 7 * scale + 2 * pad
    d.rectangle([x0, y0, x0 + w - 1, y0 + h - 1], fill=(255, 255, 255),
                outline=(0, 0, 0), width=max(1, scale // 2))
    for i, ch in enumerate(s):
        for row, bits in enumerate(_DIGITS[ch]):
            for col, bit in enumerate(bits):
                if bit != "1":
                    continue
                x = x0 + pad + (i * 6 + col) * scale
                y = y0 + pad + row * scale
                d.rectangle([x, y, x + scale - 1, y + scale - 1], fill=(0, 0, 0))

def build(cfg, paths, t_from=None, t_to=None, want=None, preset=None, cols=None, rows=None):
    """拼面板。返回 (sheet.json 的内容, 本次统计)；**不落盘**（落盘与自校验在 run() 里）。"""
    sc = cfg.get("sheet") or {}
    pname = str(preset or sc.get("preset") or PRESET_NAMES[1])
    box = dict(PRESETS).get(pname)
    if box is None:
        raise SystemExit("未知面板预设 %r（只有 %s）" % (pname, " / ".join(PRESET_NAMES)))
    cols = int(cols or sc.get("cols") or 3)
    rows = int(rows or sc.get("rows") or 4)
    per = max(1, cols * rows)
    blank_max = float(sc.get("blank_std_max", 0.02))
    max_sheets = int(sc.get("max_sheets", 8))
    cap = max(1, max_sheets * per)
    tiles_want = int(want or per)
    if tiles_want > cap:
        print("[sheet] --max %d 超过配置上限 %d（max_sheets=%d × %d×%d 格），按上限执行"
              % (tiles_want, cap, max_sheets, cols, rows))
        tiles_want = cap

    index = frames_layer.load_index(paths)
    if not index:
        raise SystemExit("没有 cache/frames/index.json（还没抽过帧）：先跑 bnote slides，"
                         "或对单个时刻用 bnote frames --at / --read")
    off = frames_layer.section_offset(cfg)
    lo = frames_layer.to_timeline(cfg, t_from) if t_from else None
    hi = frames_layer.to_timeline(cfg, t_to) if t_to else None
    if lo is not None and hi is not None and hi <= lo:
        raise SystemExit("--to（%s）必须晚于 --from（%s）" % (t_to, t_from))
    cand = [f for f in index
            if (lo is None or float(f["t"]) >= lo) and (hi is None or float(f["t"]) <= hi)]
    if not cand:
        raise SystemExit("这个区间里没有帧：%s–%s（index.json 覆盖 %.3f–%.3f s）"
                         % (t_from or "片头", t_to or "片尾",
                            float(index[0]["t"]), float(index[-1]["t"])))
    picked = sample(cand, tiles_want)

    sheets_dir = paths.meta_dir() / SHEETS_DIRNAME
    sheets_dir.mkdir(parents=True, exist_ok=True)     # 只建自己要写的那一层
    sheets_meta, tiles, missing, orphan_blank = [], [], [], 0
    cell = None
    for start in range(0, len(picked), per):
        chunk = picked[start:start + per]
        placed, blank = [], 0
        for f in chunk:
            src = paths.frames / str(f["file"])
            if not src.exists():
                missing.append(str(f["file"]))
                continue
            with Image.open(src) as im:
                rgb = im.convert("RGB")
                if cell is None:
                    cell = fit_cell(rgb.size, box)
                cell_img = rgb.resize(cell, Image.LANCZOS)
            arr = np.asarray(cell_img.convert("L"), dtype=np.float32) / 255.0
            if float(arr.std()) < blank_max:
                blank += 1        # 空白格：不编号、不补位（跳过的不占 index）
                continue
            placed.append((f, cell_img, src))
        if not placed:
            orphan_blank += blank
            continue
        sname = "sheet_%02d.png" % (len(sheets_meta) + 1)
        rows_used = (len(placed) + cols - 1) // cols
        panel = Image.new("RGB", (cols * cell[0], rows_used * cell[1]), (255, 255, 255))
        scale = max(1, min(4, int(min(cell) * _INDEX_FACTOR / 7.0)))
        for k, (f, cell_img, src) in enumerate(placed):
            r, c = divmod(k, cols)
            x0, y0 = c * cell[0], r * cell[1]
            panel.paste(cell_img, (x0, y0))
            n = len(tiles) + 1
            burn_index(panel, n, scale, x0, y0)
            tiles.append({"index": n, "sheet": sname, "row": r, "col": c,
                          "t": round(float(f["t"]), 3), "frame": str(f["file"]),
                          "sha256": slideset_layer.sha256_file(src)})
        panel.save(sheets_dir / sname, format="PNG")
        ts = [t["t"] for t in tiles if t["sheet"] == sname]
        sheets_meta.append({"name": sname, "from": min(ts), "to": max(ts),
                            "tiles": len(placed), "skipped_blank": blank})

    # 只留这一次产出的面板：上一次跑剩下的 png 会让读者看到 sheet.json 里没有的图
    keep = {s["name"] for s in sheets_meta}
    stale = sorted(p.name for p in sheets_dir.glob("*.png") if p.name not in keep)
    for nm in stale:
        (sheets_dir / nm).unlink()

    doc = {"schema": SCHEMA, "vid": paths.vid, "algo": ALGO,
           "params": {"preset": pname, "cols": cols, "rows": rows, "blank_std_max": blank_max},
           "sheets": sheets_meta, "tiles": tiles,
           "applicability": {"masked": APPLICABILITY_MASKED, "note": APPLICABILITY_NOTE}}
    info = {"cell": cell or fit_cell(box, box), "picked": len(picked), "cand": len(cand),
            "off": off, "missing": missing, "orphan_blank": orphan_blank,
            "range_txt": ("%s–%s%s" % (t_from, t_to, "")) if (t_from or t_to) else "",
            "stale": stale}
    return doc, info

def verify(paths, doc: dict) -> list[str]:
    """行列 → 帧 → t 反查自校验：面板、映射、源帧三者必须自洽。

    构建完就地跑一次 —— 面板是给写手看的"这里有东西"的索引，映射错了比没有更坏。
    """
    problems = []
    names = [str(s.get("name")) for s in doc.get("sheets") or []]
    tiles = doc.get("tiles") or []
    pcols = int((doc.get("params") or {}).get("cols") or 0)
    index = {str(f["file"]): float(f["t"]) for f in (frames_layer.load_index(paths) or [])}
    seen_slot, per_sheet = set(), {}
    for n, t in enumerate(tiles, start=1):
        if t.get("index") != n:
            problems.append("index 不连续：第 %d 条记的是 %r" % (n, t.get("index")))
        sn = str(t.get("sheet"))
        if sn not in names:
            problems.append("tile %s 的面板 %s 不在 sheets[] 里" % (n, sn))
        slot = (sn, t.get("row"), t.get("col"))
        if slot in seen_slot:
            problems.append("行列重复：面板 %s row=%r col=%r" % slot)
        seen_slot.add(slot)
        if pcols and int(t.get("col", -1)) >= pcols:
            problems.append("col %r 超出 cols=%d" % (t.get("col"), pcols))
        if not (paths.meta_dir() / SHEETS_DIRNAME / sn).exists():
            problems.append("面板图不存在：%s" % sn)
        fp = paths.frames / str(t.get("frame"))
        if not fp.exists():
            problems.append("源帧文件不存在：%s" % t.get("frame"))
            continue
        if str(t.get("sha256") or "") != slideset_layer.sha256_file(fp):
            problems.append("源帧 sha256 与 tiles 记录不符：%s" % t.get("frame"))
        if str(t.get("frame")) in index and abs(index[str(t.get("frame"))] - float(t.get("t"))) > 1e-6:
            problems.append("t 与 frames/index.json 不一致：%s" % t.get("frame"))
        per_sheet.setdefault(sn, []).append(t)
    for s in doc.get("sheets") or []:
        sn = str(s.get("name"))
        mine = per_sheet.get(sn) or []
        if len(mine) != int(s.get("tiles") or -1):
            problems.append("面板 %s 的 tiles 计数不符（记 %s，实 %d）" % (sn, s.get("tiles"), len(mine)))
        if not mine:
            continue
        if (abs(min(t["t"] for t in mine) - float(s.get("from"))) > 1e-6
                or abs(max(t["t"] for t in mine) - float(s.get("to"))) > 1e-6):
            problems.append("面板 %s 的 from/to 与 tile 不符" % sn)
        for k, t in enumerate(mine):
            want_rc = divmod(k, pcols or 1)
            if (int(t.get("row", -1)), int(t.get("col", -1))) != want_rc:
                problems.append("面板 %s 第 %d 格不是行主序（row=%r col=%r）"
                                % (sn, k + 1, t.get("row"), t.get("col")))
                break
    return problems


def _rel(paths, path) -> str:
    try:
        return str(Path(path).relative_to(paths.root))
    except ValueError:
        return str(path)


def run(cfg, paths, t_from=None, t_to=None, want=None, preset=None, cols=None, rows=None) -> dict:
    """bnote sheet 的入口：拼版 → 写 sheet.json → 就地自校验 → 打印摘要。"""
    doc, info = build(cfg, paths, t_from=t_from, t_to=t_to, want=want,
                      preset=preset, cols=cols, rows=rows)
    dest = paths.meta_dir() / SHEET_JSON_NAME
    paths.write_json(dest, doc)
    problems = verify(paths, doc)
    p = doc["params"]
    print("[sheet] 预设 %s → 实际格 %dx%d（按源帧反推），%d×%d=%d 格/张 ｜ 上限 max_sheets=%d"
          % (p["preset"], info["cell"][0], info["cell"][1], p["cols"], p["rows"],
             p["cols"] * p["rows"], int((cfg.get("sheet") or {}).get("max_sheets", 8))))
    if info["off"]:
        print("[sheet] media.sections 偏移 %.0fs 已换算（--from/--to 按媒体文件自己的时间轴给）" % info["off"])
    print("[sheet] 取样 %d 帧（候选 %d）%s"
          % (info["picked"], info["cand"],
             ("；区间 %s" % info["range_txt"]) if info["range_txt"] else ""))
    skipped = sum(int(s.get("skipped_blank") or 0) for s in doc["sheets"])
    blank_txt = ("跳过空白 %d 格" % skipped) if skipped else "区间内没有空白格（跳过 0）"
    if info["orphan_blank"]:
        blank_txt += "；另有 %d 个空白候选所在的整张面板全空、未出图" % info["orphan_blank"]
    for s in doc["sheets"]:
        print("[sheet]   %s：%d 格 ｜ %ss–%ss ｜ 跳过空白 %d"
              % (s["name"], s["tiles"], s["from"], s["to"], s["skipped_blank"]))
    if info["missing"]:
        print("[sheet] 跳过缺失的帧文件 %d 个：%s"
              % (len(info["missing"]), ", ".join(info["missing"][:5])))
    print("[sheet] %d 张面板 / %d 格 ｜ %s ｜ %s"
          % (len(doc["sheets"]), len(doc["tiles"]), blank_txt, _rel(paths, dest)))
    if problems:
        print("[sheet] 自校验未通过（%d 条）：" % len(problems))
        for s in problems[:8]:
            print("   ✗ %s" % s)
        raise SystemExit("sheet.json 与面板不一致，先按上面的条目查")
    print("[sheet] 自校验通过：index 连续、行列唯一且行主序、t 与 frames/index.json 一致、源帧 sha256 一致")
    print("[sheet] 面板里的字一律不采信（缩放拼图）：要读字请用 bnote frames --read")
    return doc
