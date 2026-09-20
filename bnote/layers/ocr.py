"""L6 OCR 层：廉价、可缺省。只用来判断"这一页的文字全不全"。

未安装 rapidocr 时 available=False，segmenter 自动退回"墨迹密度 + 清晰度"打分。
结果按帧文件内容哈希缓存，重复运行不重复推理。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class Ocr:
    def __init__(self, cfg: dict, paths):
        self.cfg = cfg
        self.paths = paths
        self.engine = None
        self.available = False
        if not cfg["ocr"]["enabled"]:
            print("[ocr] 配置关闭，跳过 OCR（退化为墨迹打分）")
            return
        try:
            from rapidocr_onnxruntime import RapidOCR
            self.engine = RapidOCR()
            self.available = True
            print("[ocr] rapidocr 就绪")
        except Exception as exc:
            print("[ocr] rapidocr 不可用（%s），退化为墨迹打分" % exc)

    def _cache_path(self, img: Path) -> Path:
        h = hashlib.sha1(img.read_bytes()).hexdigest()[:16]
        return self.paths.ocr / ("%s.json" % h)

    def text(self, img: Path, region=None) -> dict:
        """region=(l,t,r,b) 相对坐标；用于剔除烧进画面的字幕条（那部分不是幻灯片内容）"""
        empty = {"text": "", "chars": 0, "boxes": 0}
        if not self.available:
            return empty
        cache = self._cache_path(Path(img))
        key = cache
        suffix = ""
        if region and tuple(region) != (0.0, 0.0, 1.0, 1.0):
            suffix = "_r%d%d%d%d" % tuple(int(x * 100) for x in region)
            key = cache.with_name(cache.stem + suffix + ".json")
        if key.exists():
            return json.loads(key.read_text(encoding="utf-8"))
        try:
            if suffix:
                from PIL import Image
                import numpy as np
                im = Image.open(img).convert("RGB")
                w, h = im.size
                l, t, r, b = region
                im = im.crop((int(l * w), int(t * h), int(r * w), int(b * h)))
                result, _ = self.engine(np.asarray(im))
            else:
                result, _ = self.engine(str(img))
        except Exception:
            result = None
        lines = []
        for item in (result or []):
            try:
                lines.append(str(item[1]))
            except Exception:
                continue
        text = " ".join(lines).strip()
        data = {"text": text, "chars": len(text.replace(" ", "")), "boxes": len(lines)}
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
