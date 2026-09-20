"""L14 术语表评审层：脚本只**提议**，由 agent/人**确认**后才作为约束下发。

背景（用户提问暴露的问题）：白名单/黑名单此前完全自动生成、无人复核，而它们会被注入写作 prompt 当约束——
错的白名单比没有更糟（本会话实测：RG(37 次)/RNG(4 次) 这类 ASR 误写一度被写进白名单；
自动蒸馏出的黑名单也混进过「多字：应删『一』」「偏 -> 让」这类噪声）。

流程：
  1. bnote glossary <url>          → 依据 profile 生成 <state>/glossary/<vid>.json（confirmed=false）+ 控制台清单
  2. agent/人 读该文件，删掉错的、补上漏的，把 confirmed 改成 true
  3. 之后渲染 prompt 时：confirmed=true 用评审版；**未确认时拒绝派单**（除非 --allow-unconfirmed），
     因为"未确认的白名单"会被写手当成"必须使用的写法"，错的白名单比没有更糟
  4. 该文件属于 state 根（跨集沉淀），删 cache 不会带走它
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def glossary_dir(cfg) -> Path:
    return Path(cfg["paths"]["state_root"]) / "glossary"


def _dedup(items) -> list:
    """按小写去重并保持顺序"""
    out, seen = [], set()
    for x in items:
        k = str(x).lower()
        if k and k not in seen:
            seen.add(k)
            out.append(str(x))
    return out


def path_of(cfg, vid: str) -> Path:
    return glossary_dir(cfg) / ("%s.json" % vid)


def load(cfg, vid: str) -> dict | None:
    p = path_of(cfg, vid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def propose(cfg, paths, profile: dict) -> Path:
    """把画像里的术语提议落成可评审文件（**不覆盖已有文件里的评审结论**）。

    为什么：术语表评审到一半（改了 terms_use/terms_avoid、还没 --confirm）时，
    任何一次 brief 都会走到这里；早期实现会按画像整份重建，把人工编辑静默冲掉（实测发生过）。
    现在只有「文件不存在」才生成初稿，之后一律只刷新 _proposal 提议区。
    要按新画像重建白名单，删掉 <state>/glossary/<vid>.json 再跑一次。
    """
    glossary_dir(cfg).mkdir(parents=True, exist_ok=True)
    p = path_of(cfg, paths.vid)
    old = load(cfg, paths.vid)
    if old is not None:
        # 已有文件（未确认也一样）：只更新提议区，保留人工结论
        old["_proposal"] = {
            "terms_use": [t for t in profile.get("domain_terms_predicted", [])]
                         + [i["term"] for i in (profile.get("terms_slide") or [])],
            "terms_avoid": profile.get("avoid") or {},
            "auto_avoid": profile.get("auto_avoid") or {},
        }
        old["_proposal_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        p.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    doc = {
        "vid": paths.vid,
        "title": profile.get("title"),
        "confirmed": False,
        "confirmed_at": None,
        "confirmed_by": None,
        "terms_use": _dedup([t for t in profile.get("domain_terms_predicted", [])]
                            + [i["term"] for i in (profile.get("terms_slide") or [])]),
        "terms_avoid": profile.get("avoid") or {},
        "_proposal": {
            "domain_predicted": profile.get("domain_terms_predicted", []),
            "from_slides": [{"term": i["term"], "count": i["count"]} for i in (profile.get("terms_slide") or [])],
            "speech_only": [{"term": i["term"], "count": i["count"]} for i in (profile.get("terms_speech_only") or [])],
            "auto_avoid": profile.get("auto_avoid") or {},
            "history_avoid": profile.get("avoid") or {},
        },
        "note": "请逐条核对：terms_use 是必须使用的写法；terms_avoid 是禁止出现的误写（键→正确写法）。"
                "确认后把 confirmed 改成 true。未确认时下发的是自动提议，prompt 里会标注『未确认』。",
    }
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def effective(cfg, paths, profile: dict) -> tuple[list[str], dict, bool]:
    """返回 (白名单, 黑名单, 是否已人工/agent 确认)"""
    g = load(cfg, paths.vid)
    if g and g.get("confirmed"):
        use = [str(x) for x in (g.get("terms_use") or [])]
        avoid = {str(k): str(v) for k, v in (g.get("terms_avoid") or {}).items()}
        return use, avoid, True
    # 未确认：用自动提议，但要标注
    use = [str(x) for x in ((g or {}).get("terms_use") or [])]
    if not use:
        use = [t for t in profile.get("domain_terms_predicted", [])] \
              + [i["term"] for i in (profile.get("terms_slide") or [])]
    avoid = dict((g or {}).get("terms_avoid") or profile.get("avoid") or {})
    return use, avoid, False


def review(cfg, paths, drop=None, add=None, avoid=None, confirm=False, by=None) -> dict:
    """把评审结论应用到 <state>/glossary/<vid>.json。

    drop: 从白名单删除的写法列表；add: 追加的写法；avoid: 追加的误写对（形如 'RNG->RAG'）；
    confirm: 置 confirmed=true（表示已由 agent 或人核对过）。
    """
    doc = load(cfg, paths.vid) or {}
    if not doc:
        raise SystemExit("还没有提议文件，请先跑 bnote brief（会自动生成）")
    use = [str(x) for x in doc.get("terms_use") or []]
    drop_set = {d.strip().lower() for d in (drop or [])}
    use = [t for t in use if t.lower() not in drop_set]
    for a in (add or []):
        if a and a not in use:
            use.append(a)
    av = dict(doc.get("terms_avoid") or {})
    for pair in (avoid or []):
        if "->" in pair:
            k, v = pair.split("->", 1)
            av[k.strip()] = v.strip()
    doc["terms_use"] = use
    doc["terms_avoid"] = av
    if confirm:
        doc["confirmed"] = True
        doc["confirmed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        doc["confirmed_by"] = by or "agent"
    p = path_of(cfg, paths.vid)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc
