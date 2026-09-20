# DSH 环境注记（仅适用于 DeepSeek Harness 环境）

> 本文件记录**本机 DSH（0.1.2-rc.1）**特有的约束与绕法。换 harness、或 DSH 修好该缺陷后，本节内容即可忽略。
> 通用规则在 `SKILL.md` §2.3 / §2.4，不要把这些环境细节写进通用契约。

## 一、委派工具：spawn 与 fork

| 工具 | provider | 子代理看到的上下文 | 后台模式 |
|---|---|---|---|
| `subagent` | spawn | **全新空对话**（任务书必须自足） | continuable（可 `send_message` 继续对话） |
| `subagent_fork` | fork | **继承父级全部已完成轮次** | continuable |
| `subagent_codex` / `subagent_claude_code` | codex / claude-code | 由外部进程管理 | one-shot，默认 `disabled: true` |

- 预设 `standard` / `ptc` / `cordis` **都接线了 spawn 与 fork**；`minimal` 一个都没有。
- `subagent` 开了 `modelSelectionSettings`；`subagent_fork` 故意不开（保持与父级同 provider/model 以便复用 KV Cache）。
- 另有两条也会起子代理的路径：`workflow`（脚本编排批量）与 `ralph`（每轮全新 agent），两者都是 spawn 语义；
  但 **PTC 模式按设计不提供 `workflow`**。
- 控制层：`send_message`（双向 steer）/ `interrupt_agent`（只停当前轮）/ `list_agents`。

## 二、已知上游缺陷：走「chip 切换预设」的会话拿不到 `subagent`

**症状**：按 SKILL §2.3 派单时发现「没有可用的全新上下文子代理」，只剩 fork。

**现象**：这类会话的工具面只有 27 个（少一个 `subagent`），只剩 `subagent_fork`；直接以目标预设创建的会话是 28 个、齐全。

**判别条件（不是工作区、也不是"PTC 模式"本身）**：

| 会话怎么拿到预设 | 工具数 | `subagent` |
|---|---|---|
| 直接以目标预设创建（默认预设就是它、或 agent 派生） | 28 | ✅ |
| 先在**另一个预设**下形成空白会话，再用「新建会话界面 chip」切到目标预设（默认预设若为 `standard`，2026-09-19 起本机已改为 `ptc`） | 27 | ❌ |

- **旧会话不会自愈**（同一进程内继续对话仍缺；重启 DSH 后是否恢复未验证）。
- 影响仅限"少一个工具"：这些会话只能派 fork 型子代理。

**自检**：

```bash
# 1) 该会话是否走了「切换预设」路径（有事件＝有风险）
zstd -dc <DSH_HOME>/sessions/<ws>/<session>/session.jsonl.zstd | grep -c "agent-preset/selected"

# 2) 该会话的模型可见工具面（PTC 下数 SDK 里 tools.* 的键数），或看 request/header 的 system
```

**绕法**：在「设置 → preset 名单 → 设为默认值」把默认预设改成你想用的那个，再**新建**会话；不要用 chip 切换。

**上游状态**：Discussions #6166（含源码级根因：各预设的 `tool-subagent` 实例独立监听 `tools/change`，重名冲突导致
两个工具都没装上，失败的 fiber 被当作已安装阻止重试）与 #6339 已记录同缺陷；Changelog 无修复记录，
0.1.5-rc.2 与 0.1.6-alpha.2 均未修；Issues 已关闭，官方要求走 Discussions（推荐只去点赞，不必新开帖）。

## 三、PTC（run_code）模式的特点

- 模型顶层只看到一个工具 `run_code`，其余工具都在生成的 SDK 里以 `tools.xxx()` 调用；
- 因此「当前会话有没有某个工具」必须**打印 SDK 键集**或看 `request/header` 的 `system`，不能靠直觉判断；
- `workflow` 在 PTC 模式不提供。
- SKILL.md §2.3 保留了一条简短踩坑提示（拿不到 spawn 型子代理时怎么判别、不该用什么顶替），细节在本文件。

> 关于 fork 子代理：把父会话历史一并交给它，**在父会话本身是做开发/编排时**才会出现"沿用编排者角色、去动仓库"这类越界
> （本项目开发期实测）；普通使用场景下的主要代价是子代理上下文变大、每轮成本变高。不要把它当成 fork 的普遍定律。

## 四、不要用 `dsh --profile headless` 当子代理兜底

它功能上能跑（能读 skill、能写数据根），但：

1. 它**不是子代理**，而是一次性顶层进程 —— 每次运行都会在会话列表里留下一个独立会话（无 `parentSession`、无 `agentPreset`），
   无法在任务面板追踪或 steer；实测一次实验留下 5 个；
2. 对用户不透明（只有一行输出）；
3. 每次都要 boot 整个 harness，还要单独铺一份 `DSH_HOME`（含 profile 目录与凭据符号链接）。

正解是让会话拿到 spawn 型子代理（见第二节绕法），或改用非 PTC 会话。

## 五、ffmpeg：静态包 vs 系统版（2026-09-19 实测）

**典型现状**：机器上没有系统 ffmpeg，只有 `imageio-ffmpeg` 自带的静态构建（`ffmpeg-linux-x86_64-v7.0.2`，johnvansickle，`file` 报「不是动态可执行文件」）。
实测它对**域名**直接段错误，纯 IP 正常：

```
$FF -i http://127.0.0.1:8899/x.txt  → exit=8（404，正常）
$FF -i http://localhost:8899/x.txt  → exit=139（段错误，连续复现）
$FF -i http://example.com/          → exit=139
（同一沙箱里 python 的 getaddrinfo 解析 example.com 正常 → 是静态 glibc 的 NSS 解析问题，不是沙箱没网）
```

凡需域名解析的 ffmpeg 用法都不可用 → yt-dlp 的 `--sections`、HLS、音视频合并全废（`run --sections` 报 `ffmpeg is not installed` 或直接崩）。

**环境侧解决（2026-09-19）**：`sudo apt-get install -y --no-install-recommends ffmpeg` → 8.0.1 动态版；
之后 `ffmpeg -i http://example.com/` 返回 183（正常报「不是媒体」），`find_ffmpeg` 直接返回 `/usr/bin/ffmpeg`，
用**已装 skill** 跑一次 `run --sections 00:00:00-00:03:00` 可完整通过（下载 → 字幕 → 帧 → 切片 → bundle）。

**沙箱注意**：workspace-write 下 `sudo` 会被 `no-new-privileges` 拦住，装包要让用户批准放宽权限；`sudo` 也不接受外部传入 `DEBIAN_FRONTEND`。

## 六、本地 ASR 与网络（2026-09-19 实测，播客试点）

| 现象 | 绕法 |
|---|---|
| `huggingface.co` 直连不可达（curl 返回 000），whisper 首次加载报 `ConnectError: [Errno 101] Network is unreachable` | `HF_ENDPOINT=https://hf-mirror.com`（镜像 200，模型可下） |
| 沙箱下 HF 缓存目录只读：`[Errno 30] Read-only file system: ~/.cache/huggingface/...` | `HF_HOME=<工作区>/.cache/huggingface` |
| faster-whisper medium 模型 1.5 GB，首次下载 63s（镜像）；之后缓存命中、加载 3.9s | 一次性成本 |
| 沙箱内无 `/dev/nvidia*` → whisper 只能 CPU | 48 线程 CPU 上实测 medium/int8/16 线程 ≈ **5.6× 实时**（153 分钟音频约 27.6 分钟）；慢机器可用 `--whisper-model small` 或调 `whisper_threads` |
| **yt-dlp 会写回 `--cookie-file` 指向的文件**（刷新 cookie 有效期） | 跨数据根复用登录态时会改到旧根的文件（内容通常不变、mtime 变）；介意就先复制一份 cookie 再指过去 |
| ffmpeg | `/usr/bin/ffmpeg` 8.0.1 动态版正常，`--sections` 分段下载与音频切片都跑通，**没有**出现 imageio 静态包的域名段错误 |
| **沙箱 `/tmp` 每次 bash 调用都是新的 tmpfs** | 跨命令引用 `/tmp/x` 必然失败（例如「先复制 cookie 到 /tmp，下一条命令再引用」）；**复制与使用必须写在同一条 bash 调用里**——0.8.2 试点在这上面栽过一次 |
| 会话日志可只读解压，适合取证 | `zstd -dc ~/.dsh/sessions/*/*/session.jsonl.zstd` 在沙箱内可读；可用它独立核对子代理的行为（例：确认 writer 真的是「read NN → write NN」而不是一次性读完再写），比听自述可靠 |
| 上游字幕轨是**间歇性**的 | 同一天探到的「残轨 / 时间轴超片长 / subtitle_url 为空」在次日全部变成正常满覆盖轨（正常样本逐字吻合 ⇒ 接口与视频没变）。所以：覆盖度判据必须**每次实跑**、不能拿一次探测当结论；要做回归请用**合成 srt 夹具**（开发仓库里的 `guard_fixture.py`，不随 skill 发行） |

**同一轮的代码修**（0.7.5）：软链原先建在**代码目录**（`PROJ_ROOT/cache/_bin`，只读安装下必然失败且异常被 `media.py` 吞掉）→ 改到**数据根** `<root>/cache/_bin/ffmpeg`，并把失败原因如实抛出/打印。
