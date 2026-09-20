"""目录规划：所有读写路径只在这里定义，便于清理与迁移。

v0.7.0 起分三个根（都可外部配置，默认为 <cwd>/.bnote/ 下的子目录）：

state/<vid>/            跨集沉淀与部署态（术语表、画像、登录态），**删 cache 不带走**
cache/<vid>/            中间产物（可随时整体删除）
  meta.json             视频元信息
  media/                下载的 mp4 / m4a / 音频 wav
  subtitle/             字幕原始响应 + 归一化 transcript.json
  frames/               抽帧结果 + index.json
  ocr/                  OCR 结果缓存（按帧内容哈希）
  segments.json         切片结果（核心中间产物）
out/<vid>/              交付物（Markdown + 图片）—— **内部结构冻结**，正文用 ../slides/NNNN.jpg 相对引用
  transcript.md
  slides/NNNN.jpg
  slides.json
  index.md
  chapters/
  _meta/                校验报告、派单 prompt、钩子、派修与 loop 台账
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path


class WorkPaths:
    def __init__(self, cfg: dict, vid: str):
        self.vid = vid
        self.root = Path(cfg["paths"]["root"])
        self.state = Path(cfg["paths"]["state_root"]) / vid
        self.cache = Path(cfg["paths"]["cache_root"]) / vid
        self.out = Path(cfg["paths"]["out_root"]) / vid
        self.log = Path(cfg["paths"]["log_root"]) / vid

    @property
    def meta(self) -> Path: return self.cache / "meta.json"
    @property
    def media(self) -> Path: return self.cache / "media"
    @property
    def subtitle(self) -> Path: return self.cache / "subtitle"
    @property
    def frames(self) -> Path: return self.cache / "frames"
    @property
    def ocr(self) -> Path: return self.cache / "ocr"
    @property
    def segments(self) -> Path: return self.cache / "segments.json"

    def slides(self) -> Path: return self.out / "slides"
    def chapters(self) -> Path: return self.out / "chapters"
    def meta_dir(self) -> Path: return self.out / "_meta"
    def fixes(self) -> Path: return self.meta_dir() / "fix"
    @property
    def dispatch(self) -> Path: return self.meta_dir() / "dispatch.json"
    @property
    def loop(self) -> Path: return self.meta_dir() / "loop.json"
    @property
    def validation(self) -> Path: return self.meta_dir() / "validation.json"

    def ensure(self) -> "WorkPaths":
        for d in (self.state, self.cache, self.media, self.subtitle, self.frames, self.ocr,
                  self.out, self.slides(), self.chapters(), self.meta_dir()):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def read_json(self, path: Path, default=None):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return default

    def write_json(self, path: Path, obj) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def clean(self, level: str = "cache") -> list[str]:
        """level: state | cache | out | all（state 单独一档，避免顺手删掉跨集沉淀）"""
        removed = []
        targets = []
        if level in ("cache", "all"):
            targets.append(self.cache)
        if level in ("state", "all"):
            targets.append(self.state)
        if level in ("out", "all"):
            targets.append(self.out)
        for t in targets:
            if t.exists():
                shutil.rmtree(t)
                removed.append(str(t))
        return removed

    def size(self, level: str = "cache") -> int:
        """清理前先报占用（字节）"""
        t = {"cache": self.cache, "state": self.state, "out": self.out}.get(level)
        if t is None or not t.exists():
            return 0
        return sum(p.stat().st_size for p in t.rglob("*") if p.is_file())
