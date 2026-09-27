"""L6 OCR 层：廉价、可缺省。只用来判断"这一页的文字全不全"。

未安装 rapidocr 时 available=False，segmenter 自动退回"墨迹密度 + 清晰度"打分。
结果按帧文件内容哈希缓存，重复运行不重复推理。

两个可选裁剪口径（都来自 cache/<vid>/overlay.json，见 M3）：
  * region=(l,t,r,b)：**只保留**这一块再识别（旧口径：正文区之外整体丢掉）；
  * masks=[(l,t,r,b), ...]：**先挖掉**这几块（涂白）再识别。M3 起用这条——底部字幕条与
    左下角标注工具条不在同一个矩形里，一个 region 表达不了"两处都要排除"，而把工具条的
    文字留着会污染"同页判定"的 4-gram 包含度（p20/p21/p22 实测）。
缓存键带上 region/masks 的指纹，换了口径不会误用旧结果。

M4 起还有第三条：**手写笔迹**。overlay.json 里有可采信的 handwriting 区域时，本层按其中的
判据参数**逐帧**把彩色细笔画涂白再送识别（"笔迹不进 OCR"），缓存键同样带判据指纹。
为什么不把 handwriting 的 box 当 masks 直接用：笔迹逐帧移动、累积，按框整块挖会连带涂白
其余页同位置的正文（判据实现与理由见 segmenters/framesig.py 的 stroke_mask）。
不给 ocr.text() 加新参数是**有意的**：M3 已冻结 text(img, region=None, masks=None)，
"挖哪些像素"属于本层自己的口径，从 paths 里读 overlay.json 就够了。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _whiten_strokes(im, params, width: int = 320):
    """把一帧里的**彩色细笔画**（手写笔迹）涂白：先缩到判据尺度算掩膜，再按最近邻放大回原尺寸。"""
    from PIL import Image
    import numpy as np
    from ..segmenters.framesig import stroke_mask, stroke_params
    w, h = im.size
    hh = max(2, int(round(width * h / float(w))))
    small = np.asarray(im.resize((width, hh), Image.BILINEAR), dtype=np.float32) / 255.0
    m = stroke_mask(small, stroke_params(params))
    if not m.any():
        return im
    mi = Image.fromarray((m * 255).astype(np.uint8), mode="L").resize((w, h), Image.NEAREST)
    return Image.composite(Image.new("RGB", (w, h), (255, 255, 255)), im, mi)


class Ocr:
    def __init__(self, cfg: dict, paths):
        self.cfg = cfg
        self.paths = paths
        self.engine = None
        self.available = False
        # M4：手写笔迹判据参数（overlay.json 里有可采信的 handwriting 区域才生效）
        self.strokes = None
        try:
            doc = paths.read_json(paths.overlay) or {}
            if any(r.get("kind") == "handwriting" and r.get("applicability") == "ok"
                   for r in (doc.get("regions") or [])):
                self.strokes = dict(doc.get("params") or {})
        except Exception:
            self.strokes = None
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
            return
        if self.strokes:
            print("[ocr] 按 overlay.json 的 handwriting 判据逐帧涂白彩色细笔画后再识别"
                  "（手写笔迹不进 OCR 文本）")

    def _cache_path(self, img: Path) -> Path:
        h = hashlib.sha1(img.read_bytes()).hexdigest()[:16]
        return self.paths.ocr / ("%s.json" % h)

    @staticmethod
    def _params_key(d) -> str:
        """笔画判据参数的稳定指纹（缓存键用）：换了判据参数不该复用旧 OCR 结果。"""
        blob = json.dumps({k: round(float(v), 4) for k, v in sorted((d or {}).items())},
                          separators=(",", ":")).encode("utf-8")
        return hashlib.sha1(blob).hexdigest()[:8]

    @staticmethod
    def _mask_key(masks) -> str:
        """masks 的稳定指纹（排序、round 3 位后散列）：换一组遮罩不该复用旧 OCR 结果。"""
        norm = sorted(tuple(round(float(x), 3) for x in m) for m in (masks or []))
        blob = json.dumps(norm, separators=(",", ":")).encode("utf-8")
        return hashlib.sha1(blob).hexdigest()[:8]

    def text(self, img: Path, region=None, masks=None) -> dict:
        """region=(l,t,r,b)：只保留这一块；masks=[(l,t,r,b), ...]：把这些块挖掉（涂白）。

        两条口径来自 cache/<vid>/overlay.json，**二选一**：masks 非空就整幅送识别，
        不再叠加 region 裁剪——"只保留哪儿"和"挖掉哪儿"谁优先很难说清，不如让调用方选。
        """
        empty = {"text": "", "chars": 0, "boxes": 0}
        if not self.available:
            return empty
        cache = self._cache_path(Path(img))
        key = cache
        suffix = ""
        use_masks = bool(masks)
        if use_masks:
            suffix = "_m%s" % self._mask_key(masks)
        elif region and tuple(region) != (0.0, 0.0, 1.0, 1.0):
            suffix = "_r%d%d%d%d" % tuple(int(x * 100) for x in region)
        if self.strokes:
            suffix += "_h%s" % self._params_key(self.strokes)
        if suffix:
            key = cache.with_name(cache.stem + suffix + ".json")
        if key.exists():
            return json.loads(key.read_text(encoding="utf-8"))
        try:
            arr = None
            if use_masks or self.strokes or suffix:
                import numpy as np
                from PIL import Image, ImageDraw
                im = Image.open(img).convert("RGB")
                if self.strokes:                      # M4：先涂白手写笔迹，再按 region/masks 裁
                    im = _whiten_strokes(im, self.strokes)
                w, h = im.size
                if use_masks:
                    draw = ImageDraw.Draw(im)
                    for box in masks:
                        l, t, r, b = box
                        draw.rectangle([int(l * w), int(t * h),
                                        max(int(round(r * w)) - 1, 0),
                                        max(int(round(b * h)) - 1, 0)], fill=(255, 255, 255))
                elif region and tuple(region) != (0.0, 0.0, 1.0, 1.0):
                    # 注意：这里判的是 region，不是 suffix —— suffix 还可能是手写笔迹的判据指纹，
                    # 那时没有裁剪口径（写成 elif suffix 会去解包 region=None 并炸掉整次识别）
                    l, t, r, b = region
                    im = im.crop((int(l * w), int(t * h), int(r * w), int(b * h)))
                arr = np.asarray(im)
            result, _ = self.engine(arr if arr is not None else str(img))
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
