#!/usr/bin/env bash
# 首次部署：建 venv + 装依赖（venv 不随 skill 走，所以新机器上要跑一次）。
# 用法： bash scripts/setup-env.sh [venv 路径]     默认 <skill 根>/env
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${1:-$REPO/env}"
PY="${PYTHON:-python3}"
PIP_INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

echo "[setup] skill 根 : $REPO"
echo "[setup] venv     : $VENV"
"$PY" - <<'EOF'
import sys
assert sys.version_info >= (3, 10), "需要 Python >= 3.10，当前 %s" % sys.version.split()[0]
print("[setup] python   :", sys.version.split()[0])
EOF

[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q -i "$PIP_INDEX" -e "$REPO"
echo "[setup] 依赖已装（含 imageio-ffmpeg，ffmpeg 由它提供）"

# 把解释器写进 config/local.toml，避免每次手输
LOCAL="$REPO/config/local.toml"
if ! grep -q "^[[:space:]]*python[[:space:]]*=" "$LOCAL" 2>/dev/null; then
  mkdir -p "$(dirname "$LOCAL")"
  printf "\n[tools]\npython = \"%s/bin/python\"\n" "$VENV" >> "$LOCAL"
  echo "[setup] 已写入 $LOCAL 的 [tools] python"
fi

echo "[setup] 自检："
"$VENV/bin/python" -m bnote config --paths
echo "[setup] 完成。下一步： scripts/bnote auth status   （拿官方 AI 字幕需要登录态）"
