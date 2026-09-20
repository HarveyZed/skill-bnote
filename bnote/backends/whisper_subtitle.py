"""字幕后端：本地 faster-whisper ASR（不需要任何登录态，通用于所有视频）。

代价：CPU 上 small 模型约 1x 实时；medium/large 明显更慢 —— 通过配置切换。
"""
from __future__ import annotations

import time

from ..layers.media import extract_audio


def run(cfg, paths, meta, media_path=None, cookie: str = "") -> dict | None:
    s = cfg["subtitle"]
    if media_path is None:
        from ..layers.media import find_media
        media_path = find_media(paths)
    if media_path is None:
        print("[subtitle.whisper] 没有媒体文件可转写")
        return None

    wav = extract_audio(cfg, paths, media_path)
    from faster_whisper import WhisperModel  # 延迟导入：没装也不影响其它后端

    t0 = time.time()
    model = WhisperModel(s["whisper_model"], device=s["whisper_device"],
                         compute_type=s["whisper_compute"],
                         cpu_threads=int(s["whisper_threads"]))
    print("[subtitle.whisper] 模型 %s 就绪 (%.1fs)" % (s["whisper_model"], time.time() - t0))
    segments_iter, info = model.transcribe(
        str(wav),
        language=s["whisper_lang"] or None,
        beam_size=int(s["whisper_beam"]),
        vad_filter=bool(s["whisper_vad"]),
        initial_prompt=(s["whisper_prompt"] or None),
    )
    segments = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        if text:
            segments.append({"from": float(seg.start), "to": float(seg.end), "text": text})
    print("[subtitle.whisper] 转写完成 %d 段，用时 %.1fs" % (len(segments), time.time() - t0))
    return {"backend": "whisper", "language": getattr(info, "language", s["whisper_lang"]),
            "source": "faster-whisper %s/%s" % (s["whisper_model"], s["whisper_compute"]),
            "segments": segments}
