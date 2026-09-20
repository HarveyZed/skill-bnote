#!/usr/bin/env python3
"""生成 references/ 下的可移植文档（工具生成，避免与代码漂移）。

产出：
  references/params.md                            参数表（来自 config/default.toml）
  references/schema/bnote-chapters-1.schema.json  manifest 结构 JSON Schema
  references/schema/body-contract.md              正文/小节格式契约与校验口径
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bnote.config import DEFAULT_TOML, load  # noqa: E402

CHAPTER_REQUIRED = ["id", "title", "range", "slides", "body", "keypoints", "questions"]

SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "bnote-chapters/1",
    "type": "object",
    "required": ["schema", "video", "slide_count", "chapters"],
    "properties": {
        "schema": {"const": "bnote-chapters/1"},
        "video": {"type": "object"},
        "slide_count": {"type": "integer", "minimum": 1},
        "scaffolded_at": {"type": "string"},
        "chapters": {"type": "array", "minItems": 1,
                     "items": {"$ref": "#/definitions/chapter"}},
    },
    "definitions": {
        "chapter": {
            "type": "object",
            "required": CHAPTER_REQUIRED,
            "properties": {
                "id": {"type": "string", "pattern": "^[0-9]{2,}$"},
                "title": {"type": "string"},
                "range": {"type": "array", "minItems": 2, "maxItems": 2,
                          "items": {"type": "string",
                                    "pattern": "^[0-9]{2}:[0-9]{2}:[0-9]{2}$"}},
                "slides": {"type": "array", "items": {"type": "integer", "minimum": 1}},
                "body": {"type": "string", "pattern": "^[^/]+[.]md$"},
                "keypoints": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "questions": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "corrections": {"type": "array", "items": {
                    "type": "object",
                    "required": ["wrong", "right", "evidence"],
                    "properties": {"wrong": {"type": "string"},
                                   "right": {"type": "string"},
                                   "evidence": {"type": "string"}},
                }},
                "review_flags": {"type": "array", "items": {"type": "string"}},
                "coverage_notes": {"type": "string"},
                "stage_merges": {"type": "array", "items": {
                    "type": "object",
                    "required": ["slides", "kept"],
                    "properties": {"slides": {"type": "array",
                                               "items": {"type": "integer"}},
                                   "kept": {"type": "integer"},
                                   "why": {"type": "string"}},
                }},
            },
        },
    },
}

CONTRACT = """# 正文与小节的格式契约（工具校验，写手必须照做）

## 章节正文文件 chapters/NN-slug.md

* 纯正文：只有 ##/### 小节标题、段落、图、表格；**没有 front matter / meta 块**；
* 每个 ## 小节标题的下一行只写它依据的幻灯片：*slide 0006* 或 *slides 0014, 0015, 0016*；
  **不要写时间** —— 时间由 bnote retime 从 slides.json 生成，格式 *HH:MM:SS-HH:MM:SS | slide NNNN*；
* 一个小节内 >=2 张图时，每张图上方要有自己的时间行 *HH:MM:SS | slide NNNN*（同样由 retime 生成）；
* 图片用相对路径 ../slides/NNNN.jpg（**目录内部结构冻结**，故相对引用永远有效）；
* ### 小结、### 思考 用小节内的三级标题。

## 校验口径（bnote check）

| 项 | 级别 | owner |
|---|---|---|
| 小节缺时间行 / 未展开 / 非 HH:MM:SS | error | chapter:<id> |
| 小节时间重叠、逆序、越出章界 | error | chapter:<id> |
| 引用不存在或不属于本章的 slide | error | chapter:<id> / pipeline |
| 多图小节缺每图时间行 | error | chapter:<id> |
| corrections 缺 wrong/right/evidence | error | chapter:<id> |
| stage_merges 的 slides/kept 与本章不符 | error | chapter:<id> |
| manifest.slide_count 与 slides.json 页数不一致 | error | pipeline |
| 章界不连续、末章未覆盖片尾、字幕有段落无归属 | error | manifest |
| keypoints < 下限 / 缺 questions | error | chapter:<id> |
| 有 slide 未被任何章引用 | warn | chapter:? |

脚本**不检查**内容（覆盖深度、图注准确性、术语取舍）—— 那些走写作契约与可选 review。
"""


def main() -> int:
    cfg = load()
    out = subprocess.run([cfg["tools"]["python"], "-m", "bnote", "config", "--md"],
                         cwd=str(ROOT), capture_output=True, text=True)
    table = out.stdout.strip() or ("（生成失败：%s）" % out.stderr.strip())
    head = ("# bnote 参数表（由 bnote config --md 生成，勿手改）\n\n"
            "环境变量覆盖写法：BN_<段>_<键>，例如 BN_SEGMENT_DIFF_THRESHOLD=0.02。\n"
            "数据根解析顺序：BNOTE_ROOT > config/local.toml 的 [paths] root > <cwd>/.bnote。\n\n")
    (ROOT / "references" / "params.md").write_text(
        head + table + "\n\n## config/default.toml 原文\n\n```toml\n"
        + DEFAULT_TOML.read_text(encoding="utf-8").strip() + "\n```\n", encoding="utf-8")
    (ROOT / "references" / "schema").mkdir(parents=True, exist_ok=True)
    (ROOT / "references" / "schema" / "bnote-chapters-1.schema.json").write_text(
        json.dumps(SCHEMA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "references" / "schema" / "body-contract.md").write_text(CONTRACT, encoding="utf-8")
    print("[gen-references] params.md / schema/bnote-chapters-1.schema.json / schema/body-contract.md 已生成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
