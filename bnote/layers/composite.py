"""L7.5 交付期「同页无遮挡镶嵌」：临时遮挡住的那一角，从同一页的干净帧补回来。

问题（真实缺陷，不是设想）
------------------------
某几张交付的幻灯片交付的是**带标注工具条**的那一帧 —— 工具条（激光笔 / 荧光笔 / 橡皮擦 +
一排色板）压在幻灯片左下角，把当页的标题与正文糊掉了。而它们之所以
被选中，恰恰是因为工具条**多出的字**让它在 OCR 字数打分里赢了（净胜几个字）。

机制（只改 out/ 的交付图，**不动 cache/frames 里的任何原始帧**）
--------------------------------------------------------------
  1) 读 cache/<vid>/overlay.json 的 kind=transient_overlay 区域，拿到 {box, frames}
     —— 命中帧清单是 overlay 层写的（文件即接口，本模块不 import overlay 层）；
  2) 这一页的终态帧在 frames 里 → 拿同段候选帧里**没有被命中过**的那些当"干净源帧"；
  3) 选源帧：按"框外平均绝对差"从小到大试，取第一张同时过两个守卫的 ——
       框外 <= guard_out_eps    （同一页：框外几乎一样，合成不会动到框外的内容）
       框内 >= guard_in_min     （框内确实有东西要被补回来）
     "框外差最小"不是凑数：某一页有若干个干净候选，只有一张的框外差落在 0.00002 量级，其余
     0.0066~0.0100（同一页的**别的状态**）—— 取最小 + 守卫，选出来的就是同一页同一状态那张。
  4) 框内取干净帧、框外取终态帧：
       out/<vid>/_meta/composite/NNNN.png  —— **无损**合成图（审计与复核用；框外与终态帧
                                               逐像素一致，可以直接 diff 验证）
       out/<vid>/_meta/composite/NNNN.jpg  —— 交付图（bundle 拷成 slides/NNNN.jpg）
  5) 全程记账 out/<vid>/_meta/composite.json：源帧 / 两个守卫数值 / 全部试过的候选 /
     是否全遮 / 是否放弃。**放弃或全遮时不动图**（照旧交付终态帧），但账上写清是哪种。

守卫实测（4 张问题页，全分辨率灰度，框外 = 除角窗外的 91% 画面）
--------------------------------------------------------------
  终态帧 ← 干净源帧        框外平均绝对差    框内 RMS
  000517 ← 000513            0.00002          0.145
  002791 ← 002797            0.00028          0.212
  001421 ← 001318            0.00215          0.163
  002501 ← 002484            0.00322          0.169
  同段其它候选（别的状态）    0.0066~0.0336     0.16~0.21（被守卫挡掉）

**交付 JPEG 的重编码噪声（如实说）**：slides/NNNN.jpg 必须还是 JPEG（引用契约是
`../slides/NNNN.jpg`），所以交付图是一次 q100 + 4:4:4 重编码：实测相对 cache 原帧的框外
|差| 均值 0.037~0.047 灰阶 / 最大 4 灰阶 / 涉及 3.3%~4.0% 像素（灰阶 0..255）—— 这是编解码
噪声，不是内容改动；"合成有没有动框外"请看上面的无损 PNG（框外逐像素一致）。

确定性：同输入同输出（不取时间戳、不随机；并列按文件名）。本模块写的两个产物（PNG/JPEG）
与 composite.json **都不含时间戳**，所以"同参数两跑逐字节一致"可以直接验证。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

SCHEMA = "bnote-composite/1"
KIND = "transient_overlay"

# [composite] 段的默认值（键名与默认值的唯一真源；config/default.toml 里有一份带实测注释的副本）
DEFAULTS = {
    "enabled": True,
    "guard_out_eps": 0.01,      # 框外平均绝对差上限（"两张是同一页"的守卫）
    "guard_in_min": 0.02,       # 框内 RMS 下限（"框内确实有东西要补"的守卫）
    "max_tries": 8,             # 最多试几张干净候选当源帧
    "jpg_quality": 100,         # 交付 JPEG 质量（重编码噪声实测见模块头）
}


def params(cfg: dict) -> dict:
    c = (cfg or {}).get("composite") or {}
    out = {}
    for k, v in DEFAULTS.items():
        if isinstance(v, bool):
            out[k] = bool(c.get(k, v))
        elif isinstance(v, int):
            out[k] = int(c.get(k, v))
        else:
            out[k] = float(c.get(k, v))
    return out


def transient_regions(doc: dict) -> list:
    """从 overlay 文档里取**按帧生效**的临时遮挡区域：返回 [{box, frames}, ...]。

    只读冻结结构（regions[].kind/box/evidence.frames），**不 import overlay 层**（P1：
    层与层只通过文件通信）。没有区域时返回空列表。
    """
    out = []
    for r in (doc.get("regions") or []):
        if r.get("kind") != KIND or r.get("applicability") != "ok":
            continue
        box = r.get("box")
        if not (isinstance(box, list) and len(box) == 4):
            continue
        ev = r.get("evidence") or {}
        frames = [str(x) for x in (ev.get("frames") or [])]
        if frames:
            out.append({"box": [float(x) for x in box], "frames": frames})
    return out


def box_pixels(box, w: int, h: int):
    """相对坐标框 -> 像素范围 (x0, y0, x1, y1)，至少 1x1，且严格落在画面内。"""
    l, t, r, b = [float(x) for x in box]
    x0 = max(0, min(w - 1, int(round(l * w))))
    y0 = max(0, min(h - 1, int(round(t * h))))
    x1 = max(x0 + 1, min(w, int(round(r * w))))
    y1 = max(y0 + 1, min(h, int(round(b * h))))
    return x0, y0, x1, y1


def _gray(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("L"), dtype=np.float32) / 255.0


def _rgb(path: Path) -> Image.Image:
    with Image.open(path) as im:
        return im.convert("RGB").copy()


def compose_one(paths, seg: dict, box, hits: set, p: dict, out_dir: Path) -> dict:
    """给一页做合成（或写明为什么不做）。返回这一页的记账 dict。"""
    sid = int(seg.get("id"))
    chosen = str(seg["chosen"]["file"])
    rec = {"id": sid, "frame": chosen, "box": [round(float(x), 4) for x in box],
           "applied": False, "all_occluded": False, "reason": "", "src_frame": None,
           "guard_out_mean": None, "guard_out_rms": None, "guard_in_rms": None,
           "guard_out_eps": p["guard_out_eps"], "guard_in_min": p["guard_in_min"],
           "tried": [], "out_png": None, "out_jpg": None,
           "encode_out_mean": None, "encode_out_max": None}
    a_img = _rgb(paths.frames / chosen)
    A = np.asarray(a_img.convert("L"), dtype=np.float32) / 255.0
    h, w = A.shape
    x0, y0, x1, y1 = box_pixels(rec["box"], w, h)
    rec["box_px"] = [x0, y0, x1, y1]
    outside = np.ones_like(A, dtype=bool)
    outside[y0:y1, x0:x1] = False
    cands = [c for c in (seg.get("candidates") or []) if str(c.get("file")) != chosen]
    clean = [c for c in cands if str(c.get("file")) not in hits]
    rec["candidates"] = len(cands)
    rec["clean_candidates"] = len(clean)
    if not clean:
        rec["all_occluded"] = True
        rec["reason"] = ("全遮：同段 %d 个候选帧**全部**被临时遮挡命中，没有干净源帧 → 不动图，"
                         "照旧交付终态帧（记账在案）" % len(cands))
        return rec
    for c in clean[:max(1, int(p["max_tries"]))]:
        f = str(c.get("file"))
        b = paths.frames / f
        if not b.exists():
            continue
        B = _gray(b)
        if B.shape != A.shape:
            continue
        d = np.abs(A - B)
        rec["tried"].append({
            "file": f, "t": c.get("t"), "score": c.get("score"),
            "out_mean": round(float(d[outside].mean()), 5),
            "out_rms": round(float(np.sqrt((d[outside] ** 2).mean())), 5),
            "in_rms": round(float(np.sqrt((d[~outside] ** 2).mean())), 5)})
    rec["tried"].sort(key=lambda r: (r["out_mean"], r["file"]))
    best = next((t for t in rec["tried"]
                 if t["out_mean"] <= float(p["guard_out_eps"])
                 and t["in_rms"] >= float(p["guard_in_min"])), None)
    if best is None:
        why = ("框外全都不够像（%s > %.3f）" % (rec["tried"][0]["out_mean"], p["guard_out_eps"])
               if rec["tried"] else "没有任何候选能读出来")
        rec["reason"] = ("守卫不过：%s → 放弃合成、不动图（账上有全部试过的候选）" % why)
        return rec
    # 合成：框外**直接拷贝终态帧的像素**（数组层逐像素一致），框内取干净帧
    b_img = _rgb(paths.frames / best["file"])
    comp = a_img.copy()
    comp.paste(b_img.crop((x0, y0, x1, y1)), (x0, y0))
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / ("%04d.png" % sid)
    jpg = out_dir / ("%04d.jpg" % sid)
    comp.save(png, "PNG")
    comp.save(jpg, "JPEG", quality=int(p["jpg_quality"]), subsampling=0)
    C = np.asarray(_gray(png), dtype=np.float32)      # 无损合成图：框外必须与终态帧逐像素一致
    enc = np.abs(np.asarray(_gray(jpg), dtype=np.float32) * 255.0
                 - A * 255.0)
    rec.update({
        "applied": True, "src_frame": best["file"],
        "guard_out_mean": best["out_mean"], "guard_out_rms": best["out_rms"],
        "guard_in_rms": best["in_rms"],
        "out_png": str(png.relative_to(paths.out)), "out_jpg": str(jpg.relative_to(paths.out)),
        "sync_out_mean": round(float(np.abs(A - C)[outside].mean()), 8),
        "sync_in_rms": round(float(np.sqrt((np.abs(A - C)[~outside] ** 2).mean())), 5),
        "encode_out_mean": round(float(enc[outside].mean()), 4),
        "encode_out_max": int(enc[outside].max()),
        "reason": ("终态帧 %s 的左下角（框 %s）被临时遮挡命中（%d/%d 帧）→ 框内取同页干净帧 %s、"
                   "框外取终态帧" % (chosen, rec["box"], 1, 1, best["file"])),
    })
    return rec


def prepare(cfg: dict, paths, segments: list) -> dict:
    """bundle 用：给每个"终态帧被临时遮挡命中"的页准备合成图。

    返回 {"src": {seg_id: Path}（要拷进 slides/ 的交付图）, "records": [...], "ledger": {...}}：
      * src 里有这一页 → bundle 拷它（而不是 cache/frames 的终态帧）；
      * 没有 → 照旧拷终态帧（没被命中，或守卫不过/全遮）。
    产物：out/<vid>/_meta/composite/{NNNN.png,NNNN.jpg} 与 composite.json（**不含时间戳**，
    同参数两跑逐字节一致）。没有被命中的页时**不写** composite.json（不制造空产物）。
    """
    p = params(cfg)
    out_dir = paths.meta_dir() / "composite"
    plan, records = {}, []
    doc = paths.read_json(paths.overlay) or {}
    regions = transient_regions(doc)
    hits = {}
    for r in regions:
        for f in r["frames"]:
            hits.setdefault(f, r["box"])       # 同一帧被两个角命中时按 regions 顺序取第一个
    if not p["enabled"] or not hits:
        return {"src": plan, "records": records, "ledger": None, "dir": out_dir,
                "hit_frames": len(hits),
                "note": ("[composite] 已按配置关闭（[composite].enabled=false）" if not p["enabled"]
                         else "overlay.json 里没有临时遮挡命中帧 → 不做合成")}
    if out_dir.exists():
        for old in out_dir.glob("*.png"):
            old.unlink()
        for old in out_dir.glob("*.jpg"):
            old.unlink()
    for seg in segments:
        chosen = str((seg.get("chosen") or {}).get("file") or "")
        if chosen not in hits:
            continue
        rec = compose_one(paths, seg, hits[chosen], set(hits), p, out_dir)
        records.append(rec)
        if rec["applied"]:
            plan[int(seg["id"])] = out_dir / ("%04d.jpg" % int(seg["id"]))
    ledger = {"schema": SCHEMA, "vid": paths.vid,
              "params": params(cfg),
              "transient_regions": len(regions), "hit_frames": len(hits),
              "count": len(records),
              "applied": sum(1 for r in records if r["applied"]),
              "all_occluded": sum(1 for r in records if r["all_occluded"]),
              "skipped": sum(1 for r in records if not r["applied"] and not r["all_occluded"]),
              "pages": records,
              "note": "框内取同页干净帧、框外取终态帧；applied=false 的页**不动图**，"
                      "原因见该页的 reason/all_occluded。本文件不含时间戳。"}
    paths.write_json(paths.meta_dir() / "composite.json", ledger)
    return {"src": plan, "records": records, "ledger": ledger, "dir": out_dir,
            "hit_frames": len(hits), "note": ""}


def summary_line(res: dict) -> str:
    """bundle 里的一行摘要。"""
    led = res.get("ledger")
    if led is None:
        return "[bundle] 临时遮挡：%s" % (res.get("note") or "无")
    return ("[bundle] 临时遮挡：命中 %d 帧；交付页需要补全 %d 张，成功 %d、全遮 %d、守卫不过 %d"
            % (led["hit_frames"], led["count"], led["applied"], led["all_occluded"],
               led["skipped"]))
