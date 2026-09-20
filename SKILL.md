---
name: bnote
description: 把 B 站视频转成可读的 Markdown 学习资料——逐节讲义 lecture.md（按原时间轴、配幻灯片图）与单课笔记 note.md。当用户给出 B 站链接（BV 号或 URL，可能带分P）并要求以下任一项时使用：生成讲义或课程笔记、把视频变成可读文字、抽取 PPT 幻灯片页面、提取字幕、整理口播/播客类视频的字幕。不适用：只想要视频或音频文件（用 yt-dlp）、只想要一段简短摘要（本 skill 产出的是完整逐节资料）。
license: MIT
metadata:
  version: 0.9.0
  entrypoint: "scripts/bnote"
  requires: "Python>=3.10（解释器用 config/local.toml 的 [tools] python 或 BN_PYTHON 配置）；ffmpeg 由 imageio-ffmpeg 静态包提供；B 站登录态推荐（否则字幕退化为本地 ASR）"
  layout: "SKILL.md + references/（契约与 schema）+ scripts/（入口与工具）+ config/ + 代码包；运行期数据在 skill 之外的数据根"
---

# bnote — B站视频 → 讲义 + 学习笔记

## 〇、环境依赖与安装（新机器先做这一步）

这套 skill **自包含**（SKILL.md + references/ + scripts/ + config/ + 代码都在本目录），
但 **venv 与 ffmpeg 不随 skill 走** —— 换机器要各自准备，和大多数 CLI 工具的惯例一致。

**适用平台**：Linux / macOS（入口与安装脚本是 bash）。**Windows 用 WSL2**——原生 Windows 下 `scripts/bnote`、
`scripts/setup-env.sh` 跑不起来（venv 目录布局也不同）；装好 WSL2 之后本节所有命令与 Linux 完全一致。

**路径约定**：本文件所在目录就是 **skill 根**；下文的相对路径（`scripts/bnote`、`references/…`）都相对它。
若当前工作目录不在 skill 根，请用 `<skill根>/scripts/bnote` 调用（数据根与 cwd 无关，见下）。

| 依赖 | 怎么装 | 说明 |
|---|---|---|
| Python >= 3.10 | 系统自带或 conda | 本项目代码不在 PyPI 上，所以随 skill 一起复制；第三方依赖由 setup-env.sh 安装 |
| venv + 依赖 | `bash scripts/setup-env.sh` | 建 venv 并 `pip install -e .`（默认清华镜像；海外/内网环境务必用 `PIP_INDEX_URL` 覆盖为可达索引） |
| ffmpeg | **建议装系统版**：Linux `apt install ffmpeg`／macOS `brew install ffmpeg`（WSL2 里同 Linux）；或在 `[tools] ffmpeg` 写绝对路径 / `BN_TOOLS_FFMPEG` | `--sections`、HLS、音视频合并都需要能**解析域名**的 ffmpeg；`imageio-ffmpeg` 自带的静态包只作兜底（整集下载、抽帧、本地文件可用，但部分环境下碰域名直接崩）。**环境有问题就在环境里解决**（装系统 ffmpeg / 配绝对路径），不要绕过、也不要改代码去迁就 |
| 解释器 | `config/local.toml` 的 `[tools] python`，或 `BN_PYTHON` 环境变量 | 不想配也行：默认 `python3`；入口脚本 `scripts/bnote` 会自动读取 |
| B 站登录态 | `scripts/bnote auth status` / `auth login`（扫码，须本人操作） | 官方字幕轨（AI 与 CC）的**列表**目前只对登录态返回（游客实测恒为空；字幕**文件**本身不校验登录）。取不到字幕时有三条路：① 扫码登录 ② 自带 srt → `--subtitle-backends file` ③ 本地 ASR（需 `pip install -e ".[asr]"`，装不装由使用者定）。**走哪条由 agent 判断**（agent 也可以去问用户），并在回报里写明选择与理由——不要默认把决定推给用户，也不要静默降级 |

```bash
bash scripts/setup-env.sh                 # 一次性：建 venv + 装依赖
scripts/bnote config --paths              # 确认解释器与数据根解析正确
scripts/bnote auth status                # 看登录态（没有就 auth login 扫码）
scripts/bnote run <URL> --page N --sections 00:00:00-00:05:00   # 先用 5 分钟验证管线跑得通，再全量
#   换/新数据根时没有登录态：先 `auth login`，或用 `--cookie-file <旧根>/state/auth/bilibili_cookies.txt` 复用旧根登录态
```

### 数据根（默认跟随 cwd，不写死绝对路径）

解析顺序：`BNOTE_ROOT` > `config/local.toml` 的 `[paths] root` > **`<当前工作目录>/.bnote`**。三个根分开：

```
<数据根>/state/<vid>/     术语表、画像、登录态等跨集沉淀（删 cache 不带走）
<数据根>/cache/<vid>/     中间产物：视频/音频/帧/OCR/切片结果（可随时删）
<数据根>/out/<vid>/       交付物：lecture.md / note.md / slides/ / chapters/ / _meta/
```

其中 `<vid>` = `<BV号>_p<分P>`（例：`BV1xxxxxxxxx_p20`）。

**out/<vid>/ 的内部结构是冻结的**（正文用 `../slides/NNNN.jpg` 相对引用），可配置的只是"根"。
每条命令都会打印解析后的根路径：取数命令（`run/fetch/slides/meta`）是完整横幅，其余命令是一行 `[paths] …` —— 路径不对会立刻看见。
除取数命令与 `clean` 外，数据根下没有该集的取数结果时，命令会直接以人话退出（并打印解析结果与补救命令），不会抛异常。

**环境前提**：数据根必须**可写**（`cache/`、`out/` 都写在那里）；**skill 根不需要可写** —— 代码目录随 skill 分发，可能是只读安装（容器挂载 / 系统目录），运行期产物一律落在数据根。
**换数据根＝换一整套 `state/`**（术语表、画像、**登录态**都跟着根走）：要复用旧根的登录态就用 `--cookie-file <旧根>/state/auth/bilibili_cookies.txt`，或在新根重新 `auth login`。
注意：**yt-dlp 会写回它读的那个 cookie 文件**（刷新有效期），所以「跨根复用」会改到旧根的登录态文件——介意就先把 cookie 复制一份再指过去。

## 一、产出什么（三层，别混）

| 层 | 文件 | 是什么 | 读者 |
|---|---|---|---|
| L1 | `transcript.md` | 字幕原文（时间戳 + 文本；可能是平台 AI 轨、UP 上传的 CC，或本地 ASR） | 需要核对原话时 |
| L2 | `lecture.md` | **视频的文字版**：按原时间轴、逐节、配图 | 想看完整内容的人 |
| L3 | `note.md` | **单 lecture 学习笔记**：沿时间轴的 6-30 个关键节点 + 理解层注释 | 未来的自己（复习/检索） |

配套：`lecture.standalone.md`（图片内嵌，可直接外发）、`slides/`（每页终态帧）、
`chapters/manifest.json`（结构唯一来源）、`_meta/`（校验报告、派单 prompt、修复件、loop 台账、钩子）。

## 参考文件索引（按需读，正文不展开）

本 skill 依赖同目录的 `references/`：`brief` 派单时会读取 `references/contracts/*.md` 渲染任务书，
契约缺失会直接退出并提示「契约模板缺失」。**只复制 SKILL.md 一个文件无法派单**，references/ 要一起带上。

| 触发条件 | 读哪一份 |
|---|---|
| 你要派章节写作单时 | `references/contracts/chapter_writer.md` |
| 你要派笔记写作单时 | `references/contracts/note_synth.md` |
| 你要派结构修复单时 | `references/contracts/fix.md` |
| 你要派内容审阅单时 | `references/contracts/review.md` |
| 你要派信息流整理稿单时 | `references/contracts/text_writer.md` |
| 写正文、改正文格式，或要确认 `check` 的校验口径时 | `references/schema/body-contract.md` |
| 改 manifest 字段或核对章节结构时 | `references/schema/bnote-chapters-1.schema.json` |
| 查参数默认值与 `BN_<段>_<键>` 覆盖写法时 | `references/params.md` |
| 当前 harness 是 DSH，遇到委派工具或 ffmpeg 静态包的问题时 | `references/dsh-notes.md` |
| 需要查内容问题（漏讲/编造/图注错）时 | `references/flow-review.md` |
| 视频是口播/播客/访谈、画面无信息量时 | `references/mode-text.md` |
| 要把讲义导入 B 站笔记时 | `references/export.md` |
| 命令报错、校验失败或产物异常时 | `references/troubleshooting.md` |
| 要改工具、契约、参数或发布新版本时 | `references/maintenance.md` |

## 遇到问题时

产物异常时先读三个文件再判断：`<out>/_meta/validation.json`（机器可读、每条带 owner 路由）、`validation.md`（人读版）、`loop.json`（修复轮次台账）；三者都正常才轮到怀疑 skill 与工具，逐条处置见 `references/troubleshooting.md`。
确认是 skill 的问题：到 https://github.com/HarveyZed/skill-bnote/issues 提 issue，附复现命令与 `scripts/bnote config --paths` 的输出，**先去掉本机路径与个人信息**。
也可以直接按 `references/contracts/` 的契约自行修改，改完把改动说明写进回报。

## 二、端到端流程（含 agent 节点与门禁）

```
run 取数 ──▶ scaffold 分章 ──▶ brief --stage chapter ──▶ writers 并行写章
                 ▲ 门禁①                ▲ 门禁②                  │
                 └ 必须给 _groups.json    └ 术语表必须已确认        ▼
                                                              collect 汇总补丁
                                                                     │
                                        retime（工具生成时间行）◀────┘
                                                                     │
   ┌─────────────── check（结构，按 owner 路由）◀──────────────────────┘
   │ 失败 → brief --stage fix --owner X → 发回原写手（不可用/被污染则改派新的干净子代理）→ 再 check（≤2 轮，超了升级给人）
   ▼ 通过
merge 出 lecture.md ──▶ brief --stage note ──▶ 单个全局 agent 写 note.md ──▶ note 校验/导钩子
   │
   └─（可选）review：brief --stage review → 独立 agent 只审内容 → review --ingest → 回同一条 fix 流
```

### 2.1 工具层（确定性，一步一命令，幂等）

```
auth       登录态（扫码 / 手动 / 检查）
run        取数一条龙：meta + media + subtitle + frames + segment + ocr + bundle
fetch      只取数（meta + media + subtitle），不做抽帧/切片
stream     信息流/口播类（无幻灯片）：取音频+字幕 → 分段分块 → 整理稿（--assemble 拼接+校验）
meta       只取元信息（BV/p/cid/标题/时长/**简介/标签/分区**；旧缓存会自动补取这三样）
slides     只做抽帧 + 切片 + OCR（改了切片参数后重跑它，再跑 bundle）
bundle     只重建交付物（slides/ + slides.json + transcript.md），并快照旧讲义
scaffold   生成章节结构（manifest + _plan）；分章是语义判断：给 --groups，或显式 --auto 接受一页一章（--no-leading-merge：封面不与目录合并）
brief      渲染派单 prompt：--stage chapter|note|fix|review
glossary   查看/修改/确认术语表（未确认时 brief --stage chapter 会拒绝派发）
patch      打印某章的补丁文件路径与 schema（写手写这里，不直接改 manifest）
collect    把 _meta/patch/<章号>.json 汇总进 manifest（多写手并发时不丢更新）
retime     按 slides.json 幂等重写小节时间行（时间由工具生成，写手不写时间）
check      结构校验；--chapter 06,07 限定作用域（写手自检用）
merge      合并讲义（结构校验通过才落盘）+ 刷新 note_brief
note       校验 note.md + 导出钩子
export     导出可粘贴进 B 站笔记的富文本（--format bili-note；--from lecture|note；CF_HTML + Windows 装载脚本；不调平台写接口）
xref       把多集的钩子与结构汇成跨讲综合工作表（--from-page A --to-page B；输出到 <数据根>/out/_xref/；缺集会拒绝）
remap      重切片后按时间重叠同步正文与 manifest 的图号（幂等；--from <旧 slides.json> 或 --force-map 强制重写；bundle 会自动快照上一版）
dispatch   登记 owner → 该章的原写手（修复时据此发回原作者）
fix        修复队列：不带参数打印待修清单与投递对象；--done <owner> 标记已修
review     摄入审阅发现（_meta/review_<n>.json）→ 归一 owner → 并入派修流
clean      清理 <URL> --page N（--level state|cache|out|all，先报占用；--dry-run 只看不删）
config     打印生效配置 / --paths 解析后的根 / --md 参数表
```

### 2.2 agent 层与两道门禁（防止关键判断被静默跳过）

1. **分章**（门禁①）：`scaffold` 不给 `--groups` 就报错退出 —— 分章是语义判断，不接受"一页一章"的退化产物，
   要看自动产物请显式 `--auto`；
2. **术语表**（门禁②）：`brief --stage chapter` 在术语表未确认时**拒绝派发**（`--allow-unconfirmed` 可显式越过）；
   理由：未确认的白名单会被写手当成"必须使用的写法"，错的白名单比没有更糟；
3. **写作**：每章一个写手，只写正文 + 自己的补丁文件 `_meta/patch/NN.json`（不并发改同一份 manifest）；
4. **时间轴**：写手只写 `*slide 0006*` / `*slides 0014, 0015, 0016*`，由 `retime` 展开时间；
5. **同页阶段合并**：由 writer 看图决定（切片层不做），结果记进 manifest 的 `stage_merges`；
6. **笔记**：单个全局 agent 读 `note_brief.md` + 全部章节写 `note.md`（不碎片化）。

### 2.3 派单方式：写手 / 笔记用「全新上下文」的子代理

派写手、派笔记这类独立任务，用**全新上下文**的委派工具（DSH 里是 spawn 语义的 `subagent`）：子代理只拿到任务书，
不继承父会话历史——上下文小，也不会沿用父会话的角色与目标。任务书必须自包含：读哪些文件（契约 / 字幕 / slides）、
写哪些文件、**不许**动什么（`chapters/manifest.json`、仓库、已装副本）。
继承父会话全部轮次的 fork 语义，只适合「接着当前这段对话继续干活」。

> 若当前会话拿不到这类工具（DSH 有已知缺陷、官方未修），判别与绕法见 `references/dsh-notes.md` —— **不要**用 fork 顶替。

### 2.4 修复与轮次（loop 必须真的能 loop）

工具负责**把修复件写好、把队列与轮次算出来**；投递由编排者执行 —— **默认发回该章的「原写作 agent」**
（`send_message` 或你所用 harness 的等价工具）：它知道这章为什么这样写，改得最准。修复件仍要自包含
（错误原文 + `fix_hint` + 文件路径 + 只改此处）。

只有两种情况才改派一个**新的干净子代理**（只带修复件）：① 原 agent 已不可用（会话结束/被清理）；
② 原 agent 已被污染或跑偏（例如它是 fork 出来的、或动过仓库与产物）。
（唤醒子代理要用 **agent 侧工具**——DSH 里是 `send_message`；脚本调不到它，所以投递这一步只能由编排者做。）

```bash
scripts/bnote dispatch <URL> --page N --owner chapter:01,02 --agent <agent-id>   # 派单后登记一次（--owner 与 --agent 都必填）
scripts/bnote check    <URL> --page N                       # 失败 → 生成 _meta/validation.json
scripts/bnote brief    <URL> --page N --stage fix --owner chapter:01
scripts/bnote fix      <URL> --page N                       # 打印待修清单与投递对象（原 agent / 改派的新代理）
scripts/bnote fix      <URL> --page N --done chapter:01     # 子 agent 回报后标记（写 _meta/loop.json；轮次自动累加）
```

轮次上限 **2**：`fix` 按台账自动累加轮次，到上限仍在队列里会标 ⚠「建议升级给人」（无需手工传 `--round`）。owner 决定谁修：

| owner | 谁修 | 怎么修 |
|---|---|---|
| `chapter:<id>` | 该章原写作 agent | 发修复件给它，只改被指出的地方；不可用/被污染时改派新的干净子代理 |
| `manifest` | 编排者（父 agent） | 章界、时间范围、末章覆盖 |
| `pipeline` | 工具/流水线 | 例如时间轴问题跑 `retime`、页序漂移跑 `remap` —— 不是写作 agent 的锅 |

### 2.5 术语表评审闭环（谁读、几轮、何时停）

| 环节 | 谁做 | 动作 |
|---|---|---|
| 提议 | 脚本 | `brief` 自动写 `<state>/glossary/<vid>.json`（`confirmed: false`），`_proposal` 里分来源给证据 |
| 一审 | **编排者** | `glossary <URL> --page N` 看清单：① 仅字幕出现的可疑词 ② 自动判定的 ASR 变体 ③ slide OCR 噪声（水印/型号/墨迹乱码） |
| 二审（可选） | 独立 reviewer | 只审术语、不写正文，产出 drop / add / avoid |
| 应用 | 编排者 | `glossary <URL> --page N --drop A B --add C --avoid "X->Y" --confirm --by <你>` |
| 生效 | 工具 | 之后 `brief` 用评审版白名单；未确认则**拒绝派发**（门禁②） |
| 停止 | —— | 已确认；或两轮后仍有歧义 → 升级给用户 |

- 可选审阅（review）：见 `references/flow-review.md`（当需要查内容问题：漏讲/编造/图注错时）
- 信息流模式（无幻灯片）：见 `references/mode-text.md`（当视频是口播/播客/访谈、画面无信息量时）

## 三、结构 vs 内容：工具管什么、agent 管什么

- **结构**（工具强校验，失败不覆盖成品）：manifest schema、章界连续与覆盖、slide 归属、
  **正文小节时间行（格式统一 / 单调 / 不重叠 / 不越章界）**、多图小节的每图时间戳、图存在、
  `corrections` 三字段、`stage_merges` 一致性、note 节点格式与 20% 预算；校验口径详见 `references/schema/body-contract.md`；
- **内容**（写进写作契约，工具不 grep）：覆盖每个知识点、图注必须自己**打开图看**过再写（读图工具：DSH 里是 `read_image`，其它 harness 用等价工具；OCR 会漏字）、
  分类落表格、小结/思考、不编造；术语白名单与排版规则由**内容画像**注入；
- **派生数据由工具生成**：时间行（`retime`）、节点索引（`note`）、钩子（`note`）；
- 工程性信息（听写校正、存疑、覆盖说明）一律进 manifest 字段 —— 正文里没有位置可写；
- **视频页元信息**（简介 / 标签 / 分区）在取数时落进 `<数据根>/cache/<vid>/meta.json`，并注入派单 prompt（章节写手、笔记写手、整理稿写手都能看到，当背景）；人也从 `index.md` 看到它。
  它**不是课程内容**：可以用来判断主题、术语写法、是否属于某个系列，但不许写进讲义/笔记正文（讲师没说的不算课程讲的）。

## 四、故障、维护与发布（按需读）

- 常见故障：见 `references/troubleshooting.md`（当命令报错、校验失败或产物异常时；先读 `_meta/validation.json` / `validation.md` / `loop.json`）
- 要改工具或发布版本：见 `references/maintenance.md`（当要改代码、契约、参数或发新版本时）
