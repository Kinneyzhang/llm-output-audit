# LLM Output Audit

[English](README.md) | [简体中文](README.zh-CN.md)

[![CI](https://github.com/Kinneyzhang/llm-output-audit/actions/workflows/ci.yml/badge.svg)](https://github.com/Kinneyzhang/llm-output-audit/actions/workflows/ci.yml)

> 跨 Agent 的 LLM 输出审计工具：检查事实准确性、幻觉风险、过时知识、内部矛盾、来源质量，并给出可执行修改建议。

LLM Output Audit 是一个可移植的审计工具包，用于在保存、发布或复用 AI 生成的研究报告、技术对比、使用指南、部署记录、README、博客和知识库页面之前，对内容进行系统性审查。它同时提供 Python CLI、stdio MCP server，以及面向 Hermes、Claude Code、Codex、OpenCode、Gemini 和通用 coding agent 的轻量适配入口。

它不是普通 RAG。RAG 的目标通常是“检索上下文并生成回答”。LLM Output Audit 的目标是“审计已有草稿”：抽取原子事实声明，按声明类型路由到最权威的数据源，收集证据，给出评级，并生成可执行的修订建议。

## 架构图

![LLM Output Audit 架构图](docs/architecture.svg)

这个流程把“起草”和“审计”分开：文本先转成有类型的 claims，再按审计模式筛选重点 claim，由 Source Router 选择证据渠道，并行取证，最后输出 verdict 和修改建议。

## Demo report

查看 [examples/demo-report.md](examples/demo-report.md)，可以看到一份示意性审计报告，包括 verdict 分区、routed sources、source quality 和 edit suggestions。

## 独立 Web UI

想直观看到技能服务效果时，可以启动本地页面：

```bash
python3 scripts/web_app.py --host 127.0.0.1 --port 8765 --clean
```

打开 <http://127.0.0.1:8765/>。页面提供：

- 原文输入框：直接粘贴要审查的文本
- 审查选项：`evidence_mode`、`claim_extractor`、`max_claims`
- 实时过程展示：通过 Server-Sent Events 依次显示 profile、claims、verification plan、evidence、verdicts、review queue、suggestions、patches、revised text、report
- 结果页签：总览、Claims、Evidence、修订文本、完整报告

这个 Web UI 底层调用 `scripts/audit_v2.py --write-revision`，临时运行结果保存在 `.audit-web-runs/`。

## 为什么需要它

大模型很擅长起草，但长文输出常见几类问题：

- **事实幻觉**：日期、数字、名称、归属、因果关系说得很自信，但其实错了。
- **知识过时**：项目状态、版本号、下载量、生态事实已经变化。
- **无来源声明**：说法看起来合理，但缺少可靠证据。
- **内部矛盾**：同一篇文章前后说法不能同时成立。
- **来源质量弱**：本该查官方 API 或一手资料，却只用了泛搜索摘要。

LLM Output Audit 的作用是给人和 Agent 一个可重复的审稿流程，避免未审草稿直接进入笔记、Wiki、博客、README 或公开文档。

## 它如何工作

```text
草稿文章
  ↓
抽取原子事实声明
  ↓
选择审计模式：fast / spot / draft / full / auto
  ↓
为每条声明路由到最合适的数据源
  ↓
并行查询证据源
  ↓
按权威度、直接性、新鲜度给证据打分
  ↓
必要时抓取网页原文
  ↓
给每条声明评级
  ↓
对高风险错误声明做条件式对抗复审
  ↓
生成包含修改建议的审计报告
```

## 核心能力

- **声明抽取**：把长文拆成可核实的 `[DATE]`、`[NUMBER]`、`[EVENT]`、`[ATTR]`、`[STATUS]`、`[CAUSAL]` 声明。
- **Source Router**：不同事实查不同权威源，而不是所有问题都丢给通用搜索。
- **专用数据源**：GitHub、Wikipedia、arXiv、Semantic Scholar、PyPI、npm、Tavily/DuckDuckGo，以及可选本地 LLM Wiki。
- **证据评分**：根据 authority、directness、freshness 和是否结构化 API 数据来排序证据。
- **审计模式**：fast、spot、draft、full、auto，在速度和准确性之间做选择。
- **并行验证**：claim 级并行和 source 级并行，可配置 worker 数。
- **风险门控一致性检查**：只有在文章存在时间线、状态变化、对比结构等高风险信号时才跑内部矛盾检查。
- **条件式对抗复审**：降低“误判为错误”的 false positive。
- **可执行修改建议**：不只是说“对/错”，还会给出替换、弱化、补引用、删除等建议。
- **可选本地知识库**：支持 LLM Wiki，但不依赖它；没有 LLM Wiki 也能运行。
- **跨 Agent 安装**：可以为 Hermes、Claude Code、Codex、OpenCode、Gemini 或通用 Agent 安装轻量适配入口，不复制核心 CLI。
- **MCP server**：把 `audit_file`、`audit_text`、`summarize_trace` 暴露为 stdio MCP 工具，供支持 MCP 的 Agent 直接调用。

## 审计模式

LLM Output Audit 不会对所有任务都跑同样深度。不同模式用于平衡速度和可靠性。

| 模式 | 适用场景 | 行为 |
| --- | --- | --- |
| `fast` | 普通低风险聊天 | 不跑完整审计；必要时只做人工/局部点查。 |
| `spot` | 高风险短事实回答 | 审计最多 3 条最重要声明，每条查 1–2 个数据源，不跑一致性/对抗复审。 |
| `draft` | 长期保存的草稿、内部报告、笔记、Wiki 页面 | 审计最多 12 条中高重要声明，风险门控一致性检查，条件式对抗复审。 |
| `full` | 公开发布、重要报告、用户明确要求深审 | 审计最多 50 条声明，更多来源，强制一致性检查，LLM router 和对抗复审。 |
| `auto` | 默认脚本模式 | 根据 claim 数量、文章长度和一致性风险自动推断模式。 |

推荐默认策略：

- 普通短回答：`fast`
- 高风险短事实：`spot`
- 研究报告 / 使用指南 / 技术对比：`draft`
- 博客 / README / 公开文档 / 重要报告：`full`

## 能审计哪些类型的声明？

当前实现已经使用显式 claim 类型：`[DATE]`、`[NUMBER]`、`[EVENT]`、`[ATTR]`、`[STATUS]`、`[FEATURE]`、`[REQUIREMENT]`、`[COMPAT]`、`[WORKFLOW]`、`[EVAL]`、`[CAUSAL]`。功能、需求、兼容性、工作流和评价类声明现在都是一等审计对象。

只要能对应到证据，LLM Output Audit 可以审计很多信息类声明：

| 声明类型 | 示例 | 处理方式 |
| --- | --- | --- |
| 数字 / 日期 | “某包每周下载 100 万次”、“项目 Y 在 2025 年发布” | 路由到包注册表、GitHub release、网页公告等。 |
| 功能 / 能力 | “库 X 支持 streaming responses”、“工具 Y 可以索引本地 Markdown 文件” | 路由到官方文档、README、源码、issue、包文档或项目网站。表示为 `[FEATURE]`。 |
| 需求 / 约束 | “这个包要求 Python 3.10+”、“这个服务需要 PostgreSQL” | 路由到包元数据、安装文档、README、pyproject/package.json 或部署文档。表示为 `[REQUIREMENT]`。 |
| 支持 / 兼容性 | “框架 X 支持 Vite”、“模型 Y 支持 OpenAI-compatible API” | 路由到官方文档、changelog、示例、集成文档或源码。表示为 `[COMPAT]`。 |
| 流程 / 工作流 | “这个工具先抽取 claims，再路由证据源” | 路由到本仓库源码、文档或示例。表示为 `[WORKFLOW]`。 |
| 对比类声明 | “A 比 B 更快/更可靠” | 路由到 benchmark、论文、官方文档或第三方评测；证据不足时通常给 `⚠️ UNCERTAIN`。表示为 `[EVAL]` 或 `[CAUSAL]`。 |
| 类观点声明 | “这是大多数团队的最佳选择” | 不当作客观真/假处理，而是检查是否有证据支撑、是否过度自信、是否缺少 caveat，必要时建议改写成观点。 |

所以它不是只能查数字和时间。真正的边界是**可验证性**：如果一个声明能通过文档、元数据、源码、benchmark、论文或可靠评论来核查，它就可以被审计。纯主观观点不能被证明为“真”，但可以被标记为过度自信、缺少证据或需要更谨慎表达。

## Source Router

不同事实有不同的一手来源。LLM Output Audit 的原则是：**问拥有事实的数据源**。

| 声明类型 | 优先证据源 |
| --- | --- |
| GitHub stars、release、项目活跃度 | GitHub API |
| npm 包版本或下载量 | npm registry / npm downloads API |
| Python 包版本或发布时间 | PyPI API |
| 包功能或能力 | 官方文档、README、源码、示例、issue 讨论 |
| 运行时需求或安装约束 | `pyproject.toml`、`package.json`、包元数据、安装文档 |
| API 兼容性或集成支持 | 官方文档、changelog、示例、源码、release notes |
| 论文元数据、发布时间 | arXiv API |
| 论文引用数、作者、venue | Semantic Scholar API |
| 组织、人物、历史背景 | Wikipedia / 官方网页 |
| 当前公告、生态新闻 | Tavily / DuckDuckGo 搜索 |
| 对比或评价类声明 | benchmark、论文、官方文档、独立评测 |
| 用户本地已整理知识 | 可选 LLM Wiki |

示例：

```text
声明：某 npm 包每月下载量超过某个数字
路由：npm → Tavily web
```

```text
声明：github.com/assafelovic/gpt-researcher 是开源项目
路由：GitHub → Tavily web
```

## 评级体系

每条被审计的声明会得到一个评级：

| 评级 | 含义 | 常见动作 |
| --- | --- | --- |
| ✅ `CONFIRMED` | 官方来源或多个可靠来源支持。 | 保留。 |
| 🟡 `LIKELY` | 一个可靠来源支持，未发现反证。 | 保留，最好补引用。 |
| ⚠️ `UNCERTAIN` | 来源冲突、来源较弱、或声明过宽。 | 弱化表达、补引用、人工复查。 |
| ❌ `WRONG` | 可靠证据明确反驳。 | 替换成正确表述。 |
| 🔍 `UNSOURCED` | 没找到相关证据。 | 删除、弱化，或标记需要引用。 |

## 安装

克隆仓库：

```bash
git clone https://github.com/Kinneyzhang/llm-output-audit.git
cd llm-output-audit
```

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

脚本刻意保持轻量：主要使用 Python 标准库编排，用 `requests` 做 HTTP 请求。

### 安装到不同 Agent

核心实现始终保留在本仓库。`scripts/install_agent_skill.py` 只安装轻量适配文件，让不同 Agent 能发现并调用同一个 CLI。

先 dry-run 预览：

```bash
python3 scripts/install_agent_skill.py --agent hermes --scope user --dry-run
python3 scripts/install_agent_skill.py --agent claude-code --scope user --dry-run
python3 scripts/install_agent_skill.py --agent codex --scope project --dry-run
python3 scripts/install_agent_skill.py --agent mcp --scope project --dry-run
```

安装示例：

```bash
# Hermes：把仓库 symlink 到 Hermes skill 目录
python3 scripts/install_agent_skill.py --agent hermes --scope user

# Claude Code：创建 Claude skill 文件，并向 CLAUDE.md 写入 marker 管理的说明块
python3 scripts/install_agent_skill.py --agent claude-code --scope user

# Codex / 通用 coding agent：向 AGENTS.md 写入 marker 管理的说明块
python3 scripts/install_agent_skill.py --agent codex --scope project
```

支持的 adapter：`hermes`、`claude-code`、`codex`、`opencode`、`gemini`、`generic`、`mcp`。

安全行为：

- 已存在的 `AGENTS.md` 和 `CLAUDE.md` 只会更新 marker block：`<!-- llm-output-audit:start --> ... <!-- llm-output-audit:end -->`。
- Hermes 默认使用 symlink，避免多副本漂移。
- 自定义目录用 `--target PATH`；不适合 symlink 时用 `--mode copy`；只有确认要替换非 marker 管理目标时才用 `--force`。

## MCP server

直接运行 stdio MCP server：

```bash
python3 scripts/mcp_server.py
```

它暴露四个工具：

- `audit_file`：审计本地 Markdown/text 文件，并写出审计报告和 trace log。
- `audit_text`：审计 MCP client 传入的文本；server 会先写入临时 Markdown 文件，再运行审计流程。
- `summarize_trace`：总结 JSONL trace log，方便另一个 Agent 复盘审计流程。
- `install_snippet`：返回连接这个 server 所需的 MCP client 配置片段。

Hermes MCP 配置示例：

```yaml
mcp_servers:
  llm-output-audit:
    command: "python3"
    args: ["/path/to/llm-output-audit/scripts/mcp_server.py"]
    timeout: 600
    connect_timeout: 30
```

Claude Code 示例：

```bash
claude mcp add llm-output-audit -- python3 /path/to/llm-output-audit/scripts/mcp_server.py
```

生成配置片段文件：

```bash
python3 scripts/install_agent_skill.py --agent mcp --scope project
```

## 配置

声明抽取和评级需要至少一个 OpenAI-compatible LLM key。

### 必需

二选一：

```bash
export DEEPSEEK_API_KEY="..."
```

或：

```bash
export OPENAI_API_KEY="..."
```

### 推荐

Tavily 可以改善通用网页证据质量：

```bash
export TAVILY_API_KEY="..."
```

如果没有 Tavily，脚本会在可行时 fallback 到 DuckDuckGo instant-answer API。

### 可选 OpenAI-compatible endpoint

用于本地或自托管模型：

```bash
export FACT_CHECK_BASE_URL="http://localhost:8000/v1"
export FACT_CHECK_MODEL="your-model-name"
export DGX_API_KEY="..."   # 如果你的 endpoint 需要 key
```

### 可选 LLM Wiki

LLM Wiki 是可选增强源，不是必需依赖。

```bash
--use-wiki --wiki /path/to/llm-wiki
```

## 使用方法

### 快速点查高风险短事实

```bash
python3 scripts/fact_check.py \
  --file article.md \
  --mode spot \
  --workers 3 \
  --source-workers 3
```

### 审计长期保存的草稿

```bash
python3 scripts/fact_check.py \
  --file article.md \
  --output article-audit.md \
  --mode draft \
  --workers 6 \
  --source-workers 4
```

### 发布级完整审计

```bash
python3 scripts/fact_check.py \
  --file article.md \
  --output article-audit.md \
  --mode full \
  --workers 8 \
  --source-workers 4
```

### 使用本地 LLM Wiki 作为额外证据源

```bash
python3 scripts/fact_check.py \
  --file article.md \
  --output article-audit.md \
  --mode draft \
  --use-wiki \
  --wiki /path/to/llm-wiki
```

### 只抽取 claims

```bash
python3 scripts/fact_check.py \
  --file article.md \
  --dry-run
```

### 写出详细执行 trace

调试审计器本身时使用 `--trace-log`。它会写 JSONL 事件，记录 claim 抽取、模式选择、source routing、具体 source query、结构化 API 结果、证据排序、确定性 override、最终 rating。

```bash
python3 scripts/fact_check.py \
  --file article.md \
  --output article-audit.md \
  --mode full \
  --trace-log article-audit-trace.jsonl
```

这对排查 `GitHub Stars` 这类 claim 特别有用：可以直接看到它到底有没有走 GitHub API metadata，还是被通用网页搜索 snippet 污染。trace 会自动脱敏 secrets。

## CLI 参数

```text
--file FILE                    要审计的 Markdown 文章。必填。
--output OUTPUT                报告路径。默认：<file>-audit.md。
--mode auto|fast|spot|draft|full
                               速度/深度策略。默认：auto。
--workers N                    claim 级并行 worker 数。
--source-workers N             每条 claim 内 source 级并行 worker 数。
--wiki PATH                    可选 LLM Wiki 根目录；只在 --use-wiki 时生效。
--use-wiki                     启用可选本地 LLM Wiki 证据源。
--skip-consistency             跳过内部一致性检查。
--force-consistency            强制内部一致性检查。
--dry-run                      只抽取 claims。
--no-fetch                     跳过网页原文抓取。
--llm-router                   模糊场景下用 LLM 优化 source routing。
--trace-log PATH               写出详细 JSONL 执行 trace，方便调试流程。
```

## 示例输出

下面是一个示意性的报告片段，用于展示输出结构。具体 claim 和 verdict 是示例，不是本项目的基准声明。

```markdown
# LLM Output Audit Report: article.md
Checked: 2026-05-10
Claims audited: 3 / 4 extracted
Audit mode: spot
Verdict summary: ✅ CONFIRMED 2 | 🔍 UNSOURCED 1

---

## ✅ Confirmed

- **[STATUS]** github.com/assafelovic/gpt-researcher is an open-source project
  - Routed sources: github, tavily_web
  - Source quality: score=0.912 structured=True
  - Evidence: The GitHub repository is licensed under Apache-2.0 and publicly accessible.
  - Source: https://github.com/assafelovic/gpt-researcher

## 🔍 Unsourced — Could Not Verify

- **[NUMBER]** A package has a specific monthly npm download count
  - Routed sources: npm, tavily_web
  - Source quality: score=0.812 structured=True
  - Evidence: npm metadata confirms the package identity, but the exact monthly count requires a reliable download-statistics source.
  - Suggestion: Add a reliable download statistics citation or hedge the number.
```

## Agent 工作流策略

这个项目不仅是 CLI，也是一套给 Agent 使用的流程策略。

### Agent 自己生成的长期内容

当 Agent 生成要保存、发布或复用的内容：

```text
内部起草 → 运行审计 → 应用安全修订 → 输出/保存最终版
```

不要把未审计草稿当作最终答案交付。

### 用户提供的已有文本

当用户提供已有文章或文件：

```text
先审计 → 返回报告和优先级修改建议 → 等用户确认后再改原文
```

不要在用户没有明确授权时静默修改其源文件。

### 高风险短回答

如果短回答涉及当前版本、项目状态、价格、发布日期、法律/医疗/金融/安全事实：

```text
高风险 claim → source route → 快速点查 → 带不确定性/引用回答
```

## 性能与并行

用并行能力降低延迟：

- `--workers` 控制 claim 级并行。
- `--source-workers` 控制每条 claim 内 source 级并行。
- 结构化 API 证据会跳过不必要的网页抓取。
- 一致性检查、对抗复审和 LLM router 都按模式/风险条件触发。

推荐起步：

```bash
--workers 6 --source-workers 4
```

如果 API 被限流，就降低 worker；如果是本地 endpoint 或宽松 API，可以谨慎提高。

## 持续集成

本仓库使用 GitHub Actions 在每次 push 和 pull request 时运行轻量 CI。CI 不调用付费 LLM API，只验证不需要凭据也应该始终可用的部分：

- Python 语法：`python -m py_compile scripts/fact_check.py`
- CLI 能启动：`python scripts/fact_check.py --help`
- Source Router 能 import，并对 `FEATURE`、`REQUIREMENT`、`COMPAT`、`WORKFLOW`、`EVAL` 做 smoke test
- 必需文档和示例文件存在
- README 中英文切换链接存在
- 架构图 SVG 通过基础 sanity check

为什么需要 CI：

- 防止 CLI 被无意改坏。
- 防止重构后漏提交 docs/assets。
- 外部贡献者提 PR 时，可以自动知道仓库基本功能是否还可用。
- 不需要把 API key 放进 GitHub Actions，也能保持公开 release 的可信度。

## 限制

LLM Output Audit 能提高可靠性，但不能保证绝对真实。

已知限制：

- 搜索引擎可能滞后于最近事件。
- 小众、私有、内部事实可能找不到来源。
- 第三方网页可能过时或不准确。
- 因果和定性判断仍需要人工判断。
- `🔍 UNSOURCED` 表示“未验证”，不等于“错误”。
- `❌ WRONG` 在高风险发布前仍建议人工确认修正证据。

## 项目结构

```text
.
├── README.md
├── README.zh-CN.md
├── SKILL.md
├── LICENSE
├── requirements.txt
├── scripts/
│   └── fact_check.py
└── examples/
    └── smoke.md
```

## Roadmap

- 在 `~/.cache/llm-output-audit/` 下缓存 source 查询结果。
- 在 LLM 评级前增加确定性的数字/日期/版本比较。
- 增加结构化 claim normalization：`subject`、`predicate`、`claimed_value`、`time_window`。
- 增加可选 `--apply` 模式，自动应用安全的 `❌ WRONG` 修复。
- 增加 GitHub Actions smoke tests。
- 打包成 Python CLI，支持 `pipx install llm-output-audit`。

## 贡献

欢迎 issue 和 pull request。适合贡献的方向包括：

- 新的数据源 adapter。
- 更好的 claim extraction prompt。
- 更稳的 entity/package 检测。
- 评估样例和 benchmark。
- 更安全的自动修改逻辑。

## License

MIT
