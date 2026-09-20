# 信息流模式（无幻灯片）：`bnote stream`

**什么时候读我**：视频是口播 / 播客 / 访谈一类、画面无信息量（字幕或音频已包含全部信息，画面只是动画、配图、出镜）时——这类视频不分章、不截图，走 `stream` 这条轻流程。

适用：口播 / 播客 / 访谈一类——**字幕（或音频）已经包含全部信息**，画面是动画、配图、出镜等无关内容。
这类视频**不分章、不截图**，走一条轻流程：

```bash
scripts/bnote stream <URL> --page N              # 取音频+字幕 → 机械分段 → 分块（渐进式披露）
#   → 一个 writer 读 <数据根>/out/<vid>/_meta/prompt_text.md，按块读 _meta/text_chunks/NN.md、写 _meta 同级的 text/NN.md
scripts/bnote stream <URL> --page N --assemble   # 拼成 lecture.md + 生成 index.md + 结构校验
scripts/bnote stream <URL> --page N --verify     # 只校验
scripts/bnote note   <URL> --page N              # 笔记：素材自动切到段落锚点，没有 manifest 也能跑
```

- 交付两份：`transcript.md`（原样字幕）与 `lecture.md`（整理稿：补标点、按术语表校正、**逐段覆盖不摘要**）；
- **时间锚点由工具打**（段落起点）：writer 只写正文与措辞，可以合并相邻段落，**不许自编时间**（`check` 会拦）；
- 长视频用**渐进式披露**：字幕按 `text.chunk_chars` 切成 N 块，一块一个素材文件 + 一个产出文件，
  writer 一块块读、一块块写（避免一次吞下几万字、也避免后段越来越糊）；
- 结构校验（`--assemble` 内自动跑，`check` 也会走这条分支）：段落覆盖连续、锚点来自工具且单调、
  整理稿字数 ≥ 字幕正文 × `text.min_cover_ratio`（防摘要化）；
- 不做的事：不抽帧、不切片、不 OCR、不分章，没有 `manifest` / `slides/`；
  **已知降级**：无 OCR 时术语画像与术语白名单只从字幕推（比幻灯片模式瘦），无 speaker 分离（不标说话人）。
