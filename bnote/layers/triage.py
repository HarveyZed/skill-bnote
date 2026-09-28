"""L4.7 判型层（M5，契约见 VISION-PLAN §3.6-4）：开跑前给"该走 slides 还是 stream"的建议 + 证据。

产物 out/<vid>/_meta/mode_hint.json（**不放时间戳**，两次跑逐字节一致）：
  schema / algo / vid / suggest / confidence / levels / evidence / segments / applicability / note

三级证据，逐级加钱（§3.6-4）：
  * T1 元信息（**永远有**、零成本）：标题 / 分P / 分区 / 标签的关键词命中，是**弱证据**；
    只打印一行到 **stderr**（stdout 一字不变 —— bnote meta 的 stdout 以一段 JSON 收尾，
    挂在它后面会破坏"把 stdout 当 JSON 读"的用法）；
  * T2 取样量测：**只读已存在的** measure.json（取样包优先；只有整片量测存在时才退而用它并
    标明来源）。**绝不**为了判型去解码整片 —— 判型不值那个成本（复核 watch 2）；
  * T3 取样面板：由 CLI 先跑 sheet --basis sample 落盘，再把结果传进来（层与层不互相 import，P1），
    所以 mode_hint 必然写在面板之后 —— evidence.t3_panel 不会指向不存在的文件（复核 watch 1）。

只建议、不自动改行为：显式 --mode slides|stream 覆盖（§3.6-4.2 / §10-⑤）。
"""
from __future__ import annotations

import time

from ..tools import resolve_basis

SCHEMA = "bnote-mode-hint/1"
ALGO = "bnote-mode-hint/1"
MODE_HINT_NAME = "mode_hint.json"
NOTE = "只建议，不自动改行为：显式 --mode slides|stream 覆盖"
TIERS = ("t1", "t2", "t3")

DEFAULTS = {
    "enabled": True,
    "default_level": "t1",
    "t3_tiles": 12,
    "cuts_per_min_high": 3.0,
    "cuts_per_min_low": 0.5,
    "freeze_ratio_page": 0.60,
    "motion_mean_high": 0.05,
    "slides_keywords": ["课件", "幻灯片", "PPT", "讲义", "白板"],
    "stream_keywords": ["访谈", "播客", "口播", "直播", "聊天", "实录", "课堂", "滚动"],
}


def params(cfg: dict) -> dict:
    """生效参数（默认值在这里；config/default.toml 的 [triage] 是同一份口径）。"""
    t = cfg.get("triage") or {}
    return {k: t.get(k, v) for k, v in DEFAULTS.items()}


def _rel(paths, path) -> str:
    try:
        return str(path.relative_to(paths.root))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- T1 元信息（弱证据）

def t1(meta: dict, cfg: dict) -> dict:
    """T1 证据：元信息关键词命中。**只是弱证据**——它给建议加一分，不构成判据。"""
    p = params(cfg)
    text = {"title": str(meta.get("title") or ""),
            "part": str(meta.get("part") or ""),
            "tname": str(meta.get("tname") or "")}
    tags = [str(x) for x in (meta.get("tags") or [])]
    hits = []
    for key, s in text.items():
        for w in p["slides_keywords"]:
            if w and w in s:
                hits.append({"key": "%s:%s" % (key, w), "strength": "weak", "lean": "slides"})
        for w in p["stream_keywords"]:
            if w and w in s:
                hits.append({"key": "%s:%s" % (key, w), "strength": "weak", "lean": "stream"})
    for tg in tags:
        for w in p["slides_keywords"] + p["stream_keywords"]:
            if w and w in tg:
                hits.append({"key": "tag:%s" % tg, "strength": "weak",
                             "lean": "slides" if w in p["slides_keywords"] else "stream"})
    return {"duration": meta.get("duration"), "title": text["title"], "part": text["part"],
            "tname": text["tname"], "tags": tags, "hits": hits}


def t1_line(meta: dict, cfg: dict) -> str:
    """T1 的一行提示（调用方打到 **stderr**）。[triage].enabled=false 时连这行也不打。"""
    if not params(cfg).get("enabled", True):
        return ""
    ev = t1(meta, cfg)
    sl = sum(1 for h in ev["hits"] if h["lean"] == "slides")
    st = sum(1 for h in ev["hits"] if h["lean"] == "stream")
    lean = "slides" if sl > st else "stream"      # 平局按 §3.6-4.3 默认偏 stream
    page = str(meta.get("part") or "") or str(meta.get("title") or "")
    return ("[triage] T1 建议 %s（弱证据：元信息 %s；页式词 %d / 连续型词 %d）"
            " ｜ 要看画面证据：bnote triage <URL> --page N --level t3"
            % (lean, page[:28], sl, st))


# ---------------------------------------------------------------- T2 取样量测（只读）

def _window_metrics(buckets: list, cuts: list, sections: list) -> list:
    """把逐秒桶与切点按**取样窗口**归拢 —— segments 里的每个数字都要能追到这里（复核 watch 3）。"""
    out = []
    for i, s in enumerate(sections):
        lo, hi = float(s["media_from"]), float(s["media_to"])
        mine = [b for b in buckets if lo <= float(b.get("s", -1)) < hi]
        mot = [float(b.get("motion") or 0) for b in mine]
        ncut = len([c for c in cuts if lo <= float(c.get("from", -1)) < hi])
        dur = hi - lo
        out.append({"window": i, "orig_from": s["orig_from"], "orig_to": s["orig_to"],
                    "media_sec": round(dur, 3),
                    "cut_count": ncut,
                    "cuts_per_min": round(ncut / (dur / 60.0), 3) if dur > 0 else None,
                    "motion_mean": round(sum(mot) / len(mot), 6) if mot else None})
    return out


def t2(cfg: dict, paths) -> dict:
    """T2 证据：**只读已存在的**量测产物，取样包优先。

    取样包量测不在、但**整片量测已经存在**时用它并标明来源（那是已经付过的成本，不是新解码）；
    两者都没有 → insufficient + 原因，**绝不**为了判型去跑 measure（复核 watch 2）。
    """
    sample_rel = "cache/%s/sample/measure.json" % paths.vid
    full_rel = "cache/%s/measure.json" % paths.vid
    for path, rel, kind in ((paths.sample_measure, sample_rel, "sample"),
                            (paths.measure, full_rel, "full")):
        doc = paths.read_json(path, None)
        if not isinstance(doc, dict) or not doc.get("buckets"):
            continue
        src = doc.get("source") or {}
        stats = doc.get("stats") or {}
        buckets = doc.get("buckets") or []
        cuts = (doc.get("events") or {}).get("cuts") or []
        duration = float(src.get("duration") or 0) or float(len(buckets))
        mot = [float(b.get("motion") or 0) for b in buckets]
        ev = {"source": rel, "kind": kind,
              "duration": round(duration, 3),
              "cut_count": len(cuts),
              "cuts_per_min": round(len(cuts) / (duration / 60.0), 3) if duration > 0 else None,
              "freeze_ratio": stats.get("freeze_ratio"),
              "motion_mean": round(sum(mot) / len(mot), 6) if mot else None}
        if kind == "sample":
            cov = (paths.read_json(paths.sample_index, None) or {}).get("coverage") or {}
            ev["sampled_ratio"] = cov.get("sampled_ratio")
            ev["uncovered_max_gap_sec"] = cov.get("uncovered_max_gap_sec")
            ev["windows"] = _window_metrics(buckets, cuts,
                                            (paths.read_json(paths.sample_index, None) or {}).get("sections") or [])
        else:
            ev["reason"] = "取样包量测不存在，用的是**已存在的整片量测**（没有为此解码）"
        ev["status"] = "ok"      # 扁平：§3.6-4 的冻结示例就是把 source/cuts_per_min/... 直接放这一层
        return ev
    return {"status": "insufficient", "reason":
            "既没有取样包量测（%s）也没有整片量测（%s）：先跑 bnote sample" % (sample_rel, full_rel)}


# ---------------------------------------------------------------- T3 取样面板（由 CLI 先落盘）

def t3(sheet_doc: dict | None, paths) -> dict:
    """T3 证据：由 CLI 传入**已经落盘**的取样面板清单（层与层不互相 import，P1）。"""
    if sheet_doc is None:
        # 区分"没跑 T3"与"根本没有取样包"——后者才是使用者下一步该修的东西（措辞不能混）
        try:
            resolve_basis(paths, "sample")
            reason = "没跑 T3（只有 --level t3 才会产出取样面板；取样包在）"
        except SystemExit as exc:
            reason = str(exc)
        return {"status": "insufficient", "reason": reason}
    names = [str(s.get("name")) for s in (sheet_doc.get("sheets") or [])]
    if not names:
        return {"status": "insufficient", "reason": "取样面板没有产出（sheet_sample.json 里没有 sheets）"}
    return {"status": "ok",
            "basis": sheet_doc.get("basis"),
            "sheets": names,
            "tiles": len(sheet_doc.get("tiles") or []),
            "sheet_json": "out/%s/_meta/sheet_sample.json" % paths.vid,
            "coverage": sheet_doc.get("coverage")}


# ---------------------------------------------------------------- 建议（阈值全在 [triage]，默认偏 stream）

def _score(t1ev: dict, t2ev: dict | None, cfg: dict) -> dict:
    """给"页式内容"与"连续画面"各记分，并留下每一条来自哪个数字（可复核）。"""
    p = params(cfg)
    s = {"slides": 0, "stream": 0}
    why = []
    hits = (t1ev or {}).get("hits") or []
    sl = sum(1 for h in hits if h.get("lean") == "slides")
    st = sum(1 for h in hits if h.get("lean") == "stream")
    if sl:
        s["slides"] += 1
        why.append("T1 元信息命中页式词 %d 个" % sl)
    if st:
        s["stream"] += 1
        why.append("T1 元信息命中连续型词 %d 个" % st)
    if t2ev:
        cpm, fr, mm = t2ev.get("cuts_per_min"), t2ev.get("freeze_ratio"), t2ev.get("motion_mean")
        if cpm is not None:
            if cpm >= float(p["cuts_per_min_high"]):
                s["slides"] += 1
                why.append("T2 切点密度 %.2f/min ≥ %.2f（离散翻页特征）" % (cpm, float(p["cuts_per_min_high"])))
            if cpm <= float(p["cuts_per_min_low"]):
                s["stream"] += 1
                why.append("T2 切点密度 %.2f/min ≤ %.2f（几乎无硬切）" % (cpm, float(p["cuts_per_min_low"])))
        if fr is not None and float(fr) >= float(p["freeze_ratio_page"]):
            s["slides"] += 1
            why.append("T2 冻结占比 %.2f ≥ %.2f（大段静止）" % (float(fr), float(p["freeze_ratio_page"])))
        if mm is not None and float(mm) >= float(p["motion_mean_high"]):
            s["stream"] += 1
            why.append("T2 逐秒运动均值 %.3f ≥ %.3f（画面持续在动）" % (float(mm), float(p["motion_mean_high"])))
    suggest = "slides" if s["slides"] - s["stream"] >= 2 else "stream"   # 默认偏 stream（§3.6-4.3）
    conf = round(min(0.9, 0.4 + 0.15 * abs(s["slides"] - s["stream"])), 2)
    if not t2ev:
        conf = min(conf, 0.5)
    return {"suggest": suggest, "confidence": conf, "why": why, "score": s,
            "tie_break": ("按默认偏 stream 定" if s["slides"] == s["stream"] else None)}


def _segments(t2ev: dict | None, cfg: dict, suggest: str) -> list:
    """混合型才给分段：取样各窗建议不一致时才落 segments，且每段都带自己的数字（watch 3）。"""
    if not t2ev or not t2ev.get("windows"):
        return []
    p = params(cfg)
    out = []
    for w in t2ev["windows"]:
        cpm = w.get("cuts_per_min") or 0.0
        mm = w.get("motion_mean") or 0.0
        s_slides = 1 if cpm >= float(p["cuts_per_min_high"]) else 0
        s_stream = (1 if cpm <= float(p["cuts_per_min_low"]) else 0) + (
            1 if mm >= float(p["motion_mean_high"]) else 0)
        out.append({"window": w["window"], "from": w["orig_from"], "to": w["orig_to"],
                    "suggest": "slides" if s_slides - s_stream >= 2 else "stream",
                    "why": "窗内切点 %d 个（%.2f/min）、逐秒运动均值 %.3f"
                           % (w["cut_count"], cpm, mm)})
    kinds = {s["suggest"] for s in out}
    return out if len(kinds) > 1 else []      # 单一形态：空数组（§7.2）


def run(cfg: dict, paths, level: str = "t1", force: bool = False, sheet_doc: dict | None = None) -> dict:
    """bnote triage 的入口：跑 T1（+可选 T2/T3）→ 写 out/<vid>/_meta/mode_hint.json。

    **先落盘、后写清单**：T3 的面板由调用方（CLI）在进来之前跑完，这里只读它的清单。
    """
    dest = paths.meta_dir() / MODE_HINT_NAME
    if dest.exists() and not force:
        doc = paths.read_json(dest, None) or {}
        print("[triage] 已存在，跳过（--force 重跑）：%s" % _rel(paths, dest))
        print("[triage] 建议 %s（置信 %.2f）｜ 层级 %s" % (doc.get("suggest"), doc.get("confidence") or 0,
                                                          "/".join(doc.get("levels") or [])))
        return doc
    t0 = time.monotonic()
    meta = paths.read_json(paths.meta, None) or {}
    levels = ["t1"]
    if level in ("t2", "t3"):
        levels.append("t2")
    if level == "t3":
        levels.append("t3")
    t1ev = t1(meta, cfg)
    t2r = t2(cfg, paths) if "t2" in levels else None
    t3r = t3(sheet_doc, paths) if "t3" in levels else None
    t2ev = t2r if (t2r or {}).get("status") == "ok" else None
    sc = _score(t1ev, t2ev, cfg)
    evidence = {"t1_meta": t1ev,
                "reasoning": {"why": sc["why"], "score": sc["score"], "tie_break": sc["tie_break"]}}
    if t2r is not None:
        evidence["t2_measure"] = t2r
    if t3r is not None:
        evidence["t3_panel"] = t3r
    doc = {"schema": SCHEMA, "algo": ALGO, "vid": paths.vid,
           "suggest": sc["suggest"], "confidence": sc["confidence"],
           "levels": levels, "evidence": evidence,
           "segments": _segments(t2ev, cfg, sc["suggest"]),
           "applicability": {"t1": "ok",
                             "t2": (t2r or {}).get("status", "skipped"),
                             "t3": (t3r or {}).get("status", "skipped")},
           "note": NOTE}
    paths.write_json(dest, doc)
    print("[triage] 建议 %s（置信 %.2f）｜ 层级 %s ｜ 依据：%s"
          % (doc["suggest"], doc["confidence"], "/".join(levels),
             "；".join(sc["why"]) if sc["why"] else "只有元信息（弱证据）"))
    if t2ev:
        print("[triage] T2 来源 %s（%s）：切点 %s/min ｜ 冻结占比 %s ｜ 运动均值 %s ｜ 取样覆盖 %s"
              % (t2ev["source"], t2ev["kind"], t2ev.get("cuts_per_min"), t2ev.get("freeze_ratio"),
                 t2ev.get("motion_mean"), t2ev.get("sampled_ratio")))
    elif t2r is not None:
        print("[triage] T2 %s：%s" % (t2r.get("status"), t2r.get("reason") or ""))
    if t3r is not None:
        if t3r.get("status") == "ok":
            print("[triage] T3 面板 %s（%d 格）：%s" % (t3r["sheets"], t3r["tiles"], t3r["sheet_json"]))
        else:
            print("[triage] T3 %s：%s" % (t3r.get("status"), t3r.get("reason") or ""))
    print("[triage] %.1fs → %s（**只建议，不自动改行为**）" % (time.monotonic() - t0, _rel(paths, dest)))
    return doc
