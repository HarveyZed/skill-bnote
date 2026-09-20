# 要改工具时看哪里 / 版本与发布

**什么时候读我**：要改工具代码、写作契约或参数，或要发布新版本（升 `metadata.version`、写 CHANGELOG、跑 install 脚本）时。

## 要改工具时看哪里

`bnote/layers/*.py` 按层解耦（auth / meta / media / subtitle / frames / segment / ocr / bundle / merge /
**body（时间行）** / **loop（派修循环）** / manifest / prompt / profile / glossary / remap / **xref（跨讲工作表）**）；
`bnote/segmenters/stable.py` 是切片核心；`references/contracts/*.md` 是写作契约模板；
`config/default.toml` 是全部参数（`BN_<段>_<键>` 可覆盖，参数表见 `references/params.md`）。
改契约或产物结构后跑 `scripts/gen-references.py` 刷新 `references/params.md` 与 `references/schema/*`（`references/dsh-notes.md` 是手写的，不受影响）。
`xref` 的前置条件是各集已有 `_meta/note_hooks.json`（即先跑过 `note`），否则钩子为空。
设计与取舍、已知问题清单、路线图见随 skill 一起安装的 `README.md`（`SKILL.md` 只讲怎么用）。

> 工具代码的修改（含子 agent 提交的）必须回流给编排者复核后再保留。

## 版本与发布

1. SKILL.md 的 `metadata.version` 升版本（主版本=产物结构不兼容；次版本=新增能力；修订号=修 bug 与调参），**同时把 `pyproject.toml` 的 `version` 改成同一个值**（两处必须一致）；
2. CHANGELOG.md 顶部加一节（**install 脚本会检查该小节存在，缺了直接拒绝安装**）；
3. `bash scripts/install-skill.sh` 整目录同步到你所用 harness 的 skill 根目录（DSH 是 `$DSH_HOME/skills/bnote/`）；脚本会打印版本、release note、文件数、两边 md5；
4. 确认仓库版与安装版一致。

> 仓库里的 SKILL.md 是唯一真源，安装目录那份是复制品。DSH 的 skill 目录**每次 load 都重读文件**，
> 所以改正文/新增 references、scripts 不需要重开会话；只有改 frontmatter 才等 catalog 刷新。
