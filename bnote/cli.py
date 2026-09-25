"""bnote 命令行：每个阶段可单独重跑，阶段之间只通过 cache/ 里的文件通信。

  bnote run    <BV|URL>        全流程
  bnote meta   <BV|URL>        L1 元信息
  bnote fetch  <BV|URL>        L1+L2+L3 元信息/媒体/字幕
  bnote slides <BV|URL>        L4+L5+L6 抽帧/切片/OCR
  bnote bundle <BV|URL>        L7 交付物
  bnote merge  <BV|URL>        L8 合并成单文件讲义
  bnote brief  <BV|URL>        渲染派单 prompt（chapter/note）
  bnote glossary <BV|URL>      查看/修改/确认术语表提议
  bnote check  <BV|URL>        只跑结构校验并给出归属（谁该修）
  bnote scaffold <BV|URL>      自动生成章节结构（manifest + _plan）
  bnote note   <BV|URL>        L9 笔记素材 + note.md 校验
  bnote export <BV|URL>        L20 导出可直接粘贴进 B 站笔记的富文本（CF_HTML）
  bnote clean  <BV|URL>        --level cache|out|all
  bnote config                 打印当前生效配置（排查移植问题）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import auth_cli
from .config import describe, load
from .layers import auth as auth_layer
from .layers import body as body_layer
from .layers import export_bili_note as export_layer
from .layers import loop as loop_layer
from .layers import remap as remap_layer
from .layers import xref as xref_layer
from .layers import bundle as bundle_layer
from .layers import frames as frames_layer
from .layers import media as media_layer
from .layers import merge as merge_layer
from .layers import glossary as glossary_layer
from .layers import note as note_layer
from .layers import scaffold as scaffold_layer
from .layers import prompt as prompt_layer
from .layers import meta as meta_layer
from .layers import segment as segment_layer
from .layers import subtitle as subtitle_layer
from .layers import text as text_layer
from .layers.ocr import Ocr
from .paths import WorkPaths


def _overrides(args) -> dict:
    ov: dict = {}
    if args.fps is not None:
        ov.setdefault("frames", {})["fps"] = args.fps
    if args.max_height is not None:
        ov.setdefault("media", {})["max_height"] = args.max_height
    if args.sections is not None:
        ov.setdefault("media", {})["sections"] = args.sections
    if args.cookie_file:
        ov.setdefault("auth", {})["cookie_file"] = args.cookie_file
    if args.strategy:
        ov.setdefault("segment", {})["strategy"] = args.strategy
    if args.whisper_model:
        ov.setdefault("subtitle", {})["whisper_model"] = args.whisper_model
    if args.subtitle_backends:
        ov.setdefault("subtitle", {})["backends"] = args.subtitle_backends.split(",")
    if args.no_ocr:
        ov.setdefault("ocr", {})["enabled"] = False
    if args.audio_only:
        ov.setdefault("media", {})["audio_only"] = True
    if args.keep_video is not None:
        ov.setdefault("media", {})["keep_video"] = args.keep_video
    return ov


# 这些命令自己会打完整横幅（含解析后的路径），_ctx 不再重复打压缩行
_BANNER_CMDS = ("run", "fetch", "slides", "meta")
# 这些命令在"该集还没取数"时也必须能跑（取数本身，以及清理）
_NO_DATA_OK = ("run", "fetch", "slides", "meta", "stream", "clean")


def _ctx(args):
    """解析上下文。约定：
    * 除取数命令外，都要先有 cache/<vid>/meta.json —— 没有就以人话退出（而不是抛 traceback）；
    * 除取数命令外，都打印一行压缩的解析路径（路径搞错立刻可见，见 HANDOFF 原则 7）。
    """
    cfg = load(args.config, _overrides(args))
    bvid, page = meta_layer.parse_target(args.target, args.page)
    vid = "%s_p%d" % (bvid, page)
    cmd = getattr(args, "cmd", "") or ""
    p = cfg["paths"]
    remedy = ("bnote stream <URL> --page %s   # 信息流/口播类（无幻灯片）" % page
              if cmd in ("stream", "note") else
              "bnote run <URL> --page %s      # 幻灯片模式；口播/播客类改用 bnote stream" % page)
    if cmd not in _NO_DATA_OK and not (WorkPaths(cfg, vid).meta).exists():
        raise SystemExit(
            "该数据根下没有 %s 的取数结果，无法执行 bnote %s。\n"
            "  解析结果：skill=%s\n"
            "            root =%s\n"
            "  → 检查调用时的 cwd，或用 BNOTE_ROOT=<数据根> 指定；还没有取数就先跑：\n"
            "     %s" % (vid, cmd, p["skill_root"], p["root"], remedy))
    paths = WorkPaths(cfg, vid)
    if cmd != "clean":          # clean 不该在建出目录之后再告诉你要删什么
        paths = paths.ensure()
    if cmd not in _BANNER_CMDS:
        info = describe(cfg)
        print("[paths] %s ｜ %s ｜ vid=%s"
              % (info[0].split(":", 1)[1].strip(), info[1].split(":", 1)[1].strip(), vid))
    return cfg, paths, vid


def _banner(paths, cfg, vid):
    """打印解析后的根路径：路径搞错（比如 cwd 不同）会立刻暴露，而不是静默读错目录。"""
    print("=" * 72)
    print("bnote  vid=%s" % vid)
    for line in describe(cfg):
        print("  " + line)
    print("  work dir: %s" % paths.out)
    print("=" * 72)


def _stage_fetch(cfg, paths, args):
    cookie, netscape = auth_layer.resolve(cfg)
    print("[auth] 登录态: %s" % ("有" if cookie else "无（B站字幕轨将不可用，走本地 ASR）"))
    meta = meta_layer.get_or_fetch(cfg, paths, cookie, refresh=args.force)
    print("[meta] %s | %s | %ss" % (meta["bvid"], meta.get("part"), meta.get("duration")))
    media_path = media_layer.download(cfg, paths, meta, cookie, netscape, force=args.force)
    transcript = subtitle_layer.get(cfg, paths, meta, media_path, cookie, force=args.force)
    return meta, media_path, transcript


def _stage_slides(cfg, paths, args):
    meta = json.loads(paths.meta.read_text(encoding="utf-8"))
    media_path = media_layer.find_media(paths)
    if media_path is None:
        raise SystemExit("没有媒体文件，请先执行 bnote fetch")
    transcript = None
    tp = paths.subtitle / "transcript.json"
    if tp.exists():
        transcript = json.loads(tp.read_text(encoding="utf-8"))
    frames = frames_layer.extract(cfg, paths, media_path, force=args.force)
    ocr = Ocr(cfg, paths)
    seg = segment_layer.build(cfg, paths, frames, transcript, ocr, force=args.force)
    if not cfg["media"].get("keep_video", True):
        media_path.unlink()
        print("[media] 已按配置删除源视频: %s" % media_path.name)
    return meta, transcript, seg


def cmd_run(args):
    cfg, paths, vid = _ctx(args)
    _banner(paths, cfg, vid)
    t0 = time.time()
    meta, media_path, transcript = _stage_fetch(cfg, paths, args)
    meta, transcript, seg = _stage_slides(cfg, paths, args)
    if transcript is None:
        transcript = {"segments": [], "backend": "none", "source": "无"}
    bundle_layer.build(cfg, paths, meta, transcript, seg)
    _stage_merge(cfg, paths)
    _stage_note(cfg, paths)
    print("")
    print("完成，用时 %.1fs" % (time.time() - t0))
    print("清理中间产物: bnote clean %s --level cache" % args.target)
    return 0


def _stage_merge(cfg, paths):
    meta = json.loads(paths.meta.read_text(encoding="utf-8"))
    seg = json.loads(paths.segments.read_text(encoding="utf-8"))
    tp = paths.subtitle / "transcript.json"
    transcript = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else None
    if not cfg.get("merge", {}).get("enabled", True):
        return {}
    result = merge_layer.build(cfg, paths, meta, seg, transcript)
    # note_brief 在章节写完后才有内容：合并成功后刷新一次，避免写作 agent 读到空统计
    if result.get("ok"):
        try:
            note_layer.build_brief(cfg, paths, meta, seg, transcript)
        except Exception as exc:
            print("[note] brief 刷新失败：%s" % exc)
    return result


def _stage_note(cfg, paths):
    meta = json.loads(paths.meta.read_text(encoding="utf-8"))
    seg = _load_stage_inputs(paths)
    tp = paths.subtitle / "transcript.json"
    transcript = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else None
    return note_layer.build(cfg, paths, meta, seg, transcript)


def _load_stage_inputs(paths):
    """幻灯片模式读 segments.json；信息流模式（bnote stream）读 _meta/paragraphs.json。"""
    if paths.segments.exists():
        return json.loads(paths.segments.read_text(encoding="utf-8"))
    pp = paths.meta_dir() / "paragraphs.json"
    if pp.exists():
        doc = json.loads(pp.read_text(encoding="utf-8"))
        return {"mode": "text", "segments": doc.get("paragraphs") or []}
    raise SystemExit("这个集里既没有 segments.json（幻灯片模式）也没有 paragraphs.json（信息流模式）——\n"
                     "  幻灯片模式： bnote run <URL> --page N\n"
                     "  信息流模式： bnote stream <URL> --page N")


def cmd_stream(args):
    """信息流 / 口播类（无幻灯片）：只取音频 + 字幕 → 机械分段 → 分块任务书 → 拼接校验。

    不做抽帧、不切片、不 OCR、不分章；交付 transcript.md（原样）与 lecture.md（整理稿，逐段覆盖不摘要）。
    """
    cfg, paths, vid = _ctx(args)
    _banner(paths, cfg, vid)
    if args.assemble or args.verify:
        if args.assemble:
            text_layer.assemble(cfg, paths)
        errors, warns = text_layer.validate(cfg, paths)
        p = text_layer.write_validation(paths, errors, warns)
        for e in errors:
            print("[text] ✗ [%s] %s" % (e.get("owner"), e.get("message")))
        for w in warns:
            print("[text] ⚠ [%s] %s" % (w.get("owner"), w.get("message")))
        print("[text] 校验报告 → %s（%d 错 / %d 警）" % (p, len(errors), len(warns)))
        return 1 if errors else 0
    cfg.setdefault("media", {})["audio_only"] = True
    cookie, netscape = auth_layer.resolve(cfg)
    print("[auth] 登录态: %s" % ("有" if cookie else "无（拿不到官方字幕轨；三条路见 SKILL.md）"))
    meta = meta_layer.get_or_fetch(cfg, paths, cookie, refresh=args.force)
    print("[meta] %s | %s | %ss" % (meta["bvid"], meta.get("part"), meta.get("duration")))
    tp = paths.subtitle / "transcript.json"
    if tp.exists() and not args.force:
        transcript = json.loads(tp.read_text(encoding="utf-8"))
        print("[subtitle] 复用缓存的 transcript.json（%d 段，backend=%s）"
              % (len(transcript.get("segments") or []), transcript.get("backend")))
    else:
        media_path = media_layer.download(cfg, paths, meta, cookie, netscape, force=args.force)
        transcript = subtitle_layer.get(cfg, paths, meta, media_path, cookie, force=args.force)
    res = text_layer.build(cfg, paths, meta, transcript)
    print("[text] 分段 %d ｜ 分块 %d" % (res["paragraphs"], res["chunks"]))
    print("[text] 下一步：")
    print("  1) 把 %s 交给 writer（一块一块读 → 写到 out/<vid>/text/NN.md）" % (paths.meta_dir() / "prompt_text.md"))
    print("  2) 写完后： bnote stream <URL> --page %s --assemble" % paths.vid.split("_p")[-1])
    print("  3) 然后： bnote note <URL> --page %s（信息流模式的 note 素材已就位）" % paths.vid.split("_p")[-1])
    return 0


def cmd_fetch(args):
    cfg, paths, vid = _ctx(args)
    _banner(paths, cfg, vid)
    _stage_fetch(cfg, paths, args)
    return 0


def cmd_slides(args):
    cfg, paths, vid = _ctx(args)
    _banner(paths, cfg, vid)
    _stage_slides(cfg, paths, args)
    return 0


def cmd_meta(args):
    cfg, paths, vid = _ctx(args)
    _banner(paths, cfg, vid)
    cookie, _ = auth_layer.resolve(cfg)
    meta = meta_layer.get_or_fetch(cfg, paths, cookie, refresh=args.force)
    print(json.dumps({k: meta.get(k) for k in ("bvid", "part", "cid", "duration", "page_count")},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_bundle(args):
    cfg, paths, vid = _ctx(args)
    meta = json.loads(paths.meta.read_text(encoding="utf-8"))
    transcript = json.loads((paths.subtitle / "transcript.json").read_text(encoding="utf-8"))
    seg = json.loads(paths.segments.read_text(encoding="utf-8"))
    bundle_layer.build(cfg, paths, meta, transcript, seg)
    return 0


def cmd_brief(args):
    """渲染派单 prompt：chapter / note / fix / review（fix 与 review 是 v0.7.0 新增的可选阶段）"""
    cfg, paths, vid = _ctx(args)
    meta = json.loads(paths.meta.read_text(encoding="utf-8"))
    prompt_layer.render(cfg, paths, meta, args.stage,
                        allow_unconfirmed=args.allow_unconfirmed,
                        owner=args.owner, scope=args.scope, focus=args.focus,
                        round_no=args.round)
    return 0


def cmd_glossary(args):
    """查看 / 修改 / 确认术语表提议（<state>/glossary/<vid>.json）"""
    cfg, paths, vid = _ctx(args)
    if args.drop or args.add or args.avoid or args.confirm:
        doc = glossary_layer.review(cfg, paths, drop=args.drop, add=args.add,
                                    avoid=args.avoid, confirm=args.confirm, by=args.by)
        print("[glossary] 已更新 %s（confirmed=%s）" % (glossary_layer.path_of(cfg, vid), doc.get("confirmed")))
    doc = glossary_layer.load(cfg, vid)
    if doc is None:
        print("还没有提议文件；先跑一次 bnote brief 生成，或在配置目录下手建")
        return 1
    print("文件: %s" % glossary_layer.path_of(cfg, vid))
    print("状态: %s%s" % ("已确认" if doc.get("confirmed") else "未确认（脚本自动提议）",
                          ("，确认人 " + str(doc.get("confirmed_by"))) if doc.get("confirmed_by") else ""))
    print("白名单(%d): %s" % (len(doc.get("terms_use") or []), "、".join(doc.get("terms_use") or [])))
    av = doc.get("terms_avoid") or {}
    print("黑名单(%d): %s" % (len(av), "、".join("%s->%s" % (k, v) for k, v in list(av.items())[:20])))
    prop = doc.get("_proposal") or {}
    if prop.get("speech_only"):
        print("仅字幕出现（可疑，建议人工判断）: %s"
              % "、".join("%s×%d" % (i["term"], i["count"]) for i in prop["speech_only"][:12]))
    if prop.get("auto_avoid"):
        print("自动判定为 ASR 误写: %s" % "、".join("%s->%s" % (k, v) for k, v in prop["auto_avoid"].items()))
    return 0


def _chapter_scope(args):
    raw = getattr(args, "chapter", None)
    if not raw:
        return None
    out = []
    for x in str(raw).replace("，", ",").split(","):
        x = x.strip()
        if x:
            out.append(x.zfill(2) if x.isdigit() else x)
    return out or None


def cmd_check(args):
    """只跑结构校验（不写 lecture.md）。--chapter 限定作用域，供写作 agent 自检。"""
    cfg, paths, vid = _ctx(args)
    if not (paths.chapters() / "manifest.json").exists() and (paths.meta_dir() / "paragraphs.json").exists():
        errors, warns = text_layer.validate(cfg, paths)   # 信息流模式
        p = text_layer.write_validation(paths, errors, warns)
        for e in errors:
            print("[text] ✗ [%s] %s" % (e.get("owner"), e.get("message")))
        for w in warns:
            print("[text] ⚠ [%s] %s" % (w.get("owner"), w.get("message")))
        print("结果：%s（%d 错 / %d 警）→ %s" % ("通过" if not errors else "不通过", len(errors), len(warns), p))
        return 1 if errors else 0
    from .layers import manifest as manifest_layer
    meta = json.loads(paths.meta.read_text(encoding="utf-8"))
    tp = paths.subtitle / "transcript.json"
    transcript = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else None
    only = _chapter_scope(args)
    man, errors, warns = manifest_layer.validate_all(cfg, paths, meta, transcript, only=only)
    if man is None:
        print("还没有 chapters/manifest.json（章节结构未建立）")
        return 1
    doc = None
    if only:
        # 作用域模式：写独立报告，**不覆盖**权威的 validation.json（那是全量结果）
        import time as _t
        (paths.meta_dir() / ("check_%s.json" % "_".join(only))).write_text(
            json.dumps({"result": "pass" if not errors else "fail", "scope": ",".join(only),
                        "checked_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
                        "errors": errors, "warnings": warns}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    else:
        review = (json.loads(paths.validation.read_text(encoding="utf-8")).get("review")
                  if paths.validation.exists() else None)
        doc = manifest_layer.write_validation(paths, errors, warns, review=review)
    routing = {}
    for e in list(errors) + list(warns):
        routing.setdefault(e.get("owner", "?"), []).append(e)
    for k in sorted(routing):
        print("%-14s %d 条" % (k, len(routing[k])))
        for e in routing[k][:6]:
            print("   %s %s" % (e.get("message"), ("→ " + e["fix_hint"]) if e.get("fix_hint") else ""))
    print("结果：%s（%d 错 / %d 警%s）" % ("通过" if not errors else "未通过", len(errors), len(warns),
                                        "，作用域 %s" % ",".join(only) if only else ""))
    if doc is not None and doc.get("result") == "fail":
        print("派修： bnote fix                 打印待修清单与投递对象")
    return 0 if not errors else 1


def cmd_scaffold(args):
    """按切片结果生成章节结构（manifest.json + _plan.md）。

    agent 决策节点必须生效：默认**要求** --groups（分章是语义判断，由编排者/人给），
    只有显式 --auto 才接受"一页一章"的退化产物。
    """
    cfg, paths, vid = _ctx(args)
    meta = json.loads(paths.meta.read_text(encoding="utf-8"))
    if not args.groups and not args.auto:
        raise SystemExit(
            "分章是语义判断，请先给出分组 spec（避免退化成「一页一章」）：\n"
            "  1) bnote scaffold --page %s --auto            # 先看自动产物与 _plan.md\n"
            "  2) 写 out/%s/chapters/_groups.json：[{\"slides\":[1,2],\"title\":\"…\"}]\n"
            "  3) bnote scaffold --page %s --groups out/%s/chapters/_groups.json"
            % (paths.vid.split("_p")[-1], paths.vid, paths.vid.split("_p")[-1], paths.vid))
    scaffold_layer.build(cfg, paths, meta, leading_merge=not args.no_leading_merge, groups_path=args.groups)
    return 0


def cmd_collect(args):
    """把 _meta/patch/<章号>.json 汇总进 manifest（多 writer 并发时的安全写法）"""
    cfg, paths, vid = _ctx(args)
    from .layers import manifest as manifest_layer
    res = manifest_layer.collect(cfg, paths)
    for a in res["applied"]:
        print("[collect] 已合并 %s" % a)
    for s in res["skipped"]:
        print("[collect] 跳过 %s" % s)
    if not res["applied"]:
        print("[collect] 没有可合并的补丁（_meta/patch/*.json）")
    return 0


def cmd_retime(args):
    """按 slides.json 幂等重写正文小节时间行（时间由工具生成，写手只写 slide 号）"""
    cfg, paths, vid = _ctx(args)
    from .layers import manifest as manifest_layer
    man = manifest_layer.load(paths)
    if man is None:
        raise SystemExit("还没有 chapters/manifest.json")
    only = _chapter_scope(args)
    for line in body_layer.retime(man, paths, cfg, only=only):
        print("[retime] %s" % line)
    print("[retime] 已按 slides.json 统一时间行（幂等）；接着跑 bnote check")
    return 0


def cmd_patch(args):
    """打印某章的补丁文件路径与 schema（写手把结果写这里，而不是改 manifest.json）"""
    cfg, paths, vid = _ctx(args)
    only = _chapter_scope(args)
    if not only:
        raise SystemExit("需要 --chapter 06[,07]")
    for cid in only:
        p = paths.meta_dir() / "patch" / ("%s.json" % cid)
        print("[patch] 章 %s → %s" % (cid, p))
    print('schema: {"id":"06","keypoints":[…],"questions":[…],'
          '"corrections":[{"wrong":…,"right":…,"evidence":…}],"review_flags":[…],'
          '"coverage_notes":"…","stage_merges":[{"slides":[5,6],"kept":6,"why":"…"}]}')
    print("写完由编排者跑 bnote collect 汇总")
    return 0


def cmd_dispatch(args):
    """把 owner 与 subagent id 绑定（父 agent 派单后登记一次，修复时才知道发给谁）"""
    cfg, paths, vid = _ctx(args)
    if not args.owner or not args.agent:
        raise SystemExit("用法： bnote dispatch --owner chapter:01,02 --agent <subagent-id>")
    owners = [o.strip() for o in args.owner.replace("，", ",").split(",") if o.strip()]
    loop_layer.dispatch(cfg, paths, owners, args.agent)
    print("[dispatch] %s → %s" % (", ".join(owners), args.agent))
    return 0


def cmd_fix(args):
    """修复队列：不带参数打印待修清单与投递对象（编排者据此发回原 agent 或改派新代理）；--done 标记已修。"""
    cfg, paths, vid = _ctx(args)
    if args.done:
        loop_layer.mark(paths, args.done, "fixed", round_no=args.round)
        print("[fix] 已标记 %s 修复完成（写进 _meta/loop.json）" % args.done)
        return 0
    items = loop_layer.next_dispatch(cfg, paths, paths.fixes())
    if not items:
        print("[fix] 没有待修项（validation.json 里无 error / major review）")
        return 0
    for it in items:
        who = it["agent"] or "（未登记 agent：先跑 bnote dispatch --owner %s --agent <id>）" % it["owner"]
        tag = "  ⚠ 已达轮次上限，建议升级给人" if it["escalate"] else ""
        print("- %s（%d 条，第 %d 轮）%s" % (it["owner"], it["count"], it["round"], tag))
        print("    修复件 : %s" % it["fix_file"])
        print("    投递   : %s" % it["deliver_hint"])
        if it["agent"]:
            print("    （原 agent 仍可用时：send_message(%s, …) ｜ %s）" % (it["agent"], who))
    print("提示：修复件由 bnote brief --stage fix --owner <owner> 生成；跑完 `bnote check` 再 `bnote fix --done <owner>`")
    return 0


def cmd_xref(args):
    """把多集的钩子与结构汇成跨讲综合工作表（agent 据此写索引；工具不做语义判断）"""
    cfg = load(args.config, _overrides(args))
    from .layers import meta as _meta
    bvid, _ = _meta.parse_target(args.target, args.page)
    root = Path(cfg["paths"]["out_root"])
    missing = [p for p in range(args.from_page, args.to_page + 1)
               if not (root.parent / "cache" / ("%s_p%d" % (bvid, p)) / "meta.json").exists()]
    if missing:
        raise SystemExit(
            "这些集还没有取数结果：%s\n  → 先 bnote run <URL> --page <N>，或检查 BNOTE_ROOT / cwd（当前 root=%s）"
            % (", ".join("p%d" % p for p in missing), cfg["paths"]["root"]))
    res = xref_layer.build(cfg, root, bvid, args.from_page, args.to_page)
    print("[xref] 综合工作表 → %s（%d 集，%d 条钩子）" % (res["path"], res["episodes"], res["hooks"]))
    if res["missing_note"]:
        print("[xref] 注意：这些集还没 note.md（钩子会缺）→ %s" % ", ".join(res["missing_note"]))
    return 0


def cmd_remap(args):
    """重切片后按时间重叠把旧图号映射到新编号（幂等）；无 --from 时用 bundle 的快照"""
    cfg, paths, vid = _ctx(args)
    page = paths.vid.split("_p")[-1]
    prev = Path(args.from_file) if getattr(args, "from_file", None) else \
        (paths.cache / ("_prev_slides_%s.json" % page))
    if not prev.exists():
        raise SystemExit("找不到上一版 slides.json：%s\n（bundle 每次都会快照一份；或显式 --from <路径>）" % prev)
    res = remap_layer.run(cfg, paths, prev, dry=getattr(args, "dry_run", False),
                          force=getattr(args, "force_map", False))
    return 0


def cmd_review(args):
    """摄入审阅发现（_meta/review_<n>.json）→ 归一 owner → 并入同一条派修流"""
    cfg, paths, vid = _ctx(args)
    path = args.ingest or (paths.meta_dir() / ("review_%d.json" % args.round))
    from pathlib import Path as _P
    if not _P(str(path)).exists():
        raise SystemExit("找不到审阅发现文件：%s" % path)
    res = loop_layer.review_ingest(cfg, paths, _P(str(path)))
    for a in res["accepted"]:
        print("[review] %s %s %s" % (a["scope"], a["owner"], a["message"][:60]))
    for r in res["rejected"]:
        print("[review] 驳回：%s ← %r" % (r["why"], r["item"]))
    print("[review] 摄入 %d 条（%d 条驳回）；接着 bnote fix" % (len(res["accepted"]), len(res["rejected"])))
    return 0


def cmd_note(args):
    cfg, paths, vid = _ctx(args)
    _stage_note(cfg, paths)
    return 0


def cmd_export(args):
    """把讲义/笔记导出成「可直接粘贴进 B 站笔记」的富文本（CF_HTML）→ out/<vid>/_meta/。

    工具只做**格式兼容**（产出 CF_HTML + Windows 装载脚本），不调用平台的写接口；
    粘贴动作由人在笔记编辑器里完成 —— 时间标签必须整节点粘进去才可点（见 layers/export_bili_note.py）。
    """
    cfg, paths, vid = _ctx(args)
    export_layer.build(cfg, paths, source=args.from_source, out_dir=args.out)
    return 0


def cmd_merge(args):
    cfg, paths, vid = _ctx(args)
    _stage_merge(cfg, paths)
    return 0


def _human(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fP" % n


def cmd_clean(args):
    """清理前先报占用（可配根 → 先看清再删）。state 单独一档：术语表与画像沉淀不随 cache 走。"""
    cfg, paths, vid = _ctx(args)
    levels = ["state", "cache", "out"] if args.level == "all" else [args.level]
    for lv in levels:
        print("[clean] %-5s %s  %s" % (lv, _human(paths.size(lv)), getattr(paths, lv)))
    if args.dry_run:
        print("[clean] --dry-run：未删除")
        return 0
    removed = paths.clean(args.level)
    print("已删除: %s" % (", ".join(removed) if removed else "无"))
    return 0


def cmd_auth(args):
    cfg = load(args.config, None)
    if args.action == "login":
        return auth_cli.login(cfg, timeout=args.timeout,
                              show_png=not args.no_png)
    if args.action == "status":
        return auth_cli.status(cfg)
    if args.action == "set":
        sess = args.sessdata
        if sess in (None, "-"):
            sess = sys.stdin.read().strip()
            if sess.startswith("SESSDATA="):
                sess = sess.split("=", 1)[1].strip()
        return auth_cli.set_manual(cfg, sess)
    print("未知动作: %s" % args.action, file=sys.stderr)
    return 2


def cmd_config(args):
    cfg = load(args.config, None)
    if args.paths:
        for line in describe(cfg):
            print(line)
        return 0
    if args.md:
        # 参数说明（给 references/params.md 用；工具生成，避免文档与代码漂移）
        print("| 段 | 键 | 默认值 |")
        print("|---|---|---|")
        from .config import DEFAULT_TOML, _toml as _t
        raw = _t.loads(DEFAULT_TOML.read_text(encoding="utf-8"))
        for sec in sorted(raw):
            for k in sorted(raw[sec]):
                print("| %s | %s | `%r` |" % (sec, k, raw[sec][k]))
        return 0
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="bnote", description="B站视频 → slide + 讲义 流水线")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("target", nargs="?", default="", help="BV 号或视频 URL")
        sp.add_argument("--page", type=int, default=None, help="分P（URL 带 p= 时可省）")
        sp.add_argument("-c", "--config", default=None, help="local.toml 路径")
        sp.add_argument("--force", action="store_true", help="重跑当前阶段（忽略已有产物）")
        sp.add_argument("--fps", type=float, default=None)
        sp.add_argument("--max-height", type=int, default=None)
        sp.add_argument("--sections", default=None, help="只下片段，如 00:00:00-00:05:00")
        sp.add_argument("--strategy", default=None, choices=["stable", "scene"])
        sp.add_argument("--cookie-file", default=None)
        sp.add_argument("--whisper-model", default=None)
        sp.add_argument("--subtitle-backends", default=None, help="如 bili,file,whisper")
        sp.add_argument("--no-ocr", action="store_true")
        sp.add_argument("--audio-only", action="store_true")
        sp.add_argument("--keep-video", dest="keep_video", action="store_true", default=None)
        sp.add_argument("--drop-video", dest="keep_video", action="store_false")

    for name, fn in (("run", cmd_run), ("fetch", cmd_fetch), ("slides", cmd_slides),
                     ("meta", cmd_meta), ("bundle", cmd_bundle)):
        sp = sub.add_parser(name)
        common(sp)
        sp.set_defaults(func=fn)

    sp = sub.add_parser("stream", help="信息流/口播类（无幻灯片）：只取音频+字幕，不抽帧、不切片、不分章")
    common(sp)
    sp.add_argument("--assemble", action="store_true", help="把各块产出拼成 lecture.md 并校验")
    sp.add_argument("--verify", action="store_true", help="只校验（不拼接）")
    sp.set_defaults(func=cmd_stream)

    sp = sub.add_parser("merge", help="把各章讲义合并成单文件 Markdown")
    common(sp)
    sp.set_defaults(func=cmd_merge)

    sp = sub.add_parser("brief", help="渲染派单 prompt（chapter/note/fix/review）")
    common(sp)
    sp.add_argument("--stage", default="both", choices=["chapter", "note", "fix", "review", "both"])
    sp.add_argument("--owner", default=None, help="fix 阶段：chapter:06 / manifest / pipeline")
    sp.add_argument("--allow-unconfirmed", action="store_true",
                    help="术语表未确认也强行派发章节写作（默认拒绝，agent 决策节点必须生效）")
    sp.add_argument("--scope", default="all", help="review 阶段：all / chapter:06,07 / sample:3")
    sp.add_argument("--focus", default="fidelity", choices=["fidelity", "depth"], help="review 重点")
    sp.add_argument("--round", type=int, default=1, help="review/fix 轮次（写进文件名与台账）")
    sp.set_defaults(func=cmd_brief)

    sp = sub.add_parser("glossary", help="查看/修改/确认术语表提议")
    common(sp)
    sp.add_argument("--drop", nargs="*", default=None, help="从白名单删除这些写法")
    sp.add_argument("--add", nargs="*", default=None, help="往白名单追加写法")
    sp.add_argument("--avoid", nargs="*", default=None, help="追加误写对，形如 RNG->RAG")
    sp.add_argument("--confirm", action="store_true", help="标记为已确认")
    sp.add_argument("--by", default=None, help="确认人标识")
    sp.set_defaults(func=cmd_glossary)

    sp = sub.add_parser("check", help="只跑结构校验（不写 lecture.md）")
    common(sp)
    sp.add_argument("--chapter", default=None, help="只查这些章（写作 agent 自检用），如 06 或 06,07")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("scaffold", help="生成章节结构（manifest + _plan）")
    common(sp)
    sp.add_argument("--no-leading-merge", action="store_true", help="封面不与目录合并")
    sp.add_argument("--groups", default=None, help="分章 spec（JSON: [{slides:[1,2], title:...}]）")
    sp.add_argument("--auto", action="store_true",
                    help="显式接受自动分章（一页一章的退化产物）；不给 --groups 时必须显式声明")
    sp.set_defaults(func=cmd_scaffold)

    sp = sub.add_parser("collect", help="把 _meta/patch/<章号>.json 汇总进 manifest")
    common(sp)
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("retime", help="按 slides.json 幂等重写正文小节时间行")
    common(sp)
    sp.add_argument("--chapter", default=None, help="只处理这些章，如 06,09")
    sp.set_defaults(func=cmd_retime)

    sp = sub.add_parser("xref", help="汇总多集钩子与结构 → 跨讲综合工作表")
    common(sp)
    sp.add_argument("--from-page", dest="from_page", type=int, required=True, help="起始 P 号")
    sp.add_argument("--to-page", dest="to_page", type=int, required=True, help="结束 P 号")
    sp.set_defaults(func=cmd_xref)

    sp = sub.add_parser("remap", help="重切片后按时间重叠同步正文与 manifest 的图号（幂等）")
    common(sp)
    sp.add_argument("--from", dest="from_file", default=None,
                    help="上一版 slides.json（默认用 bundle 自动快照的 cache/_prev_slides_<P>.json）")
    sp.add_argument("--dry-run", action="store_true", help="只打印映射，不改文件")
    sp.add_argument("--force-map", dest="force_map", action="store_true", help="即使已同步也重写")
    sp.set_defaults(func=cmd_remap)

    sp = sub.add_parser("patch", help="打印某章的补丁文件路径与 schema")
    common(sp)
    sp.add_argument("--chapter", default=None, help="章号，如 06 或 06,07")
    sp.set_defaults(func=cmd_patch)

    sp = sub.add_parser("dispatch", help="登记 owner → subagent id（修复时才知道发给谁）")
    common(sp)
    sp.add_argument("--owner", default=None, help="如 chapter:01,02（逗号分隔）")
    sp.add_argument("--agent", default=None, help="subagent id")
    sp.set_defaults(func=cmd_dispatch)

    sp = sub.add_parser("fix", help="修复队列（不带参数打印待修清单；--done <owner> 标记完成）")
    common(sp)
    sp.add_argument("--done", default=None, help="标记该 owner 已修复，如 chapter:06")
    sp.add_argument("--round", type=int, default=None, help="轮次（写进 _meta/loop.json）")
    sp.set_defaults(func=cmd_fix)

    sp = sub.add_parser("review", help="摄入审阅发现（_meta/review_<n>.json）并入派修流")
    common(sp)
    sp.add_argument("--ingest", default=None, help="发现文件路径（默认 _meta/review_<round>.json）")
    sp.add_argument("--round", type=int, default=1)
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("note", help="汇总笔记素材并校验 note.md")
    common(sp)
    sp.set_defaults(func=cmd_note)

    sp = sub.add_parser("export", help="导出可粘贴进 B 站笔记的富文本（CF_HTML + Windows 装载脚本）")
    common(sp)
    sp.add_argument("--format", default="bili-note", choices=["bili-note"],
                    help="导出格式（目前只有 bili-note：B 站笔记的时间锚点）")
    sp.add_argument("--from", dest="from_source", default="lecture", choices=["lecture", "note"],
                    help="来源：lecture=讲义小节（默认）｜note=笔记节点")
    sp.add_argument("--out", default=None, help="输出目录（默认 <数据根>/out/<vid>/_meta）")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("clean", help="清理中间产物/交付物（先报占用；state 单独一档）")
    common(sp)
    sp.add_argument("--level", default="cache", choices=["state", "cache", "out", "all"])
    sp.add_argument("--dry-run", action="store_true", help="只报占用，不删")
    sp.set_defaults(func=cmd_clean)

    sp = sub.add_parser("auth", help="B站登录态（扫码/手动/检查）")
    sp.add_argument("action", choices=["login", "set", "status"])
    sp.add_argument("--sessdata", default=None, help="set 时使用；传 - 从 stdin 读取")
    sp.add_argument("--timeout", type=float, default=180.0, help="扫码等待秒数")
    sp.add_argument("--no-png", action="store_true", help="不保存二维码 PNG")
    sp.add_argument("-c", "--config", default=None)
    sp.set_defaults(func=cmd_auth, page=None, target="")

    sp = sub.add_parser("config", help="打印生效配置 / 解析后的路径 / 参数表")
    sp.add_argument("-c", "--config", default=None)
    sp.add_argument("--paths", action="store_true", help="只打印解析后的各根路径")
    sp.add_argument("--md", action="store_true", help="输出参数表（Markdown，供 references/params.md）")
    sp.set_defaults(func=cmd_config, page=None)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd not in ("config", "auth") and not getattr(args, "target", ""):
        print("需要一个 BV 号或 URL", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())