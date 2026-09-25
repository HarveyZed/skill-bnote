"""L11 派单层：把写作契约渲染成可直接发给 subagent 的 prompt 文件。

设计意图：约束应当**注入 agent 的 prompt**，而不是事后 grep。
契约模板是 skill 资产，落在 <contracts_dir>（默认 <skill 根>/references/contracts），
渲染后放 out/<vid>/_meta/ 下，派单时直接引用文件路径，避免每次手写导致约束漂移。

v0.7.0 起：
  * 模板目录可外部配置（paths.contracts_dir）；
  * agent 决策节点加门禁：术语表未确认时**拒绝派章节写作**（除非 allow_unconfirmed）；
  * 每个 agent 派单 prompt 里带着可直接复制的命令（含数据根与解释器），避免路径漂移。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import manifest as manifest_layer
from . import glossary as glossary_layer
from . import profile as profile_layer


def _dur(sec) -> str:
    sec = int(sec or 0)
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def _chapter_table(man: dict) -> str:
    rows = ["| 章 | 标题 | 时间范围 | slide | 正文文件 |", "|---|---|---|---|---|"]
    for ch in man.get("chapters", []):
        a, b = ch.get("range", ["", ""])
        rows.append("| %s | %s | %s-%s | %s | %s |" % (
            ch.get("id"), ch.get("title"), a, b,
            ", ".join("%04d" % n for n in ch.get("slides") or []), ch.get("body")))
    return "\n".join(rows)


def _chars(p: Path) -> int:
    if not p.exists():
        return 0
    return len("".join(p.read_text(encoding="utf-8").split()))


def _load_json(p):
    import json
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _cmd(cfg, paths) -> str:
    """给 agent 的可复制命令前缀：把数据根与入口钉死，避免 cwd 不同导致读错目录。"""
    skill = cfg["paths"]["skill_root"]
    return "BNOTE_ROOT=%s %s/scripts/bnote" % (cfg["paths"]["root"], skill)


def _sync_hint(cfg, paths, chapter_ids: str) -> str:
    return "BNOTE_ROOT=%s %s check --page %s --chapter %s" % (
        cfg["paths"]["root"], _cmd(cfg, paths), paths.vid.split("_p")[-1], chapter_ids)


def _bullets(items, empty="（无）") -> str:
    items = [str(x) for x in (items or []) if str(x).strip()]
    return "\n".join("- %s" % s for s in items) if items else "- %s" % empty


def zone_of(meta: dict) -> str:
    """分区名：接口有时返回空串，退回 tid（本课实测 tname='' 但 tid=231）。"""
    return meta.get("tname") or ("tid=%s" % meta.get("tid") if meta.get("tid") else "-")


def video_meta_block(cfg: dict, meta: dict) -> str:
    """视频页元信息块（简介/UP 置顶评论/标签/分区）——给写手当背景，明确不许当课程内容写进正文。

    截断长度：简介取 `prompt.desc_cap`（默认 1500），置顶评论取 `prompt.top_comment_cap`（默认 1000）；0 = 不截断。
    置顶评论与简介**同一权重**：都是人写的背景信息（作者常把资料链接、勘误、答疑补充在这里，
    因为改简介等于重新发布视频）；但同样不许当课程内容。
    """
    pc = (cfg or {}).get("prompt") or {}
    cap = int(pc.get("desc_cap", 1500))
    tcap = int(pc.get("top_comment_cap", 1000))
    desc = (meta.get("desc") or "").strip()
    if cap > 0 and len(desc) > cap:
        desc = desc[:cap] + "…（简介过长已截断）"
    topc = meta.get("top_comment") or {}
    top = ((topc.get("text") if isinstance(topc, dict) else str(topc or "")) or "").strip()
    if tcap > 0 and len(top) > tcap:
        top = top[:tcap] + "…（置顶评论过长已截断）"
    tags = ", ".join(meta.get("tags") or []) or "（无）"
    lines = [
        "- 标题: %s" % (meta.get("part") or meta.get("title") or ""),
        "- 合集: %s ｜ 分区: %s" % (meta.get("title") or "", zone_of(meta)),
        "- UP: %s ｜ 发布: %s" % (meta.get("owner") or "", _pubdate(meta.get("pubdate"))),
        "- 标签: %s" % tags,
    ]
    if desc:
        lines += ["- 视频简介（原文，可能有推广与资料链接）:", "", desc]
    if top:
        who = (topc.get("uname") or "").strip() if isinstance(topc, dict) else ""
        lines += ["- UP 主置顶评论（原文%s，作者常在这里补资料链接、勘误与答疑）:"
                  % ("，作者 %s" % who if who else ""), "", top]
    lines += [
        "",
        "> 简介与置顶评论都是**背景信息**：可以用它们判断这一集的主题、术语写法、是否属于某个系列，",
        "> 也可以据此找到作者给的资料链接；",
        "> 但**不许**把它们当成课程内容写进讲义/笔记正文（讲师在视频里没说的，就不算课程讲的）。",
    ]
    return "\n".join(lines)


def _pubdate(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
    except Exception:
        return "-"


def render(cfg, paths, meta: dict, stage: str = "both", allow_unconfirmed: bool = False,
           owner: str | None = None, scope: str = "all", focus: str = "fidelity",
           round_no: int = 1) -> dict:
    tpl_dir = Path(cfg["paths"]["contracts_dir"])
    man = manifest_layer.load(paths) or {"chapters": []}
    prof = profile_layer.build(cfg, paths, meta,
                               _load_json(paths.subtitle / "transcript.json"),
                               _load_json(paths.out / "slides.json"))
    glossary_layer.propose(cfg, paths, prof)
    use, avoid, confirmed = glossary_layer.effective(cfg, paths, prof)
    prof["_effective_avoid"] = avoid
    lc = _chars(paths.out / "lecture.md")
    nc = cfg.get("note", {})
    ratio = float(nc.get("max_ratio", 0.2))
    page = paths.vid.split("_p")[-1]
    ids = ", ".join(str(c.get("id")) for c in man.get("chapters", [])) or "（待划分）"

    if stage in ("chapter", "both") and not confirmed and not allow_unconfirmed:
        raise SystemExit(
            "术语表未确认，拒绝派发章节写作（agent 决策节点必须生效）。\n"
            "  先审： %s glossary --page %s\n"
            "  再确认： %s glossary --page %s --drop <噪声词> --add <漏词> --avoid \"X->Y\" --confirm --by <你>\n"
            "  或明确接受自动提议： bnote brief ... --allow-unconfirmed"
            % (_cmd(cfg, paths), page, _cmd(cfg, paths), page))

    text_mode = (not (man.get("chapters") or [])) and (paths.meta_dir() / "paragraphs.json").exists()
    if text_mode:
        slide_phrase = "无幻灯片（信息流模式）"
        chapter_table = "（信息流模式没有章节：正文按工具给的段落锚点组织）"
        evidence_sources = "`transcript.md`（**ASR 产物**：平台 AI 轨与 UP 上传的 CC 都是语音识别，都可能没校对）与**人写的元信息**（标题/简介/标签）；本集没有幻灯片，没有图可看"
        prof_mode = "text"
    else:
        slide_phrase = "slide %s 页" % (man.get("slide_count") or len(list((paths.out / "slides").glob("*.jpg"))))
        chapter_table = _chapter_table(man)
        evidence_sources = "页面图上的文字（讲师自己的课件，最接近 ground truth，要引用必须自己看图）与 `transcript.md`（**ASR 产物**，可能未校对——是证据，不是准绳）"
        prof_mode = "slides"

    ctx = {
        "OUT_DIR": str(paths.out),
        "TITLE": str((man.get("video") or {}).get("title") or meta.get("part") or ""),
        "DURATION": _dur(meta.get("duration")),
        "SLIDE_COUNT": slide_phrase,
        "KP_MIN": str(cfg.get("manifest", {}).get("keypoints_min", 2)),
        "CHAPTER_TABLE": chapter_table,
        "EVIDENCE_SOURCES": evidence_sources,
        "CHAPTER_IDS": ids,
        "NOTE_RATIO": "%.0f%%" % (ratio * 100),
        "NOTE_BUDGET": str(int(lc * ratio)) if lc else "（讲义生成后确定）",
        "LECTURE_CHARS": str(lc) if lc else "（待生成）",
        "CHECK_CMD": _sync_hint(cfg, paths, ids),
        "VIDEO_META": video_meta_block(cfg, meta),
        "TERM_POLICY": profile_layer.term_policy(prof, reviewed=use, confirmed=confirmed, mode=prof_mode),
        "FORMAT_RULES": profile_layer.format_rules(prof, mode=prof_mode),
        "STYLE_NOTE": prof.get("style", ""),
        # note 的结构参数全部来自 config，不再写死在模板里
        "HEAD_SECTIONS": _bullets([_head_desc(nc, s) for s in (nc.get("head_sections") or [])]),
        "TAIL_SECTIONS": _bullets([_tail_desc(nc, s) for s in (nc.get("tail_sections") or [])]),
        "FIELD_LABELS": " / ".join(nc.get("field_labels") or []),
        "NODE_FIELDS": _node_fields(nc),
        "NODE_RANGE": "%s-%s" % (nc.get("min_nodes", 6), nc.get("max_nodes", 30)),
        # review / fix
        "ROUND": str(round_no),
        "SCOPE_DESC": scope,
        "FOCUS_DESC": ("fidelity：漏讲 / 编造 / 图注与图不符（逐段对照 transcript）"
                       if focus == "fidelity" else
                       "depth：提炼是否到位、注释是否有增量价值、钩子是否真的可延展"),
        "FINDINGS_PATH": str(paths.meta_dir() / ("review_%d.json" % round_no)),
        "OWNER": owner or "",
        "ERRORS": _errors_for(paths, owner),
    }

    out = {}
    stages = ["chapter", "note"] if stage == "both" else [stage]
    for st in stages:
        tpl = tpl_dir / ("%s.md" % {"chapter": "chapter_writer", "note": "note_synth",
                                    "fix": "fix", "review": "review"}[st])
        if not tpl.exists():
            raise SystemExit("契约模板缺失：%s（检查 paths.contracts_dir）" % tpl)
        text = tpl.read_text(encoding="utf-8")
        for k, v in ctx.items():
            text = text.replace("{{%s}}" % k, str(v))
        if st == "fix":
            slug = (owner or "all").replace(":", "_")
            p = paths.fixes() / ("%s.md" % slug)
        else:
            p = paths.meta_dir() / ("prompt_%s.md" % st)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        out[st] = str(p)
        print("[brief] 派单 prompt（%s）→ %s" % (st, p))
    return out


def _head_desc(nc: dict, name: str) -> str:
    if name == "本课定位":
        return "`## 本课定位` —— 3-5 句：这节课解决什么问题、值不值得精读、适合什么阶段的人看"
    if name == "节点索引":
        return "`## 节点索引` —— **由工具派生，不用你写**（写了会被覆盖）"
    return "`## %s`" % name


def _tail_desc(nc: dict, name: str) -> str:
    desc = {
        "带走三件事": "这节课真正值得记住的三条，写得像备忘录",
        "未解之问": "你还没想通的、或课程没讲透的（给自己留的复习入口）",
        "自测": "3-6 个问题，检验记忆/理解/迁移各 1-2 个",
        "行动": "2-5 条可执行项",
    }.get(name, "")
    return ("`## %s` —— %s" % (name, desc)) if desc else ("`## %s`" % name)


def _node_fields(nc: dict) -> str:
    hints = {
        "提炼": "把关键信息压成 2-4 句自己的话（不要抄讲义原句）",
        "联想": "与已有知识、其它课程、实际工作场景的连接（没有就写\"暂无\"）",
        "问答": "视频在这里提出的问题 + 它的回答（视频没提问就省略这一条）",
        "钩子": "留给未来的接口 —— 例如\"这个概念后续课程会展开吗\"\"与另一个系统的做法冲突\"",
    }
    lines = []
    for lab in (nc.get("field_labels") or []):
        lines.append("- %s：%s" % (lab, hints.get(lab, "")))
    return "\n".join(lines)


def _errors_for(paths, owner: str | None) -> str:
    """从 _meta/validation.json 取该 owner 的错误原文（给 fix 派单用）"""
    doc = _load_json(paths.validation) or {}
    items = (doc.get("errors") or []) + (doc.get("review") or [])
    picked = [e for e in items if not owner or e.get("owner") == owner]
    if not picked:
        return "-（校验报告里没有属于 %s 的未修问题）" % (owner or "该 owner")
    out = []
    for e in picked:
        line = "- [%s] %s" % (e.get("owner", "?"), e.get("message", ""))
        if e.get("fix_hint"):
            line += "\n  → %s" % e["fix_hint"]
        out.append(line)
    return "\n".join(out)
