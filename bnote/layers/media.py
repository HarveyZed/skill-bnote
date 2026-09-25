"""L2 媒体层：用 yt-dlp 下载视频/音频，支持区间与画质上限。

解耦点：只负责"把媒体落到 cache/media/"，不关心抽帧与字幕。
更换下载器（yutto / 自研 API）只需替换本文件，对外契约是返回本地媒体路径。
"""
from __future__ import annotations

import glob
import subprocess
import sys
from pathlib import Path

from ..tools import PROJ_ROOT, find_ffmpeg, yt_dlp_python


def _output_name(cfg: dict, meta: dict) -> str:
    suffix = "audio" if cfg["media"]["audio_only"] else "video"
    return "%s_%s" % (meta["vid"], suffix)


def find_media(paths) -> Path | None:
    for pat in ("*.mp4", "*.mkv", "*.flv", "*.m4a", "*.mp3", "*.wav", "*.webm"):
        hits = sorted(glob.glob(str(paths.media / pat)))
        if hits:
            return Path(hits[0])
    return None


def download(cfg: dict, paths, meta: dict, cookie: str = "", netscape: str | None = None,
             force: bool = False) -> Path:
    paths.media.mkdir(parents=True, exist_ok=True)   # 只建自己要写的目录
    if not force:
        existing = find_media(paths)
        if existing:
            print("[media] 已存在，跳过下载: %s" % existing.name)
            return existing

    m = cfg["media"]
    out_tpl = str(paths.media / (_output_name(cfg, meta) + ".%(ext)s"))
    cmd = [yt_dlp_python(cfg), "-m", "yt_dlp",
           "--no-playlist", "--newline", "--no-warnings",
           "--retries", str(m["retries"]),
           "--sleep-requests", str(m["sleep_requests"]),
           "-o", out_tpl]

    if m["format"]:
        fmt = m["format"]
    elif m["audio_only"]:
        fmt = "bestaudio/best"
    else:
        h = int(m["max_height"])
        fmt = "bv*[height<=%d]+ba/b[height<=%d]" % (h, h)
    cmd += ["-f", fmt]

    if m["audio_only"]:
        cmd += ["-x", "--audio-format", "m4a"]
    else:
        cmd += ["--merge-output-format", "mp4"]

    if m["sections"]:
        cmd += ["--download-sections", "*%s" % m["sections"], "--force-keyframes-at-cuts"]

    if netscape:
        cmd += ["--cookies", netscape]
    if m["proxy"]:
        cmd += ["--proxy", m["proxy"]]
    try:
        cmd += ["--ffmpeg-location", str(Path(find_ffmpeg(cfg)).parent)]
    except Exception as exc:   # 不静默：--sections/HLS/合并 需要 ffmpeg，失败原因必须看得见
        print("[media] ⚠ 拿不到 ffmpeg：%s" % exc)
        print("[media]   yt-dlp 将不带 --ffmpeg-location：--sections / HLS / 音视频合并会失败")

    cmd.append(meta["url"])
    print("[media] %s" % " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise SystemExit(
            "✗ 找不到 yt-dlp 的解释器：%s\n"
            "  装依赖： bash %s/scripts/setup-env.sh\n"
            "  或用能跑 yt-dlp 的解释器： BN_PYTHON=/path/to/python" % (exc, PROJ_ROOT))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "✗ yt-dlp 下载失败（exit %s）。常见原因：没有登录态/被风控/网络不可达；\n"
            "  若上面日志里有 No module named yt_dlp，先跑 %s/scripts/setup-env.sh 装依赖。"
            % (exc.returncode, PROJ_ROOT))

    got = find_media(paths)
    if not got:
        raise RuntimeError("下载结束但未在 %s 找到媒体文件" % paths.media)
    print("[media] 完成: %s (%.1f MB)" % (got.name, got.stat().st_size / 1e6))
    return got


def extract_audio(cfg: dict, paths, media_path: Path, force: bool = False) -> Path:
    """抽 16k 单声道 wav，供 ASR 使用（幂等）"""
    wav = paths.media / "audio16k.wav"
    if wav.exists() and not force:
        return wav
    ff = find_ffmpeg(cfg)
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(media_path),
           "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
    subprocess.run(cmd, check=True)
    return wav
