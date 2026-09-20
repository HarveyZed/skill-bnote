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
    paths.ensure()
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
