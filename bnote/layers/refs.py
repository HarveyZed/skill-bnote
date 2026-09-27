"""图引用白名单（M2 / §3.3）：**唯一**的正则与解析入口。

正文里允许出现的图片引用只有两类：

  * `slides`（主图）—— `../slides/NNNN.jpg`，页号固定 4 位；`slides/` 的页号空间是冻结的，
    读字面板**绝不**进这个目录（否则页号空间会被污染）；
  * `sheet`（读字面板）—— `../_meta/sheets/<name>.png`，名字必须能在 `_meta/sheet.json`
    的 tile 里查到（校验口径见 references/schema/body-contract.md）。

为什么集中在一处：M2 之前四处各写一份正则，`manifest.py` 收 `\\d{4}`、`body.py` 收 `\\d{3,4}`，
同一份正文在两处会得出不同结论（3 位页号：manifest 判「没引用图」、body 判「引用了不存在的图」）。
白名单必须**一处定义、四处消费**：manifest（校验）/ body（小节解析）/ merge（相对路径重写）/
remap（重编号）。`note.py` 与 `export_bili_note.py` 只解析时间行、不解析图片路径，**不进白名单**。

两类之外的图片引用**一律不匹配**：不会被当成 slide 校验，也不会被 merge/remap 改写。这不是漏写
正则，而是白名单的语义——不认识的引用不静默改写，交由 `manifest.validate` 显式报出来。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, NamedTuple

# 两类引用的目录（相对 out/<vid>/）
SLIDES_DIR = "slides"
SHEET_DIR = "_meta/sheets"
REF_KINDS = ("slides", "sheet")
# 章节正文在 out/<vid>/chapters/ 下，故正文里的相对引用多一层
BODY_PREFIX = "../"

_PAGE_RE = re.compile(r"^\d{4}$")
_SHEET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.png$")

# 唯一的图引用正则：两类引用 + 可选的 "../" 前缀；alt 文本与前缀都原样保留，改写时不改形态。
IMG_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<target>(?P<prefix>\.\./)?"
    r"(?:(?P<slides>slides)/(?P<page>\d{4})\.jpg"
    r"|(?P<sheets>_meta/sheets)/(?P<sheet>[A-Za-z0-9][A-Za-z0-9._-]*\.png)))\)")
# 任意 Markdown 图片：白名单之外的引用也要能被指出来（见 unknown_targets）
ANY_IMG_RE = re.compile(r"!\[[^\]]*\]\((?P<target>[^)\s]+)")
# 白名单内的目标形态（供 unknown_targets 判定，不用于解析）
TARGET_RE = re.compile(
    r"^(?:\.\./)?(?:(?:%s)/\d{4}\.jpg|(?:%s)/[A-Za-z0-9][A-Za-z0-9._-]*\.png)$"
    % (SLIDES_DIR, re.escape(SHEET_DIR)))


class Ref(NamedTuple):
    """一条白名单内的图引用。"""

    kind: str          # "slides" | "sheet"
    name: str          # "0012.jpg" | "sheet_01.png"（规范名，含扩展名）
    alt: str           # ![] 里的替代文本
    prefix: str        # "../" 或 ""（原样保留，供改写时不改变形态）
    raw: str           # 整个 ![alt](target)
    start: int         # 整个匹配在原文中的起止
    end: int
    target_start: int  # 目标路径（含前缀）在原文中的起止
    target_end: int

    @property
    def page(self) -> int | None:
        """slides 的页号（int）；sheet 没有页号。"""
        return int(self.name[:-len(".jpg")]) if self.kind == "slides" else None


def _make(m: re.Match) -> Ref:
    kind = "slides" if m.group("slides") else "sheet"
    name = ("%s.jpg" % m.group("page")) if kind == "slides" else m.group("sheet")
    ts, te = m.span("target")
    return Ref(kind=kind, name=name, alt=m.group("alt"), prefix=m.group("prefix") or "",
               raw=m.group(0), start=m.start(), end=m.end(),
               target_start=ts, target_end=te)


def iter_refs(text: str, kind: str | None = None) -> list[Ref]:
    """按出现顺序列出白名单内的图引用；kind 只取 "slides" / "sheet"（None = 两类都要）。"""
    if kind is not None and kind not in REF_KINDS:
        raise ValueError("未知引用类别：%r（白名单只有 %s）" % (kind, "/".join(REF_KINDS)))
    return [r for r in (_make(m) for m in IMG_RE.finditer(text or ""))
            if kind is None or r.kind == kind]


def search(text: str, kind: str | None = None) -> Ref | None:
    """"这一行里第一条白名单图引用"，没有就 None（逐行扫描的调用方用）。"""
    hits = iter_refs(text, kind)
    return hits[0] if hits else None


def sub(text: str, fn: Callable[[Ref], str | None]) -> str:
    """改写**整个** `![alt](target)`：fn 返回 None 表示保留原样。"""
    out, pos = [], 0
    for m in IMG_RE.finditer(text or ""):
        rep = fn(_make(m))
        if rep is None:
            continue
        out.append(text[pos:m.start()])
        out.append(rep)
        pos = m.end()
    out.append((text or "")[pos:])
    return "".join(out)


def sub_path(text: str, fn: Callable[[Ref], str | None]) -> str:
    """只改写**目标路径**（`![alt](…)` 外壳不动）：fn 返回 None 表示保留原样。"""
    out, pos = [], 0
    for m in IMG_RE.finditer(text or ""):
        r = _make(m)
        rep = fn(r)
        if rep is None:
            continue
        out.append(text[pos:r.target_start])
        out.append(rep)
        pos = r.target_end
    out.append((text or "")[pos:])
    return "".join(out)


def unknown_targets(text: str) -> list[str]:
    """白名单之外的 Markdown 图片目标（去重、保持出现顺序）。

    存在的意义：3 位页号 `../slides/12.jpg` 这类引用以前在四处消费者里"有的认有的不认"，
    合并进讲义后又原样留着（读出来是坏图）。白名单要能把它**说出来**，而不是静默放行。
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in ANY_IMG_RE.finditer(text or ""):
        t = m.group("target")
        if TARGET_RE.match(t) or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def norm_name(kind: str, name) -> str:
    """把 name 规范化成"带扩展名的文件名"；slides 只接受 4 位页号（int 会补零）。"""
    if isinstance(name, int) and kind == "slides":
        name = "%04d" % name
    s = str(name).strip().lstrip("/")
    if "/" in s:
        s = s.rsplit("/", 1)[1]
    if kind == "slides":
        s = s[:-4] if s.endswith(".jpg") else s
        if not _PAGE_RE.match(s):
            raise ValueError("slides 引用必须是 4 位页号（如 0012）：%r" % (name,))
        return s + ".jpg"
    if kind == "sheet":
        s = s if s.endswith(".png") else s + ".png"
        if not _SHEET_NAME_RE.match(s):
            raise ValueError("sheet 引用必须是 _meta/sheets/ 下的 png 文件名（如 sheet_01.png）：%r"
                             % (name,))
        return s
    raise ValueError("未知引用类别：%r（白名单只有 %s）" % (kind, "/".join(REF_KINDS)))


def ref_relpath(kind: str, name) -> str:
    """相对 **out/<vid>/** 的引用路径（讲义与校验用）：`slides/0012.jpg` / `_meta/sheets/x.png`。"""
    d = SLIDES_DIR if kind == "slides" else SHEET_DIR
    return "%s/%s" % (d, norm_name(kind, name))


def ref_body_path(kind: str, name) -> str:
    """章节正文里的写法（正文在 chapters/ 下，故多一层 `../`）。"""
    return BODY_PREFIX + ref_relpath(kind, name)


def ref_out_path(out_dir, kind: str, name) -> Path:
    """落在交付物根下的真实路径（校验"图在不在"用）；**不建目录**。"""
    return Path(out_dir) / ref_relpath(kind, name)
