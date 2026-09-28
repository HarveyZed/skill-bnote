"""外部程序工具：二进制的解析，以及两个**跨层中性**的小工具（流信息探测、文件摘要）。

优先级： 配置 [tools].xxx  >  环境变量 BN_TOOLS_XXX  >  PATH  >  imageio-ffmpeg 内置静态包

为什么探测与摘要放这里（P1：层与层只通过文件通信）：它们是"外部程序/字节"层面的工具，不属于任何
流水线层。放这里，L4 抽帧（frames）与 L4.5 量测（measure）都能用同一个实现，而不是 L4 反过来 import
L4.5、或各自再写一份（两份探测迟早漂移，两份 sha256 实现更是会直接影响产物摘要的可比性）。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent.parent


def _link_as_ffmpeg(exe: str, cfg: dict) -> str:
    """imageio-ffmpeg 的二进制名不是 ffmpeg，yt-dlp 按名字找，所以做一层软链。

    软链落在**数据根**的 cache/_bin 下 —— 运行期产物不进代码目录：代码目录随 skill 分发，
    可能只读（只读安装 / 容器挂载），也可能被多份数据根共享。
    """
    import os
    from pathlib import Path
    bindir = Path(cfg["paths"]["cache_root"]) / "_bin"
    try:
        bindir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            "找不到 ffmpeg，也无法在数据根建软链：%s\n  原因：%s\n"
            "  → 装系统 ffmpeg（推荐：apt install ffmpeg），或 pip install imageio-ffmpeg；\n"
            "  → 或在 config/local.toml 的 [tools] ffmpeg / BN_TOOLS_FFMPEG 指定绝对路径；\n"
            "  → 或让数据根可写（BNOTE_ROOT 或 [paths] root）。" % (bindir, exc))
    link = bindir / "ffmpeg"
    if not link.exists() or os.path.realpath(link) != os.path.realpath(exe):
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(exe)
    return str(link)


def find_ffmpeg(cfg: dict) -> str:
    custom = cfg["tools"].get("ffmpeg")
    if custom:
        return custom
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except Exception:
        raise RuntimeError(
            "找不到 ffmpeg：请安装系统 ffmpeg（推荐：apt install ffmpeg）、或 pip install imageio-ffmpeg、"
            "或在 config/local.toml 的 [tools].ffmpeg 指定绝对路径"
        )
    # 静态包只作兜底；软链失败要带上真实原因（目录不可写等），不要在这里吞掉
    return _link_as_ffmpeg(imageio_ffmpeg.get_ffmpeg_exe(), cfg)


def find_ffprobe(cfg: dict) -> str | None:
    """ffprobe（可选）：只用来读流信息，失败时调用方退化为解析 ffmpeg 的 stderr。

    imageio-ffmpeg 的静态包只带 ffmpeg，所以这里**不**做兜底：没有就返回 None。

    溯源（别当成又一处死代码删掉）：0.9.1 清理无人调用的函数时删过它；M1 起重新引入且
    **有调用点** —— 本模块的 `probe_media()` 用它读时长/帧率/分辨率/有无音轨（`bnote measure`
    与 `bnote frames --read` 都经它），拿不到就退化为解析 `ffmpeg -i` 的 stderr。
    """
    custom = cfg["tools"].get("ffprobe")
    if custom:
        return custom
    return shutil.which("ffprobe")


# ---------------------------------------------------------------- 文件摘要（跨层中性）
_CHUNK = 4 * 1024 * 1024


def sha256_file(p) -> str:
    """流式算文件摘要（4MiB 块），返回 `sha256:<64hex>`。

    放这里而不是某一层里：产物摘要（slides 页图的身份）与读字链（现抽帧的同一性）都要用它，
    两份实现迟早会在分块/前缀上分叉，而摘要一旦分叉，"逐字节相同"这类断言就不再可信。
    """
    h = hashlib.sha256()
    with Path(p).open("rb") as fh:
        for blk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(blk)
    return "sha256:" + h.hexdigest()


# ---------------------------------------------------------------- 流信息探测（跨层中性）
# ffprobe 不可用时的兜底：只解析 ffmpeg 的 stderr 头部（不解码，只读流信息）
FF_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
FF_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video: .*?, (\d+)x(\d+)")
FF_FPS_RE = re.compile(r"(\d+(?:\.\d+)?) fps")
FF_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: ")


def _to_float(raw, default: float = 0.0) -> float:
    """探测内部的数值解析（与 layers/measure.py 的 `_float` 等价；不互相 import，
    避免为一个 5 行助手把层再连起来）。"""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _rate(raw) -> float:
    """"30/1" -> 30.0（ffprobe 的 r_frame_rate 是分数）"""
    try:
        if "/" in str(raw):
            a, b = str(raw).split("/", 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def probe_media(cfg: dict, media_path) -> dict:
    """媒体的时长/帧率/分辨率/有无音视频轨。ffprobe 优先；没有就解析 ffmpeg 的 stderr。

    `bnote measure`（一次解码前的流信息）与 `bnote frames --read`（判断缓存帧是不是原生尺寸）
    共用这一个实现 —— 媒体尺寸只有一个真源。
    """
    ffprobe = find_ffprobe(cfg)
    if ffprobe:
        return _probe_with_ffprobe(ffprobe, media_path)
    return _probe_with_ffmpeg(find_ffmpeg(cfg), media_path)


def _probe_with_ffprobe(ffprobe: str, media_path) -> dict:
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(media_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit("[tools] ffprobe 读取流信息失败：%s" % (proc.stderr or "").strip()[-400:])
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = _to_float((data.get("format") or {}).get("duration")) or _to_float(video.get("duration"))
    return {
        "duration": duration,
        "fps": _rate(video.get("r_frame_rate")) or _rate(video.get("avg_frame_rate")),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_video": bool(video),
        "has_audio": audio is not None,
    }


def _probe_with_ffmpeg(ffmpeg: str, media_path) -> dict:
    """兜底路径：`ffmpeg -i` 的头部信息就够用，不需要真的解码一帧。"""
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(media_path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = proc.stderr or ""
    dur = 0.0
    m = FF_DURATION_RE.search(text)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    width = height = 0
    fps = 0.0
    for line in text.splitlines():
        if ": Video:" in line:
            mv = FF_VIDEO_RE.search(line)
            if mv:
                width, height = int(mv.group(1)), int(mv.group(2))
            mf = FF_FPS_RE.search(line)
            if mf:
                fps = _to_float(mf.group(1))
    return {
        "duration": dur,
        "fps": fps,
        "width": width,
        "height": height,
        "has_video": ": Video:" in text,
        "has_audio": bool(FF_AUDIO_RE.search(text)),
    }


# ---------------------------------------------------------------- M5 取样包 basis（契约 §3.6）
FULL_BASIS = "cache/frames/index.json"


def panel_names(meta_dir) -> set:
    """out/<vid>/_meta/ 下两份面板清单里的面板文件名**并集**（M5，§3.6-3）。

    整片面板写 sheet.json、取样面板写 sheet_sample.json（两个 basis 装不进一个文件）。
    **只用于"面板存在性"判定**：白名单路径不变（仍只认 ../_meta/sheets/<name>.png）。
    放中性工具的理由同 sha256：manifest（幻灯片模式）与 text（信息流模式）都要用同一份，
    两处各写一份必然漂移。
    """
    out = set()
    for name in ("sheet.json", "sheet_sample.json"):
        p = Path(meta_dir) / name
        if not p.exists():
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            # 不静默当空集：清单读不了会让"面板存在性"判定全灭（引用全报"查不到 tile"），
            # 那正是读者最需要知道的一行 —— 但也别因此中断校验，继续看下一份。
            print("[panels] ⚠ 面板清单读不了（%s）：%s —— 当作空集，但这可能是产物损坏" % (p.name, exc))
            continue
        if isinstance(doc, dict):
            out |= {str(t.get("sheet")) for t in (doc.get("tiles") or []) if isinstance(t, dict)}
    return out


def check_basis_dest(dest, basis, full_name: str, sample_name: str,
                     full_dir=None, sample_dir=None) -> None:
    """写前断言（复核 2026-09-28 裁定 (b)，2026-09-28 补目录比对）：目标必须与 basis 对得上。

    防的是"将来手滑用旧路径写新数据"——例如取样 basis 却写进整片的 sheet.json，
    那会把整片面板的 tiles 洗掉，check 立刻假报"引用的面板没有 tile"。

    **为什么必须连目录一起比**：overlay / measure 的整片与取样**同名**（都是 overlay.json /
    measure.json），只比 Path(dest).name 必然相等 —— 断言会变成走过场，而这两个文件恰恰是
    "写错目录就污染顶层产物"的那两个。所以调用方要传 full_dir / sample_dir（sheet 两者相同，
    传 _meta/ 即可，那种情况靠文件名区分）。basis=取样时还要求 basis 自证是取样包。
    """
    d = Path(dest)
    want = sample_name if basis else full_name
    if d.name != want:
        raise SystemExit("[basis] 内部错误：basis=%s 却要写 %s（应为 %s）"
                         % ((basis or {}).get("name") or "full", d.name, want))
    want_dir = sample_dir if basis else full_dir
    if want_dir is not None and Path(want_dir) != d.parent:
        raise SystemExit("[basis] 内部错误：basis=%s 的落点目录是 %s，预期 %s"
                         % ((basis or {}).get("name") or "full", d.parent, want_dir))
    if basis and basis.get("name") != "sample":
        raise SystemExit("[basis] 取样 basis 自证失败：name=%r" % (basis.get("name"),))


def resolve_basis(paths, name: str = "full") -> dict | None:
    """--basis 解析：**full → None**（调用方沿用原路径，行为一个字节都不变）。

    sample → 取样包输入描述（帧列表 / 帧根目录 / fps / 时间偏移 / 索引 relpath / 整份索引）。
    没有取样包时**明确报错**，不静默退回整片 —— 静默退回会让"我在看取样数据"这个判断错
    （§3.6-3）。放中性工具里的理由：overlay 与 measure 都要用它，层与层不互相 import（P1）。
    """
    if name in (None, "", "full"):
        return None
    if name != "sample":
        raise SystemExit("未知 --basis：%r（只有 full / sample）" % (name,))
    rel = "cache/%s/sample/index.json" % paths.vid
    doc = paths.read_json(paths.sample_index, None)
    if not isinstance(doc, dict) or not doc.get("frames"):
        raise SystemExit("没有取样包（%s）：先跑 bnote sample <URL> --page N" % rel)
    return {"name": "sample", "relpath": rel, "index": doc,
            "frames": doc.get("frames") or [], "fps": doc.get("fps"),
            "root": paths.sample_frames, "offset": 0.0}


def yt_dlp_python(cfg: dict) -> str:
    """返回可执行的 python 解释器（用于 -m yt_dlp）"""
    custom = cfg["tools"].get("yt_dlp")
    if custom:
        return custom
    return sys.executable


# ---------------------------------------------------------------- 坐标换算（跨层中性）
def crop_box_to_media(box, crop: str, media_size):
    """把「抽帧画面」的相对坐标框 [l,t,r,b] 换算成「媒体原图」的相对坐标框。

    为什么需要它：overlay.json 的 box 按冻结契约是**抽帧后画面**（[frames].crop 之后）的坐标
    （与 ocr.text(region=) 同一空间）；而 measure 解码的是**媒体原图**。scale 只做等比缩放、
    不改变相对坐标，所以 [frames].crop 为空时换算恒等；crop 非空时抽帧先被裁掉 w:h:x:y
    （媒体像素坐标，不随 scale_height 变），直接套用会错位：

        X_media = (x + l * w_crop) / W_media

    crop 串解析不出来时**不猜**：原样返回并 clip（调用方应把它当「可能错位」记进日志）。
    """
    l, t, r, b = (float(x) for x in box)
    W, H = int(media_size[0]), int(media_size[1])
    parts = [p for p in str(crop or "").split(":") if p.strip() != ""]
    if len(parts) != 4 or W <= 0 or H <= 0:
        return [max(0.0, l), max(0.0, t), min(1.0, r), min(1.0, b)]
    try:
        cw, ch, cx, cy = (float(x) for x in parts)
    except ValueError:
        return [max(0.0, l), max(0.0, t), min(1.0, r), min(1.0, b)]
    return [max(0.0, min(1.0, (cx + l * cw) / W)),
            max(0.0, min(1.0, (cy + t * ch) / H)),
            max(0.0, min(1.0, (cx + r * cw) / W)),
            max(0.0, min(1.0, (cy + b * ch) / H))]