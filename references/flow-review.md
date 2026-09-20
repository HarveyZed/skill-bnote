# 可选 review（默认关，父 agent 决定）

**什么时候读我**：这一集值得多花一份 token 做内容审阅时——即 `check` 通过后仍要查内容三类风险（漏讲 / 编造 / 图注与图不符），且父 agent 已决定派 review。

`check` 只能查结构；内容三类风险（漏讲 / 编造 / 图注与图不符）只有读 transcript + 看图才能发现。
review 是**可选阶段**（多一份 token），由父 agent 按这一集的价值决定派不派：

```bash
scripts/bnote brief  <URL> --page N --stage review --scope sample:3 --focus fidelity --round 1
scripts/bnote review <URL> --page N --ingest      # 摄入 findings：按 scope 归一 owner，并入同一条 fix 流
```

| 参数 | 含义 | 推荐 |
|---|---|---|
| `--scope` | `all` / `chapter:06,07` / `sample:N`（工具只把它渲染进派单 prompt，**不强制抽样**） | 短课 all，长课 sample:3 |
| `--focus` | `fidelity`（漏讲/编造/图注）/ `depth`（提炼质量） | 讲义用 fidelity，笔记用 depth |
| `--round` | 轮次（写进 findings 文件名与 loop 台账） | 最多 2 轮 |
| severity | `major` 回派 / `minor` 只记录 | 只回派 major |

**findings 必须带 scope**（决定影响面与处理路径，选错会导致整集返工）：
`L1` 单章内容 · `L2` 跨章不一致（单 agent 串行修）· `L3` 章界（编排者决定重排范围，等于局部重做）·
`L4` 时间轴（→ pipeline，跑 retime，零 token）· `L5` 管线（重切 + remap 后回派相关章）· `L6` note（重派 note agent）。
