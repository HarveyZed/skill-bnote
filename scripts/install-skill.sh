#!/usr/bin/env bash
# 把 bnote 安装为 DSH skill：**整目录复制**（自包含：SKILL.md + references/ + scripts/ + config/ + 代码）
# 不复制运行期目录（env/ cache/ out/ logs/）与机器专属配置（config/local.toml）——这与 ffmpeg/venv 不随 skill 走同理。
#
# 用法： bash scripts/install-skill.sh [--dest=<目录>] [--with-local] [--deploy-local] [--dry-run]
#   --dest=<目录>  同步到指定目录（默认 $DSH_HOME/skills/bnote）；其它 harness 用它把 skill 装到自己的 skill 根
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_ROOT="${DSH_HOME:-$HOME/.dsh}/skills"
DEST="$DEST_ROOT/bnote"
WITH_LOCAL=0
DRY=0
DEPLOY_LOCAL=0
for a in "$@"; do
  case "$a" in
    --dest=*) DEST="${a#--dest=}" ;;
    --with-local) WITH_LOCAL=1 ;;
    --deploy-local) DEPLOY_LOCAL=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "未知参数: $a" >&2; exit 2 ;;
  esac
done

# ---- 发布纪律：版本号必须在 CHANGELOG 里有对应小节（本地有该文件时才校验）----
# CHANGELOG.md 是**作者本地的发布日记**：不进公开仓库、也不随 skill 安装。
# 版本一致性以 SKILL.md 的 metadata.version 与 pyproject.toml 的 version 为准。
VERSION="$(sed -n 's/^[[:space:]]*version:[[:space:]]*//p' "$REPO/SKILL.md" | head -1)"
NAME="$(sed -n 's/^[[:space:]]*name:[[:space:]]*//p' "$REPO/SKILL.md" | head -1)"
[ -n "$VERSION" ] || { echo "SKILL.md 缺少 metadata.version" >&2; exit 1; }
[ -n "$NAME" ] || { echo "SKILL.md 缺少 name" >&2; exit 1; }
if [ -f "$REPO/CHANGELOG.md" ]; then
  if ! grep -q "^## \[$VERSION\]" "$REPO/CHANGELOG.md"; then
    echo "✗ CHANGELOG.md 里没有 ## [$VERSION] 小节 —— 先写 release note 再安装" >&2
    exit 1
  fi
else
  echo "（本地没有 CHANGELOG.md：跳过发布日记校验；对外变更请写进 GitHub Releases）"
fi
# 版本一致性：SKILL.md 的 metadata.version 与 pyproject.toml 的 version 必须相同（规则见 references/maintenance.md）
PYV="$(sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "$REPO/pyproject.toml" | head -1)"
if [ "$PYV" != "$VERSION" ]; then
  echo "✗ 版本不一致：SKILL.md metadata.version=$VERSION，pyproject.toml version=$PYV —— 改成同一个值再安装" >&2
  exit 1
fi
# CHANGELOG 顶部应当是本次版本（否则多半是漏写小节、写到了别处）；本地没有该文件就跳过
if [ -f "$REPO/CHANGELOG.md" ]; then
  TOP_SEC="$(sed -n 's/^## \[\([^]]*\)\].*/\1/p' "$REPO/CHANGELOG.md" | head -1)"
  [ "$TOP_SEC" = "$VERSION" ] || echo "⚠ CHANGELOG 顶部小节是 $TOP_SEC，不是 $VERSION —— 确认是不是漏写了本次小节" >&2
fi

EXCLUDES=(--exclude=./env --exclude=./cache --exclude=./out --exclude=./logs
          --exclude=./auth --exclude=./.git --exclude=__pycache__ --exclude=*.pyc
          --exclude=*.egg-info --exclude=./.bnote
          # 仓库内运维/开发用文件不进 skill（使用者不需要，且运行时不依赖）
          --exclude=./HANDOFF.md --exclude=./scripts/dev --exclude=./.gitignore
          # 检查报告与检查用临时目录：仓库内产物，使用者不需要（.bnote-review 正常在仓库外，这里只作保险）
          --exclude=./SKILL-REVIEW.md --exclude=./.bnote-review
          # 发布日记：不进公开仓库，也不随 skill 安装
          --exclude=./CHANGELOG.md
          # 维护者工具：留在仓库里，使用者不需要（入口与建环境脚本仍在发行集合内）
          --exclude=./scripts/install-skill.sh --exclude=./scripts/gen-references.py)
[ "$WITH_LOCAL" -eq 1 ] || EXCLUDES+=(--exclude=./config/local.toml)

if [ "$DRY" -eq 1 ]; then
  echo "[dry-run] $REPO → $DEST"
  tar -C "$REPO" "${EXCLUDES[@]}" -cf - . | tar -tf - | head -30
  exit 0
fi

mkdir -p "$DEST"
# 清掉旧副本再整目录铺（避免删掉的文件残留）
find "$DEST" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
tar -C "$REPO" "${EXCLUDES[@]}" -cf - . | tar -C "$DEST" -xf -
chmod +x "$DEST/scripts/"*.sh "$DEST/scripts/bnote" 2>/dev/null || true

echo "installed : $DEST（自包含：SKILL.md + references/ + scripts/ + config/ + 代码）"
echo "version   : $NAME $VERSION"
echo "files     : $(find "$DEST" -type f | wc -l) 个"
echo "md5 repo  : $(md5sum "$REPO/SKILL.md" | cut -d' ' -f1)"
echo "md5 inst  : $(md5sum "$DEST/SKILL.md" | cut -d' ' -f1)"
# 已装副本需要自己的 local.toml：解释器（系统 python3 通常没装依赖）与数据根
if [ "$DEPLOY_LOCAL" -eq 1 ]; then
  PY_LOCAL="$(cd "$REPO" && ([ -x env/bin/python ] && echo "$REPO/env/bin/python" || echo python3))"
  ROOT_LOCAL="$(sed -n 's/^[[:space:]]*root[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "$REPO/config/local.toml" 2>/dev/null | head -1)"
  [ -n "$ROOT_LOCAL" ] || ROOT_LOCAL="$PWD/.bnote"
  mkdir -p "$DEST/config"
  {
    echo "# 已装 skill 的本机配置（机器专属，不随 skill 复制到别的机器）"
    echo "# 由 install-skill.sh --deploy-local 生成："
    echo "[tools]"
    echo "python = \"$PY_LOCAL\""
    echo ""
    echo "[paths]"
    echo "root = \"$ROOT_LOCAL\""
  } > "$DEST/config/local.toml"
  echo "本机配置  : $DEST/config/local.toml（python=$PY_LOCAL, root=$ROOT_LOCAL）"
fi
echo "入口      : $DEST/scripts/bnote   （解释器在 config/local.toml 或 BN_PYTHON 里配）"
echo
if [ -f "$REPO/CHANGELOG.md" ]; then
  echo "--- release note: $VERSION ---"
  awk -v v="## [$VERSION]" 'index($0,v)==1{f=1} f&&/^## \[/&&index($0,v)!=1{exit} f{print}' "$REPO/CHANGELOG.md" | head -40
else
  echo "--- 本地没有 CHANGELOG.md：对外变更请写进 GitHub Releases（https://github.com/HarveyZed/skill-bnote/releases）---"
fi
