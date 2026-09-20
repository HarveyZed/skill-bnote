"""字幕后端：B 站官方字幕轨（UP 上传的 CC / 平台 AI 字幕）。

要点：
  * AI 字幕（lan=ai-zh）通常要求登录态，未登录时 subtitles 为空 —— 属正常降级，交给下一个后端。
  * player 接口有两个版本：x/player/v2 与 x/player/wbi/v2（后者在部分场景需要 wbi 签名）。
    两个都试，谁先返回字幕轨就用谁。
"""
from __future__ import annotations

import json
import re
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
ENDPOINTS = [
    "https://api.bilibili.com/x/player/wbi/v2?bvid={bvid}&cid={cid}",
    "https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}",
]


def _get_json(url: str, cookie: str = "") -> dict:
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_track(tracks: list[dict], priority: list[str]) -> dict | None:
    for pat in priority:
        rx = re.compile(pat, re.I)
        for t in tracks:
            if rx.search((t.get("lan") or "") + " " + (t.get("lan_doc") or "")):
                return t
    return tracks[0] if tracks else None


def run(cfg, paths, meta, media_path=None, cookie: str = "") -> dict | None:
    cid = meta.get("cid")
    if not cid:
        return None
    tracks: list[dict] = []
    seen_login_gate = False
    for tpl in ENDPOINTS:
        try:
            raw = _get_json(tpl.format(bvid=meta["bvid"], cid=cid), cookie)
        except Exception:
            continue
        data = raw.get("data") or {}
        sub = data.get("subtitle") or {}
        if sub.get("need_login_subtitle"):
            seen_login_gate = True
        tracks = sub.get("subtitles") or []
        if tracks:
            break
    if not tracks:
        hint = "（该视频字幕轨需要登录态，请配置 cookie_file）" if seen_login_gate else ""
        print("[subtitle.bili] 未取到字幕轨%s" % hint)
        return None

    track = _pick_track(tracks, cfg["subtitle"]["lang_priority"])
    url = track.get("subtitle_url") or ""
    if url.startswith("//"):
        url = "https:" + url
    body = _get_json(url)
    segs = body.get("body") or []
    segments = [{"from": float(s["from"]), "to": float(s["to"]),
                 "text": (s.get("content") or "").strip()}
                for s in segs if (s.get("content") or "").strip()]
    paths.write_json(paths.subtitle / "bili_raw.json",
                     {"track": track, "tracks": tracks, "body": body})
    return {"backend": "bili", "language": track.get("lan") or "",
            "source": "bilibili player api (%s)" % track.get("lan_doc"),
            "segments": segments}
