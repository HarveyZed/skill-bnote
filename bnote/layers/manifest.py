"""L10 结构层：章节结构用 JSON manifest 承载，校验只校验结构。

设计原则（v4，来自使用反馈）：
  * **结构归结构**：章节的机器可读信息（编号/标题/时间范围/slide/要点/问题/校正/复核备注）
    全部放在 chapters/manifest.json，用严格 schema 校验 —— 目的只是"防脚本出错"；
  * **内容归 agent**：章节正文文件是**纯正文**（无 front matter、无 meta 块），
    工程碎片没有地方可写，这比事后用词表拦截可靠得多；
  * **不做用词检查**：不想让 agent 写进正文的东西，写进它的写作契约（prompt），而不是事后 grep。

manifest 结构：
{
  "schema": "bnote-chapters/1",
  "video": {"vid": "...", "bvid": "...", "page": 18, "title": "...", "duration": 586},
  "slide_count": 16,
  "chapters": [
    {"id": "01", "title": "...", "range": ["00:00:00", "00:00:48"], "slides": [1, 2],
     "body": "01-opening-and-map.md",
     "keypoints": ["..."], "questions": ["..."],
     "corrections": [{"wrong": "…", "right": "…", "evidence": "slide 0001"}],
     "review_flags": ["…"], "coverage_notes": "…"}
  ]
}
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import refs as refs_layer
from . import slideset as slideset_layer

FENCE = chr(96) * 3
NL = chr(10)
SCHEMA = "bnote-chapters/1"
TS_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")
FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
META_BEGIN = "<!-- meta:begin -->"
META_END = "<!-- meta:end -->"
# 就地标注：给"读者需要看到的推断与存疑"用的固定标记（引用块，紧跟相关段落之后，不打断阅读）
ANNOT_MARK = "**【校对】**"
# 图引用白名单不在这里定义：唯一入口是 layers/refs.py（manifest / body / merge / remap 同源）


def path_of(paths) -> Path:
    return paths.chapters() / "manifest.json"


def load(paths) -> dict | None:
    p = path_of(paths)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def write(paths, manifest: dict) -> Path:
    p = path_of(paths)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ------------------------------------------------------------------ 迁移（v3 front matter -> v4 manifest + 纯正文）

def _split_legacy(text: str):
    fm, body = {}, text
    m = FM_RE.match(text)
    if m:
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if v.startswith("[") and v.endswith("]"):
                fm[k] = [x.strip().strip('"') for x in v[1:-1].split(",") if x.strip()]
            elif v:
                fm[k] = v.strip('"')
            else:
                fm[k] = []
        body = text[m.end():]
    # 支持块状列表（keypoints:\n  - xxx）
    lines = (m.group(1).splitlines() if m else [])
    cur = None
    for line in lines:
        if re.match(r"^\s+-\s+", line) and cur:
            fm.setdefault(cur, []).append(line.split("-", 1)[1].strip().strip('"'))
        elif ":" in line:
            cur = line.split(":", 1)[0].strip()
    meta = ""
    if META_BEGIN in body:
        body, tail = body.split(META_BEGIN, 1)
        meta = tail.split(META_END, 1)[0] if META_END in tail else tail
    return fm, body.strip(), meta.strip()


def migrate(paths, meta: dict, dry: bool = False) -> dict:
    """把 v3（front matter + meta 块）章节迁移为 v4（manifest.json + 纯正文）"""
    files = sorted(paths.chapters().glob("0*.md"))
    if not files:
        return {}
    backup = paths.chapters() / "_v3_backup"
    if not dry:
        backup.mkdir(exist_ok=True)
    chapters = []
    for f in files:
        raw = f.read_text(encoding="utf-8")
        fm, body, meta_md = _split_legacy(raw)
        if not dry:
            shutil.copyfile(f, backup / f.name)
            f.write_text(body + NL, encoding="utf-8")
        ch = {
            "id": str(fm.get("chapter") or f.stem.split("-")[0]),
            "title": fm.get("title") or f.stem,
            "range": (fm.get("range") or "00:00:00-00:00:00").split("-")[:2],
            "slides": [int(x) for x in (fm.get("slides") or []) if str(x).isdigit()],
            "body": f.name,
            "keypoints": list(fm.get("keypoints") or []),
            "questions": list(fm.get("questions") or []),
        }
        if fm.get("allow_terms"):
            ch["allow_terms"] = list(fm["allow_terms"])
        if meta_md:
            ch["review_notes"] = meta_md
        chapters.append(ch)
    man = {
        "schema": SCHEMA,
        "video": {"vid": paths.vid, "bvid": meta.get("bvid"), "page": meta.get("page"),
                  "title": meta.get("part") or meta.get("title"), "duration": meta.get("duration")},
        "slide_count": len(list((paths.out / "slides").glob("*.jpg"))),
        "migrated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chapters": chapters,
    }
    if not dry:
        write(paths, man)
    print("[manifest] 迁移完成：%d 章 → %s（原文件备份在 chapters/_v3_backup/）" % (len(chapters), path_of(paths)))
    return man


# ------------------------------------------------------------------ 结构校验（只校验结构）

def _item(level: str, msg: str, owner: str = "manifest", chapter: str | None = None,
          file: str | None = None, fix: str | None = None) -> dict:
    """一条结构问题 + 归属（owner 决定谁来修）：
       chapter:<id> —— 该章写作 agent（内容/结构填错）
       manifest     —— 编排者（章节划分、时间范围、keypoints 汇总等全局结构）
       pipeline     —— 工具/流水线（切片、bundle、remap 等产出物不一致），需要改代码或人工决策

    `level` 只有两个取值：**error**（进 `errors`，拦住覆盖成品、进派修队列）与
    **warning**（进 `warnings`，只提醒、不拦流程）。落进哪个数组由调用方决定，
    两边必须一致：**不要用 _err() 生成警告**（0.9.1 之前就是这样，导致警告带 error 级）。
    """
    return {"level": level, "owner": owner, "chapter": chapter, "file": file,
            "message": msg, "fix_hint": fix or ""}


def _reader_flags(ch: dict) -> tuple[int, int]:
    """(需要读者看到的条数, 其中推断类校正的条数)。

    需要读者看到的两类：① corrections 里 basis=context（靠在语境里推断得出的更正）
    ② uncertainties（无法归位 / 语义不明等存疑）。这两类必须在正文就地标注——
    它们不是"工程碎片"（工程碎片仍留在 manifest），而是读者判断这段文字可不可信的依据。
    """
    cs = [c for c in (ch.get("corrections") or []) if isinstance(c, dict)]
    ctx = sum(1 for c in cs if str(c.get("basis") or "").lower() == "context")
    doubts = len(ch.get("uncertainties") or [])
    return ctx + doubts, ctx


def _err(msg: str, owner: str = "manifest", chapter: str | None = None,
         file: str | None = None, fix: str | None = None) -> dict:
    return _item("error", msg, owner, chapter, file, fix)


def _warn(msg: str, owner: str = "manifest", chapter: str | None = None,
          file: str | None = None, fix: str | None = None) -> dict:
    return _item("warning", msg, owner, chapter, file, fix)


def _panel_names(paths) -> set:
    """_meta/sheet.json 里有 tile 的面板文件名集合（没有产物时为空集）。

    正文引用的读字面板必须在这里查得到 —— 否则读者拿到一张谁也不能解释的图
    （面板只作索引，权威映射只在 sheet.json，见 layers/refs.py 与 body-contract）。
    """
    doc = paths.read_json(paths.meta_dir() / "sheet.json", None)
    if not isinstance(doc, dict):
        return set()
    return {str(t.get("sheet")) for t in (doc.get("tiles") or []) if isinstance(t, dict)}


def _short_sid(s: str) -> str:
    """报错文案里的指纹只显示前 19 个字符：完整值在 manifest / slides.json 里。"""
    s = str(s or "")
    return (s[:19] + "…") if len(s) > 20 else (s or "-")


def _check_slideset(paths, manifest: dict, chapters: list, errors: list, warns: list) -> None:
    """0b 切片身份：manifest 记的是「写手当时看到的那版图」，与当前 slides.json 比对。

    四类结果（输出纪律照抄既有约定：只有 id 不等与逐页 sha 不一致进 errors，其余走 _warn；
    owner 一律 pipeline —— 这是工具/流水线产出物不一致，要人决策，不是写手的锅）：
      ① 顶层（整集口径）id 不等 → error「整集错版」；章级 id 不等 → error「该章需重派写手」；
      ② 逐页 sha256 自一致失败（图被替换/拷贝半途中断）→ error，**无条件跑**
         （不挂在 ① 的 else 上，否则 manifest 一过期就不查篡改）；
      ③ 缺字段（早于本功能的存量产物）→ **warn**，不 error、不拦 merge（那时写手看的是哪一版
         图已无从证明，硬报 error 会让所有存量产物永久红灯）；
      ④ 有 slideset_remap 留痕 → warn（页号已同步，正文文字与图注未重写）。
    """
    doc = slideset_layer.read_out_slides(paths.out)
    cur = str(slideset_layer.slideset_of(doc).get("id") or "")
    top = str(manifest.get("slideset_id") or "")
    pairs = [(str(c.get("id")), str(c.get("slideset_id") or "")) for c in chapters or []]
    if doc is None:
        warns.append(_warn("缺 out/<vid>/slides.json，无法校验切片指纹（重跑 bnote bundle 恢复）", "pipeline"))
    elif not cur:
        # 当前版没有指纹（早于本功能的 slides.json）→ 判不了等，一律 warn：硬按"不等"处理成 error
        # 会让所有存量产物永久红灯，而那时写手看的是哪一版图已无从证明。
        warns.append(_warn("out/<vid>/slides.json 缺切片指纹（早于本功能的存量产物%s）：无法判断讲义与当前切片"
                           "是否同版；重跑 bnote bundle 并重派写手即可获得保护（存量产物不回填）"
                           % ("，manifest 里记着 %s" % _short_sid(top) if top else ""), "pipeline",
                           fix="bnote bundle → brief --stage chapter → collect"))
    elif not top:
        warns.append(_warn("manifest 缺 slideset_id（早于本功能）；先 bnote collect 补上", "pipeline"))
    else:
        missing = [cid for cid, sid in pairs if not sid]
        if top != cur:
            # 三种情形的**动作相同**（按章重派），但措辞必须分开：整集同一版 ≠ 各章混着几版 ≠ 还有章没盖到。
            # 只改文案选择，不改任何 error/warn 的数量、owner 与 chapter 字段。
            kinds = {sid for _, sid in pairs if sid}
            fix_ch = "按章重派：brief --scope chapter:NN（见下面的章级条目），或用 bnote remap 同步页号后再复核图注"
            if len(kinds) > 1:
                errors.append(_err("本集各章写在不同版切片上（章级指纹有 %d 种；顶层汇总 %s ≠ 当前 %s）"
                                   % (len(kinds), _short_sid(top), _short_sid(cur)), "pipeline", fix=fix_ch))
            elif missing:
                errors.append(_err("已记录的章都在同一旧版切片上（顶层汇总 %s ≠ 当前 %s；另有 %d 章没盖到指纹）"
                                   % (_short_sid(top), _short_sid(cur), len(missing)), "pipeline", fix=fix_ch))
            else:
                errors.append(_err("整集错版：manifest 记录的切片指纹 %s 与当前 slides.json 的 %s 不一致"
                                   "（讲义写的是另一版切片）" % (_short_sid(top), _short_sid(cur)),
                                   "pipeline", fix=fix_ch))
        for cid, sid in pairs:
            if sid and sid != cur:
                # 当前切片的指纹交给顶层那一条，章级只留"本章写作时的那个"，扫一眼就能定位到章
                errors.append(_err("该章需重派写手：章 %s 写作时的切片指纹是 %s（当前切片见顶层那条）"
                                   % (cid, _short_sid(sid)), "pipeline", cid,
                                   fix="brief --scope chapter:%s → 重写该章" % cid))
        if missing:
            warns.append(_warn("这些章缺 slideset_id（早期派单未覆盖）：%s；无法判断是否同版"
                               % "、".join(missing), "pipeline"))
    for p in (slideset_layer.recompute_from_out(paths.out, doc) or {}).get("pages") or []:
        if p.get("recorded") is None:
            continue                     # 存量产物没有记录值，无从比对（上面已 warn 一次）
        if not p["exists"]:
            errors.append(_err("slides/%04d.jpg 不存在，但 slides.json 记录了它的 sha256（拷贝不完整或图被删）" % p["id"], "pipeline"))
        elif p["actual"] != p["recorded"]:
            errors.append(_err("slides/%04d.jpg 实际 sha256 与 slides.json 记录不符（图被替换/拷贝半途中断）" % p["id"], "pipeline"))
    rm = manifest.get("slideset_remap")
    if isinstance(rm, dict):
        warns.append(_warn("该讲义经 remap 迁移过页号（%s → %s，%s）：页号已同步，但正文文字与图注未重写，需复核"
                           % (_short_sid(rm.get("from")), _short_sid(rm.get("to")), rm.get("at") or "-"),
                           "pipeline",
                           fix="抽样看图复核图注，或 brief --stage review；重派写手后 collect 会自动清掉这个标记"))


def _check_overlay(paths, warns: list) -> None:
    """M3：跑过切片却没有 cache/<vid>/overlay.json —— **只 warn，不拦 merge**（§3.4-6）。

    存量产物（M3 之前跑的切片）必然缺这个文件，报 error 会让它们永久红灯；而遮罩缺位
    的真实后果只是"字幕条与角状外物的像素仍参与帧差与 OCR 文本"——是质量退化，
    不是结构错误，也不该拦住交付。
    """
    if paths.segments.exists() and not paths.overlay.exists():
        warns.append(_warn("跑过切片但没有 cache/<vid>/overlay.json（M3 遮罩缺位：烧录字幕条与"
                           "标注工具条的文字仍会参与帧差与 chosen 的 OCR 文本）",
                           "pipeline", fix="bnote overlay <URL> --page N（或重跑 bnote slides）"))


def validate(manifest: dict | None, paths, meta: dict, transcript: dict | None,
             cfg: dict | None = None):
    errors, warns = [], []
    _check_overlay(paths, warns)
    tol = int(((cfg or {}).get("manifest") or {}).get("time_tolerance_sec", 1))

    if not manifest:
        return [_err("缺少 chapters/manifest.json（结构唯一来源）", "manifest",
                     fix="跑 bnote merge 会自动从 v3 章节迁移，或由编排者补齐结构文件")], warns
    if manifest.get("schema") != SCHEMA:
        errors.append(_err("manifest.schema 应为 %s，实际 %r" % (SCHEMA, manifest.get("schema")),
                           fix="编排者修正 manifest 头部"))
    video = manifest.get("video") or {}
    if not isinstance(video, dict):
        errors.append(_err("manifest.video 必须是对象", fix="编排者修正"))
    chapters = manifest.get("chapters") or []
    if not isinstance(chapters, list) or not chapters:
        errors.append(_err("manifest.chapters 必须是非空数组", fix="编排者补齐章节结构"))
        return errors, warns

    _sj = paths.out / "slides.json"
    real_slides = 0
    if _sj.exists():
        try:
            real_slides = len(json.loads(_sj.read_text(encoding="utf-8")).get("slides") or [])
        except Exception:
            real_slides = 0
    slide_count = int(manifest.get("slide_count") or len(list((paths.out / "slides").glob("*.jpg"))))
    if real_slides and slide_count and slide_count != real_slides:
        errors.append(_err("manifest.slide_count=%d 与 slides.json 的 %d 页不一致（多半是重切片后未刷新结构）"
                           % (slide_count, real_slides), "pipeline",
                           fix="跑 bnote remap --from <旧 slides.json> 同步引用，再重跑 bnote scaffold/bundle 刷新 slide_count"))
    _check_slideset(paths, manifest, chapters, errors, warns)
    kp_min = int((cfg or {}).get("manifest", {}).get("keypoints_min", 2))
    duration = int(meta.get("duration") or 0)
    prev_end = None
    used_slides = set()

    ids = [c.get("id") for c in chapters]
    if len(set(ids)) != len(ids):
        errors.append(_err("章节 id 有重复：%s" % ids, fix="编排者去重"))

    for ch in chapters:
        cid = ch.get("id", "?")
        for key, typ in (("id", str), ("title", str), ("range", list), ("slides", list),
                         ("body", str), ("keypoints", list), ("questions", list)):
            if key not in ch:
                errors.append(_err("缺少字段 %s" % key, "chapter:%s" % cid, cid,
                                   fix="该章写作 agent 在 manifest 里补齐 %s" % key))
            elif not isinstance(ch[key], typ):
                errors.append(_err("字段 %s 类型应为 %s" % (key, typ.__name__), "chapter:%s" % cid, cid,
                                   fix="该章写作 agent 修正类型"))
        if not isinstance(ch.get("range"), list) or len(ch.get("range", [])) != 2:
            errors.append(_err("range 必须是 [起, 止] 两个 HH:MM:SS 字符串", "chapter:%s" % cid, cid))
            continue
        a, b = ch["range"]
        if not (TS_RE.match(str(a)) and TS_RE.match(str(b))):
            errors.append(_err("时间格式应为 HH:MM:SS，实际 %r" % (ch["range"],), "chapter:%s" % cid, cid))
            continue

        def sec(t):
            h, m, s = (int(x) for x in str(t).split(":"))
            return h * 3600 + m * 60 + s
        s0, s1 = sec(a), sec(b)
        if s1 <= s0:
            errors.append(_err("结束时间不大于开始时间", "chapter:%s" % cid, cid))
        if prev_end is not None and s0 < prev_end - tol:
            errors.append(_err("与上一章时间重叠（%s < %s）" % (a, prev_end), "manifest", cid,
                               fix="编排者调整章界（相邻章写作 agent 各自改写衔接句）"))
        prev_end = s1 if prev_end is None else max(prev_end, s1)

        body_path = paths.chapters() / str(ch.get("body", ""))
        if not body_path.exists():
            errors.append(_err("正文文件不存在：%s" % ch.get("body"), "chapter:%s" % cid, cid,
                               file=str(ch.get("body")), fix="该章写作 agent 落盘正文文件"))
        else:
            text = body_path.read_text(encoding="utf-8")
            if not text.strip():
                errors.append(_err("正文为空", "chapter:%s" % cid, cid, str(ch.get("body"))))
            if text.lstrip().startswith("---"):
                errors.append(_err("正文不应带 front matter（结构在 manifest.json）", "chapter:%s" % cid,
                                   cid, str(ch.get("body")), fix="该章写作 agent 删除 front matter"))
            if META_BEGIN in text:
                errors.append(_err("正文不应包含 meta 区块（复核信息放 manifest 字段）", "chapter:%s" % cid,
                                   cid, str(ch.get("body")), fix="该章写作 agent 把片段移入 manifest 字段"))
            # 图引用一律走白名单（layers/refs.py 是唯一定义处）：slides 主图 + sheet 读字面板
            img_refs = refs_layer.iter_refs(text)
            pages = [r.page for r in img_refs if r.kind == "slides"]
            if not pages:
                errors.append(_err("正文没有引用任何 slide 图", "chapter:%s" % cid, cid, str(ch.get("body"))))
            for r in img_refs:
                if r.kind != "slides":
                    continue
                used_slides.add(r.page)
                if not refs_layer.ref_out_path(paths.out, r.kind, r.name).exists():
                    errors.append(_err("引用了不存在的图 slides/%04d.jpg" % r.page, "pipeline", cid,
                                       fix="多半是重切片后未跑 bnote remap，或 bundle 未刷新 slides/"))
            # 白名单之外：报出来而不是静默放行（merge 不改写它们，讲义里就是坏图）
            for target in refs_layer.unknown_targets(text):
                errors.append(_err("图片引用不在两类白名单内：%s（只认 ../slides/NNNN.jpg 与 "
                                   "../_meta/sheets/<name>.png）" % target, "chapter:%s" % cid, cid,
                                   str(ch.get("body")),
                                   fix="按契约改成 ../slides/NNNN.jpg（页号 4 位）；读字面板放 "
                                       "_meta/sheets/ 并由 bnote sheet 生成"))
            # 面板引用：必须在 sheet.json 里有对应 tile（否则没人能解释这张图）
            panels = [r for r in img_refs if r.kind == "sheet"]
            if panels:
                known = _panel_names(paths)
                for r in panels:
                    if r.name not in known:
                        errors.append(_err("正文引用了读字面板 %s，但 _meta/sheet.json 里没有它的 tile"
                                           % refs_layer.ref_body_path(r.kind, r.name),
                                           "chapter:%s" % cid, cid, str(ch.get("body")),
                                           fix="跑 bnote sheet 生成面板（行列→帧→t 的映射只在 sheet.json），"
                                               "或删掉这条引用"))
            need, ctx = _reader_flags(ch)
            have = text.count(ANNOT_MARK)
            if need > have:
                errors.append(_err(
                    "需要读者看到的推断/存疑有 %d 条（其中推断类校正 %d 条），正文里只有 %d 处就地标注"
                    % (need, ctx, have), "chapter:%s" % cid, cid, str(ch.get("body")),
                    fix="该章写作 agent 在涉及段落之后补引用块 %s：写明原文 → 更正（或存疑点）→ 依据；"
                        "不要写进句子中间，也不要打断正文阅读" % ANNOT_MARK))

        kps = ch.get("keypoints") or []
        if len(kps) < kp_min:
            errors.append(_err("keypoints 少于 %d 条" % kp_min, "chapter:%s" % cid, cid,
                               fix="该章写作 agent 在 manifest 补 keypoints"))
        if not (ch.get("questions") or []):
            errors.append(_err("缺少 questions", "chapter:%s" % cid, cid))
        for n in (ch.get("slides") or []):
            if not isinstance(n, int) or n < 1 or (slide_count and n > slide_count):
                errors.append(_err("slides 含越界页码 %r（共 %d 页）" % (n, slide_count), "pipeline", cid,
                                   fix="重切片后需同步 manifest 页码（bnote remap 已支持），或由编排者校正"))

    if prev_end is not None and duration and prev_end < duration - 5:
        errors.append(_err("最后一章结束于 %ds，视频 %ds，结尾未覆盖" % (prev_end, duration), "manifest",
                           fix="编排者确认末章是否需要补时段或末页未写"))
    if transcript and transcript.get("segments"):
        spans = []
        for ch in chapters:
            r = ch.get("range") or []
            if len(r) == 2 and TS_RE.match(str(r[0])):
                spans.append((sec(r[0]), sec(r[1])))
        miss = [int(s["from"]) for s in transcript["segments"]
                if not any(a - tol <= int(s["from"]) <= b for a, b in spans)]
        if miss:
            errors.append(_err("有 %d 段字幕不在任何章节时间范围内（例：%s）"
                               % (len(miss), miss[:3]), "manifest",
                               fix="编排者补时段或调整章界（往往意味着某章漏写）"))
    missing = sorted(set(range(1, slide_count + 1)) - used_slides)
    if missing:
        warns.append(_warn("有 %d 张 slide 未被正文引用：%s" % (len(missing), missing), "chapter:?",
                           fix="确认是否为过渡帧；若是内容页，请对应章补引用"))
    return errors, warns


# ---------------------------------------------------------------- v0.7.0 新增
PATCH_KEYS = ("keypoints", "questions", "corrections", "uncertainties", "review_flags", "coverage_notes",
              "stage_merges", "title")


def _owner_match(e: dict, only: list[str]) -> bool:
    o = str(e.get("owner") or "")
    if o.startswith("chapter:"):
        cid = o.split(":", 1)[1]
        return cid in only or cid == "?" and False
    # manifest / pipeline 级问题：只有明确挂在这一章上才在作用域内显示
    return str(e.get("chapter") or "") in only


def validate_all(cfg, paths, meta: dict, transcript: dict | None, only: list[str] | None = None):
    """结构校验总入口：manifest 结构 + 正文小节结构（含时间行）。

    only = 只校验这些章号（写作 agent 自检用，避免看到别人的半成品）
    """
    man = load(paths)
    errors, warns = validate(man, paths, meta, transcript, cfg)
    if man:
        from . import body as body_layer
        be, bw = body_layer.validate_bodies(man, paths, cfg, only=only)
        errors = errors + be
        warns = warns + bw
    if only:
        errors = [e for e in errors if _owner_match(e, only)]
        warns = [w for w in warns if _owner_match(w, only)]
    return man, errors, warns


def write_validation(paths, errors: list, warns: list, review: list | None = None,
                     scope: str = "full", extra: list[str] | None = None) -> dict:
    """写 _meta/validation.json + validation.md（每次重写，避免读到过期报告）。"""
    review = review or []
    routing: dict = {}
    for e in list(errors) + list(warns) + list(review):
        routing.setdefault(e.get("owner", "?"), []).append(e.get("message", ""))

    def _line(e):
        bits = ["[%s]" % e.get("owner", "?")]
        if e.get("chapter"):
            bits.append("章 %s" % e["chapter"])
        if e.get("severity"):
            bits.append("(%s)" % e["severity"])
        bits.append(e.get("message", ""))
        if e.get("fix_hint"):
            bits.append("→ %s" % e["fix_hint"])
        return "- " + " ".join(bits)

    report = ["# 结构校验报告（scope=%s）" % scope, "",
              "- 结果：%s" % ("通过" if not errors else "未通过（未覆盖 lecture.md）"),
              "- 错误：%d ｜ 警告：%d ｜ 审阅发现：%d" % (len(errors), len(warns), len(review)),
              "- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"), ""]
    if extra:
        report += extra + [""]
    if routing:
        report += ["## 归属（谁该修）", ""] + ["- %s：%d 条" % (k, len(v)) for k, v in sorted(routing.items())] + [""]
    for title, items in (("错误", errors), ("警告", warns), ("审阅发现", review)):
        if items:
            report += ["## %s" % title, ""] + [_line(e) for e in items] + [""]
    if not errors and not warns and not review:
        report += ["校验通过：manifest schema、字段类型、正文时间行（格式/单调/不重叠/不越章界/slide 归属）、"
                   "图存在、多图每图时间戳、keypoints/questions。", ""]
    paths.meta_dir().mkdir(parents=True, exist_ok=True)
    (paths.meta_dir() / "validation.md").write_text("\n".join(report), encoding="utf-8")
    doc = {"result": "pass" if not errors else "fail",
           "scope": scope,
           "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "errors": errors, "warnings": warns, "review": review,
           "routing": {k: v for k, v in sorted(routing.items())}}
    (paths.validation).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


def _stamp_slideset(paths, man: dict) -> dict | None:
    """把派单台账里最近一条记录搬进 manifest：**每章** slideset_id + 顶层汇总。

    只有 `brief --stage chapter` 会往台账里记（scaffold 不盖章——它能独立重跑，盖章会把
    真实漂移洗掉）。这里**只搬运、不自行取值**：collect 在写作之后才跑，「写手此刻看到的是
    哪一版图」只有派单那一刻知道。

    顶层（整集口径）只在**本次派单覆盖 manifest 全部章**时刷新，或 manifest 还没有顶层指纹时
    首次落值；否则部分派单会把「其余章仍写在旧版切片上」这件事在顶层洗掉。
    返回本次搬运的摘要（供 cli 打印）；没有台账记录时返回 None。
    """
    rec = slideset_layer.last_record(paths)
    if rec is None:
        return None
    sid = str(rec.get("slideset_id") or "")
    if not sid:
        return None
    stamped = {str(x) for x in (rec.get("chapters") or [])}
    ids = [str(c.get("id")) for c in man.get("chapters") or []]
    hit = [cid for cid in ids if cid in stamped]
    whole = bool(ids) and len(hit) == len(ids)
    changed = 0
    for ch in man.get("chapters") or []:
        if str(ch.get("id")) in stamped:
            if ch.get("slideset_id") != sid:
                changed += 1
            ch["slideset_id"] = sid
    top_updated = False
    if whole or not man.get("slideset_id"):
        top_updated = man.get("slideset_id") != sid
        man["slideset_id"] = sid
        man["slideset_algo"] = rec.get("algo") or slideset_layer.ALGO
        man["slideset_at"] = rec.get("at")
        man["slideset_count"] = rec.get("count")
    # remap 的「图注待复核」标记是待办：写手已按**当前**切片重新派单并 collect → 清掉
    cleared = False
    rm = man.get("slideset_remap")
    if isinstance(rm, dict) and str(rec.get("at") or "") > str(rm.get("at") or ""):
        if slideset_layer.current_id(paths.out) == sid:
            man.pop("slideset_remap", None)
            cleared = True
    return {"id": sid, "at": rec.get("at"), "chapters": hit, "whole": whole,
            "changed": changed, "top_updated": top_updated, "cleared_remap": cleared}


def collect(cfg, paths) -> dict:
    """把 _meta/patch/<章号>.json 汇总进 manifest（替代多 writer 并发写同一文件）。

    写手各自只写自己那章的补丁文件，编排者跑本命令汇总 —— 层与层只通过文件通信，且不丢更新。
    0b 起还负责**搬运切片指纹**（派单台账 → manifest 的每章 + 顶层）；因此写盘条件从
    「有补丁」放宽成「有补丁**或**有指纹搬运」——否则"没有补丁"这条常见路径会把指纹吞掉。
    """
    man = load(paths)
    if man is None:
        raise SystemExit("还没有 chapters/manifest.json，先跑 bnote scaffold")
    patch_dir = paths.meta_dir() / "patch"
    by_id = {str(c.get("id")): c for c in man.get("chapters", [])}
    applied, skipped = [], []
    if patch_dir.exists():
        for fp in sorted(patch_dir.glob("*.json")):
            try:
                doc = json.loads(fp.read_text(encoding="utf-8"))
            except Exception as exc:
                skipped.append("%s（解析失败：%s）" % (fp.name, exc))
                continue
            cid = str(doc.get("id") or fp.stem).zfill(2)
            ch = by_id.get(cid)
            if ch is None:
                skipped.append("%s（manifest 里没有章 %s）" % (fp.name, cid))
                continue
            n = 0
            for k in PATCH_KEYS:
                if k in doc and doc[k] not in (None, "", [], {}):
                    ch[k] = doc[k]
                    n += 1
            applied.append("%s←%s" % (cid, n))
    stamped = _stamp_slideset(paths, man)
    if applied or stamped:
        write(paths, man)
    return {"applied": applied, "skipped": skipped, "slideset": stamped}
