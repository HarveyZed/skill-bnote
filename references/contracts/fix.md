# 派单：结构修复（bnote 契约 fix v1）

你是**原写作 agent**，现在修复结构校验报出的问题。工作目录：{{OUT_DIR}}，第 {{ROUND}} 轮修复。

## 只修这些（脚本原文，逐条改）

{{ERRORS}}

## 规矩

- **只改被指出的地方**，不要顺手改别的章、别的措辞；
- 时间行**不要手写**：小节标题下只写 `*slide NNNN*` / `*slides NNNN, NNNN*`，时间由 `bnote retime` 生成；
- 涉及 manifest 字段的（`corrections` 缺字段、`stage_merges` 与 slides 不符）：**同样写成补丁** `_meta/patch/NN.json`（只写你改动的键），不要直接改 `chapters/manifest.json`；只改你负责的那几章；
- 改完自检：

```bash
{{CHECK_CMD}}
```

## 回报格式

逐条说明改了什么、`bnote check --chapter <你的章号>` 的结果。
