"""L1 元信息层：把 URL/BV 解析成 (bvid, page) 并取回标题/时长/cid。

解耦点：只依赖 B 站 web-interface 公开接口；接口失效时只需改这里，下游全部不受影响。
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_TAGS = "https://api.bilibili.com/x/tag/archive/tags"
API_REPLY = "https://api.bilibili.com/x/v2/reply"
PAGE_URL = "https://www.bilibili.com/video/%s/"
# 视频页元信息里对下游有用的几样（简介/置顶评论常有课程目录、资料链接、勘误与术语说明）
# aid 是取置顶评论必需的 oid，一并落盘，省得下游再解析一次
META_EXTRA_KEYS = ("aid", "desc", "tags", "tname", "tid", "top_comment")
META_EXTRA_LABEL = "视频简介 / 标签 / 分区 / aid / 置顶评论"
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


def _seed_cookie_jar(jar, cookie: str) -> None:
    """把登录态 cookie 串塞进 jar，之后交给 jar 自己带（服务端下发的 buvid3 也留在里面）。"""
    for part in (cookie or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if not name:
            continue
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=".bilibili.com", domain_specified=True, domain_initial_dot=True,
            path="/", path_specified=True, secure=False, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False))


def _fetch_top_comment(cfg: dict | None, bvid: str, aid, cookie: str = "") -> dict | None:
    """UP 主置顶评论（只取这一条，不做评论区批量采集）。

    接口：GET x/v2/reply?type=1&oid=<aid>&pn=1&sort=2 → data.upper.top
    前置：先取一次视频页拿 buvid3 —— 缺了它直接调评论接口必被风控挡回 HTTP 412。
    失败一律返回 None（元信息是增强，不阻塞取数）；只有"调用出问题"才打印一行提示。
    """
    mc = (cfg or {}).get("meta") or {}
    if not mc.get("top_comment", True) or not aid:
        return None
    timeout = int(mc.get("top_comment_timeout", 20))
    jar = http.cookiejar.CookieJar()
    _seed_cookie_jar(jar, cookie)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
               "Accept-Language": "zh-CN,zh;q=0.9",
               "Referer": PAGE_URL % bvid}
    try:
        if "buvid3" not in {c.name for c in jar}:
            try:
                opener.open(urllib.request.Request(
                    PAGE_URL % bvid, headers=dict(headers, Accept="text/html")),
                    timeout=timeout).read(1024)
            except Exception:
                pass
        url = "%s?type=1&oid=%s&pn=1&sort=2" % (API_REPLY, aid)
        with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print("[meta] 置顶评论取不到（沿用空值）：%s" % exc)
        return None
    if raw.get("code") != 0:
        print("[meta] 置顶评论接口返回 %s %s（沿用空值）" % (raw.get("code"), raw.get("message")))
        return None
    top = (((raw.get("data") or {}).get("upper") or {}).get("top") or {})
    text = ((top.get("content") or {}).get("message") or "").strip()
    if not text:
        return None                          # 作者没有置顶评论：正常情况，不提示
    return {
        "rpid": top.get("rpid"),
        "mid": top.get("mid"),
        "uname": (top.get("member") or {}).get("uname"),
        "like": top.get("like"),
        "ctime": top.get("ctime"),
        "text": text,
    }


def _fetch_tags(bvid: str, cookie: str = "") -> list:
    """标签：单独接口，失败不影响主流程（元信息是增强，不是必需）。"""
    try:
        raw = _get_json("%s?bvid=%s" % (API_TAGS, bvid), cookie)
        if raw.get("code") != 0:
            return []
        return [t.get("tag_name") for t in (raw.get("data") or []) if t.get("tag_name")]
    except Exception:
        return []


def fetch(bvid: str, page: int, cookie: str = "", cfg: dict | None = None) -> dict:
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
        "aid": d.get("aid"),                     # 评论区的 oid（取置顶评论用）
        # UP 主置顶评论：作者更愿意改这里而不是改简介（改简介等于重新发布视频）
        "top_comment": _fetch_top_comment(cfg, bvid, d.get("aid"), cookie),
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
        try:   # 旧缓存：补一次元信息（失败就用旧的，不阻塞）
            fresh = fetch(meta["bvid"], int(meta.get("page") or 1), cookie, cfg)
        except Exception as exc:
            print("[meta] 旧 meta 缺少 %s，补取失败（沿用旧值）：%s" % ("/".join(META_EXTRA_KEYS), exc))
            return meta
        for k in META_EXTRA_KEYS:
            meta[k] = fresh.get(k)
        paths.write_json(paths.meta, meta)
        print("[meta] 旧缓存已补上 %s" % META_EXTRA_LABEL)
        return meta
    bvid, page = parse_target(paths.vid.split("_p")[0], int(paths.vid.split("_p")[1]))
    meta = fetch(bvid, page, cookie, cfg)
    paths.write_json(paths.meta, meta)
    return meta
