"""L1 元信息层：把 URL/BV 解析成 (bvid, page) 并取回标题/时长/cid。

解耦点：只依赖 B 站 web-interface 公开接口；接口失效时只需改这里，下游全部不受影响。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_TAGS = "https://api.bilibili.com/x/tag/archive/tags"
# 视频页元信息里对下游有用的三样（简介常有课程目录/资料链接/术语说明）
META_EXTRA_KEYS = ("desc", "tags", "tname", "tid")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


def parse_target(target: str, page: int | None = None) -> tuple[str, int]:
    m = BV_RE.search(target)
    if not m:
        raise ValueError("无法从 %r 解析出 BV 号" % target)
    bvid = m.group(1)
    if page is None:
        q = urllib.parse.urlparse(target).query
        p = urllib.parse.parse_qs(q).get("p")
        page = int(p[0]) if p else 1
    return bvid, int(page)


def _get_json(url: str, cookie: str = "") -> dict:
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_tags(bvid: str, cookie: str = "") -> list:
    """标签：单独接口，失败不影响主流程（元信息是增强，不是必需）。"""
    try:
        raw = _get_json("%s?bvid=%s" % (API_TAGS, bvid), cookie)
        if raw.get("code") != 0:
            return []
        return [t.get("tag_name") for t in (raw.get("data") or []) if t.get("tag_name")]
    except Exception:
        return []


def fetch(bvid: str, page: int, cookie: str = "") -> dict:
    raw = _get_json("%s?bvid=%s" % (API_VIEW, bvid), cookie)
    if raw.get("code") != 0:
        raise RuntimeError("取视频信息失败: %s %s" % (raw.get("code"), raw.get("message")))
    d = raw["data"]
    pages = d.get("pages") or []
    if page < 1 or page > max(1, len(pages)):
        raise ValueError("p=%s 越界（该视频共 %d P）" % (page, len(pages)))
    cur = pages[page - 1] if pages else {}
    return {
        "bvid": bvid,
        "page": page,
        "vid": "%s_p%d" % (bvid, page),
        "title": d.get("title"),
        "part": cur.get("part"),
        "cid": cur.get("cid"),
        "duration": cur.get("duration") or d.get("duration"),
        "page_count": len(pages),
        "owner": (d.get("owner") or {}).get("name"),
        "pubdate": d.get("pubdate"),
        "tname": d.get("tname"),                 # 分区名（接口有时返回空串）
        "tid": d.get("tid"),                     # 分区 id（tname 为空时的兜底）
        "desc": (d.get("desc") or "").strip(),   # 视频简介：常有课程目录/资料链接/术语说明
        "tags": _fetch_tags(bvid, cookie),
        "url": "https://www.bilibili.com/video/%s?p=%d" % (bvid, page),
        "all_parts": [{"page": p.get("page"), "cid": p.get("cid"),
                       "part": p.get("part"), "duration": p.get("duration")} for p in pages],
    }


def get_or_fetch(cfg: dict, paths, cookie: str = "", refresh: bool = False) -> dict:
    if paths.meta.exists() and not refresh:
        meta = json.loads(paths.meta.read_text(encoding="utf-8"))
        if all(k in meta for k in META_EXTRA_KEYS):
            return meta
        try:   # 旧缓存：补一次简介/标签/分区（一次 API 调用；失败就用旧的，不阻塞）
            fresh = fetch(meta["bvid"], int(meta.get("page") or 1), cookie)
        except Exception as exc:
            print("[meta] 旧 meta 缺少 %s，补取失败（沿用旧值）：%s" % ("/".join(META_EXTRA_KEYS), exc))
            return meta
        for k in META_EXTRA_KEYS:
            meta[k] = fresh.get(k)
        paths.write_json(paths.meta, meta)
        print("[meta] 旧缓存已补上视频简介 / 标签 / 分区")
        return meta
    bvid, page = parse_target(paths.vid.split("_p")[0], int(paths.vid.split("_p")[1]))
    meta = fetch(bvid, page, cookie)
    paths.write_json(paths.meta, meta)
    return meta
