"""切片身份指纹（slideset identity）：证明「这份讲义写的是哪一版切片」。

问题：正文用 ../slides/NNNN.jpg 按**页序号**引用图。重切片（换策略 / 调阈值 / 改候选帧算法）后
页号可能一个都不变、而**同一页号下的图换了** —— 讲义引用的图与写手当时看到的图不是同一张，
而 `bnote check` 过去只看页数与章界，完全静默。

三段式（谁在什么时刻知道什么，就由谁写）：
  * `bundle` 在这一版产物上盖指纹：`slides.json` 顶层 `slideset{algo,id,count}`，每页补
    `frame` / `chosen_t` / `sha256`；
  * `brief --stage chapter` 在**派单时刻**把当前指纹追加进 `out/<vid>/_meta/slideset_dispatch.json`
    （只有那一刻知道「写手看到的是哪一版图」；`scaffold` 不盖章——它能独立重跑，盖章会把真实漂移洗掉）；
  * `collect` 只负责搬运：把指纹写进**每一章**的 `slideset_id`，另写一份顶层汇总；
  * `check` 比对（顶层=整集错版 / 章级=该章需重派写手）+ 逐页 sha256 自一致；`remap` 重算并留痕，
    `check` 见到留痕降级为 warn。

两条硬约束：
  1. 摘要**只依赖 out/**：`cache/` 可被 `clean` 整体删除，所以 frame/chosen_t 必须落进
     `slides.json`，不能只留在 `cache/<vid>/segments.json` 里；
  2. 只用标准库（`hashlib` + `json`），跨机器可复算，不依赖 locale 与第三方。

本模块只放纯函数与 sidecar 读写（不读 config、不做结构校验）。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ..tools import sha256_file     # 实现已挪到 tools（跨层中性）；这里保留这个名字给既有调用方用

ALGO = "bnote-slideset/1"                       # 算法版本：同时进摘要前缀与 slideset.algo
DISPATCH_SCHEMA = "bnote-slideset-dispatch/1"
DISPATCH_NAME = "slideset_dispatch.json"


def r3(x) -> float:
    """进摘要的浮点一律先 round 到 3 位（Python 的 repr 是最短往返表示，跨机一致）。"""
    try:
        return round(float(x), 3)
    except (TypeError, ValueError):
        return 0.0



def page_rows(slides: list, need_hash: bool = False, slides_dir: Path | None = None) -> list:
    """把每页规范化成摘要输入的一行（按 id 升序）。

    纳入：页号（id）、页界（t_start/t_end）、哪一帧（frame/chosen_t）、图字节（sha256）。
    排除（每条都有理由）：generated_at（每次都变）、strategy（怎么切，不改变图的身份）、
    time（chosen_t 的派生物）、ocr_text/ocr_chars（重跑 OCR 会误报，图其实没变）、
    merged_from（只记合并来源）、boundary_evidence（诊断用且切片调参就变语义）、
    jpg 相对路径（由 id 决定，绝对路径不可移植）。

    need_hash=True：对 out/<vid>/slides/NNNN.jpg **现算** sha256（check 的复算路径）；
    need_hash=False：用记录值（bundle 刚 copy 完就写过，不重复读盘）。
    """
    rows = []
    for s in sorted(slides or [], key=lambda x: int(x["id"])):
        if need_hash:
            f = Path(slides_dir) / ("%04d.jpg" % int(s["id"])) if slides_dir else None
            digest = sha256_file(f) if (f is not None and f.exists()) else ""
        else:
            digest = str(s.get("sha256") or "")
        rows.append({"id": int(s["id"]), "t_start": r3(s.get("t_start")), "t_end": r3(s.get("t_end")),
                     "frame": str(s.get("frame") or ""), "chosen_t": r3(s.get("chosen_t")),
                     "sha256": digest})
    return rows


def digest(rows: list) -> str:
    """规范摘要：`bnote-slideset/1\\n` + 紧凑 JSON（sort_keys）→ sha256。"""
    blob = (ALGO + chr(10)).encode("utf-8") + json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def compute(slides: list) -> dict:
    """bundle 用：读已记录的 sha256 组装 `slideset{algo,id,count}`（文件已 copy 完）。"""
    rows = page_rows(slides, need_hash=False)
    return {"algo": ALGO, "id": digest(rows), "count": len(rows)}


def read_out_slides(out_dir: Path) -> dict | None:
    p = Path(out_dir) / "slides.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def slideset_of(doc: dict | None) -> dict:
    ss = (doc or {}).get("slideset")
    return ss if isinstance(ss, dict) else {}


def current_id(out_dir: Path) -> str | None:
    """当前 out/slides.json 的指纹 id；旧产物（无 slideset）返回 None。"""
    return str(slideset_of(read_out_slides(out_dir)).get("id") or "") or None


def recompute_from_out(out_dir: Path, doc: dict | None = None) -> dict | None:
    """check 用：对 out/<vid>/slides/NNNN.jpg 现算 sha256 后复算 id，并给出逐页差异细节。

    返回 None = 这份产物没有可复算的东西（没有 slides.json / 没有页）。
    返回 {recorded_id, id, pages:[{id,recorded,actual,exists}], missing, mismatch}
    —— pages 里的 (记录值, 实际值, 文件是否存在) 是 check 定位「第几页」的依据。
    """
    out_dir = Path(out_dir)
    doc = doc if doc is not None else read_out_slides(out_dir)
    ss = slideset_of(doc)
    slides = (doc or {}).get("slides") or []
    if not slides:
        return None
    slides_dir = out_dir / "slides"
    pages = []
    for s in slides:
        sid = int(s["id"])
        f = slides_dir / ("%04d.jpg" % sid)
        pages.append({"id": sid, "recorded": s.get("sha256"), "actual": sha256_file(f) if f.exists() else None,
                      "exists": f.exists()})
    rows = page_rows(slides, need_hash=True, slides_dir=slides_dir)
    return {"recorded_id": ss.get("id"), "id": digest(rows), "pages": pages,
            "missing": [p["id"] for p in pages if not p["exists"]],
            "mismatch": [p["id"] for p in pages if p["exists"] and p["actual"] != (p["recorded"] or "")]}


# ------------------------------------------------------------------ 派单台账（sidecar）

def dispatch_path(paths) -> Path:
    return paths.meta_dir() / DISPATCH_NAME


def last_record(paths) -> dict | None:
    """collect 用：取派单 history 的最后一条。"""
    p = dispatch_path(paths)
    if not p.exists():
        return None
    try:
        hist = json.loads(p.read_text(encoding="utf-8")).get("history") or []
    except Exception:
        return None
    rec = hist[-1] if hist else None
    return rec if isinstance(rec, dict) and rec.get("slideset_id") else None


def record_dispatch(paths, stage: str, chapters: list) -> dict | None:
    """**派单时刻**记一次指纹（追加式 history，不覆盖历史）。

    `chapters` 是**本次实际派发**的章号（来自 --scope/--owner），不是 manifest 的全部章
    —— 「重切片 + 只为某章重跑 brief」不能把其余章的漂移洗掉。
    只有 `brief --stage chapter` 有资格盖章（scaffold 能独立重跑，盖章会洗掉真实漂移）。
    """
    ss = slideset_of(read_out_slides(paths.out))
    if not ss.get("id"):
        print("[slideset] out/slides.json 没有切片指纹（旧产物或还没跑 bundle）→ 本次不记录；"
              "重跑 bnote bundle 后即可获得保护")
        return None
    p = dispatch_path(paths)
    data = {"schema": DISPATCH_SCHEMA, "history": []}
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(old.get("history"), list):
                data["history"] = old["history"]
        except Exception:
            pass
    rec = {"stage": stage, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "slideset_id": ss["id"],
           "algo": ss.get("algo") or ALGO, "count": ss.get("count"),
           "chapters": [str(c) for c in (chapters or [])]}
    data["history"].append(rec)
    paths.write_json(p, data)
    print("[slideset] 派单指纹已记录（stage=%s，章 %s）→ %s"
          % (stage, ",".join(rec["chapters"]) or "-", p))
    return rec
