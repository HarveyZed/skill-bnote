"""外部程序探测：只在这里解析二进制的绝对路径。

优先级： 配置 [tools].xxx  >  环境变量 BN_TOOLS_XXX  >  PATH  >  imageio-ffmpeg 内置静态包
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent.parent


def _link_as_ffmpeg(exe: str, cfg: dict) -> str:
    """imageio-ffmpeg 的二进制名不是 ffmpeg，yt-dlp 按名字找，所以做一层软链。

    软链落在**数据根**的 cache/_bin 下 —— 运行期产物不进代码目录：代码目录随 skill 分发，
    可能只读（只读安装 / 容器挂载），也可能被多份数据根共享。
    """
    import os
    from pathlib import Path
    bindir = Path(cfg["paths"]["cache_root"]) / "_bin"
    try:
        bindir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            "找不到 ffmpeg，也无法在数据根建软链：%s\n  原因：%s\n"
            "  → 装系统 ffmpeg（推荐：apt install ffmpeg），或 pip install imageio-ffmpeg；\n"
            "  → 或在 config/local.toml 的 [tools] ffmpeg / BN_TOOLS_FFMPEG 指定绝对路径；\n"
            "  → 或让数据根可写（BNOTE_ROOT 或 [paths] root）。" % (bindir, exc))
    link = bindir / "ffmpeg"
    if not link.exists() or os.path.realpath(link) != os.path.realpath(exe):
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(exe)
    return str(link)


def find_ffmpeg(cfg: dict) -> str:
    custom = cfg["tools"].get("ffmpeg")
    if custom:
        return custom
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except Exception:
        raise RuntimeError(
            "找不到 ffmpeg：请安装系统 ffmpeg（推荐：apt install ffmpeg）、或 pip install imageio-ffmpeg、"
            "或在 config/local.toml 的 [tools].ffmpeg 指定绝对路径"
        )
    # 静态包只作兜底；软链失败要带上真实原因（目录不可写等），不要在这里吞掉
    return _link_as_ffmpeg(imageio_ffmpeg.get_ffmpeg_exe(), cfg)


def find_ffprobe(cfg: dict) -> str:
    custom = cfg["tools"].get("ffprobe")
    if custom:
        return custom
    found = shutil.which("ffprobe")
    if found:
        return found
    ff = find_ffmpeg(cfg)
    guess = ff.replace("ffmpeg", "ffprobe")
    if shutil.which(guess) or guess != ff:
        import os
        if os.path.exists(guess):
            return guess
    raise RuntimeError("找不到 ffprobe（imageio-ffmpeg 只带 ffmpeg，无 ffprobe；可改用 web-interface API 取元信息）")


def yt_dlp_python(cfg: dict) -> str:
    """返回可执行的 python 解释器（用于 -m yt_dlp）"""
    custom = cfg["tools"].get("yt_dlp")
    if custom:
        return custom
    return sys.executable