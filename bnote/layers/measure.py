"""L4.5 量测层（M1，零 token）：一次 ffmpeg 解码 → 逐秒桶 + 事件。

读字面板 / 采样盲区可核算 / 索引与时间映射分离这三条**做法**参考了 CFITCorporation/video-watch-skill（MIT）的思路；本项目按自身链路用已有 Pillow+numpy 自实现，**未使用其代码**。若将来真复制其代码，必须按其 MIT 许可证附版权声明。

产物 `cache/<vid>/measure.json`（**接口冻结**，键序即结构：
schema / vid / algo / source / params / applicability / coverage / buckets / events / stats）：

  * `source`      —— 媒体 basename + 时长/帧率/分辨率/有无音轨；
  * `buckets[s]`  —— 该秒的 `lavfi.scd.mafd` 均值与最大值、`lavfi.scd.score` 最大值、解到的帧数；
  * `events`      —— cuts（帧级 score 达下限）/ freezes / silences，一律 from/to/dur；
  * `coverage`    —— **解码**盲区（帧数 + 相邻 pts_time 最大间隔）与**抽帧采样**盲区
                     （读 `cache/frames/index.json`：抽了几帧、最大间隔多少）。

为什么不用 signalstats：整链的墙钟几乎全花在它身上（实测 331 s → 25 s，占 92%），
而 M1 要的运动量 scdet 每帧免费就给（`lavfi.scd.mafd`）。去掉它之后 `motion` 只反映
**画面变化量**，**不含亮度/色度统计** —— 别以为这里还有色彩统计。

只读媒体文件与 `cache/frames/index.json`，**不写别的文件**；产物里**不放任何时间戳**
（两次跑必须逐字节一致，闸门直接比 sha256）。M1 也**不接进 run/slides**：只在显式
`bnote measure` 时跑，现有产物与既有成本零变化。

M3 起**按遮罩算**：存在 `cache/<vid>/overlay.json` 时，给**同一条滤镜链的最前面**每个遮挡区
加一个 `drawbox=...:t=fill`（涂成常数），freezedetect / scdet 于是只看没被遮住的像素 ——
"挖掉遮罩后重算"**不需要再解码一遍**（这正是 M1 把量测做成单遍链的用处）。拿不到 overlay.json
时按**全画面**算，并在产物 applicability 里写明 masked=false + mask_source=null（不静默降级）。
烧录字幕每秒在变，未遮罩时 freezedetect 会被切得很碎：BV1CCtz6WEvF_p1 实测未遮罩 1670 段 /
覆盖 96.5% 时长，遮罩后段数大幅下降。
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from ..tools import crop_box_to_media
from ..tools import check_basis_dest
from ..tools import find_ffmpeg
from ..tools import probe_media

SCHEMA = "bnote-measure/1"
ALGO = "bnote-measure/1"

APPLICABILITY_NOTE = ("未按遮罩算：本集没有 cache/<vid>/overlay.json，烧录字幕与标注工具条"
                      "每秒在变，会污染 motion 与 freezes（先 bnote slides 抽帧产出遮罩，"
                      "或直接跑 bnote overlay）")
MASK_NOTE = ("已按遮罩算：给同一条 ffmpeg 滤镜链的最前面加 drawbox=...:t=fill，把遮挡区涂成"
             "常数，freezedetect / scdet 只看没被遮住的像素（**没有多解码一遍**）")
COVERAGE_NOTE = "sampling_* 才是「抽了几帧、可能漏什么」的上界；没有 frames/index.json 时为 null"
COVERAGE_NOTE_NO_FRAMES = ("sampling_* 才是「抽了几帧、可能漏什么」的上界；本集还没有 "
                           "cache/frames/index.json（先跑 bnote slides 抽帧）")
SAMPLING_SOURCE = "cache/frames/index.json"

# metadata=print 的格式：每个解码帧一段 "frame:<n> pts:<t> pts_time:<sec>" 头 + 若干 "key=value"
FRAME_RE = re.compile(r"^frame:(\d+)\s+pts:(-?\d+)\s+pts_time:(\S+)\s*$")
ATTR_RE = re.compile(r"^([A-Za-z0-9_.]+)=(.*)$")
FREEZE_START_KEY = "lavfi.freezedetect.freeze_start"
FREEZE_END_KEY = "lavfi.freezedetect.freeze_end"

SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
SILENCE_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")

# 流信息探测（含 ffprobe 不可用时的三条兜底正则）已挪到 tools.probe_media —— L4 取帧与 L4.5 量测共用一份


def _overlay_masks(paths, basis=None):
    """读遮罩里**可采信**的区域框；没有就返回 []。

    整片读 cache/<vid>/overlay.json；basis（取样包）读 cache/<vid>/sample/overlay.json ——
    取样量测必须用**取样自己的**遮罩，否则整片遮罩套在取样片段上会错位。
    **文件就是接口**（P1）：这里不 import overlay 层，只认冻结的 regions[].box 与
    applicability 两个字段。缺文件或结构不对一律当"没有遮罩"，由 applicability 写明。
    """
    doc = paths.read_json(paths.sample_overlay if basis else paths.overlay) or {}
    out = []
    for r in (doc.get("regions") or []):
        box = r.get("box")
        # kind=handwriting 的区域**不能** drawbox：它的 box 是"整集笔迹落在哪"的审计包围盒
        # （实测能覆盖 77%~91% 画面），而笔迹要按帧用 params 的判据重算。挖它等于把整幅涂黑。
        if (r.get("applicability") == "ok" and r.get("kind") != "handwriting"
                and isinstance(box, list) and len(box) == 4):
            out.append([float(x) for x in box])
    return out


def _drawboxes(masks, cfg, media_size, basis=None) -> list[str]:
    """遮挡区 -> drawbox 参数（像素）。掩码作用在**同一条解码链**上，不额外解码。

    overlay 的坐标空间是**抽帧后的画面**，量测解码的是媒体原图：`[frames].crop` 非空时
    必须换算（tools.crop_box_to_media），否则掩码错位 —— 错位的掩码比不遮更坏。

    取样模式**不做 crop 换算**：取样帧是直接从取样媒体抽的（只过 fps/scale，没裁过），
    而 basis 里那两帧的坐标系本来就一致 —— 拿整片的 crop 去换算反而会错位。
    """
    if not masks or not media_size:
        return []
    W, H = int(media_size[0] or 0), int(media_size[1] or 0)
    if W <= 0 or H <= 0:
        return []
    crop = "" if basis else ((cfg.get("frames") or {}).get("crop") or "")
    out = []
    for box in masks:
        l, t, r, b = crop_box_to_media(box, crop, (W, H))
        x0, y0 = min(max(0, int(l * W)), W - 1), min(max(0, int(t * H)), H - 1)
        w = min(max(1, int(round(r * W)) - x0), W - x0)
        h = min(max(1, int(round(b * H)) - y0), H - y0)
        out.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=black:t=fill" % (x0, y0, w, h))
    return out


def _applicability(paths, masks, basis=None) -> dict:
    """适用性：按遮罩算还是全画面算、遮罩从哪来（M1 已有该字段，M3 才真正用起来）。"""
    if not masks:
        return {"masked": False, "mask_source": None, "note": APPLICABILITY_NOTE}
    src = (basis or {}).get("relpath") or _rel(paths, paths.overlay)
    return {"masked": True, "mask_source": "%s（%d 个区域）" % (src, len(masks)),
            "note": MASK_NOTE}


def _num(x) -> str:
    """浮点参数写进滤镜串时不带多余小数（8.0 -> 8、-60 -> -60、0.5 -> 0.5）。"""
    return "%g" % float(x)


def _float(raw, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# 流信息探测见 tools.probe_media（ffprobe 优先、没有就解析 ffmpeg -i 的 stderr）


# ---------------------------------------------------------------- 一次解码
def build_command(ffmpeg: str, media_path: Path, cfg: dict, meta_file: str,
                  has_audio: bool, masks=None, media_size=None, basis=None) -> list[str]:
    """视频侧 freezedetect → scdet → metadata 打点（逐帧 mafd/score），音频侧 silencedetect。

    `-loglevel info` 是给 silencedetect 用的：它的 silence_start/end 走日志，不走帧 metadata；
    `-nostats` 顺手关掉进度刷屏。**不加 signalstats**（占 92% 墙钟，M1 不需要）。
    """
    m = cfg["measure"]
    chain = _drawboxes(masks, cfg, media_size, basis)   # 遮罩在这一步就位，链路不变长
    chain.append("freezedetect=n=%sdB:d=%s" % (_num(m["freeze_noise_db"]),
                                               _num(m["freeze_min_sec"])))
    chain.append("scdet=threshold=%s" % _num(m["scdet_threshold"]))
    chain.append("metadata=print:file=%s" % meta_file)
    vf = ",".join(chain)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
           "-i", str(media_path), "-vf", vf]
    if has_audio:
        cmd += ["-af", "silencedetect=noise=%sdB:d=%s"
                % (_num(m["silence_noise_db"]), _num(m["silence_min_sec"]))]
    else:
        cmd += ["-an"]
    cmd += ["-f", "null", "-"]
    return cmd


def _parse_meta(text: str) -> tuple[list[dict], list[tuple], float | None]:
    """解析 metadata=print 落下的文本。

    返回 (frames, freeze_pairs, open_freeze_start)：
      frames 逐帧 {t, mafd, score}；freeze_pairs 已配对的 (start, end)；
      open_freeze_start 是到 EOF 还没闭合的那一段（由调用方补到 duration）。
    """
    frames: list[dict] = []
    pairs: list[tuple] = []
    cur: dict | None = None
    open_start: float | None = None
    for line in text.splitlines():
        line = line.strip()
        m = FRAME_RE.match(line)
        if m:
            if cur is not None:
                frames.append(cur)
            cur = {"t": _float(m.group(3)), "mafd": 0.0, "score": 0.0}
            continue
        if cur is None:
            continue
        a = ATTR_RE.match(line)
        if not a:
            continue
        key, raw = a.group(1), a.group(2)
        if key == "lavfi.scd.mafd":
            cur["mafd"] = _float(raw)
        elif key == "lavfi.scd.score":
            cur["score"] = _float(raw)
        elif key == FREEZE_START_KEY:
            if open_start is not None:          # 上一段没等到 end：用当前起点收口
                pairs.append((open_start, _float(raw)))
            open_start = _float(raw)
        elif key == FREEZE_END_KEY:
            if open_start is not None:          # 配对成功
                pairs.append((open_start, _float(raw)))
                open_start = None
    if cur is not None:
        frames.append(cur)
    return frames, pairs, open_start


def _parse_silences(stderr: str, duration: float) -> list[tuple]:
    """silencedetect 的事件只在日志里（`silence_start: t` / `silence_end: t`），按出现顺序配对。"""
    pairs: list[tuple] = []
    open_start: float | None = None
    for line in stderr.splitlines():
        if "silence_start" in line:
            m = SILENCE_START_RE.search(line)
            if m:
                if open_start is not None:
                    pairs.append((open_start, _float(m.group(1))))
                open_start = _float(m.group(1))
        elif "silence_end" in line:
            m = SILENCE_END_RE.search(line)
            if m and open_start is not None:
                pairs.append((open_start, _float(m.group(1))))
                open_start = None
    if open_start is not None:
        pairs.append((open_start, duration))
    return pairs


def _segments(pairs: list[tuple], open_start: float | None, duration: float) -> list[dict]:
    """(from,to) → {from,to,dur}（round 3），未闭合的最后一段补到 duration。"""
    all_pairs = list(pairs)
    if open_start is not None:
        all_pairs.append((open_start, duration))
    out = []
    for a, b in all_pairs:
        frm, to = round(a, 3), round(b, 3)
        out.append({"from": frm, "to": to, "dur": round(to - frm, 3)})
    return out


def _sampling(paths, basis=None) -> tuple[int | None, float | None]:
    """抽帧采样盲区上界：整片读 cache/frames/index.json，取样读取样包索引（不存在就 None）。

    **取样模式下这个数不小**：它是"相邻取样帧的最大时间间隔"，而取样帧只落在几个窗口里，
    所以它天然包含**窗口之间**的距离（P25 实测 105.4 s，窗口 1 末帧 62.6 s → 窗口 2 首帧 168.0 s）。
    别把它当"窗口内的稀疏程度"；"哪几段完全没看"另有取样包 index.json 的
    coverage.uncovered_max_gap_sec（按窗口边界算，实测 104.4 s）——两者相差的是帧粒度。
    """
    idx = (basis or {}).get("index") or paths.read_json(paths.frames / "index.json")
    if not idx:
        return None, None
    ts = [_float(f.get("t")) for f in (idx.get("frames") or []) if f.get("t") is not None]
    if len(ts) < 2:
        return (len(ts) or None), None
    return len(ts), round(max(b - a for a, b in zip(ts, ts[1:])), 3)


def _buckets(frames: list[dict], duration: float) -> list[dict]:
    """逐秒桶：motion=mafd 均值、motion_max/max score、n=该秒解码帧数（0 基，整秒）。"""
    nb = int(duration) + 1 if duration > 0 else 0
    if frames:
        nb = max(nb, int(frames[-1]["t"]) + 1)
    sums = [0.0] * nb
    peaks = [0.0] * nb
    scores = [0.0] * nb
    counts = [0] * nb
    for f in frames:
        s = int(f["t"])
        if s < 0 or s >= nb:
            continue
        sums[s] += f["mafd"]
        counts[s] += 1
        if f["mafd"] > peaks[s]:
            peaks[s] = f["mafd"]
        if f["score"] > scores[s]:
            scores[s] = f["score"]
    return [{"s": i,
             "motion": round(sums[i] / counts[i], 6) if counts[i] else 0.0,
             "motion_max": round(peaks[i], 6),
             "cut_score": round(scores[i], 6),
             "n": counts[i]} for i in range(nb)]


def _source(media_path: Path, duration: float, info: dict, basis) -> dict:
    """source 段。**只有取样模式**加 basis 字段（§3.6-5 定稿）。

    顶层的 cache/<vid>/measure.json **一行不动**：它是 M1 的确定性产物，36f248f4… 这条锚点
    被两份报告引用着；而 source.file 已经指明媒体，"整片"是零信息量常量。
    """
    out = {"file": media_path.name,
           "duration": round(duration, 3),
           "fps": round(info["fps"], 3),
           "width": info["width"],
           "height": info["height"],
           "has_audio": info["has_audio"]}
    if basis:
        out["basis"] = basis["relpath"]
    return out


def analyze(cfg: dict, paths, media_path: Path, basis=None) -> dict:
    """跑一次解码并组出 measure 文档（**不落盘**，便于单独测试与复算）。

    basis=None = 整片（行为与 M1/M3 逐字节相同）；basis = 取样包（遮罩/采样上界/basis 字段
    都取自取样包，落点也换成 sample/measure.json）。
    """
    info = probe_media(cfg, media_path)
    if not info["has_video"]:
        raise SystemExit("[measure] %s 没有视频轨：量测需要画面（口播/播客类请走 bnote stream）"
                         % media_path.name)
    ffmpeg = find_ffmpeg(cfg)
    masks = _overlay_masks(paths, basis)
    media_size = (info["width"], info["height"])
    if masks:
        print("[measure] 按遮罩算：%d 个区域来自 %s → drawbox 涂掉后再 freezedetect/scdet"
              % (len(masks), (basis or {}).get("relpath") or _rel(paths, paths.overlay)))
    else:
        print("[measure] 未按遮罩算（没有 cache/<vid>/overlay.json）：全画面参与 motion/freeze")
    with tempfile.TemporaryDirectory(prefix="bnote-measure-") as td:
        meta_file = str(Path(td) / "meta.txt")
        cmd = build_command(ffmpeg, media_path, cfg, meta_file, info["has_audio"],
                            masks=masks, media_size=media_size, basis=basis)
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise SystemExit("[measure] ffmpeg 解码失败（rc=%s）：%s"
                             % (proc.returncode, (proc.stderr or "").strip()[-800:]))
        meta_text = Path(meta_file).read_text(encoding="utf-8", errors="replace")

    frames, freeze_pairs, open_freeze = _parse_meta(meta_text)
    duration = info["duration"]
    if duration <= 0 and frames:                               # 兜底：流信息没给时长
        duration = frames[-1]["t"]
    ts = [f["t"] for f in frames]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    decode_gap = round(max(gaps), 3) if gaps else 0.0

    freezes = _segments(freeze_pairs, open_freeze, duration)
    silences = (_segments(_parse_silences(proc.stderr, duration), None, duration)
                if info["has_audio"] else [])

    buckets = _buckets(frames, duration)
    m = cfg["measure"]
    cut_min = float(m["cut_score_min"])
    cuts = [{"t": round(f["t"], 3), "score": round(f["score"], 3)}
            for f in frames if f["score"] >= cut_min]

    sampled, sampling_gap = _sampling(paths, basis)
    freeze_total = round(sum(s["dur"] for s in freezes), 3)
    silence_total = round(sum(s["dur"] for s in silences), 3)
    return {
        "schema": SCHEMA,
        "vid": paths.vid,
        "algo": ALGO,
        "source": _source(media_path, duration, info, basis),
        "params": {"scdet_threshold": m["scdet_threshold"],
                   "cut_score_min": m["cut_score_min"],
                   "freeze_noise_db": m["freeze_noise_db"],
                   "freeze_min_sec": m["freeze_min_sec"],
                   "silence_noise_db": m["silence_noise_db"],
                   "silence_min_sec": m["silence_min_sec"]},
        "applicability": _applicability(paths, masks, basis),
        "coverage": {"decode_frames": len(frames),
                     "decode_max_gap_sec": decode_gap,
                     "sampled_frames": sampled,
                     "sampling_max_gap_sec": sampling_gap,
                     "sampling_source": SAMPLING_SOURCE if sampled is not None else None,
                     "buckets": len(buckets),
                     "note": COVERAGE_NOTE if sampled is not None else COVERAGE_NOTE_NO_FRAMES},
        "buckets": buckets,
        "events": {"cuts": cuts, "freezes": freezes, "silences": silences},
        "stats": {"cut_count": len(cuts),
                  "freeze_total_sec": freeze_total,
                  "freeze_ratio": round(freeze_total / duration, 3) if duration > 0 else 0.0,
                  "silence_total_sec": silence_total,
                  "silence_ratio": round(silence_total / duration, 3) if duration > 0 else 0.0},
    }


def _rel(paths, path: Path) -> str:
    try:
        return str(path.relative_to(paths.root))
    except ValueError:
        return path.name


def summary_line(doc: dict, elapsed: float, dest: str) -> str:
    """一行摘要：秒数/桶数/帧数/cuts/freezes/silences/max_gap/耗时。"""
    src = doc.get("source") or {}
    cov = doc.get("coverage") or {}
    ev = doc.get("events") or {}
    stats = doc.get("stats") or {}
    gap = cov.get("sampling_max_gap_sec")
    gap_txt = "%ss" % gap if gap is not None else "无（缺 frames/index.json）"
    return ("[measure] %ss / %d 桶 / %d 帧 | cuts %d · freezes %d · silences %d | "
            "解码最大间隔 %ss ／ 抽帧采样上界 %s | %.1fs → %s"
            % (src.get("duration"), cov.get("buckets"), cov.get("decode_frames"),
               stats.get("cut_count"), len(ev.get("freezes") or []), len(ev.get("silences") or []),
               cov.get("decode_max_gap_sec"), gap_txt, elapsed, dest))


def run(cfg: dict, paths, media_path: Path, force: bool = False, basis=None) -> dict:
    """bnote measure 的入口：已存在且非 --force 就跳过；跑完打印一行摘要。

    basis=None 写 cache/<vid>/measure.json；basis=取样包写 cache/<vid>/sample/measure.json
    （**不碰顶层**）。
    """
    dest = paths.sample_measure if basis else paths.measure
    check_basis_dest(dest, basis, "measure.json", "measure.json")   # 同名不同目录：靠 paths 决定
    if not cfg["measure"].get("enabled", True):
        print("[measure] 已按配置关闭（[measure].enabled=false），跳过")
        return {}
    if dest.exists() and not force:
        doc = paths.read_json(dest) or {}
        print("[measure] 已存在，跳过（--force 重跑）：%s" % _rel(paths, dest))
        return doc
    t0 = time.monotonic()
    doc = analyze(cfg, paths, media_path, basis=basis)
    paths.cache.mkdir(parents=True, exist_ok=True)     # 只建自己要写的那一层
    paths.write_json(dest, doc)
    print(summary_line(doc, time.monotonic() - t0, _rel(paths, dest)))
    return doc
