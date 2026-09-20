"""L16 loop 层：派单台账 / 修复队列 / 审阅发现摄入 —— 让流程真正可 loop。

设计（v0.7.0）：
  * 工具负责"把修复件与收件人算好"，**投递由父 agent 做**（send_message 是 agent 工具，脚本调不到）；
  * 所有轮次与结果写进 _meta/loop.json，可复盘、可判断何时升级给人；
  * 审阅发现（review）与结构错误走**同一条 owner 路由**，因此复用同一套派修机制。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

SCOPES = ("L1", "L2", "L3", "L4", "L5", "L6")
SCOPE_DESC = {
    "L1": "单章内容问题 → 回原写手修",
    "L2": "跨章不一致 → 单个 agent 串行修（多写手并行会互相覆盖）",
    "L3": "章界问题 → 编排者决定重排范围（改章号＝局部重做）",
    "L4": "时间轴问题 → 工具跑 bnote retime，零 token",
    "L5": "管线问题（切错页/字幕缺口）→ pipeline：重切 + remap 后回派相关章",
    "L6": "note 问题 → 重派 note agent（note 是单次全局写作，重写比修补稳）",
}
OWNER_BY_SCOPE = {"L4": "pipeline", "L3": "manifest", "L5": "pipeline"}


def _load(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _save(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def dispatch(cfg, paths, owners: list[str], agent: str) -> dict:
    """把 owner 与 agent id 绑定（父 agent 派单后调用一次）。"""
    doc = _load(paths.dispatch, {"chapters": {}, "note": None, "history": []})
    for o in owners:
        cid = o.split(":", 1)[1] if ":" in o else o
        doc.setdefault("chapters", {})[cid] = {"agent": agent,
                                               "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    doc.setdefault("history", []).append({"owners": owners, "agent": agent,
                                          "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _save(paths.dispatch, doc)
    return doc


def agent_for(paths, owner: str):
    doc = _load(paths.dispatch, {})
    if owner in ("manifest", "pipeline"):
        return None
    cid = owner.split(":", 1)[1] if ":" in owner else owner
    if owner.startswith("note"):
        return (doc.get("note") or {}).get("agent")
    return ((doc.get("chapters") or {}).get(cid) or {}).get("agent")


def pending(cfg, paths) -> list[dict]:
    """未修的 owner 及其条目（结构错误 + major 审阅发现）。"""
    doc = _load(paths.validation, {})
    items = list(doc.get("errors") or [])
    items += [r for r in (doc.get("review") or []) if str(r.get("severity", "major")) == "major"]
    by_owner: dict = {}
    for e in items:
        by_owner.setdefault(e.get("owner", "?"), []).append(e)
    done = _done_map(paths)
    out = []
    for owner, errs in sorted(by_owner.items()):
        out.append({"owner": owner, "count": len(errs),
                    "agent": agent_for(paths, owner),
                    "done": bool(done.get(owner)),
                    "sample": errs[0].get("message", "")[:120]})
    return out


def _done_map(paths) -> dict:
    doc = _load(paths.loop, {"done": {}})
    return doc.get("done") or {}


def mark(paths, owner: str, result: str, round_no: int | None = None, note: str = "") -> dict:
    doc = _load(paths.loop, {"rounds": [], "done": {}})
    doc.setdefault("rounds", []).append({"owner": owner, "result": result,
                                         "round": round_no,
                                         "agent": agent_for(paths, owner),
                                         "note": note,
                                         "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    if result == "fixed":
        doc.setdefault("done", {})[owner] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(paths.loop, doc)
    return doc


def next_dispatch(cfg, paths, fix_dir: Path) -> list[dict]:
    """算好"发给谁、发什么"（父 agent 照抄执行）。

    轮次上限取 `loop.max_rounds`（默认 2）；轮次既看台账里记的 round，也看台账条数，
    因此 `fix --done` 未传 --round 时轮次同样递进。
    """
    max_rounds = int(((cfg or {}).get("loop") or {}).get("max_rounds", 2))
    doc = _load(paths.loop, {"rounds": []})
    rounds = {}
    counts = {}
    for r in doc.get("rounds", []):
        o = r.get("owner")
        rounds[o] = max(rounds.get(o, 0), int(r.get("round") or 0))
        # 每写一条台账 = 为这个 owner 派修过一次。fix --done 不带 --round 时只记 result，
        # 所以轮次必须同时看条数，否则 rounds 恒为 0、上限永不触发（0.8.6 修）。
        counts[o] = counts.get(o, 0) + 1
    out = []
    for item in pending(None, paths):
        o = item["owner"]
        r = max(rounds.get(o, 0), counts.get(o, 0)) + 1
        slug = o.replace(":", "_")
        fix_file = fix_dir / ("%s.md" % slug)
        out.append({
            "owner": o,
            "agent": item["agent"],
            "round": r,
            "escalate": r > max_rounds,
            "fix_file": str(fix_file),
            "deliver_hint": ("默认把 %s 发回该 owner 的原写作 agent（send_message 唤醒它）；"
                             "只有原 agent 不可用或被污染时，才改派一个新的干净子代理执行" % fix_file),
            "message": ("读 %s 并按其中要求逐条修复；改完跑 bnote check --chapter %s 并把结果回报。"
                        % (fix_file, o.split(":")[-1])),
            "count": item["count"],
        })
    return out


def review_ingest(cfg, paths, findings_path: Path) -> dict:
    """摄入审阅发现（JSON 数组）→ 按 scope 归一 owner → 并入 validation.json 的 review 段。"""
    raw = json.loads(Path(findings_path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("findings") or raw.get("items") or []
    accepted, rejected = [], []
    for item in raw:
        scope = str(item.get("scope") or "").upper()
        if scope not in SCOPES:
            rejected.append({"item": item, "why": "scope 必填且须是 %s" % "/".join(SCOPES)})
            continue
        what = str(item.get("what") or item.get("message") or "").strip()
        if not what:
            rejected.append({"item": item, "why": "缺 what"})
            continue
        owner = OWNER_BY_SCOPE.get(scope) or str(item.get("owner") or "")
        if not owner:
            rejected.append({"item": item, "why": "scope=%s 需要 owner（chapter:NN / note）" % scope})
            continue
        rec = {"level": "review", "scope": scope, "owner": owner,
               "severity": str(item.get("severity") or "major").lower(),
               "chapter": owner.split(":", 1)[1] if owner.startswith("chapter:") else None,
               "message": what,
               "evidence": str(item.get("evidence") or ""),
               "fix_hint": str(item.get("fix_hint") or SCOPE_DESC.get(scope, ""))}
        accepted.append(rec)
    doc = _load(paths.validation, {})
    review = list(doc.get("review") or []) + accepted
    errors = doc.get("errors") or []
    warns = doc.get("warnings") or []
    from . import manifest as manifest_layer
    manifest_layer.write_validation(paths, errors, warns, review=review,
                                   scope=doc.get("scope") or "full")
    return {"accepted": accepted, "rejected": rejected}
