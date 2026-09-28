"""stable 策略：稳定态窗口 + 段内终态收敛。

为什么这样切：
  PPT 一页有多个动画中间态，任何"变化即切帧"的做法都会截到加载一半的画面。
  真正的"一页讲完"= 画面连续 T 秒几乎不变。因此：
    1) 先用廉价的 dHash+像素差画出 Δ(t)；
    2) 取 Δ 连续低于阈值且持续 >= stable_min_sec 的区间为"稳定态"；
    3) 每个稳定态作为一页，时间范围延伸到下一个稳定态开始；
    4) 段内先按墨迹/清晰度粗排取 top-k 候选，再对候选跑 OCR，
       取"文字最全 + 最清晰"的帧作为该页终态（动画构建序列自然收敛到最后一帧）；
    5) 相邻页若终态 dHash 相近则合并（转场造成的碎片）；
    6) 段边界吸附到最近的字幕句首，避免切在半句话中间。

M3 起：``cache/<vid>/overlay.json`` 说"哪些像素不是幻灯片内容"（烧录字幕条 / 标注工具条 /
水印）。这些像素会**同时**从帧差的两项（dHash 汉明距离 + 像素平均绝对差）、墨迹 ink、清晰度
sharpness 与送 OCR 的图里挖掉 —— 外物既不该搅乱稳定性判定，也不该把文字塞进 chosen 的
OCR 文本（OCR 文本参与同页判定的 4-gram 包含度，实测多集都被工具条文字污染过）。
没有 overlay.json 时**按全画面算**（M3 之前的行为）并打印一行说明，不报错。
"""
from __future__ import annotations

import re
from collections import Counter

from .framesig import frame_diff, hamming, ink_params, ink_ratio, sharpness, signature


def _norm_text(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", s or "")


_NORMCH = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")


def _boiler_re(patterns):
    """样板文字正则：窗口标题栏、页码、站名水印等每页都出现的文字，来自 config 的
    segment.boilerplate_patterns（不在代码里写死，课程专有字样放 config/local.toml）。

    同页判定前必须剔除这些文字，否则任意两页都"很像"。
    """
    pats = [str(p) for p in (patterns or []) if str(p).strip()]
    return re.compile("|".join(pats), re.I) if pats else None


def _norm_grams(text: str, n: int = 4, boiler=None) -> set:
    t = boiler.sub(" ", text) if boiler else (text or "")
    t = _NORMCH.sub("", t)
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def _gram_containment(a: str, b: str, n: int = 4, boiler=None) -> float:
    """4-gram 包含度：短的那页有多少 n-gram 出现在长的那页里。

    相比于字符集包含度，它对"同页不同阶段"（内容只增不减）敏感，而对"两页都提到 RAG/知识库"
    这种字符层面的巧合不敏感 —— 实测两张完全不同的页字符集包含度 0.88，4-gram 只有 0.17。
    """
    A, B = _norm_grams(a, n, boiler), _norm_grams(b, n, boiler)
    if not A or not B:
        return 0.0
    small, big = (A, B) if len(A) <= len(B) else (B, A)
    return len(small & big) / len(small)


def _containment(a: str, b: str) -> float:
    """字符多重集包含度：较小文本有多少比例出现在较大文本里（防重排/多字少字）"""
    if not a or not b:
        return 0.0
    ca, cb = Counter(a), Counter(b)
    inter = sum(min(ca[k], cb[k]) for k in ca)
    return inter / max(1, min(len(a), len(b)))


def _is_additive(prev_small, cur_small, changed_max: float, old_ink_max: float) -> bool:
    """增量绘制判据：变化只发生在原本接近空白的区域 -> 同页动画，而不是换页。

    换页通常是整页重绘（变化区域大、且变化区域原本就有内容）。

    注意这里的 `< 0.62` 是**固定阈值的历史口径**（`additive_old_ink_max=0.12` 是照它标定的），
    与 ink_ratio 的逐帧自适应门槛**不是同一个量**：本判据只在 OCR 不可用时兜底
    （`_why_same` 里 ocr_on 为真就先返回），改它会动"同页合并"的语义，不在本次范围。
    深色主题下它与 ink 有同样的退化（判断恒为否），已记进 PACK-INK-ADAPTIVE 的未验证项。
    """
    import numpy as np
    d = np.abs(prev_small - cur_small)
    mask = d > 0.15
    changed_ratio = float(mask.mean())
    if changed_ratio == 0.0 or changed_ratio > changed_max:
        return False
    old_ink = float((prev_small[mask] < 0.62).mean()) if mask.any() else 1.0
    return old_ink <= old_ink_max


def _overlay_spec(paths):
    """读 cache/<vid>/overlay.json：返回 (遮罩框列表, 手写笔迹判据参数或 None)。

    **文件就是接口**（P1）：这里不 import overlay 层，只认冻结的 regions[] 与 params。
    四条规矩：
      * 只有 applicability == "ok" 的区域可采信；
      * kind == "handwriting" 的区域**不进遮罩框** —— 它的 box 只是审计范围，按框整块挖会把
        其余帧同位置的正文一起挖掉；笔迹要按帧用 params 里的 handwriting_* 判据重算（M4）；
      * kind == "transient_overlay" 的区域**不进遮罩框**，只把 evidence.frames（命中帧清单）
        带出来 —— 它只对那几帧成立（§3.4-11）；交付期的补全由 layers/composite.py 做；
      * 读不出来（缺文件 / 结构不对）一律当"没有遮罩"处理，由调用方打印说明后退化为全画面。

    返回 (遮罩框列表, 手写笔迹判据参数或 None, 临时遮挡命中帧集合)。
    """
    doc = paths.read_json(paths.overlay) or {}
    boxes = []
    hand = None
    transient = set()
    for r in (doc.get("regions") or []):
        if r.get("applicability") != "ok":
            continue
        if r.get("kind") == "handwriting":
            hand = dict(doc.get("params") or {})
            continue
        if r.get("kind") == "transient_overlay":
            # 临时遮挡**按帧**生效：把命中帧清单带出来给候选层标记（**不进 boxes** ——
            # 整块挖会伤其余帧同位置的正文）
            for name in ((r.get("evidence") or {}).get("frames") or []):
                transient.add(str(name))
            continue
        box = r.get("box")
        if isinstance(box, list) and len(box) == 4:
            boxes.append(tuple(float(x) for x in box))
    return boxes, hand, transient


def _mask_cells(masks, rows, cols):
    """把相对坐标的遮罩框落到签名网格 (rows, cols)：框中心落在格里就算遮住那一格。

    只给**墨迹**用（被涂白的格子不该算进"这一页画了多少东西"）；帧差不用它 —— 签名阶段
    已经把遮罩区涂成同一个常数，两帧之差在那里恒为 0。返回 None 表示没有遮罩。
    """
    if not masks:
        return None
    import numpy as np
    yy, xx = np.mgrid[0:rows, 0:cols]
    cy = (yy + 0.5) / rows
    cx = (xx + 0.5) / cols
    keep = np.ones((rows, cols), dtype=bool)
    for (l, t, r, b) in masks:
        keep &= ~((cx >= l) & (cx <= r) & (cy >= t) & (cy <= b))
    return keep


def _build_signals(cfg, paths, frames, masks=None, strokes=None, app=None):
    region = tuple(cfg["frames"].get("region") or (0, 0, 1, 1))
    # 墨迹门槛逐帧自适应（深色主题下固定 0.62 会让 ink 恒为 1.0）：判据与实测见 framesig.INK_DEFAULTS
    inkp = ink_params(cfg.get("segment") or {})
    sigs, cheap = [], []
    keep = None
    for f in frames:
        p = paths.frames / f["file"]
        s = signature(p, region, masks, strokes=strokes, app=app)
        sigs.append(s)
        if keep is None:
            keep = _mask_cells(masks, s[0].shape[0], s[0].shape[1])
        cheap.append({"ink": ink_ratio(s[0][keep], inkp) if keep is not None else ink_ratio(s[0], inkp),
                      "sharp": sharpness(s[0])})
    alpha = float(cfg["segment"].get("diff_alpha", 0.5))
    diffs = [0.0] + [frame_diff(sigs[i - 1], sigs[i], alpha) for i in range(1, len(sigs))]
    return sigs, cheap, diffs


def _legacy_caption_strip(sigs, cfg):
    """**冻结的 M3 之前路径**：识别烧进画面的字幕条；只在没有 overlay.json 时才用。

    M3 的新判据（``band_change_rate``，含按行剖面拟合带）只有一份实现，在
    bnote/layers/overlay.py；这里保留旧实现逐字不动，是为了让"没有 overlay.json"的旧数据根
    仍得到与 M3 之前**完全一样**的切片结果（``[segment].auto_caption_strip`` 两种取值都照旧）。
    新路径（``bnote slides`` 会自动先跑 overlay）不会走到这里。
    """
    if not cfg["segment"].get("auto_caption_strip", True) or len(sigs) < 8:
        return None, None
    import numpy as np
    rows = sigs[0][0].shape[0]
    cut = int(rows * (1.0 - float(cfg["segment"].get("caption_strip_ratio", 0.14))))
    band_bottom = (cut, rows)
    band_top = (0, max(2, int(rows * 0.35)))     # 标题区：幻灯片切换时才变，用来做对照
    if band_bottom[1] - band_bottom[0] < 2:
        return None, None
    thr = float(cfg["segment"].get("diff_threshold", 0.035)) * 0.6   # 字幕条多为细字，变化幅度小于整页阈值
    rate = {"bottom": 0, "mid": 0, "n": 0}
    for i in range(1, len(sigs)):
        a, b = sigs[i - 1][0], sigs[i][0]
        for name, (y0, y1) in (("bottom", band_bottom), ("mid", band_top)):
            d = float(np.abs(a[y0:y1] - b[y0:y1]).mean())
            if d > thr:
                rate[name] += 1
        rate["n"] += 1
    if not rate["n"]:
        return None, None
    rb = rate["bottom"] / rate["n"]
    rm = rate["mid"] / rate["n"]
    mult = float(cfg["segment"].get("caption_band_multiple", 3.0))
    if rb > 0.15 and rb > max(rm * mult, rm + 0.08):
        region = (0.0, 0.0, 1.0, cut / rows)
        print("[stable] 检测到字幕条：底部 %d%% 区域变化率 %.2f 远高于标题区 %.2f → 判定为口播字幕并排除"
              % (int(100 * (rows - cut) / rows), rb, rm))
        return band_bottom, region
    return None, None


def _hard_cuts(diffs, cfg):
    """标出"硬切"帧：帧差达到分位数门槛的位置才可能是换页，小幅变化（手写标注）不算。

    返回 bool 列表（长度与 diffs 相同）。
    """
    import numpy as np
    d = np.asarray(diffs, dtype=np.float32)
    if d.size < 8:
        return [False] * len(diffs)
    q = float(cfg["segment"].get("page_cut_percentile", 0.85))
    mult = float(cfg["segment"].get("page_cut_multiple", 3.0))
    thr = max(float(np.quantile(d, q)), float(d.mean()) * mult * 0.5)
    return [bool(x >= thr) for x in d]


def _stable_runs(diffs, fps, threshold, min_sec):
    runs, start = [], None
    for i, d in enumerate(diffs):
        if i == 0:
            continue
        if d <= threshold:
            if start is None:
                start = i - 1
        else:
            if start is not None:
                runs.append((start, i - 1))
                start = None
    if start is not None:
        runs.append((start, len(diffs) - 1))
    return [(s, e) for (s, e) in runs if (e - s) / fps >= min_sec]


def _score_candidate(cfg, cheap_metrics, ocr_info, pos_ratio):
    w = cfg["ocr"]
    norm_sharp = min(cheap_metrics["sharp"] / 0.02, 1.0)
    return (float(w["weight_chars"]) * ocr_info["chars"]
            + float(w["weight_ink"]) * cheap_metrics["ink"]
            + float(w["weight_sharp"]) * norm_sharp
            + 0.001 * pos_ratio)


def _pick_frame(cfg, paths, frames, idxs, sigs, cheap, ocr, stable_mask=None, ocr_region=None,
               ocr_masks=None, transient_frames=None):
    """从该页的帧里挑"终态帧"。

    关键：动画页的完整状态出现在**稳定区间的末尾**。所以候选池只取"稳定帧"
    （Δ 低的帧，排除转场/运动模糊帧），并强制包含 首帧 / 末帧 / 墨迹最多 / 最清晰 的帧，
    再用均匀采样补足到 candidates_per_seg 个，最后交给 OCR 打分择优。
    """
    pool = [i for i in idxs if (stable_mask[i] if stable_mask else True)] or list(idxs)
    k = max(3, int(cfg["segment"]["candidates_per_seg"]))

    chosen_idx = {pool[0], pool[-1]}
    if len(pool) >= 3:
        chosen_idx.add(max(pool, key=lambda i: cheap[i]["ink"]))
        chosen_idx.add(max(pool, key=lambda i: cheap[i]["sharp"]))
    if len(chosen_idx) < k:
        step = max(1, len(pool) // (k - len(chosen_idx) + 1))
        for i in pool[::step]:
            chosen_idx.add(i)
            if len(chosen_idx) >= k:
                break
    if len(chosen_idx) > k:  # 超了就保留首尾并均匀降采样
        ordered = sorted(chosen_idx)
        keep = {ordered[0], ordered[-1]}
        step = max(1, len(ordered) // (k - len(keep)))
        for i in ordered[::step]:
            keep.add(i)
            if len(keep) >= k:
                break
        chosen_idx = keep

    scored = []
    for i in sorted(chosen_idx):
        m = dict(cheap[i])
        m["idx"] = i
        m["t"] = frames[i]["t"]
        m["file"] = frames[i]["file"]
        m["dhash"] = sigs[i][1]          # 角色终选可能改选另一张候选帧，dhash 要能整条搬过去
        scored.append(m)

    span = max(1, len(idxs) - 1)
    base = min(idxs)
    transient_frames = transient_frames or set()
    pen = float((cfg.get("segment") or {}).get("transient_overlay_penalty", 0.0))
    for m in scored:
        ocr_info = (ocr.text(paths.frames / m["file"], region=ocr_region, masks=ocr_masks)
                    if ocr else {"text": "", "chars": 0, "boxes": 0})
        m["ocr_chars"] = ocr_info["chars"]
        m["ocr_boxes"] = ocr_info["boxes"]
        m["ocr_text"] = ocr_info["text"]
        m["score"] = _score_candidate(cfg, m, ocr_info, (m["idx"] - base) / span)
        # §3.4-11：这一帧的角上被**临时遮挡**命中（overlay.json 的 transient_overlay 帧清单）。
        # 默认只**标记**（penalty=0）：实测这几张交付页的净胜分**全部**来自叠加层自己的字数
        # （每页净胜几分到二十几分、字数几个到十几个），一旦扣分就会把**终态帧**换成
        # 同页更早的候选（要退回近一分钟前的帧）—— 那正是 M4b occlusion_swap 判错
        # 的同一类退化。被遮住的那一角改由**交付期**的同页镶嵌补回（layers/composite.py），
        # 终态帧照旧是主图。想试扣分的人把 [segment].transient_overlay_penalty 打开。
        m["transient_overlay"] = m["file"] in transient_frames
        if m["transient_overlay"] and pen > 0:
            m["score"] -= pen
    scored.sort(key=lambda m: m["score"], reverse=True)

    # OCR 是概率性识别：对前两名候选各跑一次，按行合并（模糊去重）得到更完整的页面文字
    if ocr and cfg["segment"].get("ocr_consensus", True) and len(scored) > 1:
        import difflib
        lines = []
        for m in scored[:2]:
            if ocr_masks or ocr_region:
                info = ocr.text(paths.frames / m["file"], region=ocr_region, masks=ocr_masks)
            else:
                info = ocr.text(paths.frames / m["file"])
            for ln in re.split(r"[\n；;。]", info.get("text", "")):
                ln = ln.strip()
                if len(ln) < 2:
                    continue
                if any(difflib.SequenceMatcher(None, ln, old).ratio() >= 0.8 for old in lines):
                    continue
                lines.append(ln)
        if lines:
            merged_text = "；".join(lines)
            scored[0]["ocr_text"] = merged_text
            scored[0]["ocr_chars"] = len(merged_text.replace(" ", ""))
    return scored[0], scored


def _cand_doc(c: dict) -> dict:
    """候选帧落盘的字段（角色与判据一起写进去，check 要靠它判"整页优先"是否生效）。"""
    return {"t": c["t"], "file": c["file"], "score": round(c["score"], 4),
            "ocr_chars": c["ocr_chars"], "ink": round(c["ink"], 4),
            "role": c.get("role"), "role_evidence": c.get("role_evidence"),
            # M4b「遮挡最少」的判据数字：最大连通前景块占整幅的比例（越大=被挡得越多）。
            # 它只作**同页候选之间**的相对比较（同页背景相同），不跨页比。
            "occlusion": c.get("occlusion"),
            # §3.4-11：这一帧的角上有没有被**临时遮挡**命中（overlay.json 的帧清单）。
            # 无论扣分开关是否打开都写，证据可查（交付期镶嵌只看它）。
            "transient_overlay": bool(c.get("transient_overlay"))}


def _union_cands(dst: dict, src: dict) -> None:
    """把 src 的候选并进 dst（按文件名去重，保序）——合并/吸收都要保留双方的候选证据。"""
    seen = {c.get("file") for c in (dst.get("_cands") or [])}
    for c in (src.get("_cands") or []):
        if c.get("file") not in seen:
            dst.setdefault("_cands", []).append(c)
            seen.add(c.get("file"))


def _chosen_doc(c: dict) -> dict:
    return {"t": c["t"], "file": c["file"], "score": round(c["score"], 4),
            "ocr_chars": c["ocr_chars"], "ocr_text": c.get("ocr_text", ""),
            "ink": round(c["ink"], 4), "sharp": round(c["sharp"], 5), "dhash": c["dhash"],
            "role": c.get("role"), "role_evidence": c.get("role_evidence")}


# M4b「遮挡最少」：**换帧默认关闭**（[roles].occlusion_swap，默认 false），判据本身保留。
#   occlusion_swap=false 时选帧**完全不做**遮挡换帧，但 occlusion 照旧逐帧算并落盘（证据可查）——
#   为什么默认关：这条判据在十余集语料里只在某一集触发过一次，而那一次就是判错的（把完整渲染
#   的那一帧换成了淡入中的半渲染帧）；逐帧目视证据见 config/default.toml。
# 打开后（true）用它下面这两个门槛（都在 [segment] 段可覆盖，见 config/default.toml）：
#   occ_score_tol：只在"信息量接近"的候选之间比遮挡 —— 打分差超过这个比例就不换，
#     否则会把**动画中途**的不完整帧（信息少但没被挡）选成主图，那是 M4 一直在防的退化。
#   occ_min_gain：遮挡要明显更少才值得换（同一页内最大连通前景块的占比差）。
def _apply_roles(cfg, paths, out, masks, role_fn, page_roles=("full_page",)) -> None:
    """M4/M4b：判角色 → **整页优先**（+ 可选的**遮挡最少**换帧，默认关）选帧（§3.5-2/3 + M4b）。

    四件事，顺序不能换：
      1) 把全集候选帧交给角色层一次性判完（同一文件只读一次；笔画中位数是**本集口径**）；
      2) 每段：只要存在可当主图的候选（page_roles，full_page 优先、其次 app_screen），
         终态就必须换成它们里的一张；
      3) 仅当 [roles].occlusion_swap=true（**默认 false**）时，在这一档候选里挑
         **信息量接近**（打分 >= best*(1-occ_score_tol)）而**遮挡明显更少**
         （occlusion <= 当前 - occ_min_gain）的那张换帧 —— "人/桌面挡住半页"时让位；
         默认关是因为该判据在唯一真实样本上判错（见 config/default.toml 的注释）；
      4) 每段写 role / role_evidence（取自终选帧），候选表里也各写一份（check 靠它复核）；
         occlusion 无论开关都照旧写进候选表（判据保留，只是不参与换帧）。
    """
    segcfg = cfg.get("segment") or {}
    swap_on = bool((cfg.get("roles") or {}).get("occlusion_swap", False))
    score_tol = float(segcfg.get("occ_score_tol", 0.10))
    min_gain = float(segcfg.get("occ_min_gain", 0.10))
    files = []
    for seg in out:
        for c in (seg.get("_cands") or []):
            if c.get("file"):
                files.append(c["file"])
    res = role_fn(cfg, paths, files, masks=masks) or {}
    switched = 0
    for seg in out:
        cands = seg.get("_cands") or []
        for c in cands:
            r = res.get(c.get("file"))
            c["role"] = r["role"] if r else None
            c["role_evidence"] = r["evidence"] if r else None
            c["occlusion"] = (r["features"].get("occlusion") if r else None)
        chosen = seg.get("chosen") or {}
        # 终选帧在候选表里的那条记录（角色 / 判据都挂在候选上；chosen 自己不带）
        cur = next((c for c in cands if c.get("file") == chosen.get("file")), None)
        # "可当主图"的候选：full_page 与 app_screen **同等对待**（app_screen 仍是整屏内容，
        # M4b 的语义就是"整页优先照旧可作主图"，不做分档，否则整集 IDE 录屏素材会被
        # 强行换成同一页里得分更高的幻灯片式候选，反而偏离基线）
        pool = [c for c in cands if c.get("role") in page_roles]
        if pool and (cur is None or cur.get("role") not in page_roles or cur not in pool):
            best = max(pool, key=lambda c: c["score"])
            if best.get("file") != chosen.get("file"):
                switched += 1
                print("[stable] 第 %s 页整页优先：终选 %s（%s）→ %s（%s，得分 %.4f）"
                      % (seg.get("id"), chosen.get("file"),
                         (cur.get("role") if cur else "未判"), best.get("file"),
                         best.get("role"), best["score"]))
            seg["chosen"] = _chosen_doc(best)
            cur = best
        # M4b-3：同档候选里挑遮挡最少的（只换"信息量接近且遮挡明显更少"的）。
        # **默认关闭**（[roles].occlusion_swap=false）：这条判据在十余集语料里唯一的真实样本上
        # 判错了方向（见上方注释与 config/default.toml）；occlusion 数值照旧逐帧落盘。
        if swap_on and pool and cur is not None:
            top = max(c["score"] for c in pool)
            cur_occ = cur.get("occlusion")
            cands_ok = [c for c in pool
                        if c["score"] >= top * (1.0 - score_tol)
                        and c.get("occlusion") is not None
                        and (cur_occ is None or c["occlusion"] <= cur_occ - min_gain)]
            if cands_ok:
                best = min(cands_ok, key=lambda c: (c["occlusion"], -c["score"]))
                if best.get("file") != cur.get("file"):
                    switched += 1
                    print("[stable] 第 %s 页遮挡最少：%s（occlusion %.3f，得分 %.4f）→ %s"
                          "（occlusion %.3f，得分 %.4f）"
                          % (seg.get("id"), cur.get("file"), cur_occ or 0.0, cur["score"],
                             best.get("file"), best["occlusion"], best["score"]))
                    seg["chosen"] = _chosen_doc(best)
                    cur = best
        if cur is not None and cur.get("role"):
            seg["role"] = cur["role"]
            seg["role_evidence"] = cur.get("role_evidence")
            # 契约 §3.5 要求 chosen 里也写 role（check 只认这一份，cache 被 clean 后 out/ 侧还有）
            seg["chosen"]["role"] = cur["role"]
            seg["chosen"]["role_evidence"] = cur.get("role_evidence")
        seg["candidates"] = [_cand_doc(c) for c in cands]
    if switched:
        print("[stable] 角色选帧共换帧 %d 处" % switched)


def _snap_to_transcript(t, transcript, window):
    if not transcript:
        return t, None
    best, best_d = None, None
    for seg in transcript["segments"]:
        d = abs(seg["from"] - t)
        if best_d is None or d < best_d:
            best, best_d = seg["from"], d
    if best is not None and best_d is not None and best_d <= window:
        return round(best, 3), best_d
    return t, None


def segment(cfg, paths, frames, transcript, ocr, role_fn=None, page_roles=("full_page",)):
    fps = float(cfg["frames"]["fps"])
    th = float(cfg["segment"]["diff_threshold"])
    min_sec = float(cfg["segment"]["stable_min_sec"])
    min_seg = float(cfg["segment"]["min_seg_sec"])

    # M3：先看 cache/<vid>/overlay.json 说"哪些像素不是幻灯片内容"，再决定帧差/墨迹/OCR 用什么。
    masks, strokes, transient = _overlay_spec(paths)
    if masks:
        print("[stable] 按 overlay.json 的 %d 个遮罩区域计算帧差/墨迹/清晰度/送 OCR：%s"
              % (len(masks), "、".join("[%.3f,%.3f,%.3f,%.3f]" % m for m in masks)))
    elif not paths.overlay.exists():
        print("[stable] 没有 cache/<vid>/overlay.json → 按**全画面**计算（M3 之前的行为）；"
              "重跑 bnote slides 会自动产出它")
    else:
        print("[stable] overlay.json 里没有可采信的遮挡框（只有手写笔迹判据）→ 帧差/墨迹按**全画面**算")
    if transient:
        pen = float((cfg.get("segment") or {}).get("transient_overlay_penalty", 0.0))
        print("[stable] %d 帧被**临时遮挡**命中（按帧生效，不进遮罩）：%s%s"
              % (len(transient), "、".join(sorted(transient)[:8]) + ("…" if len(transient) > 8 else ""),
                 ("；候选层按 [segment].transient_overlay_penalty=%.3f 扣分" % pen) if pen > 0 else
                 "；候选层只标记不扣分（[segment].transient_overlay_penalty=0，理由见 default.toml）"))
    app = None
    if strokes:
        # M4b：手写守卫 —— 整屏应用/录屏（IDE/浏览器/终端）帧整帧跳过涂白，参数与角色判据同源
        app = dict((cfg.get("roles") or {}))
        print("[stable] 按 overlay.json 的 handwriting 判据**逐帧**挖掉彩色细笔画（笔迹不进帧差/墨迹/OCR）；"
              "app_screen 帧跳过（整屏 UI 不是手写）")
    sigs, cheap, diffs = _build_signals(cfg, paths, frames, masks, strokes, app)
    strip, strip_region = (None, None)
    if not masks:
        # 旧路径（无 overlay.json）保留冻结的旧实现，结果与 M3 之前逐字节一致
        strip, strip_region = _legacy_caption_strip(sigs, cfg)
    if strip is not None:
        # 用"排除字幕条"的区域重算帧差（字幕每秒都在变，会把稳定性判定搅乱）
        keep = [True] * sigs[0][0].shape[0]
        for y in range(strip[0], strip[1]):
            keep[y] = False
        import numpy as np
        alpha = float(cfg["segment"].get("diff_alpha", 0.5))
        diffs = [0.0]
        for i in range(1, len(sigs)):
            ham = hamming(sigs[i - 1][1], sigs[i][1]) / 64.0
            a, b = sigs[i - 1][0], sigs[i][0]
            pix = float(np.abs(a[keep] - b[keep]).mean())
            diffs.append(alpha * ham + (1 - alpha) * pix)
        cheap = []
        inkp = ink_params(cfg.get("segment") or {})
        for s in sigs:
            sub = s[0][keep]
            cheap.append({"ink": ink_ratio(sub, inkp), "sharp": sharpness(sub)})
    runs = _stable_runs(diffs, fps, th, min_sec)
    cuts = _hard_cuts(diffs, cfg)
    if cfg["segment"].get("page_cut_only_cuts", True):
        # 只有"硬切"之后开启的稳定段才算新页；否则视为同页内的标注停顿
        kept = []
        for (s, e) in runs:
            look_back = max(0, s - int(fps * float(cfg["segment"].get("page_cut_lookback_sec", 1.5))))
            if any(cuts[i] for i in range(look_back, s + 1)):
                kept.append((s, e))
        if kept:
            print("[stable] 稳定段 %d 个 → 硬切后 %d 个页边界（其余按同页标注处理）" % (len(runs), len(kept)))
            runs = kept
    stable_mask = [False] * len(frames)
    for (s, e) in runs:
        for i in range(max(0, s), min(len(frames) - 1, e) + 1):
            stable_mask[i] = True
    print("[stable] 稳定态区间 %d 个（阈值=%.3f，最短=%.1fs）" % (len(runs), th, min_sec))

    # 用稳定态起点切分时间轴；开头的抖动区自成一页
    bounds = [0] + [s for (s, _) in runs if s > 0]
    bounds = sorted(set(bounds))
    raw_segments = []
    for bi, b in enumerate(bounds):
        end = bounds[bi + 1] if bi + 1 < len(bounds) else len(frames)
        idxs = list(range(b, max(end, b + 1)))
        idxs = [i for i in idxs if 0 <= i < len(frames)]
        if idxs:
            raw_segments.append(idxs)

    # 合并过短的段
    merged_idx = []
    for idxs in raw_segments:
        if merged_idx and (idxs[-1] - idxs[0] + 1) / fps < min_seg:
            merged_idx[-1].extend(idxs)
        else:
            merged_idx.append(list(idxs))

    segments = []
    for gi, idxs in enumerate(merged_idx, start=1):
        chosen, cands = _pick_frame(cfg, paths, frames, idxs, sigs, cheap, ocr, stable_mask, strip_region,
                                     ocr_masks=masks, transient_frames=transient)
        segments.append({
            "id": gi,
            "t_start": frames[idxs[0]]["t"],
            "t_end": round(frames[idxs[-1]]["t"] + 1.0 / fps, 3),
            "n_frames": len(idxs),
            "stable_sec": round(len(idxs) / fps, 2),
            "chosen": {"t": chosen["t"], "file": chosen["file"], "score": round(chosen["score"], 4),
                       "ocr_chars": chosen["ocr_chars"], "ocr_text": chosen.get("ocr_text", ""),
                       "ink": round(chosen["ink"], 4), "sharp": round(chosen["sharp"], 5),
                       "dhash": sigs[chosen["idx"]][1]},
            "candidates": [_cand_doc(c) for c in cands],
            "merged_from": [],
            "_idx": chosen["idx"],
            "_cands": cands,             # 内存里的完整候选记录（角色终选要用，不落盘）
        })

    # ---- 相邻页合并 ----
    # 实测（幻灯片课程）：真正同页的构建阶段，OCR 文本包含度 ≈ 1.00；
    # 换页时降到 <= 0.75。因此以"文本包含度 + 文本只增不减"为主判据，
    # 像素级"增量绘制"仅在 OCR 不可用时兜底。
    segcfg = cfg["segment"]
    max_hash = int(segcfg["merge_hash_dist"])
    contain_th = float(segcfg.get("merge_text_contain", 0.85))          # 旧判据（已弃用，仅兼容）
    gram_th = float(segcfg.get("merge_gram_contain", 0.45))
    contain_min = int(segcfg.get("merge_text_min_chars", 40))
    max_span = float(segcfg.get("merge_max_span_sec", 45.0))
    thin_chars = int(segcfg.get("absorb_thin_chars", 20))
    thin_max_sec = float(segcfg.get("absorb_thin_max_sec", 60.0))
    span_cap = float(segcfg.get("merge_max_span_sec", 120.0))
    thin_contain = float(segcfg.get("absorb_thin_contain", 0.5))
    use_additive = bool(segcfg.get("merge_additive", True))
    changed_max = float(segcfg.get("additive_changed_max", 0.6))
    old_ink_max = float(segcfg.get("additive_old_ink_max", 0.12))
    ocr_on = bool(getattr(ocr, "available", False))
    boiler = _boiler_re(segcfg.get("boilerplate_patterns"))

    def _why_same(a: dict, b: dict) -> str:
        """返回合并理由；空串表示判定为两页"""
        if hamming(a["chosen"]["dhash"], b["chosen"]["dhash"]) <= max_hash:
            return "hash"
        ta, tb = a["chosen"].get("ocr_text", ""), b["chosen"].get("ocr_text", "")
        if ocr_on:
            na, nb = _norm_text(ta), _norm_text(tb)
            if min(len(na), len(nb)) >= contain_min and _gram_containment(ta, tb, boiler=boiler) >= gram_th:
                return "gram"
            return ""
        if use_additive:
            if _is_additive(sigs[a["_idx"]][0], sigs[b["_idx"]][0], changed_max, old_ink_max):
                return "additive"
        return ""

    def _merge_into(keep: dict, drop: dict, reason: str):
        keep["t_start"] = min(keep["t_start"], drop["t_start"])
        keep["t_end"] = max(keep["t_end"], drop["t_end"])
        keep["n_frames"] += drop["n_frames"]
        keep["merged_from"] = keep.get("merged_from", []) + [drop["id"]] + drop.get("merged_from", [])
        keep["merge_reason"] = reason
        _union_cands(keep, drop)      # 合并后候选表要含双方，否则角色复核会漏掉被并走的整页候选

    out = []
    for seg in segments:
        if out:
            prev = out[-1]
            span_ok = (max(seg["t_end"], prev["t_end"]) - min(seg["t_start"], prev["t_start"])) <= max_span
            reason = _why_same(prev, seg) if span_ok else ""
            if reason:
                # 保留"信息更全"的一帧（构建阶段取终态）
                if seg["chosen"]["score"] > prev["chosen"]["score"]:
                    keep, drop = seg, prev
                    keep["t_start"], keep["t_end"] = prev["t_start"], seg["t_end"]
                    keep["n_frames"] += prev["n_frames"]
                    keep["merged_from"] = prev.get("merged_from", []) + [prev["id"]]
                    keep["merge_reason"] = reason
                    _union_cands(keep, drop)
                    out[-1] = keep
                else:
                    _merge_into(prev, seg, reason)
                continue
        out.append(seg)

    # 吸收"薄"段：文字极少、时长短、且与邻居文本高度相关 -> 判为动画中间态
    absorbed = []
    for i, seg in enumerate(out):
        chars = seg["chosen"].get("ocr_chars", 0)
        dur = seg["t_end"] - seg["t_start"]
        if chars >= thin_chars or dur > thin_max_sec:
            absorbed.append(seg)
            continue
        stxt = _norm_text(seg["chosen"].get("ocr_text", ""))
        target = None
        for cand in (absorbed[-1] if absorbed else None, out[i + 1] if i + 1 < len(out) else None):
            if cand is None or cand["chosen"].get("ocr_chars", 0) < thin_chars:
                continue
            if _containment(stxt, _norm_text(cand["chosen"].get("ocr_text", ""))) < thin_contain:
                continue
            span = max(seg["t_end"], cand["t_end"]) - min(seg["t_start"], cand["t_start"])
            if span > span_cap:      # 合并后不能超过单页时长上限
                continue
            target = cand
            break
        if target is None:
            absorbed.append(seg)
            continue
        target["t_start"] = min(target["t_start"], seg["t_start"])
        target["t_end"] = max(target["t_end"], seg["t_end"])
        target["merged_from"] = target.get("merged_from", []) + [seg["id"]]
        target.setdefault("merge_reason", "absorb-thin")
        _union_cands(target, seg)
    out = absorbed

    # 边界吸附到字幕句首
    if cfg["segment"].get("snap_to_transcript") and transcript:
        window = float(cfg["segment"]["snap_window_sec"])
        for i, seg in enumerate(out):
            if i == 0:
                continue
            snapped, delta = _snap_to_transcript(seg["t_start"], transcript, window)
            if delta is not None and snapped > out[i - 1]["t_start"]:
                seg["raw_t_start"] = seg["t_start"]
                seg["t_start"] = snapped
                seg["snap_delta"] = round(delta, 3)
        for i in range(len(out) - 1):
            out[i]["t_end"] = min(out[i]["t_end"], out[i + 1]["t_start"])
        out[-1]["t_end"] = round(frames[-1]["t"] + 1.0 / fps, 3)

    # 显式交接：切片层**不做同页合并判断**（实测三种廉价判据都不可靠），
    # 只把可测量的边界证据交给写作 agent，由它看图决定是否把相邻页视为同一张幻灯片的不同阶段。
    import numpy as _np
    for i, seg in enumerate(out):
        seg["boundary_evidence"] = None
        if i == 0:
            continue
        t1 = float(seg.get("t_start", 0.0))
        hi = int(t1 * float(fps))
        lo = max(0, hi - int(fps * 1.5))
        win = _np.asarray(diffs[lo:hi + 1], dtype=_np.float32) if hi > lo else _np.asarray([0.0], dtype=_np.float32)
        prev = out[i - 1]
        a = sigs[prev["_idx"]][0] if prev.get("_idx") is not None else None
        b = sigs[seg["_idx"]][0] if seg.get("_idx") is not None else None
        ev = {"delta_max": round(float(win.max()), 4)}
        if a is not None and b is not None:
            ch = _np.abs(a - b) > 0.08
            ev["unchanged_frac"] = round(1.0 - float(ch.mean()), 3)
            # 同样沿用**固定阈值的历史口径**（这两个数只是交给写手的旁证，不参与打分与页界：
            # 改成自适应会让"变化区域里原本有多少墨迹"这句解释在深色主题下换个含义，本次不动）
            ev["old_ink"] = round(float((a[ch] < 0.62).mean()), 3) if ch.any() else 1.0
            ev["new_ink"] = round(float((b[ch] < 0.62).mean()), 3) if ch.any() else 1.0
        seg["boundary_evidence"] = ev

    for i, seg in enumerate(out, start=1):
        seg["id"] = i

    # M4：角色分类 + 整页优先（放在合并/吸收/吸附之后，作用在**最终**的段与候选集上）
    if role_fn is not None:
        _apply_roles(cfg, paths, out, masks, role_fn, page_roles)

    for seg in out:
        seg.pop("_idx", None)
        seg.pop("_cands", None)
    return out