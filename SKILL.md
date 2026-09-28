---
name: bnote
description: 把 B 站视频转成可读的 Markdown 学习资料——逐节讲义 lecture.md（按原时间轴、配幻灯片图）与单课笔记 note.md。当用户给出 B 站链接（BV 号或 URL，可能带分P）并要求以下任一项时使用：生成讲义或课程笔记、把视频变成可读文字、抽取 PPT 幻灯片页面、提取字幕、整理口播/播客类视频的字幕。不适用：只想要视频或音频文件（用 yt-dlp）、只想要一段简短摘要（本 skill 产出的是完整逐节资料）。
license: MIT
metadata:
  version: 0.15.0
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
                     （brief 在派单时刻记下切片指纹）        collect 汇总补丁 + 搬指纹
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
run        取数一条龙：meta + media + subtitle + frames + segment + ocr + bundle；--mode 显式声明模式（不触发探测）
fetch      只取数（meta + media + subtitle），不做抽帧/切片
stream     信息流/口播类（无幻灯片）：取音频+字幕 → 分段分块 → 整理稿（--assemble 拼接+校验）
           M5 起可选 --with-vision：额外产出**取样面板**当画面旁证给写手（需先跑 bnote sample；
           面板只覆盖几个窗口、不代表全片；**能否在正文引用**由 [sheet].inline 定，默认不许引）；
           --mode 显式声明模式（与 mode_hint 冲突只 warn、按显式值走）；两者都**不改**机械分段与锚点规则
meta       只取元信息（BV/p/cid/标题/时长/**简介/标签/分区/UP 置顶评论**；旧缓存会自动补取）
slides     只做抽帧 + 切片 + OCR（改了切片参数后重跑它，再跑 bundle）；M3 起顺序是抽帧 → overlay → 切片；
           M4 起切片前还会做**帧角色分类 + 整页优先**：段内只要存在可当主图的候选
           （full_page 或 app_screen）就不能选别的。**「遮挡最少」换帧默认关闭**
           （[roles].occlusion_swap=false；它唯一的真实样本 p23 段 11 上判错了方向，逐帧证据见
           config/default.toml），打开后才在同一档候选里再挑遮挡最少的那张（信息量接近时）。
           遮挡数值照旧落盘：每段写 role / role_evidence，候选表逐帧带 role 与 occlusion，
           bundle 再把它们镜像进
           slides.json 每页。M4b 的第六个角色 app_screen = 整屏 IDE/浏览器/终端/桌面录屏：
           **仍是整屏内容、整页优先照旧可作主图**，只是不走手写涂白（见 overlay 那条）
bundle     只重建交付物（slides/ + slides.json + transcript.md），并快照旧讲义；slides.json 顶层写这一版切片的指纹
           `slideset`（每页含 frame/chosen_t/sha256）—— 摘要只依赖 out/，cache 删掉也能复算
overlay    M3 遮挡区识别：从**已抽出的帧**里认出"不是幻灯片内容"的那几块像素 → cache/<vid>/overlay.json
           （① band_change_rate：底部烧录字幕条，按逐行变化剖面拟合带；② corner_static_glyphs：
           角状外物（标注工具条/水印/进度条）＝位置固定在边角 + 帧间几乎不变 + 有细小字符或色块，
           后一条在**全分辨率**的若干帧上量，并用多帧交集收紧方框。每条区域带判据、置信与适用性；
           **不落逐帧掩码**、不含时间戳。只读 cache/frames/，不重新解码整片；认不出来写 insufficient，
           不静默返回空。切片与量测都读它；没有它时两者退化为全画面并在日志说明。
           M4 起还有第三条 ③ color_stroke：**手写笔迹**（彩色细笔画）→ kind=handwriting 的区域。
           它**只用于把笔迹从 OCR 与帧差/墨迹里排除，交付图照旧保留笔迹**，而且消费方按判据参数
           **逐帧**重算掩膜、**不按 box 整块挖**（笔迹逐帧移动累积，整块挖会伤其余页同位置的正文）。
           M4b 两道收紧：判据按**厚度**区分笔画与实心色块（红底白字条上的字不再被吃掉）、亮度下限
           收到 0.80（桌面壁纸/网页深色区/IDE 主题里的彩色图标不算笔迹）；并且**整屏应用帧
           （app_screen）整帧跳过手写判定与涂白**（那里没有手写））
           M5 起接受 `--basis full|sample`：sample 时读**取样帧与取样包索引**、落 cache/<vid>/sample/overlay.json，
           source.basis 写取样包 relpath；**整片模式的取值与产物一字不变**（不要 --basis 就与 0.12.0 相同）
measure    零 token 媒体验测：**一次解码**量出逐秒运动 + 切点/冻结段/静音段 → cache/<vid>/measure.json
           （只读媒体文件，另可选读 cache/frames/index.json 给"抽了几帧、可能漏什么"的上界；
           进度/帧数/最大间隔都写进产物的 coverage；**M3 起按遮罩算**：有 overlay.json 就给同一条
           滤镜链加 drawbox=...:t=fill 把遮挡区涂掉（不额外解码），applicability 写 masked=true +
           mask_source；没有就按全画面算并写 masked=false。**不接进 run/slides**，只在显式调用时跑；
           M5 起接受 `--basis full|sample`：sample 时量的是**取样媒体**、遮罩取取样包、落 cache/<vid>/sample/measure.json，
           且 `source.basis` **只在取样模式写**（顶层 measure.json 一行不动，M1 的确定性锚点继续有效））
sample     M5 取样包：判型与信息流画面旁证用的「少量窗口取样帧」→ cache/<vid>/sample/（**私有根**）
           （默认片头 10% / 中段 50% / 片尾 90% 各 `[sample].window_sec=30 s`。**每窗单独下载**再由 ffmpeg
           concat 拼成一个媒体文件 —— yt-dlp 的逗号多窗口在本机实测**只下第一段**（两种写法产物字节相同），
           所以不交给它；抽帧 `[sample].fps=1`、index.json 是**分段线性**的权威时间映射（禁用 section_offset）。
           自带三条自检：拼接实测时长 / 每窗首帧 t≈窗口起点 / coverage.sampled_sec 用实测值；
           覆盖口径会打印一行"只看了 X s / Y s"，**不许当成全片结论**。`--window-sec` 可临时改窗口长度。
           绝不写 cache/frames/index.json、cache/<vid>/media/ 或顶层 overlay.json / measure.json）
 → out/<vid>/_meta/sheets/*.png + _meta/sheet.json
           （每格只烧序号、**不烧时间码**；行列→帧→t 的权威映射只在 sheet.json；面板里的字**一律不采信**，
           读字请用 frames --read。--from/--to 给时间区间、--max 给格数上限，不指定就从 cache/frames/ 均匀取样；
           空白格按标准差跳过、不占序号；拼版只用已有 Pillow+numpy。**只在显式调用时跑**；
           M5 起接受 `--basis full|sample`：sample 时帧来自取样包、面板用 sample_NN.png、清单写
           **_meta/sheet_sample.json**（与整片 sheet.json **分文件**，互不覆盖；--from/--to 按**原始时间轴**，
           不再做 media.sections 换算；t 一致性与新增的 window 校验都比对**取样包索引**，不会假失败））
frames     M2 按时间取帧：--at HH:MM:SS 给该时刻最近的一帧 + 前后各一帧（3 条，含路径、t、实际尺寸与来源）；
           --read 给该时刻的**读字单帧**，默认**媒体原生分辨率**：缓存帧本身已是原生尺寸就直接复用它
           （sha256 与 cache/frames 那张逐字节相同），否则用 ffmpeg 现抽原生帧（PNG 无损、无 scale 滤镜）到
           _meta/frames_at/，并按「t + 原生尺寸」缓存复用（第二次不重新解码）；两种情形都打印实际尺寸与来源。
           cache/frames/ 被 clean 掉也照样现抽；媒体也没有就明确报错，不静默返回空
scaffold   生成章节结构（manifest + _plan）；分章是语义判断：给 --groups，或显式 --auto 接受一页一章（--no-leading-merge：封面不与目录合并）
brief      渲染派单 prompt：--stage chapter|note|fix|review；chapter 阶段同时把**派单时刻**的切片指纹记进 _meta/slideset_dispatch.json（只记本次实际派发的章）
glossary   查看/修改/确认术语表（未确认时 brief --stage chapter 会拒绝派发）
patch      打印某章的补丁文件路径与 schema（写手写这里，不直接改 manifest）
collect    把 _meta/patch/<章号>.json 汇总进 manifest（多写手并发时不丢更新）；并把派单台账里的切片指纹
           搬进**每章**的 slideset_id 与顶层汇总（没有补丁也会写盘）
retime     按 slides.json 幂等重写小节时间行（时间由工具生成，写手不写时间）
check      结构校验；--chapter 06,07 限定作用域（写手自检用）。含切片身份：顶层指纹不符 = 整集错版（error）、
           M5 起信息流模式也校图引用：不在两类白名单内 / 引用的面板名不在 sheet.json ∪ sheet_sample.json 的 tiles 里 → error；
           面板配额超 [sheet].max_stream → warn（只扫 lecture.md、按整集时长、不拦流程）。t 一致性仍只在 sheet.verify() 查；
           幻灯片模式引用**取样**面板（sample_NN.png，只在 sheet_sample.json 里）**不再报错** —— 这是**有意放宽**：并集只放宽"面板存在性"，
           白名单路径与其余 error 语义不变（真实 p20 交付物旧码/新码 check 完全一致）
           某章指纹不符 = 该章需重派写手（error）、逐页图片 sha256 不符 = 图被替换（error）、
           缺指纹的存量产物与 remap 留痕 = warn
           M4 角色（§3.5-2）：段内存在可当主图的候选（full_page / app_screen）却选了别的 = error
           （owner=pipeline，fix 指向重跑 slides）；整段没有可当主图的候选 = warn（工具判不了，不把产物
           判红）；旧产物无 role = warn
merge      合并讲义（结构校验通过才落盘）+ 刷新 note_brief
note       校验 note.md + 导出钩子
export     导出可粘贴进 B 站笔记的富文本（--format bili-note；--from lecture|note；CF_HTML + Windows 装载脚本；不调平台写接口）
xref       把多集的钩子与结构汇成跨讲综合工作表（--from-page A --to-page B；输出到 <数据根>/out/_xref/；缺集会拒绝）
remap      重切片后按时间重叠同步正文与 manifest 的图号（幂等；--from <旧 slides.json> 或 --force-map 强制重写；bundle 会自动快照上一版）。
           同时把 manifest 的指纹换成**当前版**并留痕 `slideset_remap`：它只搬页号、**不改正文文字**，
           所以 check 对留痕报一条 warn（图注需复核）；重派写手后 collect 会自动清掉该标记
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
  `corrections` 三字段、`stage_merges` 一致性、**切片身份（顶层/章级指纹 + 逐页图片 sha256）**、
  note 节点格式与 20% 预算；校验口径详见 `references/schema/body-contract.md`；
- **内容**（写进写作契约，工具不 grep）：覆盖每个知识点、图注必须自己**打开图看**过再写（读图工具：DSH 里是 `read_image`，其它 harness 用等价工具；OCR 会漏字）、
  分类落表格、小结/思考、不编造；术语白名单与排版规则由**内容画像**注入；
- **派生数据由工具生成**：时间行（`retime`）、节点索引（`note`）、钩子（`note`）、**切片指纹**（`slideset`：bundle 产出、
  brief 在派单时刻记录、collect 搬进 manifest、check 比对）—— 写手不写也不改；
- 工程性信息（听写校正、存疑、覆盖说明）进 manifest 字段；**唯一例外**：依据只能靠语境推断出来的更正（`corrections` 的 `basis=context`）与无法归位的存疑（`uncertainties`），要在**正文相关段落之后**就地标注：引用块内**每条一行**（`> **【校对】**` 后接 `> - 原文 → 更正（或存疑点）→ 依据`）——那是读者判断这段可不可信的凭据，不是工程碎片。位置在段落之后、自成一段，不插进句子中间；「校对」两字写全。
- **视频页元信息**（简介 / 标签 / 分区 / **UP 主置顶评论**）在取数时落进 `<数据根>/cache/<vid>/meta.json`，并注入派单 prompt（章节写手、笔记写手、整理稿写手都能看到，当背景）；人也从 `index.md` 看到它。
  它们**不是课程内容**：可以用来判断主题、术语写法、是否属于某个系列，也能据此找到作者给的资料链接，但不许写进讲义/笔记正文（讲师没说的不算课程讲的）。
  置顶评论与简介**同一权重**（作者常把资料链接、勘误、答疑补充在这里——改简介等于重新发布视频）；工具**只取作者置顶的那一条**，不做评论区批量采集，开关 `meta.top_comment`。
  这一条依赖平台的上游接口：**上游接口变化或风控加强时它可能取不到**，这不是使用者或本 skill 能控制的——取不到是软失败（打印一行说明、取数照常），不会影响讲义与笔记。

## 四、故障、维护与发布（按需读）

- 常见故障：见 `references/troubleshooting.md`（当命令报错、校验失败或产物异常时；先读 `_meta/validation.json` / `validation.md` / `loop.json`）
- 要改工具或发布版本：见 `references/maintenance.md`（当要改代码、契约、参数或发新版本时）
