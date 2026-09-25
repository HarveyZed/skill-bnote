"""L0 登录态层：把各种 cookie 写法统一成 (cookie_header, netscape_file)。

支持：
  1) Netscape cookies.txt（浏览器插件导出，yt-dlp 直接可用）
  2) 简单文本文件，内容为 "SESSDATA=xxx" 或直接一行 xxx
  3) 环境变量 BN_AUTH_SESSDATA
没有任何登录态时不报错 —— 各层自行降级（下载降画质、字幕换 ASR 后端）。
"""
from __future__ import annotations

import os
from pathlib import Path

NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
COOKIE_DOMAIN = ".bilibili.com"


def _read_env() -> str:
    return (os.environ.get("BN_AUTH_SESSDATA") or "").strip()


def _pairs_from_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore")
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:  # netscape 格式
            f = line.split("\t")
            if len(f) >= 7:
                pairs[f[5]] = f[6]
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            pairs[k.strip()] = v.strip()
    if not pairs and text.strip():
        pairs["SESSDATA"] = text.strip().splitlines()[0].strip()
    return pairs


def resolve(cfg: dict) -> tuple[str, str | None]:
    """返回 (Cookie 头字符串, netscape cookies 文件路径或 None)"""
    cookie_cfg = (cfg["auth"].get("cookie_file") or "").strip()
    pairs: dict[str, str] = {}
    netscape: str | None = None

    if cookie_cfg:
        p = Path(cookie_cfg).expanduser()
        if not p.exists():
            raise FileNotFoundError("配置的 cookie_file 不存在: %s" % p)
        if NETSCAPE_HEADER in p.read_text(encoding="utf-8", errors="ignore")[:200]:
            netscape = str(p)
            pairs = _pairs_from_file(p)
        else:
            pairs = _pairs_from_file(p)

    if not pairs:
        # 回退到 bnote auth login 保存的凭据（<root>/auth/bilibili_cookies.txt）
        try:
            from ..auth_store import default_cookie_file, load_cookies
            stored = default_cookie_file(cfg)
            if stored:
                netscape = stored
                pairs = load_cookies(cfg)
        except Exception:
            pass

    sessdata = pairs.get("SESSDATA") or _read_env()
    if sessdata:
        pairs.setdefault("SESSDATA", sessdata)
    header = "; ".join("%s=%s" % (k, v) for k, v in pairs.items() if v)
    return header, netscape
