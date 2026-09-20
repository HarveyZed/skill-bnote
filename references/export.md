# 导出：把讲义贴进 B 站笔记（`bnote export`）

**什么时候读我**：要把讲义导入 B 站笔记时——用户说「想在笔记里点一下就跳到视频那一刻」「把讲义贴进 B 站笔记」「导出一份能粘的带时间轴内容」。

## 一句话

`bnote export` 把 `lecture.md` 的小节（或 `note.md` 的节点）转成 **B 站笔记编辑器认得的富文本**：
每一节先一行普通文字（小节标题），下面跟一个灰色圆角的时间标签（点一下跳到那一刻）。
**不调用平台的写接口**：只产出剪贴板格式与一个装载脚本，粘贴动作由人在编辑器里完成。

```bash
scripts/bnote export <URL> --page N --format bili-note              # 默认用讲义小节
scripts/bnote export <URL> --page N --format bili-note --from note  # 改用笔记节点
scripts/bnote export <URL> --page N --out /tmp/x                    # 换个输出目录（默认 <数据根>/out/<vid>/_meta）
```

产出（默认落在 `<数据根>/out/<vid>/_meta/`）：

| 文件 | 是什么 |
|---|---|
| `bili_note_lecture.html` | CF_HTML（Windows 剪贴板 `HTML Format`）——**要粘的是它**；`--from note` 时叫 `bili_note_note.html` |
| `bili_note_lecture.md` | 同一份内容的 Markdown 预览（给人核对，不含 CF_HTML 头，**别拿它去粘**） |
| `bili_note_clip.ps1` | Windows 装载脚本：把 html 的**原始字节**写进剪贴板，同时附纯文本 |

## 怎么粘（Windows）

```powershell
powershell -ExecutionPolicy Bypass -File <_meta 路径>\bili_note_clip.ps1
```

跑完到 B 站笔记编辑器里 Ctrl+V（一次粘完全部小节）。

**只有 Windows 装载脚本**。其它平台要自己把 html 的**原始字节**写进剪贴板的 `HTML Format`：
CF_HTML 头里的偏移是 **UTF-8 字节偏移**，用 UTF-16 编码或经过文本模式的换行转换都会错位（错位后就是乱码）。

## 节点结构与字段来源（为什么不能自己造一段文本）

B 站笔记是 Quill 编辑器，「可跳转的时间标签」是一个 `div.ql-tag-blot`，信息全在属性上：

| 属性 | 我们写什么 |
|---|---|
| `data-cid` | `cache/<vid>/meta.json` 的 `cid` |
| `data-index` | 分P号（meta.json 的 `page`） |
| `data-seconds` | 小节/节点的起始秒数（讲义时间行或 `[MM:SS]` 里的时间） |
| `data-title` | meta.json 的 `title`（`export.title_field = "part"` 时改用分P标题） |
| `data-desc` | 小节标题，**≤ 14 个字符**（超出截断并以「…」结尾） |
| `data-key` | 当前毫秒时间戳 |
| `data-oid_type` / `data-epid` / `data-status` | 固定 `"0"` |
| `data-cid-count` | 固定 `1`（平台语义未公开，照实测最小可用值写） |

内层是视觉层（`span > div.time-tag-item > 小旗图标 + 文本 + 可选 desc`），内层 span 前后各有一个 U+FEFF。

**为什么不用纯文本时间戳**：实测把「`[02:20] 标题`」「`标题 P1 - 02:20`」这类文本粘进笔记，
编辑器只当普通文字，不会变成可点标签（方括号时间戳也不被识别）。这就是放弃「回发笔记」路线、
改成本命令的原因——只能整节点粘。详见 CHANGELOG 0.9.0。

## 参数（可用 `BN_EXPORT_<键>` 覆盖）

| 参数 | 默认 | 说明 |
|---|---|---|
| `export.desc_max_chars` | 14 | `data-desc` 的字数上限（0 = 不截断）。**平台实测上限就是 14**，调大没有意义 |
| `export.title_max_chars` | 30 | 显示文本里标题部分的字数上限（0 = 不截断） |
| `export.title_field` | `title` | 改 `part` 用分P标题。多分P课程建议改：合集标题常是「全 N 集」这种，分P标题才说明这一讲讲什么 |

例：`BN_EXPORT_TITLE_FIELD=part scripts/bnote export <URL> --page 20`

## 来源文件怎么被解析

- `--from lecture`（默认）：标题行 + **紧随其后的一行** `*HH:MM:SS–HH:MM:SS ｜ slide NNNN*`（`bnote retime` 生成的格式），取范围起点；
  文档里同时有 `##` 章与 `###` 小节时**逐章下沉**——这一章有小节就用小节，没有就用章本身，保证时间轴连续覆盖；
  信息流模式的 `lecture.md`（`## [HH:MM:SS] 标题`）同样支持；目录 / 章节一览这类索引小节其后不是时间行，自然被跳过。
- `--from note`：`note.md` 的节点 `## [MM:SS] 标题`（「节点索引」里是列表项，不会被误收）。

## 常见问题

| 现象 | 处理 |
|---|---|
| 「meta.json 里没有 cid」 | 老缓存（v0.8 以前）没有这个字段：`bnote meta <URL> --page N` 补一次元信息（不重下媒体/字幕），再导出 |
| 「没解析出小节/节点」 | 讲义需要时间行（跑 `bnote retime` 生成）；笔记节点需要 `## [MM:SS] 标题` 格式 |
| 粘贴后是普通文字、不是灰色标签 | 走成了纯文本通道：确认装载脚本写的是 `HTML Format`（原始字节），不是只写 UnicodeText |
| 是灰色标签但点了不跳 | 核对 `data-cid` 与 `data-index` 是否与目标视频/分P一致（cid 来自取数时的 meta.json，换过 P 就会对不上） |

## 还没验证的地方

- 本命令**没有在真实 Windows + B 站编辑器里端到端验证过**（开发环境是 Linux）：节点结构与 CF_HTML
  的字节偏移来自已实测的样本（夹具见 `scripts/dev/bili_note_fixture.py`），本命令只是把那份结构参数化、
  按小节批量产出。「一口气粘 20 个标签会不会被编辑器截断」还没试过。
- `data-cid-count` 的语义未公开，写 `1` 是照实测最小可用值。
