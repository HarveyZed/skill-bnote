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
