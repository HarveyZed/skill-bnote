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

from . import media as media_layer
from . import slideset as slideset_layer


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
AT_DIRNAME = "frames_at"     # cache/frames/ 被清掉时的现抽落脚点：out/<vid>/_meta/frames_at/


def _rel(paths, path) -> str:
    try:
        return str(Path(path).relative_to(paths.root))
    except ValueError:
        return str(path)


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


def extract_at(cfg: dict, paths, t: float) -> dict:
    """现抽一张：ffmpeg -ss <t> -i <media> -frames:v 1 → out/<vid>/_meta/frames_at/。

    **不缩放**（要读字就得全分辨率）。ffmpeg 会重编码，所以这张与 cache/frames/ 里那张**不逐字节相同**；
    媒体也没有时明确报错，**不许静默返回空**。
    """
    media = media_layer.find_media(paths)
    if media is None:
        raise SystemExit(
            "既没有 cache/frames/index.json 里可用的帧，也没有媒体文件可现抽帧：\n"
            "  帧目录：%s\n  媒体目录：%s\n"
            "  → 先跑 bnote fetch（或 bnote run）把媒体取下来" % (paths.frames, paths.media))
    dest_dir = paths.meta_dir() / AT_DIRNAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("at_%09.3f.jpg" % float(t))
    ff = find_ffmpeg(cfg)
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % float(t),
           "-i", str(media), "-frames:v", "1",
           "-q:v", str(cfg["frames"]["jpg_quality"]), str(dest)]
    print("[frames] cache/frames/ 里没有可用的帧 → 现抽一张：%s" % " ".join(cmd))
    subprocess.run(cmd, check=True)
    if not dest.exists():
        raise SystemExit("ffmpeg 没有产出帧文件：%s（先看它上面的报错）" % dest)
    return {"t": round(float(t), 3), "path": dest, "frame": None, "extracted": True,
            "sha256": slideset_layer.sha256_file(dest)}


def entry(cfg: dict, paths, t: float, frames: list | None = None) -> dict:
    """t 处那一帧：index 命中就用现成 jpg（**逐字节就是它**），否则现抽一张。"""
    if frames is None:
        frames = load_index(paths) or []
    if frames:
        f = frames[pick_index(frames, t)]
        p = paths.frames / str(f["file"])
        if p.exists():
            return {"t": round(float(f["t"]), 3), "path": p, "frame": str(f["file"]),
                    "extracted": False, "sha256": slideset_layer.sha256_file(p)}
        print("[frames] cache/frames/%s 不在（被 clean 过？）→ 现抽" % f["file"])
    return extract_at(cfg, paths, t)


def around(cfg: dict, paths, t: float) -> list[tuple[int, dict]]:
    """该时刻最近的一帧 + 前后各一帧（3 条，含路径与 t）。没有 index 时按抽帧间隔现抽三张。"""
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
    """bnote frames 的入口：--at 打印 3 条；--read 额外给该时刻的全分辨率单帧路径。"""
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
        print("  %s  t=%.3fs  %s%s" % (tags[d], it["t"], _rel(paths, it["path"]),
              "（现抽）" if it["extracted"] else ""))
    hit = next(it for d, it in items if d == 0)
    if read:
        print("[frames] --read 全分辨率单帧：%s" % _rel(paths, hit["path"]))
        if hit["extracted"]:
            print("[frames] sha256 %s ｜ 现抽帧（ffmpeg 重编码，不缩放）：cache/frames/ 里没有对应文件，"
                  "读字以这张为准" % hit["sha256"])
        else:
            src = paths.frames / str(hit["frame"])
            same = hit["sha256"] == slideset_layer.sha256_file(src)
            print("[frames] sha256 %s ｜ 与 cache/frames/%s %s"
                  % (hit["sha256"], hit["frame"],
                     "逐字节相同（未缩放、未重编码）" if same
                     else "不一致（缓存被动过，重跑 bnote slides）"))
    return hit
