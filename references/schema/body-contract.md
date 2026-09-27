# 正文与小节的格式契约（工具校验，写手必须照做）

## 章节正文文件 chapters/NN-slug.md

* 纯正文：只有 ##/### 小节标题、段落、图、表格；**没有 front matter / meta 块**；
* 每个 ## 小节标题的下一行只写它依据的幻灯片：*slide 0006* 或 *slides 0014, 0015, 0016*；
  **不要写时间** —— 时间由 bnote retime 从 slides.json 生成，格式 *HH:MM:SS-HH:MM:SS | slide NNNN*；
* 一个小节内 >=2 张图时，每张图上方要有自己的时间行 *HH:MM:SS | slide NNNN*（同样由 retime 生成）；
* 图片引用只有**两类**（白名单，`bnote/layers/refs.py` 是唯一定义处）：主图 `../slides/NNNN.jpg`
  （页号固定 **4 位**，`slides/` 的页号空间冻结）与读字面板 `../_meta/sheets/<name>.png`
  （名字必须能在 `_meta/sheet.json` 的 tile 里查到）；两类之外的引用**不认**——既不会被校验放过，
  也不会被 `merge`/`remap` 改写（相对引用永远有效的前提就是这条白名单）；
* 面板图是**缩放拼图**，里面的字**一律不采信**：要读字用 `bnote frames --read` 取**媒体原生分辨率**单帧（缓存帧本身已是原生尺寸时直接复用，sha256 与 `cache/frames/` 那张逐字节相同）；
* ### 小结、### 思考 用小节内的三级标题。

## 就地标注（给读者看的推断与存疑）

凡是**推断出来的更正**（manifest 的 corrections 里 basis=context）与**无法归位的存疑**
（manifest 的 uncertainties），都要在**相关段落之后**补一个引用块，写法固定为：

```markdown
> **【校对】** 字幕作「CARL」，页面无此词，按语境推断为 KL 散度（00:11:16）。
```

* 位置：**紧跟相关那一段之后**，自成一段；**不要写进句子中间**、不要打断正文阅读；
* 内容写全三样：原文（错成什么）→ 更正或存疑点 → 依据（slide 编号 / 元信息 / 语境）；
* 「校对」两字**写全、不要简写**（工具按 `**【校对】**` 这个标记计数）；
* 有页面用字或人写元信息作依据的普通更正**不必**标注——照旧写干净正文、记进 manifest 即可；
* 工程性碎片（看图比对过程、切片判断等）仍只进 manifest，**不进正文**。

## 校验口径（bnote check）

| 项 | 级别 | owner |
|---|---|---|
| 小节缺时间行 / 未展开 / 非 HH:MM:SS | error | chapter:<id> |
| 小节时间重叠、逆序、越出章界 | error | chapter:<id> |
| 引用不存在或不属于本章的 slide | error | chapter:<id> / pipeline |
| 图片引用不在两类白名单内（非 ../slides/NNNN.jpg 或 ../_meta/sheets/<name>.png） | error | chapter:<id> |
| 正文引用的面板图在 _meta/sheet.json 里找不到对应 tile | error | chapter:<id> |
| 多图小节缺每图时间行 | error | chapter:<id> |
| corrections 缺 wrong/right/evidence | error | chapter:<id> |
| 推断类校正(basis=context) / 存疑(uncertainties) 没在正文就地标注 | error | chapter:<id> |
| stage_merges 的 slides/kept 与本章不符 | error | chapter:<id> |
| manifest.slide_count 与 slides.json 页数不一致 | error | pipeline |
| manifest.slideset_id 与当前 slides.json 的切片指纹不一致（整集错版） | error | pipeline |
| 某章的 slideset_id 与当前切片不一致（该章需重派写手） | error | pipeline |
| slides/NNNN.jpg 实际 sha256 与 slides.json 记录不符（图被替换/拷贝中断） | error | pipeline |
| 缺切片指纹：manifest / slides.json 早于本功能，或某几章没被派单覆盖 | warn | pipeline |
| 存在 slideset_remap 留痕：页号已同步但正文与图注未重写 | warn | pipeline |
| 章界不连续、末章未覆盖片尾、字幕有段落无归属 | error | manifest |
| keypoints < 下限 / 缺 questions | error | chapter:<id> |
| 有 slide 未被任何章引用 | warn | chapter:? |

脚本**不检查**内容（覆盖深度、图注准确性、术语取舍）—— 那些走写作契约与可选 review。
