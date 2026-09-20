"""L3 字幕层：按配置顺序尝试多个后端，统一归一化成 transcript.json。

归一化契约（下游只认这个结构，换后端不改下游）：
{
  "backend": "bili" | "file" | "whisper",
  "language": "ai-zh",
  "source": "说明文字",
  "segments": [ {"from": 1.23, "to": 4.56, "text": "..."} ],
  "coverage": 0.998,            # 末段结束时间 / 片长
  "partial": false,             # 覆盖度不达标时为 true（残轨或时间轴超片长）
  "fetched_at": "2026-09-20T15:04:05"
}

缓存策略：
  * 覆盖度达标 → **直接复用，不请求上游**（--force 除外）；
  * 覆盖度不达标（残轨 / 时间轴超片长）→ 自动重试一次，只有拿到更优的轨才替换缓存；
  * 重试仍不达标或全部后端失败 → 保留原缓存并给出补救提示，不抛异常。
  关闭自动重试：subtitle.auto_retry_partial = false（此时行为回到"缓存永远优先"）。
"""
from __future__ import annotations

import json
import time

from ..backends import bili_subtitle, file_subtitle, whisper_subtitle

BACKENDS = {
    "bili": bili_subtitle.run,
    "file": file_subtitle.run,
    "whisper": whisper_subtitle.run,
}


def _judge(data: dict, duration: float, min_cov: float, max_cov: float) -> tuple:
    """返回 (覆盖度, 不达标原因或 None)。判据只有覆盖度：下限=残轨，上限=时间轴超片长。"""
    segs = data.get("segments") or []
    if not segs:
        return 0.0, "没有解析到字幕段"
    last_to = max(float(s.get("to") or 0) for s in segs)
    cover = (last_to / duration) if duration else float(data.get("coverage") or 1.0)
    if duration and min_cov and cover < min_cov:
        return cover, "覆盖度 %.1f%%（末段 %.0fs / 片长 %.0fs），疑似残轨" % (cover * 100, last_to, duration)
    if duration and max_cov and cover > max_cov:
        return cover, ("覆盖度 %.1f%%（末段 %.0fs / 片长 %.0fs），时间轴超出片长，疑似整段轨或串了别的分 P"
                       % (cover * 100, last_to, duration))
    return cover, None


def get(cfg, paths, meta, media_path=None, cookie: str = "", force: bool = False) -> dict:
    cached = paths.subtitle / "transcript.json"
    min_cov = float(cfg["subtitle"].get("min_coverage", 0.8) or 0)
    max_cov = float(cfg["subtitle"].get("max_coverage", 1.2) or 0)
    retry_partial = bool(cfg["subtitle"].get("auto_retry_partial", True))
    duration = float(meta.get("duration") or 0)

    reused, reused_cover = None, 0.0
    if cached.exists() and not force:
        reused = json.loads(cached.read_text(encoding="utf-8"))
        reused_cover, flawed = _judge(reused, duration, min_cov, max_cov)
        if flawed is None or not duration:
            # 达标（或片长未知、无从判断）→ 直接用，不打上游
            print("[subtitle] 复用缓存（backend=%s，%d 段，覆盖度 %.1f%%）—— 未请求上游；要重取加 --force"
                  % (reused.get("backend"), len(reused.get("segments", [])), reused_cover * 100))
            return reused
        if not retry_partial:
            print("[subtitle] 复用缓存（backend=%s，%d 段，覆盖度 %.1f%%）—— %s；"
                  "subtitle.auto_retry_partial=false，未请求上游"
                  % (reused.get("backend"), len(reused.get("segments", [])), reused_cover * 100, flawed))
            return reused
        print("[subtitle] 缓存不达标：%s —— 自动重试一次取数" % flawed)

    errors = []
    best_partial = None
    for name in cfg["subtitle"]["backends"]:
        fn = BACKENDS.get(name)
        if not fn:
            errors.append("%s: 未知后端" % name)
            continue
        print("[subtitle] 尝试后端: %s" % name)
        try:
            data = fn(cfg, paths, meta, media_path, cookie)
        except Exception as exc:  # 后端失败不影响其它后端
            errors.append("%s: %s" % (name, exc))
            print("[subtitle]   %s 失败: %s" % (name, exc))
            continue
        if data and data.get("segments"):
            cover, bad = _judge(data, duration, min_cov, max_cov)
            data["coverage"] = round(cover, 4)
            if bad:
                msg = "%s: %s" % (name, bad)
                errors.append(msg)
                print("[subtitle]   ! %s -- 继续试下一个后端" % msg)
                if best_partial is None or cover > best_partial[0]:
                    best_partial = (cover, data)
                continue
            data["attempt_errors"] = errors
            data["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            paths.write_json(cached, data)
            if reused is not None:
                print("[subtitle] 重试成功：覆盖度 %.1f%% → %.1f%%，已替换缓存"
                      % (reused_cover * 100, cover * 100))
            print("[subtitle] 采用 %s，共 %d 段（覆盖度 %.1f%%）"
                  % (data.get("backend"), len(data["segments"]), cover * 100))
            return data
        errors.append("%s: 无可用字幕" % name)

    if best_partial is not None and reused is not None and reused_cover >= best_partial[0]:
        # 重试拿到的还不如缓存：保留原缓存，别把好的换成差的
        print("[subtitle] !! 重试结果（%.1f%%）不比缓存（%.1f%%）好，保留原缓存"
              % (best_partial[0] * 100, reused_cover * 100))
        print("[subtitle]    补救：① 换后端（--subtitle-backends whisper / file）"
              "② 确认登录态（官方 AI 轨需要登录）③ 事后可查 transcript.json 的 coverage 字段")
        return reused

    if best_partial is not None:
        cover, data = best_partial
        data["attempt_errors"] = errors
        data["partial"] = True
        data["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        paths.write_json(cached, data)
        print("[subtitle] !! 所有后端都只给出残轨，采用覆盖度最高的一条：%s（%.1f%%）"
              % (data.get("backend"), cover * 100))
        if cover < 1.0:
            print("[subtitle] !! 它只覆盖了片长的 %.1f%%——下游产物会相应变短，别当成完整讲义。" % (cover * 100))
        else:
            print("[subtitle] !! 它的时间轴是片长的 %.1f 倍——产物里会混进别的分 P 的内容或时间错位，别当成本集讲义。" % cover)
        print("[subtitle]    补救：① 换后端（--subtitle-backends whisper / file）"
              "② 确认登录态（官方 AI 轨需要登录）③ 事后可查 transcript.json 的 coverage 字段")
        return data

    if reused is not None:
        # 有缓存打底：重试全军覆没也不能让流程挂掉
        print("[subtitle] !! 重试没有拿到可用字幕，继续用缓存（backend=%s，覆盖度 %.1f%%）"
              % (reused.get("backend"), reused_cover * 100))
        print("[subtitle]    失败明细：%s" % "；".join(errors) if errors else "")
        print("[subtitle]    补救：① 换后端（--subtitle-backends whisper / file）"
              "② 确认登录态（官方 AI 轨需要登录：bnote auth login）")
        return reused

    raise SystemExit(
        "所有字幕后端都失败：\n  - %s\n"
        "  → 三条路：① 确认登录态（官方 AI 轨需要登录：bnote auth status / auth login）\n"
        "           ② 换后端（--subtitle-backends whisper，需要 pip install -e \".[asr]\"；或 file + 自带 srt）\n"
        "           ③ 若轨道存在但异常（残轨/时间轴超片长），transcript.json 里会有 coverage 与 partial 供事后核对"
        % "\n  - ".join(errors))
