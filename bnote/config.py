"""配置装载： default.toml  <  local.toml  <  环境变量 BN_<SECTION>_<KEY>  <  CLI 覆盖。

所有层只通过 config dict 取参数，不在代码里写死路径和阈值 —— 方便移植与迭代。

路径三分（v0.7.0，为"skill 可移植"服务）：
  1) skill 根：代码 + 契约模板 + 默认配置，随 skill 一起搬走（PROJ_ROOT）；
  2) 数据根：state / cache / out 三个根，**默认跟随 cwd**（<cwd>/.bnote），绝不写死绝对路径；
     解析顺序 BNOTE_ROOT > config.paths.root（BN_PATHS_ROOT）> <cwd>/.bnote；
  3) 解释器：config.tools.python（BN_PYTHON 覆盖），venv 与 ffmpeg 不随 skill 走。
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib as _toml
except ModuleNotFoundError:  # py3.10
    import tomli as _toml

PKG_DIR = Path(__file__).resolve().parent
PROJ_ROOT = PKG_DIR.parent          # skill 根（SKILL.md / references/ / config/ 所在）
DEFAULT_TOML = PROJ_ROOT / "config" / "default.toml"


def skill_root() -> Path:
    return PROJ_ROOT


def _deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _coerce(raw: str, ref):
    if isinstance(ref, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(ref, int):
        return int(raw)
    if isinstance(ref, float):
        return float(raw)
    if isinstance(ref, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def _env_overrides(cfg: dict) -> dict:
    """BN_MEDIA_MAX_HEIGHT=720 -> cfg['media']['max_height']"""
    for name, raw in os.environ.items():
        if not name.startswith("BN_"):
            continue
        parts = name[3:].lower().split("_", 1)
        if len(parts) != 2:
            continue
        sec, key = parts
        if sec in cfg and isinstance(cfg[sec], dict) and key in cfg[sec]:
            try:
                cfg[sec][key] = _coerce(raw, cfg[sec][key])
            except ValueError:
                pass
    return cfg


def data_root(cfg: dict) -> Path:
    """数据根：BNOTE_ROOT > config.paths.root > <cwd>/.bnote（默认永远是相对 cwd 的）。"""
    raw = os.environ.get("BNOTE_ROOT") or (cfg.get("paths", {}).get("root") or "")
    p = Path(str(raw)).expanduser() if str(raw).strip() else Path.cwd() / ".bnote"
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


def _pick(raw, base: Path, default: str) -> str:
    """空 -> base/default；相对 -> base/相对；绝对 -> 原样。"""
    if str(raw or "").strip():
        p = Path(str(raw)).expanduser()
        return str(p if p.is_absolute() else (base / p))
    return str(base / default)


def load(config_path: str | None = None, overrides: dict | None = None) -> dict:
    cfg: dict = {}
    _deep_merge(cfg, _toml.loads(DEFAULT_TOML.read_text(encoding="utf-8")))
    local = Path(config_path) if config_path else PROJ_ROOT / "config" / "local.toml"
    if local.exists():
        _deep_merge(cfg, _toml.loads(local.read_text(encoding="utf-8")))
    _env_overrides(cfg)
    if overrides:
        _deep_merge(cfg, {k: v for k, v in overrides.items() if v is not None})

    p = cfg.setdefault("paths", {})
    root = data_root(cfg)
    p["root"] = str(root)
    p["skill_root"] = str(PROJ_ROOT)
    p["state_root"] = _pick(p.get("state_dir", ""), root, "state")
    p["cache_root"] = _pick(p.get("cache_dir", ""), root, "cache")
    p["out_root"] = _pick(p.get("out_dir", ""), root, "out")
    p["log_root"] = _pick(p.get("log_dir", ""), root, "logs")
    # 契约模板相对 **skill 根**（不是数据根）：它属于 skill 资产
    p["contracts_dir"] = _pick(p.get("contracts_dir", ""), PROJ_ROOT, "references/contracts")

    t = cfg.setdefault("tools", {})
    t["python"] = os.environ.get("BN_PYTHON") or t.get("python") or "python3"
    return cfg


def describe(cfg: dict) -> list[str]:
    """把解析后的路径摆出来（每条命令都打印，路径搞错立刻可见）。"""
    p = cfg["paths"]
    return [
        "skill    : %s" % p["skill_root"],
        "data root: %s" % p["root"],
        "  state  : %s" % p["state_root"],
        "  cache  : %s" % p["cache_root"],
        "  out    : %s" % p["out_root"],
        "contracts: %s" % p["contracts_dir"],
        "python   : %s" % cfg.get("tools", {}).get("python"),
    ]
