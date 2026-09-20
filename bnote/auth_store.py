"""B站登录态：保存与读取 cookie。

位置：<state_root>/auth/bilibili_cookies.txt（Netscape 格式，chmod 600，在数据根里，不在代码仓库里）
      <state_root>/auth/profile.json（用户名等展示信息，不含 token）

注意：这里存的是**你自己的账号凭据**，等同密码，不要提交、不要外传。
优先级：config[auth].cookie_file > BN_AUTH_SESSDATA > <root>/auth/bilibili_cookies.txt
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

IMPORTANT = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid", "buvid3", "buvid4")


def auth_dir(cfg: dict) -> Path:
    """登录态属于 state（跨集复用、不随 cache 清理）"""
    d = Path(cfg["paths"]["state_root"]) / "auth"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def cookie_path(cfg: dict) -> Path:
    return auth_dir(cfg) / "bilibili_cookies.txt"


def write_cookies(cfg: dict, pairs: dict) -> Path:
    p = cookie_path(cfg)
    lines = [NETSCAPE_HEADER, "# 由 bnote auth login 生成；等同账号凭据，勿提交/外传",
             "# HttpOnly 前缀 #HttpOnly_ 说明见 Netscape 格式"]
    for k, v in pairs.items():
        if not v:
            continue
        lines.append("\t".join([".bilibili.com", "TRUE", "/", "FALSE", "0", k, v]))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(p, 0o600)
    (auth_dir(cfg) / "profile.json").write_text(
        json.dumps({"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "keys": sorted(pairs.keys())}, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_cookies(cfg: dict) -> dict:
    p = cookie_path(cfg)
    if not p.exists():
        return {}
    pairs = {}
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) >= 7:
            pairs[f[5]] = f[6]
    return pairs


def default_cookie_file(cfg: dict) -> str:
    p = cookie_path(cfg)
    return str(p) if p.exists() else ""


def cookie_header(pairs: dict) -> str:
    return "; ".join("%s=%s" % (k, v) for k, v in pairs.items() if v)
