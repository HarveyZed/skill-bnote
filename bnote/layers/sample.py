"""L4.6 取样包（M5，契约见 VISION-PLAN §3.6）：判型与信息流画面旁证用的「少量窗口取样帧」。

每窗**单独**下一次（默认片头 10% / 中段 50% / 片尾 90%，各 [sample].window_sec），再用 ffmpeg concat
拼成一个媒体文件，最后 ffmpeg 定频抽帧 —— 产物全部落在**取样包私有根** cache/<vid>/sample/：
（为什么不一次下多窗口：实测 yt-dlp 2026.08.19 对 B站分片 MP4 **只下第一段**，见 _download_windows 的注释）

    sample/media/sample_video.mp4   取样媒体（**绝不**写 cache/<vid>/media/：那里会被
                                    media.find_media() 当成整片复用）
    sample/frames/000001.jpg …      取样帧
    sample/index.json               取样索引 = **权威时间映射**（分段线性）
    sample/overlay.json             取样帧上的遮罩（M5 第二步接线）
    sample/measure.json             取样片段上的量测（M5 第二步接线）

顶层 cache/<vid>/overlay.json / measure.json 是「整片 2fps 抽帧」的产物，**语义不变**：判型发生在
"还没决定跑哪种模式"之前，覆盖它们会顺手污染后面的 slides 路径；顶层 measure.json 连 basis 字段
都不加（§3.6-5 定稿），所以同一个输入的字节不变、M1 的确定性锚点继续有效。

**时间映射必须自己做**：frames.section_offset() 只解析 media.sections 的**第一个**窗口
（SECTION_RE.match），多窗口拼接下第二窗起的时间会整体偏掉。本模块用 sections 的分段线性
映射（to_original），并带自检 selfcheck_mapping()——它显式断言"不是单偏移实现"，
否则只有一个窗口的实现也能蒙混过关。

产物**不放时间戳**（两次跑逐字节一致）；只由显式 `bnote sample` 触发 —— [sample].enabled 是给
未来自动路径预留的，显式命令不受它拦（与 [sheet] 同一条约定，§3.6-5）。
"""
from __future__ import annotations

import copy
import shutil
import subprocess
import time
from pathlib import Path

from ..tools import find_ffmpeg, probe_media
from . import auth as auth_layer
from . import media as media_layer

SCHEMA = "bnote-sample-frames/1"
ALGO = "bnote-sample/1"
ANCHORS = (0.10, 0.50, 0.90)          # 片头 / 中段 / 片尾（§3.6-2 规则 3）
COVERAGE_NOTE = "只看了这些窗口，不代表全片"


def _params(cfg: dict) -> dict:
    """生效参数（默认值写在这里；config/default.toml 的 [sample] 是同一份口径）。"""
    s = cfg.get("sample") or {}
    return {"window_sec": float(s.get("window_sec", 30.0)),
            "fps": float(s.get("fps", 1.0)),
            "scale_height": int(s.get("scale_height", 720) or 0),
            "max_height": int(s.get("max_height", 720) or 0),
            "dur_tol_sec": float(s.get("dur_tol_sec", 0.5)),
            "first_frame_tol_sec": float(s.get("first_frame_tol_sec", 1.0)),
            "keep_parts": bool(s.get("keep_parts", True)),
            "anchors": [float(x) for x in (s.get("window_anchors") or ANCHORS)]}


# ---------------------------------------------------------------- 窗口规划与时间映射（纯函数，可单测）

def plan_windows(duration: float, window_sec: float, anchors) -> list:
    """片头/中段/片尾各一个窗口：起点取锚点、**夹取到片长内**、**互不重叠**。

    片长 <= 一个窗口时退化成整段一个窗口 —— 这比产出三个互相重叠的窗口诚实。
    窗口放不下就丢掉该窗口（片长极短时宁可少看，也不重叠）。
    """
    if duration <= 0:
        return []
    w = min(float(window_sec), float(duration))
    if duration <= w + 1e-6:
        return [{"orig_from": 0.0, "orig_to": round(float(duration), 3)}]
    out, prev_end = [], 0.0
    for a in sorted(float(x) for x in anchors):
        s0 = max(duration * a, prev_end)          # 与上一个窗口重叠就右移
        if s0 + w > duration:
            s0 = duration - w                     # 越界就整体往前挪
        if s0 < prev_end - 1e-6 or s0 + w > duration + 1e-6:
            continue                              # 挪不动（片长不够放这么多窗口）→ 丢掉
        out.append({"orig_from": round(s0, 3), "orig_to": round(s0 + w, 3)})
        prev_end = s0 + w
    return out


def sections_of(windows: list) -> list:
    """窗口 → sections（补上**拼接媒体时间轴**上的 media_from/media_to）。"""
    out, acc = [], 0.0
    for w in windows:
        ln = round(float(w["orig_to"]) - float(w["orig_from"]), 3)
        out.append({"orig_from": float(w["orig_from"]), "orig_to": float(w["orig_to"]),
                    "media_from": round(acc, 3), "media_to": round(acc + ln, 3)})
        acc += ln
    return out


def _hms(x: float) -> str:
    """秒 → HH:MM:SS（yt-dlp 的 --download-sections 只认整秒时间戳）。"""
    t = int(round(float(x)))
    return "%02d:%02d:%02d" % (t // 3600, (t % 3600) // 60, t % 60)


def sections_param(sections: list) -> str:
    """人读/日志用的窗口串（各窗一个 A-B；实际下载是**每窗一次**，见 _download_windows）。"""
    return " / ".join("%s-%s" % (_hms(s["orig_from"]), _hms(s["orig_to"])) for s in sections)


def to_original(media_t: float, sections: list) -> float:
    """拼接媒体时间轴 → **原始时间轴**（分段线性）。

    第 j 窗的媒体区间是 [media_from_j, media_to_j)，其中 media_from_j = Σ_{i<j} 窗长。
    这是本模块最容易写错的一处：**不许**换成 frames.section_offset()（它只认第一个窗口）。
    """
    rest = max(0.0, float(media_t))
    last = len(sections) - 1
    for k, s in enumerate(sections):
        ln = float(s["media_to"]) - float(s["media_from"])
        if rest < ln or k == last:
            off = min(rest, ln) if ln > 0 else 0.0
            return round(float(s["orig_from"]) + off, 3)
        rest -= ln
    return round(float(sections[last]["orig_to"]), 3)


def window_of(media_t: float, sections: list) -> int:
    """该媒体时刻落在第几窗（0 起）。"""
    rest = max(0.0, float(media_t))
    for k, s in enumerate(sections):
        ln = float(s["media_to"]) - float(s["media_from"])
        if rest < ln or k == len(sections) - 1:
            return k
        rest -= ln
    return len(sections) - 1


def selfcheck_mapping(sections: list) -> list:
    """多窗口映射自检（§3.6-2 规则 1 的"必做自检"）。

    三件事：① 每个窗口的起点/中点/终点都映回原时间轴；② 至少两个窗口（单窗口测不出这个错）；
    ③ **显式断言"单偏移实现"（orig_from0 + m）会得出不同结果** —— 否则一个碰巧只处理了第一个
    窗口的实现也能通过自检。
    """
    problems = []
    if len(sections) < 2:
        return ["自检需要至少 2 个窗口（当前 %d）：单窗口测不出多窗口映射的错" % len(sections)]
    probes = [(0.0, sections[0]["orig_from"])]
    for s in sections:
        ln = float(s["media_to"]) - float(s["media_from"])
        probes.append((float(s["media_from"]), float(s["orig_from"])))
        probes.append((float(s["media_from"]) + ln / 2.0, float(s["orig_from"]) + ln / 2.0))
        probes.append((float(s["media_to"]) - 0.001, float(s["orig_to"]) - 0.001))
    for m, want in probes:
        got = to_original(m, sections)
        if abs(got - want) > 0.01:
            problems.append("映射不符：媒体 %.3fs 应为 %.3fs，实得 %.3fs" % (m, want, got))
    last = sections[-1]
    single_offset = round(float(sections[0]["orig_from"]) + float(last["media_from"]), 3)
    if abs(single_offset - float(last["orig_from"])) < 1e-6:
        problems.append("自检无效：多窗口下「单偏移」与分段线性结果相同（样例构造有错）")
    return problems


def coverage(duration: float, sections: list, sampled_sec: float) -> dict:
    """覆盖口径（§3.6-2 规则 5）：sampled_sec 用**实测**时长，另给最大未采样间隔。"""
    gaps = []
    prev = 0.0
    for s in sections:
        gaps.append(max(0.0, float(s["orig_from"]) - prev))
        prev = float(s["orig_to"])
    gaps.append(max(0.0, float(duration) - prev))
    return {"duration": round(float(duration), 3),
            "sampled_sec": round(float(sampled_sec), 3),
            "sampled_ratio": round(float(sampled_sec) / float(duration), 3) if duration > 0 else 0.0,
            "uncovered_max_gap_sec": round(max(gaps) if gaps else 0.0, 3),
            "note": COVERAGE_NOTE}


def coverage_line(doc: dict) -> str:
    """一行覆盖摘要（规则 5 要求必须打印）：不许把取样结果说成全片结论。"""
    c = doc.get("coverage") or {}
    return ("[sample] 只看了 %.0f s / %.0f s（%.1f%%），最大未采样间隔 %.0f s"
            % (c.get("sampled_sec") or 0, c.get("duration") or 0,
               (c.get("sampled_ratio") or 0) * 100, c.get("uncovered_max_gap_sec") or 0))


def _filter(p: dict) -> str:
    chain = ["fps=%s" % p["fps"]]
    if p["scale_height"] > 0:
        chain.append("scale=-2:%d" % p["scale_height"])
    return ",".join(chain)


def _img_size(path: Path):
    from PIL import Image
    with Image.open(path) as im:
        return [im.width, im.height]


def _rel(paths, path: Path) -> str:
    try:
        return str(path.relative_to(paths.root))
    except ValueError:
        return path.name


def _extract(cfg: dict, p: dict, paths, media_path: Path) -> list:
    """从取样媒体抽帧 —— **唯一的**抽帧实现。

    正常路径与"帧被 clean 后从现存媒体重抽"共用它：两处各写一份必然漂移，而重抽的价值
    正是"零联网、几秒、且产物与原来一致"（index.json 不动，摘要不变）。
    """
    frames_dir = paths.sample_frames
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in sorted(frames_dir.glob("*.jpg")):
        old.unlink()
    cmd = [find_ffmpeg(cfg), "-y", "-hide_banner", "-loglevel", "error", "-i", str(media_path),
           "-vf", _filter(p), "-q:v", str(int((cfg.get("frames") or {}).get("jpg_quality", 3))),
           str(frames_dir / "%06d.jpg")]
    print("[sample] %s" % " ".join(cmd))
    subprocess.run(cmd, check=True)
    files = sorted(frames_dir.glob("*.jpg"))
    if not files:
        raise SystemExit("ffmpeg 没有产出取样帧：%s（先看它上面的报错）" % frames_dir)
    return files


def _concat(cfg: dict, paths, parts: list) -> Path:
    """把各窗口拼成一个媒体文件（sample/media/sample_video.mp4）。

    先试 -c copy（同源分片、参数一致时无损且快）；拼不出来就重编码兜底 —— 两条路都会打印，
    不留"到底走了哪条"的疑问。**只读 parts、只写私有目录**。
    """
    dest = paths.sample_media / "sample_video.mp4"
    lst = paths.sample_media / "concat.txt"
    lst.write_text("".join("file '%s'\n" % Path(p).resolve() for p in parts), encoding="utf-8")
    base = [find_ffmpeg(cfg), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(lst)]
    if dest.exists():
        dest.unlink()
    cmd = base + ["-c", "copy", str(dest)]
    print("[sample] %s" % " ".join(cmd))
    if subprocess.run(cmd).returncode != 0 or not dest.exists():
        print("[sample] -c copy 拼不起来（各分片的编码参数可能不一致）→ 重编码兜底")
        cmd = base + ["-c:v", "libx264", "-crf", "23", "-c:a", "aac", str(dest)]
        print("[sample] %s" % " ".join(cmd))
        subprocess.run(cmd, check=True)
    if not dest.exists():
        raise SystemExit("拼接没有产出 %s（先看上面 ffmpeg 的报错）" % dest)
    return dest


class _MediaRoot:
    """把 media.download() 的输出重定向到取样包私有媒体目录（它只需要 .media 一个属性）。"""

    def __init__(self, media_dir: Path):
        self.media = media_dir


def run(cfg: dict, paths, force: bool = False, window_sec: float | None = None) -> dict:
    """bnote sample 的入口：规划窗口 → 一次多窗口下载 → 抽帧 → 写 index.json → 自检 + 摘要。"""
    t0 = time.monotonic()
    p = _params(cfg)
    if window_sec is not None:
        p["window_sec"] = float(window_sec)
    duration = float((paths.read_json(paths.meta, None) or {}).get("duration") or 0.0)
    if duration <= 0:
        raise SystemExit("cache/<vid>/meta.json 里没有可用时长，规划不出取样窗口 —— 先跑 bnote fetch")
    sections = sections_of(plan_windows(duration, p["window_sec"], p["anchors"]))
    if not sections:
        raise SystemExit("规划不出取样窗口（片长 %.1fs / 窗口 %.1fs）" % (duration, p["window_sec"]))
    problems = selfcheck_mapping(sections)
    if problems:
        raise SystemExit("取样映射自检未通过（实现错，不是数据错）：\n  " + "\n  ".join(problems))
    dest = paths.sample_index
    if dest.exists() and not force:
        # §3.6-2 规则 7：**媒体**没了要明确报错（否则"跳过"会掩盖一个再也复算不了的包）；
        # 帧被 clean 删掉则只提醒——index.json 与包内 overlay/measure 仍可复算，不该因此报错。
        sample_media = paths.sample_media / "sample_video.mp4"
        if not sample_media.exists():
            raise SystemExit(
                "取样包的 index.json 还在，但取样媒体已不在（被 clean 过？）：\n"
                "  媒体：%s\n  → 重跑 bnote sample --force" % sample_media)
        if not any(paths.sample_frames.glob("*.jpg")):
            # 帧被 clean 删掉、**媒体还在** → 从现存媒体重抽（零联网、几秒），而不是逼人 --force
            # 重新联网下载 90 s 取样视频；重抽失败才降级为提醒（index.json 始终未动、仍有效）。
            print("[sample] 取样帧已不在（被 clean 过）→ 从现存媒体重抽（零联网）：%s"
                  % paths.sample_frames)
            try:
                files = _extract(cfg, p, paths, sample_media)
                print("[sample] 已重抽 %d 帧（index.json 未动，仍可复算）" % len(files))
            except Exception as exc:
                print("[sample] ⚠ 重抽失败（%s）：index.json 仍有效，要复算请 --force" % exc)
        doc = paths.read_json(dest) or {}
        print("[sample] 已存在，跳过（--force 重跑）：%s" % _rel(paths, dest))
        print(coverage_line(doc))
        return doc

    # ---- ① 每窗单独下载 → 本地 concat 拼成一个媒体文件（落**私有**媒体目录）
    # 实测（2026-09-28，yt-dlp 2026.08.19 + B站分片 MP4）：`--download-sections "*A-B,C-D"` 与
    # 重复写两次该参数，yt-dlp 都只打印 "Downloading 2 time ranges"，**产物却只有第一段**
    # （5 s 而不是 10 s，两种写法的产物字节完全相同）。所以多窗口不能交给 yt-dlp：
    # 每窗下一次，再用 ffmpeg concat 拼 —— 拼出来的时间轴才和 sections 的 media_from/media_to 一致。
    paths.sample_media.mkdir(parents=True, exist_ok=True)
    cfg2 = copy.deepcopy(cfg)
    m = cfg2.setdefault("media", {})
    m["audio_only"] = False
    m["format"] = ""                              # 留空 = 按 max_height 自动挑
    if p["max_height"] > 0:
        m["max_height"] = p["max_height"]
    cookie, netscape = auth_layer.resolve(cfg2)
    print("[auth] 登录态: %s" % ("有" if cookie else "无（B站限流时可能失败）"))
    meta_doc = paths.read_json(paths.meta, None) or {}
    parts = []
    for k, s in enumerate(sections):
        wdir = paths.sample_media / ("w%02d" % (k + 1))
        if force and wdir.exists():
            shutil.rmtree(wdir)
        wdir.mkdir(parents=True, exist_ok=True)
        m["sections"] = "%s-%s" % (_hms(s["orig_from"]), _hms(s["orig_to"]))
        print("[sample] 窗口 %d/%d %s（%.0f s）→ 下载"
              % (k + 1, len(sections), m["sections"], s["orig_to"] - s["orig_from"]))
        parts.append(media_layer.download(cfg2, _MediaRoot(wdir), meta_doc, cookie, netscape,
                                          force=force))
    media_path = _concat(cfg2, paths, parts)
    print("[sample] 窗口串: %s" % sections_param(sections))

    # ---- ② 抽帧（只用取样媒体，不碰整片帧）—— 与"帧被 clean 后重抽"共用同一段代码
    files = _extract(cfg2, p, paths, media_path)

    # ---- ③ 实测时长 + 建索引（t 走分段线性）
    info = probe_media(cfg2, media_path)
    measured = float(info.get("duration") or 0.0)
    requested = round(sum(float(s["orig_to"]) - float(s["orig_from"]) for s in sections), 3)
    drift = round(measured - requested, 3)
    fps = float(p["fps"])
    frames = [{"i": i + 1, "file": fp.name, "t": to_original(i / fps, sections),
               "window": window_of(i / fps, sections)} for i, fp in enumerate(files)]
    doc = {"schema": SCHEMA, "algo": ALGO, "vid": paths.vid, "fps": fps,
           "size": _img_size(files[0]),
           "media": _rel(paths, media_path),
           "sections": sections,
           "coverage": coverage(duration, sections, measured),
           "count": len(frames), "frames": frames}

    # ---- ④ 三条自检（§3.6-2 规则 8）：任一不过就报错，不落盘半成品
    checks = []
    if abs(drift) > p["dur_tol_sec"]:
        checks.append("拼接实测时长 %.3fs 与请求窗口之和 %.3fs 差 %.3fs（容差 %.3fs）"
                      % (measured, requested, drift, p["dur_tol_sec"]))
    devs = []
    for k, s in enumerate(sections):
        mine = [f for f in frames if f["window"] == k]
        if not mine:
            checks.append("窗口 %d 一帧都没有" % (k + 1))
            continue
        dev = round(min(float(f["t"]) for f in mine) - float(s["orig_from"]), 3)
        devs.append(dev)
        if abs(dev) > p["first_frame_tol_sec"]:
            checks.append("窗口 %d 首帧 t 偏 %.3fs（容差 %.3fs）" % (k + 1, dev, p["first_frame_tol_sec"]))
    if checks:
        raise SystemExit("取样自检未通过：\n  " + "\n  ".join(checks))

    paths.write_json(dest, doc)
    print("[sample] %d 帧 / %d 窗 ｜ t %ss–%ss ｜ 拼接实测 %.3fs（请求 %.3fs，差 %+.3fs）"
          % (len(frames), len(sections), frames[0]["t"], frames[-1]["t"], measured, requested, drift))
    print("[sample] 首帧偏差（各窗）: %s ｜ 容差 %.3fs"
          % (", ".join("%+.3f" % d for d in devs), p["first_frame_tol_sec"]))
    print(coverage_line(doc))
    print("[sample] 映射自检 + 时长/首帧自检通过 ｜ %.1fs → %s" % (time.monotonic() - t0, _rel(paths, dest)))
    dirs = [d for d in sorted(paths.sample_media.glob("w*")) if d.is_dir()]
    if p["keep_parts"]:
        print("[sample] 保留原始分片 %s（[sample].keep_parts=false 可关）"
              % "、".join(d.name for d in dirs))
    else:
        names = [d.name for d in dirs]
        for d in dirs:
            shutil.rmtree(d)
        # concat.txt 指的是刚被删掉的分片：留着就是一份指向已删文件的清单，一起清。
        (paths.sample_media / "concat.txt").unlink(missing_ok=True)
        print("[sample] 已删原始分片 %s 与 concat.txt（keep_parts=false）；只留 sample_video.mp4"
              % "、".join(names))
    return doc
