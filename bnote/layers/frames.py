"""L4 抽帧层：ffmpeg 定频抽帧，落 cache/frames/ + index.json。

只负责"把画面按时间采样出来"，不做任何切片判断（判断在 segmenter 里）。
"""
from __future__ import annotations

import glob
import json
import re
import subprocess
from pathlib import Path

from ..tools import find_ffmpeg
from ..tools import probe_media
from ..tools import sha256_file

from . import media as media_layer


SECTION_RE = re.compile(r"^(\d{1,2}:\d{2}(?::\d{2})?)-")
HMS_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def parse_hms(s: str) -> float:
    """把 "HH:MM:SS" / "MM:SS" 解析成秒。只认这两种写法：少个冒号、带小数点这类笔误
    直接报错，不静默当成 0 —— 取帧取到第 0 秒比报错更难发现。"""
    m = HMS_RE.match(str(s or "").strip())
    if not m:
        raise SystemExit("时间格式应为 HH:MM:SS 或 MM:SS，实际 %r" % (s,))
    a, b, c = m.groups()
    if c is None:
        return float(int(a) * 60 + int(b))
    return float(int(a) * 3600 + int(b) * 60 + int(c))


def to_timeline(cfg: dict, hms: str) -> float:
    """把「媒体文件自己的时间轴」上的时刻换算到**原始时间轴**（cache/frames/index.json 的 t 就是它）。

    `--sections` 试跑时 media 只含片段：你在片段里看到的 00:03:00 对应原片 00:08:00，
    差值就是 section_offset()。整集运行时偏移为 0，换算即恒等 —— 但这一步不能省，
    否则试跑产物上取帧会整体偏掉一个片段起点。
    """
    return round(section_offset(cfg) + parse_hms(hms), 3)


def section_offset(cfg: dict) -> float:
    """media.sections 形如 00:05:00-00:20:00 时，返回起始偏移秒（用于把帧时间映射回原始时间轴）"""
    s = (cfg["media"].get("sections") or "").strip()
    m = SECTION_RE.match(s)
    if not m:
        return 0.0
    parts = [int(x) for x in m.group(1).split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _build_filter(cfg: dict) -> str:
    f = cfg["frames"]
    chain = []
    if f.get("crop"):
        chain.append("crop=%s" % f["crop"])
    chain.append("fps=%s" % f["fps"])
    if int(f.get("scale_height") or 0) > 0:
        chain.append("scale=-2:%d" % int(f["scale_height"]))
    return ",".join(chain)


def load_index(paths) -> list[dict] | None:
    idx = paths.read_json(paths.frames / "index.json")
    if not idx:
        return None
    return idx.get("frames")


def extract(cfg: dict, paths, media_path: Path, force: bool = False) -> list[dict]:
    paths.frames.mkdir(parents=True, exist_ok=True)   # 只建自己要写的目录
    old = glob.glob(str(paths.frames / "*.jpg"))
    if old and not force:
        frames = load_index(paths)
        if frames:
            print("[frames] 已存在 %d 帧，跳过抽帧" % len(frames))
            return frames
    for p in old:
        Path(p).unlink()

    ff = find_ffmpeg(cfg)
    out_tpl = str(paths.frames / "%06d.jpg")
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(media_path),
           "-vf", _build_filter(cfg), "-q:v", str(cfg["frames"]["jpg_quality"]), out_tpl]
    print("[frames] %s" % " ".join(cmd))
    subprocess.run(cmd, check=True)

    fps = float(cfg["frames"]["fps"])
    offset = section_offset(cfg)
    files = sorted(glob.glob(str(paths.frames / "*.jpg")))
    frames = [{"i": i + 1, "file": Path(p).name, "t": round(offset + i / fps, 3)}
              for i, p in enumerate(files)]
    paths.write_json(paths.frames / "index.json",
                     {"fps": fps, "offset": offset, "count": len(frames), "frames": frames})
    print("[frames] 抽出 %d 帧（%.1f fps，offset=%.0fs）→ %s" % (len(frames), fps, offset, paths.frames))
    return frames


# ---------------------------------------------------------------- M2 按时间取帧（bnote frames --at/--read）
AT_DIRNAME = "frames_at"     # 现抽原生帧的落脚点：out/<vid>/_meta/frames_at/


def _rel(paths, path) -> str:
    try:
        return str(Path(path).relative_to(paths.root))
    except ValueError:
        return str(path)


def _size_txt(size) -> str:
    return ("%dx%d" % (size[0], size[1])) if size else "尺寸未知"


def _img_size(path):
    """图片尺寸（只读文件头，不解码全图）；读不出来返回 None。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return (int(im.size[0]), int(im.size[1]))
    except Exception:
        return None


def native_size(cfg: dict, paths, media=None):
    """媒体**原生**尺寸 (宽, 高)；媒体不在或探测失败返回 None。

    探测走 tools.probe_media（ffprobe 优先、没有就解析 ffmpeg -i 的 stderr）：媒体尺寸只有一个
    真源，且 L4 不 import L4.5（P1：层与层只通过文件通信）。
    """
    media = media if media is not None else media_layer.find_media(paths)
    if media is None:
        return None
    try:
        info = probe_media(cfg, media)
    except (Exception, SystemExit):   # 探测失败只影响"能否证明原生"：退回复用缓存 + 明确警告
        return None
    w, h = int(info.get("width") or 0), int(info.get("height") or 0)
    return (w, h) if w and h else None


def pick_index(frames: list, t: float) -> int:
    """index 里离 t 最近的那一帧的下标（frames 按 t 升序，二分）。"""
    lo, hi = 0, len(frames) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if float(frames[mid]["t"]) < t:
            lo = mid + 1
        else:
            hi = mid
    cands = [k for k in (lo - 1, lo, lo + 1) if 0 <= k < len(frames)]
    return min(cands, key=lambda k: abs(float(frames[k]["t"]) - t))


def _step(cfg: dict, frames: list) -> float:
    """前后各一帧的间隔：有 index 就用它的抽帧间隔，否则用 [frames].fps 反推。"""
    if frames and len(frames) > 1:
        return max(1e-3, round(float(frames[1]["t"]) - float(frames[0]["t"]), 3))
    try:
        return round(1.0 / max(1e-6, float(cfg["frames"]["fps"])), 3)
    except Exception:
        return 0.5


def extract_native(cfg: dict, paths, t: float, native=None, media=None) -> dict:
    """现抽一张**原生分辨率**单帧：`ffmpeg -ss <t> -i <media> -frames:v 1` → _meta/frames_at/。

    为什么不缩放、为什么 PNG：这条路径的用途是**读字**（小字 / 烧录字幕 / 页脚），缩放等于丢像素，
    有损编码等于给字加噪声。落盘名带 t 与原生尺寸，同一时刻第二次跑直接复用、**不再解码**。
    媒体也没有时明确报错，**不许静默返回空**。
    """
    media = media if media is not None else media_layer.find_media(paths)
    if media is None:
        raise SystemExit(
            "既没有 cache/frames/ 里可用的帧，也没有媒体文件可现抽原生帧：\n"
            "  帧目录：%s\n  媒体目录：%s\n"
            "  → 先跑 bnote fetch（或 bnote run）把媒体取下来" % (paths.frames, paths.media))
    if native is None:
        native = native_size(cfg, paths, media)
    tag = ("%dx%d" % (native[0], native[1])) if native else "unknown"
    dest_dir = paths.meta_dir() / AT_DIRNAME
    dest_dir.mkdir(parents=True, exist_ok=True)          # 只建自己要写的那一层
    dest = dest_dir / ("at_%09.3f_%s.png" % (float(t), tag))
    hit = dest.exists()
    if not hit:
        ff = find_ffmpeg(cfg)
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % float(t),
               "-i", str(media), "-frames:v", "1", str(dest)]
        print("[frames] 现抽原生帧（无 scale 滤镜、PNG 无损）：%s" % " ".join(cmd))
        subprocess.run(cmd, check=True)
        if not dest.exists():
            raise SystemExit("ffmpeg 没有产出帧文件：%s（先看它上面的报错）" % dest)
    return {"t": round(float(t), 3), "path": dest, "frame": None, "extracted": True,
            "size": _img_size(dest), "reused": hit,
            "source": "原生现抽（PNG 无损%s）"
                      % ("；该时刻的现抽结果已缓存，未重新解码" if hit else ""),
            "sha256": sha256_file(dest)}


def read_frame(cfg: dict, paths, t: float) -> dict:
    """`--read`：t 处那一帧，**默认给媒体原生分辨率**（读字用途）。

    三条分支都会被打印出来（尺寸 + 来源），不让调用者猜自己拿到的是哪一档：
      ① 缓存帧本身就是原生尺寸（scale_height=0，或媒体尺寸 <= scale_height）→ **直接复用缓存**，
         sha256 与 cache/frames/ 那张相同 —— 「没有缩放/重编码」这条性质仍然可验证；
      ② 缓存帧是缩放图（如 scale_height=720 而媒体 1080p）→ 现抽原生帧（PNG 无损），按 t+尺寸缓存；
      ③ 媒体已清理、无法确认原生尺寸 → 复用缓存**并明确警告**（不静默把缩放图当原生）。
    """
    frames = load_index(paths) or []
    f = frames[pick_index(frames, t)] if frames else None
    cached = (paths.frames / str(f["file"])) if f is not None else None
    media = media_layer.find_media(paths)
    native = native_size(cfg, paths, media)
    if cached is not None and cached.exists():
        size = _img_size(cached)
        if native is None:
            scaled = int(((cfg.get("frames") or {}).get("scale_height") or 0)) > 0
            return {"t": round(float(f["t"]), 3), "path": cached, "frame": str(f["file"]),
                    "extracted": False, "size": size, "reused": True,
                    "source": "缓存复用（%s）" % ("scale_height=0，未缩放" if not scaled
                                                 else "媒体已清理，无法确认原生尺寸"),
                    "warn": None if not scaled else
                            "媒体已清理，取不到原生分辨率；要读小字请先 bnote fetch 取回媒体",
                    "sha256": sha256_file(cached)}
        if size == native:
            return {"t": round(float(f["t"]), 3), "path": cached, "frame": str(f["file"]),
                    "extracted": False, "size": size, "reused": True, "warn": None,
                    "source": "缓存复用（已是原生 %dx%d，与 cache/frames/ 逐字节相同）"
                              % (native[0], native[1]),
                    "sha256": sha256_file(cached)}
        print("[frames] cache/frames/%s 是 %s 的缩放图（媒体原生 %s）→ 现抽原生帧"
              % (f["file"], _size_txt(size), _size_txt(native)))
    return extract_native(cfg, paths, t, native=native, media=media)


def entry(cfg: dict, paths, t: float, frames: list | None = None) -> dict:
    """`--at` 列表用：index 命中就用现成 jpg（**逐字节就是它**），否则现抽一张原生 PNG。"""
    if frames is None:
        frames = load_index(paths) or []
    if frames:
        f = frames[pick_index(frames, t)]
        p = paths.frames / str(f["file"])
        if p.exists():
            return {"t": round(float(f["t"]), 3), "path": p, "frame": str(f["file"]),
                    "extracted": False, "size": _img_size(p), "reused": True, "warn": None,
                    "source": "缓存（cache/frames/）", "sha256": sha256_file(p)}
        print("[frames] cache/frames/%s 不在（被 clean 过？）→ 现抽" % f["file"])
    return extract_native(cfg, paths, t)


def around(cfg: dict, paths, t: float):
    """该时刻最近的一帧 + 前后各一帧（3 条，含路径、t、尺寸与来源）。没有 index 时按抽帧间隔现抽三张。"""
    frames = load_index(paths) or []
    if not frames:
        step = _step(cfg, frames)
        return [(d, entry(cfg, paths, t + d * step, frames=[])) for d in (-1, 0, 1)]
    k = pick_index(frames, t)
    out = []
    for pos in (k - 1, k, k + 1):
        if 0 <= pos < len(frames):
            out.append((pos - k, entry(cfg, paths, float(frames[pos]["t"]), frames=frames)))
    return out


def run_at(cfg: dict, paths, hms: str, read: bool = False) -> dict:
    """bnote frames 的入口：`--at` 打印 3 条（含尺寸与来源）；`--read` 给该时刻的**读字单帧**。

    `--read` 默认就是**媒体原生分辨率**（这条路径的用途就是读字），所以不另加 `--native` 开关：
    多一个开关只会让人以为"默认不是原生"。
    """
    off = section_offset(cfg)
    t = to_timeline(cfg, hms)
    if off:
        print("[frames] media.sections 偏移 %.0fs 已换算：%s（媒体内）→ %.3fs（原始时间轴）"
              % (off, hms, t))
    items = around(cfg, paths, t)
    if not items:
        raise SystemExit("取不到帧：%s" % paths.frames)
    print("[frames] --at %s（原始时间轴 %.3fs）最近的帧 + 前后各一帧：" % (hms, t))
    tags = {-1: "← prev", 0: "● hit ", 1: "→ next"}
    for d, it in items:
        print("  %s  t=%.3fs  %s  %s  %s" % (tags[d], it["t"], _size_txt(it.get("size")),
                                             it.get("source") or "", _rel(paths, it["path"])))
    hit = next(it for d, it in items if d == 0)
    if not read:
        return hit
    frame = read_frame(cfg, paths, t)
    print("[frames] --read 读字单帧：%s ｜ %s ｜ 来源：%s"
          % (_rel(paths, frame["path"]), _size_txt(frame.get("size")), frame.get("source")))
    print("[frames] sha256 %s" % frame["sha256"])
    if frame.get("frame"):
        same = frame["sha256"] == sha256_file(paths.frames / str(frame["frame"]))
        print("[frames] 与 cache/frames/%s %s"
              % (frame["frame"], "逐字节相同（未缩放、未重编码）" if same
                 else "不一致（缓存被动过，重跑 bnote slides）"))
    if frame.get("warn"):
        print("[frames] ⚠ %s" % frame["warn"])
    return frame
