# 参与开发（CONTRIBUTING）

本文件面向要修改 bnote 的贡献者。使用说明见 [README.md](README.md)，运行流程与门禁见 [SKILL.md](SKILL.md)。

## 开发环境

```bash
git clone https://github.com/HarveyZed/skill-bnote.git && cd skill-bnote
bash scripts/setup-env.sh          # 建 venv 并 pip install -e .
scripts/bnote config --paths       # 确认解释器与数据根
```

运行期数据不在仓库里（默认 `<cwd>/.bnote`）。请勿把数据根、`cache/`、`out/`、登录态或任何本机路径提交进仓库。

## 代码结构

```
bnote/layers/*.py      流水线各层（auth/meta/media/subtitle/frames/segment/ocr/bundle/merge/body/loop/manifest/prompt/profile/glossary/remap/scaffold/note/text/xref）
bnote/segmenters/      切片算法（stable 为默认，scene 为基线对照）
bnote/backends/        字幕后端（bili / file / whisper）
references/contracts/  派单契约模板（渲染进任务书，占位符由 prompt 层填充）
references/schema/     manifest 结构与正文格式契约（由脚本生成）
config/default.toml    全部默认参数
scripts/               入口（bnote）、环境准备（setup-env.sh）、文档生成（gen-references.py）、发布（install-skill.sh）
```

设计约定：层与层只通过文件通信；阈值与路径一律进 `config/default.toml`（可被 `config/local.toml` 或 `BN_<段>_<键>` 覆盖）；结构能判定的部分由 `bnote check` 强校验，内容判断交给契约与写手。

## 写法约定

- **术语**：讲义（幻灯片模式的 `lecture.md`）／整理稿（信息流模式的 `lecture.md`）／字幕原文（`transcript.md`）／章节正文（`chapters/NN-slug.md`）／小节（幻灯片模式的 `##` 单位）／段落（信息流模式的 `##` 单位）／锚点（小节标题里的 `[HH:MM:SS]`）／写手（子代理）／编排者（负责编排的会话）。同一概念全文只用一种叫法。
- **表达**：陈述句 = 条件 + 动作；阈值带单位与依据；不使用第一人称叙事（「我踩过」「实测发生过」改写为客观陈述）。
- **注释**：模块 docstring 写「这一层做什么 / 输入输出 / 为什么这样设计」；函数注释写前置条件、副作用与失败行为。
- **不要在会被读进提示词的文本里写双花括号占位符**（只有 `references/contracts/` 这类模板文件例外）。

## 改动流程

1. 一处逻辑改动一个提交，提交信息写清「改了什么 + 为什么」；
2. 改了产物结构或契约后，跑 `python scripts/gen-references.py` 刷新 `references/params.md` 与 `references/schema/*`，并确认无差异残留；
3. 改了对外行为，要同步 `SKILL.md`、`README.md`，并按下面的版本规则升版本；
4. 提交前自查：`python -m compileall bnote`；有可用数据根时跑 `scripts/bnote check <URL> --page N`。

## 版本与发布

- `SKILL.md` 的 `metadata.version` 与 `pyproject.toml` 的 `version` **必须相同**（`scripts/install-skill.sh` 会校验，不一致直接拒绝安装）；
- 对外变更写进 **GitHub Releases**（不使用仓库内的 CHANGELOG：那是个人的发布日记，不进公开仓库、也不随 skill 安装）；
- 主版本 = 产物结构不兼容；次版本 = 新增能力或流程；修订号 = 修 bug 与调参；
- `bash scripts/install-skill.sh --deploy-local` 会把整目录同步到 `$DSH_HOME/skills/bnote`（DSH 专属路径，其它 harness 复制到自己的 skill 根目录即可）。

## 反馈

缺陷与需求走 [Issues](https://github.com/HarveyZed/skill-bnote/issues)。提 issue 时请附：复现命令、`bnote config --paths` 的输出、以及 `<数据根>/out/<vid>/_meta/` 下 `validation.json` 的相关片段；**贴之前先去掉本机路径与个人信息**。
