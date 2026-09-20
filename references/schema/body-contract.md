# 正文与小节的格式契约（工具校验，写手必须照做）

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
