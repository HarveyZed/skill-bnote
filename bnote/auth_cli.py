"""bnote auth —— 获取/检查 B 站登录态。

  bnote auth login          扫码登录（终端打印二维码 + 存 PNG），成功后写入 <root>/auth/
  bnote auth set --sessdata 手动写入（也可 echo SESSDATA=xxx | bnote auth set -）
  bnote auth status         检查登录态与大会员状态

为什么需要登录：B 站的 AI 字幕轨（lan=ai-zh）只对已登录用户返回 subtitle_url，
未登录时 x/player/v2 与 x/player/wbi/v2 的 subtitles 都是空数组（已实测）。
没有登录态就只能退化为本地 ASR，讲课类音频的识别质量会明显下降。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

from . import auth_store

UA = auth_store.UA
QR_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=%s"
NAV = "https://api.bilibili.com/x/web-interface/nav"


def _req(url: str, cookie: str = ""):
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com"}
    if cookie:
        headers["Cookie"] = cookie
    return urllib.request.Request(url, headers=headers)


def _get_json(url: str, cookie: str = ""):
    with urllib.request.urlopen(_req(url, cookie), timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8")), resp.headers


def _parse_set_cookie(headers) -> dict:
    pairs = {}
    for raw in headers.get_all("Set-Cookie") or []:
        first = raw.split(";")[0]
        if "=" in first:
            k, v = first.split("=", 1)
            pairs[k.strip()] = v.strip()
    return pairs


def status(cfg: dict) -> int:
    pairs = auth_store.load_cookies(cfg)
    if not pairs:
        print("未保存登录态。执行: bnote auth login（扫码）或 bnote auth set --sessdata ...")
        return 1
    try:
        data, _ = _get_json(NAV, auth_store.cookie_header(pairs))
    except Exception as exc:
        print("检查失败（网络？）: %s" % exc)
        return 2
    d = data.get("data") or {}
    if not d.get("isLogin"):
        print("登录态已失效（isLogin=false），请重新 bnote auth login")
        return 1
    vip = (d.get("vipStatus") == 1)
    print("登录成功: %s (uid=%s, 大会员=%s)" % (d.get("uname"), d.get("mid"), "是" if vip else "否"))
    print("cookie 文件: %s" % auth_store.cookie_path(cfg))
    return 0


def set_manual(cfg: dict, sessdata: str) -> int:
    sessdata = (sessdata or "").strip()
    if not sessdata:
        print("需要 SESSDATA", file=sys.stderr)
        return 2
    pairs = auth_store.load_cookies(cfg)
    pairs["SESSDATA"] = sessdata
    p = auth_store.write_cookies(cfg, pairs)
    print("已写入 %s" % p)
    return status(cfg)


def login(cfg: dict, timeout: float = 180.0, show_png: bool = True) -> int:
    try:
        data, _ = _get_json(QR_GEN)
    except Exception as exc:
        print("获取二维码失败: %s" % exc)
        return 2
    d = data.get("data") or {}
    url, key = d.get("url"), d.get("qrcode_key")
    if not url or not key:
        print("二维码接口返回异常: %s" % json.dumps(data, ensure_ascii=False)[:200])
        return 2

    # 二维码默认落成 PNG（终端字符画在部分终端行距会被拉大、扫不动，图片更稳）。
    # segno 是**可选**依赖：缺了不影响登录（仍会给出链接），需要图片时在当前解释器里装一下即可。
    png, term = None, None
    try:
        import segno
        qr = segno.make(url, error="m")
        png = auth_store.auth_dir(cfg) / "login_qr.png"
        qr.save(str(png), scale=8, border=2)
        term = qr.terminal(compact=True)
    except Exception as exc:
        print("（没有二维码图片：%s）" % exc)
        print("  需要图片就在当前解释器里装一下： %s -m pip install segno" % sys.executable)
    if png:
        print("二维码图片（推荐用它扫码）: %s" % png)
    if term:
        print(term)
    print("二维码链接（也可以自己转成二维码）: %s" % url)
    print("请用哔哩哔哩 App 扫码并确认登录（%.0f 秒内有效）..." % timeout)

    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            data, headers = _get_json(QR_POLL % key)
        except Exception as exc:
            print("轮询失败: %s" % exc)
            time.sleep(3)
            continue
        code = (data.get("data") or {}).get("code")
        if code == 0:
            pairs = auth_store.load_cookies(cfg)
            pairs.update(_parse_set_cookie(headers))
            p = auth_store.write_cookies(cfg, pairs)
            print("登录成功，cookie 已写入 %s（权限 600）" % p)
            return status(cfg)
        if code == 86090:
            print("已扫码，请在手机上确认...")
        time.sleep(3)
    print("超时未确认，请重试")
    return 1
