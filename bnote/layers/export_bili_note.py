"""L20 导出层：把讲义/笔记导出成「可直接粘贴进 B 站笔记」的富文本（CF_HTML）。

做什么
  1. 读 out/<vid>/lecture.md 的小节（或 note.md 的节点）→ 每节的「标题 + 起始秒数」；
  2. 配上 cache/<vid>/meta.json 的 cid / 视频标题 / 分P号；
  3. 在 out/<vid>/_meta/ 下写三份文件：
       bili_note_<source>.html  CF_HTML（Windows 剪贴板 "HTML Format" 的格式）：
                                一个片段 = 每节「一段普通文字（小节标题）+ 一个时间锚点节点」
       bili_note_<source>.md    同样内容的 Markdown 预览（给人看，不含 CF_HTML 头）
       bili_note_clip.ps1       Windows 装载脚本：把 html 的**原始字节**写进剪贴板，人去浏览器 Ctrl+V

为什么这样设计
  * B 站笔记是 Quill 编辑器，「可跳转的时间标签」是一个 div.ql-tag-blot：视频、时间、分P 全在
    data-* 属性上，内层的 span/div 只是视觉层。纯文本时间戳（「[00:01] 标题」）与方括号写法都
    **不被编辑器识别**（实测见 CHANGELOG 0.9.0），所以这里导出的是**节点本身**，不是一段巧妙的文本；
  * 因此也**不调用平台的写接口**（写接口文档缺失、风险高于读接口）：本层只做格式兼容 ——
    把 CF_HTML 放进系统剪贴板，粘贴动作由人在编辑器里完成；
  * CF_HTML 头里的偏移是 **UTF-8 字节偏移**（不是字符数），10 位零填充；算错一个字节，
    浏览器就会从半个汉字中间开始解析 → 乱码。所以这里一律 write_bytes，不经过文本模式的换行转换；
  * data-desc 用小节标题，但平台上限 **14 个字符**（实测）：给多了节点会退化成不可点，
    所以宁可截断（配置 export.desc_max_chars，超出以「…」结尾）也不冒险；
  * data-key 用当前毫秒时间戳、data-cid-count 写 1：这两个字段平台语义未公开，
    按「实测能用的最小值」写，不臆造含义。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from pathlib import Path

NL = "\n"
TICK = chr(96)          # 反引号：预览 Markdown 里用来包时间标签（写成 chr(96) 免得嵌套引号难读）

# 讲义小节的时间行（由 bnote retime / merge 生成）：*HH:MM:SS–HH:MM:SS ｜ slide NNNN*
SEC_TIME_RE = re.compile(r"^\*(\d{1,2}:\d{2}(?::\d{2})?)\s*[–\-—~]\s*\d{1,2}:\d{2}(?::\d{2})?\s*[|｜]\s*slide\b")
# 标题自带时间的形式：## [HH:MM:SS] 标题（note.md 的节点；信息流模式的 lecture.md 也是这个形状）
HEAD_TS_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.+?)\s*$")
HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
# merge 给章标题加的顺序号（"## 1. 开场与课程地图"）是排版产物，不进锚点文字
CHAPTER_NO_RE = re.compile(r"^\d+[.、]\s*")

# ---------------------------------------------------------------- 节点模板
# 下面这段就是实测通过的锚点节点（2026-09-20 实测四条：纯 ASCII 标题 / 中文标题 / 改秒数 /
# 带 desc 全部粘成可点标签并跳对时间）。**属性名一个都不能改**：平台靠 data-* 跳转，
# 内层只是视觉层。内层 span 前后各有一个 U+FEFF，实测保留即可（Quill 的占位习惯）。
ANCHOR_TMPL = (
    '<div class="ql-tag-blot" data-oid_type="0" data-cid="{cid}" data-epid="0" data-status="0"'
    ' data-index="{index}" data-seconds="{seconds}" data-cid-count="{cid_count}" data-key="{key}"'
    ' data-title="{title}" data-desc="{desc}"'
    ' style="margin: 0px; padding: 10px 0px; display: inline-block; position: relative;">\ufeff'
    '<span contenteditable="false"><div class="time-tag-item" contenteditable="false"'
    ' style="margin: 0px; padding: 3.5px 10px 3.5px 5px; box-sizing: border-box; outline: none;'
    ' cursor: pointer; background: none 0% 0% / auto repeat scroll padding-box border-box rgb(241, 242, 243);'
    ' border-radius: 4px; height: auto; line-height: 20px; font-size: 14px; font-weight: 700;'
    ' display: inline-flex; align-items: center; gap: 4px;">'
    '<i class="bili-note-iconfont iconicon_flag_s"'
    ' style="font-size: 16px; font-style: normal; font-family: bili-note-iconfont !important;'
    ' color: rgb(24, 25, 28); line-height: 1;"></i>'
    '<span class="time-tag-item__text" title="{label}"'
    ' style="font-size: 14px; line-height: 20px; color: rgb(24, 25, 28); font-weight: 400;'
    ' display: flex; align-items: center;">{label}'
    '<desc class="time-tag-item__desc" title="{desc}"'
    ' style="margin-left: 8px; display: inline-block; font-size: 14px; line-height: 20px;'
    ' color: rgb(97, 102, 109);">{desc}</desc></span></div></span>\ufeff</div>'
)

# data-cid-count：平台语义未公开（可能指"该锚点关联的 cid 数"，也可能不是）。
# 实测写 1 与写 2 都能粘成可点标签，这里固定写 1 —— 照实测最小可用值写，不臆造含义。
CID_COUNT = 1

CF_HEAD_TMPL = ("Version:1.0\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\n"
                "StartFragment:{:010d}\r\nEndFragment:{:010d}\r\nSourceURL:{}\r\n")
CF_PREFIX = "<html>\r\n<body>\r\n"
CF_SUFFIX = "\r\n</body>\r\n</html>\r\n"
START_MARK = "<!--StartFragment-->"
END_MARK = "<!--EndFragment-->"


# ---------------------------------------------------------------- 小工具
def _sec(text: str) -> int:
    """'01:02:03' / '02:03' -> 秒"""
    parts = [int(x) for x in str(text).split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _mmss(sec: int) -> str:
    """锚点显示用的时间：不足 1 小时写 MM:SS，超过才写 H:MM:SS（一小时以上的课别显示成 60:00）"""
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%02d:%02d" % (m, s))


def _clip(text: str, limit: int) -> str:
    """按**字符**截断（平台按字符计数），超出以「…」结尾；limit<=0 表示不截断"""
    text = text or ""
    limit = int(limit)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:max(1, limit - 1)] + "…"


def _esc(text) -> str:
    """属性与正文共用：& < > " 转义（标题来自视频元信息，可能带引号）"""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def canonical_url(meta: dict) -> str:
    """SourceURL 只用**规范视频 URL**：丢掉 vd_source / spm_id_from 之类追踪参数，只留分P。

    meta.json 的 url 通常已经规范，但旧缓存或手工改过的可能带追踪参数，所以再去一次。
    """
    raw = str(meta.get("url") or "").strip()
    bvid = str(meta.get("bvid") or "")
    page = int(meta.get("page") or 1)
    if not raw:
        return "https://www.bilibili.com/video/%s?p=%d" % (bvid, page)
    parts = urllib.parse.urlsplit(raw)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k == "p"]
    path = parts.path.rstrip("/") or "/"
    if bvid and bvid not in path:
        path = "/video/%s" % bvid
    return urllib.parse.urlunsplit((parts.scheme or "https", parts.netloc or "www.bilibili.com",
                                    path, urllib.parse.urlencode(query), ""))


# ---------------------------------------------------------------- 解析来源
def parse_lecture(text: str) -> list[dict]:
    """讲义的小节：每节「标题 + 起始秒数」。支持本项目产出的两种讲义形状：

      1) 幻灯片模式 lecture.md：标题行 + **紧随其后的一行** *HH:MM:SS–HH:MM:SS ｜ slide NNNN*；
         文档里同时有 ## 章与 ### 小节时**逐章下沉**：这一章有小节就用小节，没有就用章本身
         —— 这样时间轴仍连续覆盖全片，不会因为某章缺小节而整章丢失；
      2) 信息流模式 lecture.md：## [HH:MM:SS] 标题（与 note.md 节点同形）。

    目录 / 章节一览这类索引小节，其后一行不是时间行，自然被过滤掉。
    """
    lines = text.splitlines()
    timed, inline = [], []
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if not m:
            continue
        level, title = len(m.group(1)), m.group(2).strip()
        tm = HEAD_TS_RE.match(title)
        if tm:
            inline.append({"title": tm.group(2).strip(), "seconds": _sec(tm.group(1)), "line": i + 1})
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            continue
        sm = SEC_TIME_RE.match(lines[j].strip())
        if sm:
            timed.append({"level": level, "title": title, "seconds": _sec(sm.group(1)),
                          "line": i + 1, "at": i})

    if not timed:
        return inline

    top = min(e["level"] for e in timed)
    chapters = [e for e in timed if e["level"] == top]
    out = []
    for k, ch in enumerate(chapters):
        end = chapters[k + 1]["at"] if k + 1 < len(chapters) else len(lines)
        subs = [e for e in timed if e["level"] > top and ch["at"] < e["at"] < end]
        for e in (subs or [ch]):
            title = CHAPTER_NO_RE.sub("", e["title"]) if e["level"] == top else e["title"]
            out.append({"title": title.strip(), "seconds": e["seconds"], "line": e["line"]})
    return out


def parse_note(text: str) -> list[dict]:
    """note.md 的节点：## [MM:SS] 标题（「节点索引」里是列表项、不是标题行，不会被误收）"""
    out = []
    for i, line in enumerate(text.splitlines()):
        m = HEADING_RE.match(line)
        if not m:
            continue
        tm = HEAD_TS_RE.match(m.group(2).strip())
        if tm:
            out.append({"title": tm.group(2).strip(), "seconds": _sec(tm.group(1)), "line": i + 1})
    return out


def load_source(paths, source: str) -> tuple[Path, list[dict]]:
    src_name = "lecture.md" if source == "lecture" else "note.md"
    src = paths.out / src_name
    if not src.exists():
        raise SystemExit(
            "找不到来源文件：%s\n"
            "  → 讲义由 bnote merge 产出、笔记由 bnote note 产出；先跑完那一步再导出。" % src)
    text = src.read_text(encoding="utf-8")
    items = parse_lecture(text) if source == "lecture" else parse_note(text)
    if not items:
        raise SystemExit(
            "%s 里没解析出小节/节点。\n"
            "  讲义小节要形如「## 标题 + 下一行 *HH:MM:SS–HH:MM:SS ｜ slide NNNN*」，\n"
            "  笔记节点要形如「## [MM:SS] 标题」；格式不对就先跑 bnote retime / bnote note。" % src)
    return src, items


# ---------------------------------------------------------------- 生成
def anchor_html(*, cid, page: int, seconds: int, data_title: str, label: str,
                desc: str, key_ms: int) -> str:
    """一个锚点节点（结构与实测通过的样本逐字一致，见文件头注释）"""
    return ANCHOR_TMPL.format(cid=_esc(cid), index=int(page), seconds=int(seconds),
                              cid_count=CID_COUNT, key=int(key_ms), title=_esc(data_title),
                              label=_esc(label), desc=_esc(desc))


def build_rows(meta: dict, items: list[dict], ecfg: dict, key_ms=None) -> list[dict]:
    """小节/节点 + 元信息 → 每个锚点一行数据（标题、desc、可见文字、data-title、data-seconds）"""
    key_ms = int(time.time() * 1000) if key_ms is None else int(key_ms)
    page = int(meta.get("page") or 1)
    field = str(ecfg.get("title_field") or "title").lower()
    data_title = str((meta.get("part") if field == "part" else meta.get("title")) or "").strip()
    desc_max = int(ecfg.get("desc_max_chars", 14))
    title_max = int(ecfg.get("title_max_chars", 30))
    rows = []
    for it in items:
        sec = int(it["seconds"])
        desc = _clip(it["title"], desc_max)
        label = "%s P%d - %s" % (_clip(data_title, title_max), page, _mmss(sec))
        rows.append({
            "title": it["title"], "seconds": sec, "desc": desc, "label": label,
            "data_title": data_title, "data_seconds": sec, "page": page,
            "cid": meta.get("cid"), "key": key_ms,
            "html": anchor_html(cid=meta.get("cid"), page=page, seconds=sec,
                                data_title=data_title, label=label, desc=desc, key_ms=key_ms),
        })
    return rows


def build_fragment(rows: list[dict]) -> str:
    """片段：每节一段普通文字（小节标题）+ 一个锚点节点。

    节点之间**不加换行/空格** —— 片段里的空白会被 Quill 当成额外内容；
    全挤在一行不影响粘贴（HTML 里块级元素之间的空白本来就忽略）。
    """
    return "".join("<p>%s</p>%s" % (_esc(r["title"]), r["html"]) for r in rows)


def cf_html(fragment: str, source_url: str) -> str:
    """包成 CF_HTML：头里的偏移一律是 **UTF-8 字节偏移**，10 位零填充。

    偏移固定 10 位 => 头部长度已知，可以先算长度、再回填真实偏移。
    """
    head_len = len(CF_HEAD_TMPL.format(0, 0, 0, 0, source_url).encode("utf-8"))
    start_html = head_len
    start_frag = start_html + len(CF_PREFIX) + len(START_MARK)
    end_frag = start_frag + len(fragment.encode("utf-8"))
    end_html = end_frag + len(END_MARK) + len(CF_SUFFIX)
    for name, value in (("StartHTML", start_html), ("EndHTML", end_html),
                        ("StartFragment", start_frag), ("EndFragment", end_frag)):
        if value > 9999999999:
            raise SystemExit("CF_HTML 偏移超出 10 位：%s=%d（片段太大？）" % (name, value))
    head = CF_HEAD_TMPL.format(start_html, end_html, start_frag, end_frag, source_url)
    return head + CF_PREFIX + START_MARK + fragment + END_MARK + CF_SUFFIX


PS1_TMPL = """<#
  bnote · 把生成的 CF_HTML 装进 Windows 剪贴板（装载后到 B 站笔记编辑器里 Ctrl+V）

  用法（**Windows PowerShell**，不是 WSL）：
    powershell -ExecutionPolicy Bypass -File .\\bili_note_clip.ps1
    powershell -ExecutionPolicy Bypass -File .\\bili_note_clip.ps1 -Path D:\\x\\bili_note_lecture.html
  默认读与本脚本同目录的 {default_name}。
  注意：html 要跟本脚本放在一起（把整个 _meta/ 目录拷到 Windows 上最省事）。
#>
param([string]$Path = (Join-Path $PSScriptRoot '{default_name}'))

if (-not (Test-Path -LiteralPath $Path)) {{
  Write-Host ('找不到 CF_HTML：' + $Path)
  Write-Host '用法： powershell -ExecutionPolicy Bypass -File .\\bili_note_clip.ps1 [-Path <bili_note_*.html>]'
  exit 1
}}

Add-Type -AssemblyName System.Windows.Forms
# 关键：以**原始字节流**写 HTML Format。若直接写字符串，.NET 会按 UTF-16 存，
# 而 CF_HTML 头里的偏移是按 UTF-8 字节算的 —— 两者错位就会乱码（实测踩过这个坑）。
$bytes = [System.IO.File]::ReadAllBytes($Path)
$ms = New-Object System.IO.MemoryStream(,$bytes)
$data = New-Object System.Windows.Forms.DataObject
$data.SetData('HTML Format', $ms)
$text = [System.Text.Encoding]::UTF8.GetString($bytes)
$seg = ($text -split '<!--StartFragment-->')[1]
$seg = ($seg -split '<!--EndFragment-->')[0]
$plain = ($seg -replace '<[^>]+>','').Trim()
$data.SetData([System.Windows.Forms.DataFormats]::UnicodeText, $plain)
[System.Windows.Forms.Clipboard]::SetDataObject($data, $true)
Write-Host ('已写入剪贴板：HTML Format(原始字节) + 纯文本 -> ' + $plain)
Write-Host '现在到 B 站笔记编辑器里 Ctrl+V（一次粘贴全部小节）。'
"""


def preview_md(meta: dict, rows: list[dict], source: str, src_name: str, url: str) -> str:
    """给人看的预览：与 html 一一对应，但不含 CF_HTML 头（直接粘 Markdown 进笔记没用）"""
    lines = [
        "# B 站笔记粘贴预览 · %s" % meta.get("bvid"),
        "",
        "- 视频：%s" % (meta.get("title") or ""),
        "- 分P：P%s ｜ cid %s ｜ 锚点 %d 个" % (meta.get("page"), meta.get("cid"), len(rows)),
        "- 来源：out/%s/%s" % (meta.get("vid") or "", src_name),
        "- 规范 URL：%s" % url,
        "",
        "粘贴后每一节长这样：一行普通文字（小节标题），下面跟一个灰色圆角的时间标签（点击跳到那一刻）。",
        "",
        "本文件只是预览 —— **别**把它粘进笔记；要粘的是 bili_note_%s.html，"
        "用 bili_note_clip.ps1 把它装进剪贴板再粘贴。" % source,
        "",
        "---",
        "",
    ]
    for i, r in enumerate(rows, start=1):
        lines.append("## %d. %s" % (i, r["title"]))
        lines.append("")
        lines.append("%s%s%s ｜ data-seconds=%d ｜ desc=%s"
                     % (TICK, r["label"], TICK, r["data_seconds"], r["desc"]))
        lines.append("")
    return NL.join(lines)


def build(cfg: dict, paths, source: str = "lecture", out_dir=None) -> dict:
    """生成三份文件并打印路径与用法。source: lecture | note"""
    meta = json.loads(paths.meta.read_text(encoding="utf-8"))
    cid = meta.get("cid")
    if not cid:
        raise SystemExit(
            "meta.json 里没有 cid，无法生成 B 站笔记的时间锚点（锚点必须带 cid 才会跳转）。\n"
            "  meta.json: %s\n"
            "  cid 来自取数阶段写下的 meta.json —— 老缓存（v0.8 以前）没有这个字段。\n"
            "  → 补一次元信息取数即可（不重新下载媒体/字幕）：\n"
            "     bnote meta <URL> --page %s\n"
            "     （要顺带补媒体与字幕才用 bnote fetch <URL> --page %s）"
            % (paths.meta, meta.get("page", 1), meta.get("page", 1)))

    src, items = load_source(paths, source)
    ecfg = cfg.get("export") or {}
    rows = build_rows(meta, items, ecfg)
    fragment = build_fragment(rows)
    url = canonical_url(meta)

    dest = Path(out_dir) if out_dir else paths.meta_dir()
    dest.mkdir(parents=True, exist_ok=True)
    html_path = dest / ("bili_note_%s.html" % source)
    md_path = dest / ("bili_note_%s.md" % source)
    ps1_path = dest / "bili_note_clip.ps1"

    # 一律字节写入：CF_HTML 的偏移按字节算，绝不能让文本模式的换行转换动它
    html_path.write_bytes(cf_html(fragment, url).encode("utf-8"))
    md_path.write_bytes(preview_md(meta, rows, source, src.name, url).encode("utf-8"))
    ps1_path.write_bytes(PS1_TMPL.format(default_name=html_path.name)
                         .replace(NL, "\r\n").encode("utf-8"))

    print("[export] 来源 out/%s/%s ｜ 锚点 %d 个 ｜ desc 上限 %d 字符 ｜ 显示文本标题上限 %d 字符"
          % (paths.vid, src.name, len(rows),
             int(ecfg.get("desc_max_chars", 14)), int(ecfg.get("title_max_chars", 30))))
    print("[export] CF_HTML  : %s" % html_path)
    print("[export] Markdown : %s" % md_path)
    print("[export] 装载脚本 : %s" % ps1_path)
    print("在 Windows 上跑 powershell -ExecutionPolicy Bypass -File %s，然后到 B 站笔记 Ctrl+V" % ps1_path)
    print("[export] 只有 Windows 装载脚本；其它平台请自行把这个 html 的**原始字节**写进剪贴板的 "
          "\"HTML Format\"（偏移是 UTF-8 字节，别用 UTF-16、也别经过文本换行转换）")
    return {"html": html_path, "md": md_path, "ps1": ps1_path, "rows": rows, "url": url}
