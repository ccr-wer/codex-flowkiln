# Flowkiln for Codex

> **Turn repeated Codex prompts into reviewable, draft `SKILL.md` files — offline, deterministic, and never auto-installed.**

把本地 Codex 会话里反复出现的提示词，离线、确定性地挖掘成**需要人工审阅的 `SKILL.md` 草稿**。它做四件事：采集（可选 hook，写入前脱敏）、挖掘（TF-IDF 余弦 + 共享实词双判据聚类，零模型调用、零联网）、**把候选清单贴进对话里请人裁决**、按裁决晋升或删除。产物永远是带 `[TODO]` 标记的草稿，不自动安装、不覆盖已有 skill，也不内置任何具体业务流程。

从重复的痕迹里，烧出可复用的能力——但清单摆上桌、由人决定要不要烧；说不要的，当场从清单里划掉。

---

## 当前状态

**Skeleton / pre-alpha（v0.1.1-skeleton）。** 开放出来是为了让人审阅机制本身，不是宣称它已经能在你的 Codex 上跑通。

| 部分 | 状态 |
| --- | --- |
| 引擎（`collect` / `mine` / `list` / `dismiss` / `promote` / `selftest`） | 已实现；33/33 自测通过；中文短提示词链路已实测 |
| 对话审阅环节（清单进对话、用户点头/拒绝） | 已实现并实测：拒绝后再次挖掘不再复现 |
| Codex 挂载层（hooks 配置、`.agents/skills/` 发现） | 按公开规范编写，**尚未在真实 Codex 环境验证** |
| 具体业务工作流（日报、开发日志等） | 刻意不内置 |
| 全自动晋升（识别后直接写入并挂载） | 刻意不做 |

**请不要在未验证挂载层之前，把本仓库当作开箱即用的工具。**

## 它解决什么问题

Codex 一类工具通常能记录记忆、推荐技能，但从「一堆重复的痕迹」到「一份可复用的 skill」之间，缺一段**可审计、可复现、不偷偷改你文件**的桥。更常见的做法是把候选丢成一个 JSON 文件，让使用者自己去翻——那等于没审。

Flowkiln 把清单直接摆在对话里：重复了什么、出现几次、长什么样，一眼可见；要的点头，不要的说一声就从清单里删掉。判定过程零依赖、零联网、确定性可复现，「要不要变成 skill」的判断权明确留给人。同一套挖掘内核后续可复用于 DSH 侧适配层，但本仓库当前只覆盖 Codex。

## 四段流程

```
Codex hook (可选) 或手工 JSONL
        │
        ▼
[1] collect   stdin JSON → ~/.codex-flowkiln/prompts.jsonl   写入前脱敏
        │
        ▼
[2] mine      jsonl → candidates.json                        离线、确定性、零 token
        │      判据：TF-IDF 余弦 ≥ 0.72  或  共享实词数 ≥ 3；簇至少 3 条；并查集聚类
        │      已拒绝的簇自动跳过，不再出现在清单里
        ▼
[3] review    list → 候选清单贴进对话                       人在对话里裁决
        │      每条给出 id / 次数 / 关键词 / 样本，等用户表态
        ▼
[4] act       点头 → promote（默认 dry-run，--apply 才写盘）
              拒绝 → dismiss（从清单删除 + 记录指纹，之后不再复现）
```

`mine` 不会调用模型，相同输入必然得到相同的簇和相同的候选 ID。清单包含重复次数、共享关键词、可变槽位、工具签名与最多 5 条样本提示词，便于判断「高频是否等于值得沉淀」。

**关键设计：拒绝是一等操作。** `dismiss` 不只是把条目从文件里删掉——同时记录该簇的骨架词指纹，下次挖掘命中同一指纹就跳过。否则同一簇会在每次 `mine` 后卷土重来，审阅就成了空话。

## 与相邻方案的定位差异

以下依据各项目公开 README 的自述（截至 2026-11），非独立评测：

| 项目 | 生态 | 晋升策略 |
| --- | --- | --- |
| [codex-skill-factory](https://github.com/coreline-ai/codex-skill-factory) | Codex | 生成后必须人工 approve，批准前不创建、不激活 |
| [codex-self-improving-loop](https://github.com/newcatshuang/codex-self-improving-loop) | Codex | 默认只产候选，显式晋升 |
| [dsh-retro](https://github.com/liaoli-hero/dsh-retro) | DSH | 默认全自动：重复达到阈值即注册技能 |
| **Flowkiln（本仓库）** | Codex（DSH 为规划） | 清单进对话，人点头才写盘、说拒绝即从清单删除；不覆盖；只出草稿 |

一句话：**本仓库不是要做得更自动，而是要做得更可审计——审阅发生在对话里，拒绝会被记住。**

## 快速开始

要求：Python 3.8+，Windows / macOS / Linux，仅标准库，无 `pip install`、无网络请求。

```bash
# 0. 先自测；不通过就不要信任后续输出
python scripts/wfminer.py selftest          # 期望 33/33 checks passed

# 1. 采集：安装 hook 后自动进行，或手工按同一 JSONL 形状写入 prompts.jsonl
#    {"ts":"2026-01-01T00:00:00Z","event":"UserPromptSubmit","session_id":"s1","tool":"","text":"..."}

# 2. 挖掘：离线、确定性，已拒绝的簇自动跳过
python scripts/wfminer.py mine
python scripts/wfminer.py mine --json       # 查看候选详情
#    可调：--threshold 0.72（余弦）  --shared-keywords 3（共享实词）  --min-count 3

# 3. 审阅：把清单贴进对话，等用户表态
python scripts/wfminer.py list

# 4a. 用户点头 → 晋升：默认 dry-run，只报告落点、不写文件
python scripts/wfminer.py promote <candidate-id>
python scripts/wfminer.py promote <candidate-id> --apply

# 4b. 用户说不要 → 从清单删除（并记指纹，之后不再复现）
python scripts/wfminer.py dismiss <candidate-id> --reason "用户说不需要"
```

数据目录可用环境变量 `WFM_DATA_DIR` 覆盖，便于测试和隔离。

## 跨平台注意事项

引擎本身是纯 Python 标准库，Windows / macOS / Linux 通用。Windows 上已实测（33/33）；macOS / Linux 按代码审查应无差异，但未逐一真机验证。装 hook 时有三个常见坑：

1. **命令名不统一**：`hooks.json` 示例写的是 `python`。Linux / macOS 上常用 `python3`，Windows 上可能只有 `py` 启动器——按本机实际情况改。
2. **Windows 路径转义**：绝对路径写进 JSON，反斜杠要转义（写作 `C:\\path\\to\\wfminer.py`），或直接用正斜杠（Windows 同样接受：`C:/path/to/wfminer.py`）。
3. **控制台中文乱码**：Windows 默认 GBK 终端下，`list` 输出的中文样本可能显示乱码（写盘始终是 UTF-8，数据不受影响）。跑之前设 `set PYTHONIOENCODING=utf-8` 或 `chcp 65001`；macOS / Linux 通常无此问题。

## 安装 hook（可选）

1. 把 `hooks/hooks.json` 中的 `ABSOLUTE_PATH` 替换为 `scripts/wfminer.py` 的真实绝对路径。
2. 复制到 `~/.codex/hooks.json`（用户级）；项目级为 `<repo>/.codex/hooks.json`，仅在 `.codex/` 被信任时加载。
3. 在 `~/.codex/config.toml` 的 `[features]` 下设置 `hooks = true`。旧键名 `codex_hooks` 已弃用。
4. 重启 Codex 生效。

## 挂载 skill（关键）

Codex 从 **`.agents/skills/`** 扫描 skill，**不是** `.codex/skills/`——后者是从其他工具迁移时最常见的错误，放进去不会被发现。

- 用户级：把整个 `codex-flowkiln/` 放到 `~/.agents/skills/` 下。
- 项目级：放到 `<repo>/.agents/skills/` 下。
- `SKILL.md` frontmatter 的 `name` 必须与父目录名完全一致，否则加载失败且常常没有明确报错。
- 装完用 `/skills` 验证是否列出。

## 数据与隐私

- 采集记录：时间戳、事件名、会话 ID、工作目录、工具名、提示词文本，落在 `~/.codex-flowkiln/prompts.jsonl`。
- 候选清单 `candidates.json`、拒绝记录 `dismissed.json`（含骨架词指纹、拒绝原因、时间）同目录。
- 写入前做正则脱敏（OpenAI key、GitHub PAT、Bearer token、`api_key` / `secret` / `token` 赋值、邮箱地址），单条文本截断到 4000 字符。**但这不是安全边界**——不要向采集链路输入含敏感信息的提示词。
- 引擎不联网、不调用模型、不安装依赖；`promote` 默认 dry-run，`--apply` 才写盘，且从不覆盖已有文件。
- 启用第三方 hook 意味着允许脚本在本机执行。代码全在 `scripts/wfminer.py`，启用前建议通读一遍。

## 验证状态与规范依据（as-is disclosure）

- 引擎自测 33/33 通过；另以 4 条中文提示词做过端到端演练：正确聚出 1 个候选（count=3），无关条目未被误并；`list` 清单可直接呈现；`dismiss` 后再次挖掘为 0 候选并提示 `skipped 1 dismissed cluster(s)`，拒绝确实被记住。字符级 TF-IDF 对中文短句曾出现相似度偏低，现已由「共享实词数」第二判据修正。
- **挂载层未做真机验证**：编写时不具备可用的 Codex 运行环境，hooks 事件与 skill 发现行为均未经实际运行确认。
- hook 事件 / I/O 形状、skill frontmatter 与扫描目录依据来自社区维护的 Codex 约定页及官方 build-skills 页的公开镜像；部分网络环境下官方文档站不可直接访问，且官方材料在 skill 安装目录与 hook 生效时机上口径不完全一致。**一切挂载相关说明以你本机 Codex 版本的实际行为为准。**

## 已知边界与风险（as-is disclosure）

- 这是骨架，生成物是**草稿**而非已验证的流程，含 `[TODO]` 标记，必须人工补全后才能使用。
- 聚类基于文本相似度，不是语义理解：同义改写可能漏掉，闲聊可能被聚成一簇。
- 默认只采集 `UserPromptSubmit`。要扩展到工具调用序列（`PreToolUse` / `PostToolUse`），需自行在 hooks 配置中增加事件；引擎的 `collect` 已能解析 `tool_name` / `tool_input`，但本骨架未默认开启。
- 重复 ≠ 重要。一条高频一句话不一定值得变成 skill，判断权在审阅者——这也是清单必须进对话的原因。
- 本仓库未内置任何具体业务工作流，也未启用无人值守的自动晋升。

## 路线图

1. 在真实 Codex 环境验证 hook 采集与 `/skills` 挂载，修正规范偏差。
2. 可选：把聚类依据从提示词文本扩展到工具调用序列骨架。
3. 抽出共享 `mine()` 内核，产出 DSH 适配层 `dsh-flowkiln`（走 `ctx.skills.registerProvider`）。

## 贡献

欢迎 issue 与 PR。报告问题时请附 `selftest` 输出、Python 版本、操作系统，以及**脱敏后**的复现数据。涉及自动写盘、自动挂载或自动裁决（promote/dismiss）的行为变更，请保持「人在对话里点头」这条底线，并在 PR 中明确说明副作用。
