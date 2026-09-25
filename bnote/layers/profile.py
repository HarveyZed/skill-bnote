"""L12 内容画像层：**在知道课程/视频之后**再决定契约里注入什么，而不是把某个项目的要求写死在模板里。

思路（来自使用反馈）：模板应当领域无关，具体策略由"画像"参数化注入：
  1) 标题/合集名 -> 预测领域术语（预测可先于任何内容获取）；
  2) 字幕 + slide OCR -> 观测真实术语分布、中英混排比例、有无代码/公式；
  3) 同合集历史 lecture 的 corrections -> 累积 ASR 易错词（越跑越准）；
  4) 以上产出一个 profile.json，由 prompt 渲染时注入 {{TERM_POLICY}} / {{FORMAT_RULES}} / {{STYLE_NOTE}}。

画像来源全部是廉价信号（正则 + 词频 + 历史沉淀），不需要模型推理。
"""
from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from pathlib import Path

LATIN = re.compile(r"[A-Za-z][A-Za-z0-9+#._\-]{1,}")
CODE_HINT = re.compile(r"(\bdef \b|\bimport \b|\bclass \b|\{\s*\w+\s*:|\breturn\b|</?\w+>|::|=>|\$\{|\bfunction\b|;\s*$)", re.M)
FORMULA_HINT = re.compile(r"(\\frac|\\sum|\bsoftmax\b|\bcos\s*\(|\bargmax\b|\bO\(n|= *[a-zA-Z]\s*[+\-*/]\s*[a-zA-Z])")
CJK = re.compile(r"[\u4e00-\u9fff]")

# 停用词（profile.stopwords）与领域词典（profile.domain_lexicon）都来自 config：
# 领域词典的键是"标题/合集名里出现的关键词（逗号分隔）"，值是命中后作为预测白名单的术语。

# 通用 ASR 混淆（会被同合集历史 corrections 进一步扩充）
def _table_pairs(text: str):
    """从 Markdown 表格里挖 原文/更正 对（v3 迁移留下的听写校正表就是这个形式）"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or not cells[0] or not cells[1]:
            continue
        if any(k in cells[0] for k in ("原文", "错误", "听写", "wrong")) or cells[0].startswith("-"):
            continue
        if len(cells[0]) <= 40 and len(cells[1]) <= 40:
            out.append((cells[0], cells[1]))
    return out


PAIR = re.compile(r"([^\s，。；、（()]{1,24}?)\s*(?:->|→|—>|⇒)\s*([^\s，。；、）)]{1,24})")
# 内置为空：ASR 误写大多是课程特有的，靠"同合集历史 corrections"与 [profile].extra_avoid 积累
ASR_CONFUSIONS: dict = {}


def _latin_terms(text: str, min_count: int, top: int, stop=frozenset()) -> list[tuple[str, int]]:
    c = Counter(m.group(0) for m in LATIN.finditer(text or ""))
    # 归并大小写变体，取出现最多的写法
    merged: dict[str, tuple[str, int]] = {}
    for term, n in c.items():
        key = term.lower()
        prev = merged.get(key)
        if prev is None or n > prev[1] or (n == prev[1] and term.isupper()):
            merged[key] = (term, n + (prev[1] if prev else 0))
    items = [(t, n) for t, n in merged.values()
             if n >= min_count and len(t) > 1 and t.lower() not in stop]
    return sorted(items, key=lambda x: -x[1])[:top]


def _collection_corrections(paths) -> dict:
    """扫描同合集其它 lecture 的 manifest.corrections，累积 ASR 易错词"""
    acc = Counter()
    bvid = paths.vid.split("_p")[0]
    root = paths.cache.parent.parent / "out"
    if not root.exists():
        return {}
    for d in sorted(root.glob("%s_p*" % bvid)):
        if d.name == paths.vid:
            continue
        mp = d / "chapters" / "manifest.json"
        if not mp.exists():
            continue
        try:
            man = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for ch in man.get("chapters", []):
            for c in ch.get("corrections") or []:
                if isinstance(c, dict) and c.get("wrong") and c.get("right"):
                    acc[(str(c["wrong"]), str(c["right"]))] += 1
                elif isinstance(c, str):
                    for m in PAIR.finditer(c):
                        acc[(m.group(1).strip(), m.group(2).strip())] += 1
            # v3 迁移留下的自由文本（听写校正表）也一起挖
            for field in ("review_notes", "coverage_notes"):
                txt = ch.get(field) or ""
                if isinstance(txt, str):
                    for m in PAIR.finditer(txt):
                        acc[(m.group(1).strip(), m.group(2).strip())] += 1
                    for w, r in _table_pairs(txt):
                        acc[(w, r)] += 1
    return dict(acc)


def build(cfg: dict, paths, meta: dict, transcript: dict | None = None, slides: dict | None = None) -> dict:
    pc = cfg.get("profile", {})
    title = " ".join(str(x) for x in (meta.get("part"), meta.get("title")) if x)
    # 领域预测额外看简介 / 置顶评论 / 标签（**不进术语观测**：它们常混推广链接与口号，会把白名单搞脏）
    # 置顶评论与简介同一权重：都是作者自己写的视频页说明（数据取自 meta.json 的 top_comment.text）
    _toc = meta.get("top_comment") or {}
    _toc_text = _toc.get("text") if isinstance(_toc, dict) else str(_toc or "")
    predict_src = " ".join([title, str(meta.get("desc") or ""), str(_toc_text or ""),
                            " ".join(meta.get("tags") or [])])
    ttext = "".join(s.get("text", "") for s in (transcript or {}).get("segments", []))
    stext = " ".join(s.get("ocr_text", "") for s in (slides or {}).get("slides", []))
    combined = ttext + "\n" + stext

    # 1) 预测术语（只看标题也能给出）
    predicted = []
    lexicon = (cfg.get("profile") or {}).get("domain_lexicon") or {}
    stop = frozenset(str(s).lower() for s in ((cfg.get("profile") or {}).get("stopwords") or []))
    low = predict_src.lower()
    for keys, terms in lexicon.items():
        if any(k.strip() in low for k in keys.split(",")):
            predicted += terms
    predicted = sorted(set(predicted + list(pc.get("extra_terms") or [])))

    # 2) 观测术语：**幻灯片 OCR 是拼写权威**，字幕只用于挖 ASR 变体
    min_c = int(pc.get("min_latin_count", 2))
    max_t = int(pc.get("max_terms", 25))
    slide_terms = _latin_terms(stext, 1, max_t, stop)           # slide 出现过即算候选（拼写可信）
    # 白名单过滤 OCR 噪声：出现不低于 2 次，或属领域词典，或全大写缩写（API / RAG 这类）
    _lex = {t.lower() for t in predicted}
    slide_terms = [(t, n) for t, n in slide_terms
                   if n >= 2 or t.lower() in _lex or (t.isupper() and len(t) >= 2)]
    speech_terms = _latin_terms(ttext, min_c, max_t, stop)      # 只出现在字幕里的，优先怀疑是 ASR 变体
    observed = slide_terms + [x for x in speech_terms if x[0].lower() not in {t.lower() for t, _ in slide_terms}]

    # 3) 格式画像
    latin_chars = len(LATIN.findall(combined))
    cjk_chars = len(CJK.findall(combined))
    bilingual = bool(combined) and (latin_chars >= 8 or len(observed) >= 3)
    has_code = bool(CODE_HINT.search(combined))
    has_formula = bool(FORMULA_HINT.search(combined))
    dur = int(meta.get("duration") or 0)
    slides_n = len((slides or {}).get("slides", [])) or 0
    density = (len(stext) / slides_n) if slides_n else 0
    if dur and slides_n:
        style = ("概念总览型（页少字多，讲义应克制、只讲页面与讲解给出的东西）"
                 if density > 200 and dur < 600 else
                 "实操/演示型（出现命令或代码时按行内代码与代码块排版）" if has_code else
                 "讲解展开型（信息密度中等，可适当补衔接句）")
    else:
        style = "未知（缺内容信号，按讲解展开型处理）"

    # 4) 避免用词（黑名单）：自动挖 ASR 变体 + 通用混淆 + 同合集历史
    #    判据：某术语只出现在字幕（未在幻灯片出现过），且与幻灯片/领域白名单里的术语只差一个编辑操作
    canonical = [t for t, _ in slide_terms] + predicted

    def _edit1(a: str, b: str) -> bool:
        a, b = a.lower(), b.lower()
        if abs(len(a) - len(b)) > 1 or not a or not b:
            return False
        if a == b:
            return True
        if len(a) == len(b):
            return sum(1 for x, y in zip(a, b) if x != y) == 1
        s, l = (a, b) if len(a) < len(b) else (b, a)
        for i in range(len(l)):
            if l[:i] + l[i + 1:] == s:
                return True
        return False

    auto_avoid = {}
    slide_lower = {t.lower() for t, _ in slide_terms}
    for term, cnt in speech_terms:
        if term.lower() in slide_lower or len(term) < 2:
            continue
        for can in canonical:
            # 2 字词只接受"少一个字符"的删除型误写（如 RG 对 RAG），减少误判
            if len(can) >= 3 and _edit1(term, can) and (len(term) >= 3 or len(can) == len(term) + 1):
                auto_avoid[term] = can
                break

    avoid = dict(ASR_CONFUSIONS)
    avoid.update(auto_avoid)
    hist = _collection_corrections(paths)
    for (wrong, right), n in sorted(hist.items(), key=lambda x: -x[1])[:20]:
        avoid[wrong] = right
    for pair in pc.get("extra_avoid") or []:
        if "->" in pair:
            k, v = pair.split("->", 1)
            avoid[k.strip()] = v.strip()

    profile = {
        "vid": paths.vid,
        "title": title,
        "domain_terms_predicted": predicted,
        "terms_observed": [{"term": t, "count": n} for t, n in observed],
        "terms_slide": [{"term": t, "count": n} for t, n in slide_terms],
        "terms_speech_only": [{"term": t, "count": n} for t, n in speech_terms
                              if t.lower() not in slide_lower],
        "auto_avoid": auto_avoid,
        "bilingual": bilingual,
        "has_code": has_code,
        "has_formula": has_formula,
        "style": style,
        "avoid": avoid,
        "stats": {"latin_tokens": latin_chars, "cjk_chars": cjk_chars,
                  "slide_count": slides_n, "duration": dur},
    }
    paths.cache.mkdir(parents=True, exist_ok=True)   # 只建自己要写的目录
    (paths.cache / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[profile] 领域=%s ｜ 观测术语 %d 个 ｜ 中英混排=%s 代码=%s 公式=%s"
          % (", ".join(predicted[:4]) or "-", len(observed), bilingual, has_code, has_formula))
    return profile


# 蒸馏时不要产出的"规则"：单字改动、纯标点、以及中文虚词 ——
# 实测这些碎片会把噪音变成硬约束（例如「一个 -> （」「C -> eek」「N -> A」），
# 而契约里黑名单的含义是"左侧禁止出现"，等于凭空禁掉一个正常用词。
_PUNCT_ONLY = re.compile(r"^[\s\W_]+$", re.UNICODE)
_FUNCTION_WORDS = {"一个", "的", "了", "在", "好", "是", "也", "都", "就", "和", "与", "这", "那",
                   "他", "它", "我", "你", "我们", "他们", "可以", "然后", "所以", "因为", "这个", "那个"}


def _usable_pair(wrong: str, right: str) -> bool:
    """一条词级规则是否值得下发（防止把 diff 碎片当约束）"""
    w, r = wrong.strip(), right.strip()
    if w and _PUNCT_ONLY.match(w):
        return False
    if r and _PUNCT_ONLY.match(r):
        return False
    if w and len(w) < 2:          # 单字误写（A / C / 图 / N）：规则价值低于误伤风险
        return False
    if r and len(r) < 2:
        return False
    if w in _FUNCTION_WORDS:
        return False
    if w and r and w.lower() == r.lower():   # 自反噪声（FDE -> FDE / API -> API）
        return False
    return True


def _distill(pairs) -> tuple:
    """把"句子级校正"蒸馏成"词级易错对"：短对直接用；长对做 diff 抽取改动片段。

    这样注入契约的黑名单是"rug -> RAG、智博爱景行 -> 景行"这种可执行的形式，而不是一墙长句子。
    长句子仍保留少量作为示例；抽出来的碎片要过 _usable_pair 的筛子（见上）。
    """
    term = Counter()
    example = []
    for item in pairs:
        if len(item) == 2 and not isinstance(item[1], int):
            wrong, right, n = item[0], item[1], 1
        else:
            (wrong, right), n = item
        w, r = str(wrong).strip(), str(right).strip()
        if len(w) <= 8 and len(r) <= 8:
            if (w or r) and _usable_pair(w, r):
                term[(w, r)] += n
            continue
        # 长句不做"片段蒸馏"：实测从长对里抽出来的碎片（「应删『好了』」「检索（ -> 保持英文原形」这类）
        # 会把噪音变成硬约束，而黑名单的语义是"左侧禁止出现"。长句只作为示例，供写手体会 ASR 误差风格。
        if len(example) < 5:
            example.append((w, r))
    return term, example


def term_policy(profile: dict, reviewed=None, confirmed: bool = False, mode: str = "slides") -> str:
    """给 agent 的术语策略（白名单 + 黑名单）。

    reviewed: 经 agent/人评审确认的白名单（<state>/glossary/<vid>.json）；为空则用自动提议。
    confirmed: 未确认时会在 prompt 里显式标注，提醒写作 agent 自行判断。
    """
    if reviewed:
        use = list(reviewed)
    else:
        # 未确认：**不能**说成"必须使用的写法"（错的白名单比没有更糟），只作为候选，且不带计数后缀
        use = []
        for t in profile.get("domain_terms_predicted", [])[:12]:
            use.append(t)
        # 信息流 + ASR：观测词里混着 of/ok/wifi 这类短英文词，先滤掉（否则候选表会误导）
        _junk = re.compile(r"^[a-z]{1,6}$")
        skip_junk = mode == "text" and not (profile.get("terms_slide") or [])
        for item in (profile.get("terms_slide") or profile.get("terms_observed") or [])[:15]:
            if item["term"] in use:
                continue
            if skip_junk and _junk.match(item["term"] or ""):
                continue
            use.append(item["term"])
    # 证据优先级：**字幕不是准绳**——平台 AI 轨与 UP 上传的 CC 都是语音识别，都可能没校对。
    if mode == "slides":
        evidence = ("**判断用字时的优先级**：① 页面图上的文字（讲师自己的课件，最接近 ground truth）"
                    "② 元信息（标题/分P标题/简介/标签，人写的）③ 字幕（ASR，可能未校对）——字幕只是证据，不是准绳；"
                    "拿不准就就地括注存疑，不要猜、也不要静默统一。")
    else:
        evidence = ("**判断用字时的优先级**：① 元信息（标题/分P标题/简介/标签，人写的）"
                    "② 字幕（本集没有幻灯片；平台 AI 轨与 UP 上传的 CC 都是语音识别，都可能没校对）——"
                    "所以字幕只是证据、不是准绳：不要因为字幕里出现某写法就认定它对，也不要因为对照表说把 A 换成 B 就全篇替换；"
                    "拿不准就就地括注存疑（例如音近疑为某词），不要猜、也不要静默统一。")
    term, example = _distill(list(((profile.get("_effective_avoid") or profile.get("avoid")) or {}).items()))
    def fmt(pair):
        a, b = pair
        a, b = a.strip(), b.strip()
        if not a and not b:
            return ""
        if not a:
            return "漏字：应补「%s」" % b
        if not b:
            return "多字：应删「%s」" % a
        return "%s -> %s" % (a, b)
    if confirmed:
        fallback = "（暂无，按 slide 原文用字）" if mode == "slides" else "（暂无，按字幕上下文用字）"
        lines = ["**必须使用的写法（白名单，已确认）**：" + ("、".join(use) if use else fallback)]
        avoid_tag = "**易错词表（左侧写法禁止出现）**："
    else:
        if mode == "slides":
            advice = "以上只是候选，**以 slide 上的原文用字为准**；不确定就按 slide，并把疑问记进 manifest 的 review_flags。"
        else:
            advice = ("以上只是候选（本集没有幻灯片，也没有 manifest）：判断用字时**以人写的元信息与上下文为准**，"
                      "字幕只是证据、不是准绳；拿不准就在正文里就地标出（例如括注音近疑为某词），不要猜着写。")
            advice += " 注意：信息流 + 本地 ASR 下自动提议噪音偏多（短英文词尤其），不要照单全收。"
        lines = ["**参考写法（脚本自动提议、未经确认）**：" + ("、".join(use) if use else "（暂无）"), advice]
        # 未确认的历史蒸馏**不能**当禁令下发：实测下发过 `AI -> API`（原意是「某集 ASR 把 API 听成了 AI」），
        # 写手照办就会把「AI 项目」写成「API 项目」；自反噪声（`FDE -> FDE`）只说明蒸馏该过滤。
        avoid_tag = ("**可疑对照（同合集历史蒸馏、未经确认）：**只作参考，**不要照单全改**——"
                     "例如 `AI -> API` 的原意是「某一集里 ASR 把 API 听成了 AI」，**不是**让你把所有 AI 都改成 API。")
    if term:
        rendered = []
        for p, _ in term.most_common(14):
            s = fmt(p)
            if s and s not in rendered:
                rendered.append(s)
        if rendered:
            lines.append(avoid_tag + "、".join(rendered))
    if example:
        lines.append("**句子级示例（仅供体会 ASR 误差风格，不必照抄）**："
                     + "；".join("%s -> %s" % e for e in example[:3]))
    if not term and not example:
        lines.append("**易错词**：" + ("（暂无历史，按 slide 原文用字）" if mode == "slides"
                                        else "（暂无历史，按字幕上下文用字）"))
    lines.append(evidence)
    return "\n".join("- " + x for x in lines)


def format_rules(profile: dict, mode: str = "slides") -> str:
    """按画像条件注入排版规则：领域无关，规则随内容特征开关"""
    rules = []
    if profile.get("bilingual"):
        rules += ["术语保留英文原形，不要硬译；首次出现可写「中文（English）」，之后统一用英文",
                  "中文与英文/数字之间加一个半角空格（如「调用 API」「top 5」）",
                  "英文大小写按通用写法统一（RAG、API、LLM、Python）"]
    if profile.get("has_code"):
        rules += ["命令、字段名、参数、类名用行内代码；多行代码/伪代码用围栏代码块并标注语言"]
    if profile.get("has_formula"):
        rules += ["公式与维度写成行内代码或独立公式行（如 dim=1536），不要口语化描述"]
    if mode == "slides":
        rules += ["ASR 音译/误写按 slide 用字还原；无法确认的写进 manifest 的 review_flags，不要猜"]
    else:
        rules += ["ASR 音译/误写按上下文与**人写的元信息**（标题/简介/标签）还原；字幕本身也是 ASR、可能未校对，拿不准就地括注说明，不要猜"]
    return "\n".join("- " + r for r in rules)
