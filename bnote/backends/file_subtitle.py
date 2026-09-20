"""字幕后端：读取用户自备的 srt / vtt / json（B站字幕 json 也行）。

适用场景：已有更好的字幕（人工校对、官方导出），直接喂给流水线。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def _sec(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def _parse_srt(text: str) -> list[dict]:
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        m = TS.search(block)
        if not m:
            continue
        body = block[m.end():].strip().replace("\r", "")
        lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
        if lines:
            out.append({"from": _sec(*m.groups()[:4]),
                        "to": _sec(*m.groups()[4:]),
                        "text": " ".join(lines)})
    return out


def run(cfg, paths, meta, media_path=None, cookie: str = "") -> dict | None:
    raw_path = (cfg["subtitle"].get("file_path") or "").strip()
    if not raw_path:
        return None
    p = Path(raw_path).expanduser()
    if not p.exists():
        print("[subtitle.file] 文件不存在: %s" % p)
        return None
    text = p.read_text(encoding="utf-8", errors="ignore")

    if p.suffix.lower() == ".json":
        data = json.loads(text)
        body = data.get("body") or data.get("segments") or []
        segments = [{"from": float(s.get("from", s.get("start", 0))),
                     "to": float(s.get("to", s.get("end", 0))),
                     "text": (s.get("content") or s.get("text") or "").strip()}
                    for s in body]
        segments = [s for s in segments if s["text"]]
        # B 站字幕也可能是扁平 {"body":[...]}，同样处理
    else:
        segments = _parse_srt(text)

    if not segments:
        print("[subtitle.file] 解析后没有字幕条目")
        return None
    return {"backend": "file", "language": "unknown",
            "source": str(p), "segments": segments}
