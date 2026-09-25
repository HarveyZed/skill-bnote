# 常见故障

**什么时候读我**：命令报错、结构校验失败、或产物明显异常（字幕只覆盖片头、页数不对、图片错位）时——先读下面三个文件，再对照本表处置。

**先看哪三个文件**：`<out>/_meta/validation.json`（机器可读、每条带 owner 路由）→ `validation.md`（人读版）→ `loop.json`（修复轮次台账）。

| 现象 | 处理 |
|---|---|
| 命令报「该数据根下没有 <vid> 的取数结果」 | cwd 不对：加 `BNOTE_ROOT=<数据根>`，或看命令开头那行 `[paths]` |
| 字幕只覆盖片头（整理稿异常短） | 官方轨可能是**残轨**（实测某播客集只有 63 秒片头歌词，占片长 0.7%），工具会报「疑似残轨」并在 `transcript.json` 里标 `partial`+`coverage`；换后端（`--subtitle-backends whisper`）或确认登录态。阈值 `BN_SUBTITLE_MIN_COVERAGE`（默认 0.8）。重跑时若发现**缓存**是残轨会自动重试一次；覆盖度达标的缓存不会重复请求上游 |
| 字幕后端全部失败 | 三条路选一条并说明理由：`auth login`（扫码）／`--subtitle-backends file` + 自带 srt／`pip install -e ".[asr]"` 后 `--subtitle-backends whisper` |
| `--sections` 报 `ffmpeg is not installed` 或 ffmpeg 段错误(139) | 环境里没有可用的 ffmpeg：装系统版（Linux `apt install ffmpeg`／macOS `brew install ffmpeg`）并在 `[tools] ffmpeg` 指路，或先跑整集（整集下载/抽帧用静态包也能跑）。静态包的域名解析缺陷与实测见 `references/dsh-notes.md` |
| 页数偏多（动画被切成多页） | 调大 `BN_SEGMENT_STABLE_MIN_SEC`（1.5→2.0/2.5）后 `run --force` |
| 页数偏少（漏页） | 调小 `BN_SEGMENT_DIFF_THRESHOLD`（0.035→0.02） |
| 重切片后图片错位 | `remap <URL> --from <数据根>/cache/_prev_slides_<P>.json`（**不带 `--from` 时自动用数据根里的快照**；`--from` 是按**当前目录**解析的，写全路径才保险） |
| 抽到摄像头/弹幕/字幕条 | 字幕条默认自动识别排除；其余设 `frames.region` / `frames.crop` |
| 校验报"小节时间未展开" | 写手没跑 retime：`retime` 后重跑 `check` |
| 术语表忘了确认导致 brief 拒绝 | 按提示 `glossary --confirm`；确有把握可 `--allow-unconfirmed` |
| `stream` 报「还没有字幕」 | 登录态三条路：`auth login`（扫码）／`--subtitle-backends file` + 自带 srt／`pip install -e ".[asr]"` 后走 whisper |
| 整理稿校验报「锚点不是工具给的段落起点」 | writer 自编了时间：改回 `_meta/text_chunks/NN.md` 里给的锚点 |
| 整理稿校验报「像是做了摘要压缩」 | 整理稿字数 < 字幕正文 × `text.min_cover_ratio`：回去补全，不要概括 |
| 讲义里出现的是页内放大截图、完整页面丢了 | 终态选帧按"文字最全 + 最清晰"打分，讲师放大讲解的截图帧得分最高。**写手逐张看图**，发现是放大截图就丢掉、换回整页那帧；根治办法在路线图（帧角色分类 → 整页优先） |
| 同一页被切成好几页 | 页面上的手写圈画会抬高帧差、也会被 OCR 读成字符。术语表评审时把噪声词丢掉；圈画多的课建议人工过一遍图（路线图在做笔迹与印刷体判别） |
| 讲义里多出没有信息量的图（讲师出镜、插播片段） | "画面长时间不变 = 一页"对静止人像同样成立。写手看图时删掉；纯人像或纯动画的视频改用 `stream`（信息流模式） |
| 整理稿里留着听错的术语 | 本地识别会把中文人名、缩写、产品名听错，且本项目不做自动纠正。在 `subtitle.whisper_prompt` 预填术语提示；写作阶段用页面文字校正；自动纠正见路线图 |
| 换了幻灯片版式后，同页合并的判断不对 | 相似度阈值是按常见版式校准的。先跑一遍看相似度分布，再调 `segment.merge_gram_contain`（见 `references/params.md`），不要直接沿用默认值 |
| 校验报告里"有若干张幻灯片没有被正文引用"看着像错误 | 这条其实是提醒，只有严重程度字段被标成了错误级（已知瑕疵）。看条目内容或人读版 `validation.md`；它不影响把修复发回给谁 |
| 两套字幕列表接口都取不到字幕 | 工具会轮流试两个列表接口，但没有实现平台要求的签名计算——平台加强校验时可能两套都失败。此时走三条路：扫码登录 / 自带 srt / 本地语音识别 |
| 取数时打印一行「置顶评论取不到」 | 评论接口被风控挡回（最常见是 HTTP 412）：工具已在同一次会话里先请求一次视频页拿 `buvid3`，仍失败说明平台侧风控加强或网络异常。这不影响取数，只是少了这段背景；持续失败就把 `meta.top_comment` 设为 `false`（少两次 HTTP 请求） |
| `index.md` 里置顶评论写着「没有置顶评论，或本次未能取到」 | 先看取数日志有没有那一行提示：没有提示就是作者确实没置顶（正常）；有提示按上一条处理。若是**旧缓存**（当时还没存 `aid`），重跑一次取数即可补上——`bnote meta <URL> --page N` |
| 不想要评论相关的抓取 | `meta.top_comment = false`。工具只取作者置顶的那一条，本就不做评论区批量采集 |
