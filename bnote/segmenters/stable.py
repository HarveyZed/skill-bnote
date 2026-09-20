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
"""
from __future__ import annotations

import re
from collections import Counter

from .framesig import frame_diff, hamming, ink_ratio, sharpness, signature


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
    这种字符层面的巧合不敏感 —— 实测 p24 两张完全不同的页字符集包含度 0.88，4-gram 只有 0.17。
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
    """
    import numpy as np
    d = np.abs(prev_small - cur_small)
    mask = d > 0.15
    changed_ratio = float(mask.mean())
    if changed_ratio == 0.0 or changed_ratio > changed_max:
        return False
    old_ink = float((prev_small[mask] < 0.62).mean()) if mask.any() else 1.0
    return old_ink <= old_ink_max


def _build_signals(cfg, paths, frames):
    region = tuple(cfg["frames"].get("region") or (0, 0, 1, 1))
    sigs, cheap = [], []
    for f in frames:
        p = paths.frames / f["file"]
        s = signature(p, region)
        sigs.append(s)
        cheap.append({"ink": ink_ratio(s[0]), "sharp": sharpness(s[0])})
    alpha = float(cfg["segment"].get("diff_alpha", 0.5))
    diffs = [0.0] + [frame_diff(sigs[i - 1], sigs[i], alpha) for i in range(1, len(sigs))]
    return sigs, cheap, diffs


def _detect_caption_strip(sigs, cfg):
    """识别烧进画面的字幕条：底部横带的变化频率远高于中部时，认为它是口播字幕而非幻灯片内容。

    返回 (行区间 [y0,y1), 相对坐标 region 或 None)
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


def _pick_frame(cfg, paths, frames, idxs, sigs, cheap, ocr, stable_mask=None, ocr_region=None):
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
        scored.append(m)

    span = max(1, len(idxs) - 1)
    base = min(idxs)
    for m in scored:
        ocr_info = ocr.text(paths.frames / m["file"], region=ocr_region) if ocr else {"text": "", "chars": 0, "boxes": 0}
        m["ocr_chars"] = ocr_info["chars"]
        m["ocr_boxes"] = ocr_info["boxes"]
        m["ocr_text"] = ocr_info["text"]
        m["score"] = _score_candidate(cfg, m, ocr_info, (m["idx"] - base) / span)
    scored.sort(key=lambda m: m["score"], reverse=True)

    # OCR 是概率性识别：对前两名候选各跑一次，按行合并（模糊去重）得到更完整的页面文字
    if ocr and cfg["segment"].get("ocr_consensus", True) and len(scored) > 1:
        import difflib
        lines = []
        for m in scored[:2]:
            info = ocr.text(paths.frames / m["file"], region=ocr_region) if ocr_region else ocr.text(paths.frames / m["file"])
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


def segment(cfg, paths, frames, transcript, ocr):
    fps = float(cfg["frames"]["fps"])
    th = float(cfg["segment"]["diff_threshold"])
    min_sec = float(cfg["segment"]["stable_min_sec"])
    min_seg = float(cfg["segment"]["min_seg_sec"])

    sigs, cheap, diffs = _build_signals(cfg, paths, frames)
    strip, strip_region = _detect_caption_strip(sigs, cfg)
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
        for s in sigs:
            sub = s[0][keep]
            cheap.append({"ink": ink_ratio(sub), "sharp": sharpness(sub)})
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
        chosen, cands = _pick_frame(cfg, paths, frames, idxs, sigs, cheap, ocr, stable_mask, strip_region)
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
            "candidates": [{"t": c["t"], "file": c["file"], "score": round(c["score"], 4),
                            "ocr_chars": c["ocr_chars"], "ink": round(c["ink"], 4)} for c in cands],
            "merged_from": [],
            "_idx": chosen["idx"],
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
            ev["old_ink"] = round(float((a[ch] < 0.62).mean()), 3) if ch.any() else 1.0
            ev["new_ink"] = round(float((b[ch] < 0.62).mean()), 3) if ch.any() else 1.0
        seg["boundary_evidence"] = ev

    for i, seg in enumerate(out, start=1):
        seg["id"] = i
        seg.pop("_idx", None)
    return out