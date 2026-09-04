# TradingAgents — BeixiHub 适配版

> [!IMPORTANT]
> 本私有仓库是 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
> 的 BeixiHub 适配版本，不是官方上游仓库。原始项目仍是框架更新、作者归属、论文引用和
> Apache 2.0 许可的来源。

## 仓库关系

- **公司仓库（`origin`）：** [BeixiHub/trading-agents-adapted](https://github.com/BeixiHub/trading-agents-adapted)
- **原始开源仓库（`upstream`）：** [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)

### 维护方式

- `origin/main` 是公司适配版本的维护基线；公司需求、修复和适配变更均在本仓库开发、评审和合并。
- `upstream` 仅作为原始开源代码的同步来源。上游更新按需人工同步：从最新的 `origin/main` 创建独立同步分支，合并 `upstream/main`，完成冲突处理和验证后，再通过 PR 合入公司仓库；不要在无关功能分支中顺带同步。
- 上游变更不会直接覆盖公司适配。涉及数据源路由、A 股标识与数据口径、新闻、宏观数据或提示词等内容时，应先评估与现有适配的冲突，再决定保留、调整或采用上游实现。
- 同步 PR 应记录对应的上游提交、主要冲突及验证结果。除运行仓库 CI 外，还应针对受影响的公司数据链路进行验证；未经验证的能力不得表述为已经支持。

## 已完成的迁移与适配

- 确定性的中国 A 股身份解析及交易所代码规范化。
- 通过 Tushare、阿里云 PostgreSQL 和 ETF PostgreSQL 提供 A 股行情，并基于同一份行情在本地计算技术指标。
- 通过 ETF PostgreSQL 提供基本面和财务报表，并按公告日期限制查询，避免前视。
- 从只读原始事件层提供个股和全局新闻，支持 `mysql-talks`，不读取解析、预处理或融合后的事件内容。
- 通过环境变量选择数据源并保留显式回退，同时使用 uv 开发流程和 CI 测试覆盖适配链路。

详细范围与边界见[中国 A 股迁移映射](docs/china-a-share-migration-mapping.html)。
所有配置均通过环境变量提供，请从 [`.env.example`](.env.example) 开始，严禁提交真实凭据。
在策略代码中调用 ETF Platform Dynamic Agent API 的复制示例、错误处理和服务器自动配置见
[Dynamic Agent API 开发文档](docs/dynamic-agent-api.md)。

## 公司版本快速开始

```bash
git clone https://github.com/BeixiHub/trading-agents-adapted.git
cd trading-agents-adapted
cp .env.example .env
```

### A 股数据源配置

- 使用 Tushare 行情时，设置 `TUSHARE_TOKEN`，并将
  `TRADINGAGENTS_DATA_VENDOR_CORE_STOCK_APIS` 配置为 `tushare,yfinance`。
- 多股票组合会在任何数据或模型工作前检查所选 Analyst 的显式数据源链能否覆盖全部输入市场。
  混合 A 股与非 A 股时，行情、技术指标和基本面链必须分别包含可服务两类标的的 provider；推荐
  将对应的三个 `TRADINGAGENTS_DATA_VENDOR_*` 变量配置为 `tushare,yfinance`。不兼容配置会
  给出可操作错误，程序不会静默添加未配置的数据源或继续生成不完整组合。
- 使用阿里云/ETF PostgreSQL 时，设置完整的 `ALIYUN_STOCK_POSTGRES_*` 变量。
  行情查询使用只读事务，技术指标基于同一份 OHLCV 在本地计算；符合条件时，基本面和财务工具也使用只读 ETF 数据适配器。
- 使用 ETF 事件库新闻时，优先设置完整的 `ETF_EVENT_POSTGRES_*` 变量。
  如果这一组变量全部缺失，程序可复用完整的 `ALIYUN_STOCK_POSTGRES_*` 连接信息并显式连接 `etf_event_analysis`；部分配置不会静默回退。
- 个股新闻只使用 `event_companies` 建立股票与 `raw_events` 的关联；全局新闻按自然日读取来源白名单中的原始事件，不执行 ticker 或关键词检索。

### 开发与验证

```bash
uv sync
uv run pytest -q
uv run ruff check .
```

## 交接与待完成工作

- **中国宏观数据：** 当前 `get_macro_indicators` 只接入 FRED，相关工具说明和新闻分析提示词也仍以 FRED 为准；中国宏观替代尚未实现。下一阶段应以现有数据库中的中国宏观因子作为候选基础，先人工核对因子定义、数据覆盖、公告或可用日期及业务口径，再决定可接入范围。对没有严格对应数据的利率、债券收益率、就业、GDP 等，不得使用其他指标静默替代。完成审核后，再更新宏观数据路由、相关提示词和测试。
- **情绪分析限制：** 代码保留 Sentiment Analyst 节点和 `social` 选择开关，但 A 股适配尚未完成可用情绪数据源的接入与验证，因此当前不能将其视为可用能力或分析依据。交接期间的 A 股运行应不选择 `social`，或明确标注为不可用；只有在连接、数据来源和验证补齐后才能重新启用。

## 原始项目文档

以下内容保持原始仓库的说明、社区链接、研究免责声明、论文引用和作者归属，不混入公司适配说明。
<!-- 原始 README 起点 -->
<p align="center">
  <img src="assets/TauricResearch.png" style="width: 60%; height: auto;">
</p>

<div align="center" style="line-height: 1;">
  <a href="https://arxiv.org/abs/2412.20138" target="_blank"><img alt="arXiv" src="https://img.shields.io/badge/arXiv-2412.20138-B31B1B?logo=arxiv"/></a>
  <a href="https://discord.com/invite/hk9PGKShPK" target="_blank"><img alt="Discord" src="https://img.shields.io/badge/Discord-TradingResearch-7289da?logo=discord&logoColor=white&color=7289da"/></a>
  <a href="https://x.com/TauricResearch" target="_blank"><img alt="X Follow" src="https://img.shields.io/badge/X-TauricResearch-white?logo=x&logoColor=white"/></a>
  <a href="https://github.com/TauricResearch/" target="_blank"><img alt="Community" src="https://img.shields.io/badge/GitHub_Community-TauricResearch-14C290?logo=discourse"/></a>
</div>
<br>
<div align="center">
  <a href="https://github.com/TauricResearch" target="_blank"><img alt="TradingAgents #1 Repository of the Day" src="https://trendshift.io/api/badge/repositories/16192" width="250" height="55"/></a>
</div>
<br>
<div align="center">
  <!-- Keep these links. Translations will automatically update with the README. -->
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=de">Deutsch</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=es">Español</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=fr">français</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ja">日本語</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ko">한국어</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=pt">Português</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ru">Русский</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=zh">中文</a>
</div>

---

# TradingAgents: Multi-Agents LLM Financial Trading Framework

## News
- [2026-07] **TradingAgents v0.3.1** released with correctness and stability fixes: Alpha Vantage look-ahead filtering, graph-router crash-safety, graph-shape-aware checkpoint resume, working crypto sentiment sources, a configurable LLM retry budget, Bedrock API-key auth, and Claude Sonnet 5 / Fable 5 support. See [CHANGELOG.md](CHANGELOG.md) for the full list.
- [2026-06] **TradingAgents v0.3.0** released with a verified data-access contract, an expanded provider registry (NVIDIA, Kimi, Groq, Mistral, Bedrock, and any OpenAI-compatible endpoint), FRED and Polymarket data vendors, a current-generation model catalog, and a CI gate.
- [2026-05] **TradingAgents v0.2.5** released with the grounded Sentiment Analyst, GPT-5.5 etc. model coverage, Qwen/GLM/MiniMax dual-region support, `TRADINGAGENTS_*` env-var configurability with API-key auto-detection, remote Ollama support, non-US alpha benchmarks, and ticker path-traversal hardening.
- [2026-04] **TradingAgents v0.2.4** released with structured-output agents (Research Manager, Trader, Portfolio Manager), LangGraph checkpoint resume, persistent decision log, DeepSeek/Qwen/GLM/Azure provider support, Docker, and a Windows UTF-8 encoding fix.
- [2026-03] **TradingAgents v0.2.3** released with multi-language support, GPT-5.4 family models, unified model catalog, backtesting date fidelity, and proxy support.
- [2026-03] **TradingAgents v0.2.2** released with GPT-5.4/Gemini 3.1/Claude 4.6 model coverage, five-tier rating scale, OpenAI Responses API, Anthropic effort control, and cross-platform stability.
- [2026-02] **TradingAgents v0.2.0** released with multi-provider LLM support (GPT-5.x, Gemini 3.x, Claude 4.x, Grok 4.x) and improved system architecture.
- [2026-01] **Trading-R1** [Technical Report](https://arxiv.org/abs/2509.11420) released, with [Terminal](https://github.com/TauricResearch/Trading-R1) expected to land soon.

<div align="center">

🚀 [TradingAgents](#tradingagents-framework) | ⚡ [Installation & CLI](#installation-and-cli) | 🎬 [Demo](https://www.youtube.com/watch?v=90gr5lwjIho) | 📦 [Package Usage](#tradingagents-package) | 🤝 [Contributing](#contributing) | 📄 [Citation](#citation)

</div>

> 🎉 **TradingAgents** officially released! We have received numerous inquiries about the work, and we would like to express our thanks for the enthusiasm in our community.
>
> So we decided to fully open-source the framework. Looking forward to building impactful projects with you!

## TradingAgents Framework

TradingAgents is a multi-agent trading framework that mirrors the dynamics of real-world trading firms. By deploying specialized LLM-powered agents: from fundamental analysts, sentiment experts, and technical analysts, to trader, risk management team, the platform collaboratively evaluates market conditions and informs trading decisions. Moreover, these agents engage in dynamic discussions to pinpoint the optimal strategy.

<p align="center">
  <img src="assets/schema.png" style="width: 100%; height: auto;">
</p>

> TradingAgents framework is designed for research purposes. Trading performance may vary based on many factors, including the chosen backbone language models, model temperature, trading periods, the quality of data, and other non-deterministic factors. [It is not intended as financial, investment, or trading advice.](https://tauric.ai/disclaimer/)

Our framework decomposes complex trading tasks into specialized roles.

### Analyst Team
- Fundamentals Analyst: Evaluates company financials and performance metrics, identifying intrinsic values and potential red flags.
- Sentiment Analyst: Aggregates news headlines, StockTwits, and Reddit chatter into a single sentiment read to gauge short-term market mood.
- News Analyst: Monitors global news and macroeconomic indicators, interpreting the impact of events on market conditions.
- Technical Analyst: Utilizes technical indicators (like MACD and RSI) to detect trading patterns and forecast price movements.

<p align="center">
  <img src="assets/analyst.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### Researcher Team
- Comprises both bullish and bearish researchers who critically assess the insights provided by the Analyst Team. Through structured debates, they balance potential gains against inherent risks.

<p align="center">
  <img src="assets/researcher.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Trader Agent
- Composes reports from the analysts and researchers to make informed trading decisions, determining the timing and magnitude of trades.

<p align="center">
  <img src="assets/trader.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Risk Management and Portfolio Manager
- Continuously evaluates portfolio risk by assessing market volatility, liquidity, and other risk factors. The risk management team evaluates and adjusts trading strategies, providing assessment reports to the Portfolio Manager for final decision.
- The Portfolio Manager approves/rejects the transaction proposal. If approved, the order will be sent to the simulated exchange and executed.

<p align="center">
  <img src="assets/risk.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

## Installation and CLI

### Installation

Clone TradingAgents:
```bash
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents
```

Create a virtual environment in any of your favorite environment managers:
```bash
conda create -n tradingagents python=3.12
conda activate tradingagents
```

Install the package and its dependencies:
```bash
pip install .
```

### Docker

Alternatively, run with Docker:
```bash
cp .env.example .env  # add your API keys
docker compose run --rm tradingagents
```

For local models with Ollama:
```bash
docker compose --profile ollama run --rm tradingagents-ollama
```

The immutable server release preserves the interactive CLI toolbox and also
contains private, authenticated single-event and batch event-portfolio plan
resources for approved machine consumers. The API publishes no host port and
never submits broker orders. The compatible contracts and ownership boundaries
are documented in [docs/event-trade-plan-api.md](docs/event-trade-plan-api.md)
and [docs/event-portfolio-plan-api.md](docs/event-portfolio-plan-api.md).
Portfolio schema 1.0 remains S-only/ordinary-A-share compatible; schema 1.1 explicitly adds exact
A-level events and `688xxx/689xxx` instruments without changing the single-event resource.
Batch instrument research uses one independent mutable graph per in-flight member, with a closed
`TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY=1..4` setting that defaults to 4. Any member failure
still fails the entire batch, waits for already in-flight work, and never returns a partial portfolio.
Operators can start
an interactive analysis inside the verified CLI container with:

```bash
deploy_dir=/opt/trading-agents-adapted
sha="$(cat "$deploy_dir/.deploy-current-sha")"
image_uri="$(sed -n 's/^image_uri=//p' "$deploy_dir/.deploy-current")"
TRADINGAGENTS_IMAGE="$image_uri" \
TRADINGAGENTS_ENV_FILE="$deploy_dir/.env" \
TRADINGAGENTS_API_ENV_FILE="$deploy_dir/.event-plan-api.env" \
AGENT_RUNTIME_ENV_FILE="/opt/etf-agent-runtime-tunnel/secrets/internal-agent-caller.env" \
GIT_SHA="$sha" \
  docker compose -p trading-agents-adapted \
    --project-directory "$deploy_dir" \
    -f "$deploy_dir/releases/$sha/docker-compose.server.yml" \
    exec tradingagents tradingagents
```

See [docs/docker-deployment.md](docs/docker-deployment.md) for the PR gate,
ACR publishing, detached server deployment, rollback, and operations runbook.

### Required APIs

TradingAgents supports multiple LLM providers. Set the API key for your chosen provider:

```bash
export OPENAI_API_KEY=...          # OpenAI (GPT)
export GOOGLE_API_KEY=...          # Google (Gemini)
export ANTHROPIC_API_KEY=...       # Anthropic (Claude)
export XAI_API_KEY=...             # xAI (Grok)
export DEEPSEEK_API_KEY=...        # DeepSeek
export DASHSCOPE_API_KEY=...       # Qwen — International (dashscope-intl.aliyuncs.com)
export DASHSCOPE_CN_API_KEY=...    # Qwen — China (dashscope.aliyuncs.com)
export ZHIPU_API_KEY=...           # GLM via Z.AI (international)
export ZHIPU_CN_API_KEY=...        # GLM via BigModel (China, open.bigmodel.cn)
export MINIMAX_API_KEY=...         # MiniMax — Global (api.minimax.io)
export MINIMAX_CN_API_KEY=...      # MiniMax — China (api.minimaxi.com)
export OPENROUTER_API_KEY=...      # OpenRouter
export ALPHA_VANTAGE_API_KEY=...   # Alpha Vantage
```

For Azure OpenAI, copy `.env.enterprise.example` to `.env.enterprise` and fill in your credentials.

For AWS Bedrock, install the extra with `pip install ".[bedrock]"`, set `llm_provider: "bedrock"`, configure AWS credentials (environment variables, `~/.aws/credentials`, or an IAM role) and `AWS_DEFAULT_REGION`, and use a Bedrock model ID, e.g. `us.anthropic.claude-opus-4-8-v1:0`.

For local models, configure Ollama with `llm_provider: "ollama"`. The default endpoint is `http://localhost:11434/v1`; set `OLLAMA_BASE_URL` to point at a remote `ollama-serve`. Pull models with `ollama pull <name>`, and pick "Custom model ID" in the CLI for any model not listed by default.

For any other OpenAI-compatible server (vLLM, LM Studio, llama.cpp, or a custom relay), use `llm_provider: "openai_compatible"` and set the endpoint via `backend_url` (or `TRADINGAGENTS_LLM_BACKEND_URL`), e.g. `http://localhost:8000/v1` for vLLM or `http://localhost:1234/v1` for LM Studio. The model is whatever your server serves. No key is needed for local servers; set `OPENAI_COMPATIBLE_API_KEY` when the endpoint requires one.

Alternatively, copy `.env.example` to `.env` and fill in your keys:
```bash
cp .env.example .env
```

### CLI Usage

Launch the interactive CLI:
```bash
tradingagents          # installed command
python -m cli.main     # alternative: run directly from source
```
You will see a screen where you can select your desired tickers, analysis date, LLM provider, research depth, and more.

### Markets and tickers

TradingAgents works with any market Yahoo Finance covers, using the exchange-suffixed ticker. Company identity and the alpha benchmark resolve automatically per market.

- US: `AAPL`, `SPY`
- Hong Kong: `0700.HK` · Tokyo: `7203.T` · London: `AZN.L`
- India: `RELIANCE.NS`, `.BO` · Canada: `.TO` · Australia: `.AX`
- China A-shares: Shanghai `.SS`, Shenzhen `.SZ` (e.g. `600519.SS` for Kweichow Moutai)
- Crypto: `BTC-USD`, `ETH-USD`

<p align="center">
  <img src="assets/cli/cli_init.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

An interface will appear showing results as they load, letting you track the agent's progress as it runs.

<p align="center">
  <img src="assets/cli/cli_news.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

<p align="center">
  <img src="assets/cli/cli_transaction.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

## TradingAgents Package

### Implementation Details

We built TradingAgents with LangGraph to ensure flexibility and modularity. The framework supports multiple LLM providers: OpenAI, Google, Anthropic, xAI, DeepSeek, Qwen (Alibaba DashScope, international and China endpoints), GLM (Zhipu), MiniMax (global + China), OpenRouter, Ollama for local models, and Azure OpenAI for enterprise.

### Python Usage

To use TradingAgents inside your code, you can import the `tradingagents` module and initialize a `TradingAgentsGraph()` object. The `.propagate()` function will return a decision. You can run `main.py`, here's also a quick example:

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

# forward propagate
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

### Multi-stock portfolio mode

Portfolio mode analyzes 2–20 stocks with the existing single-stock
workflow, then makes one cross-sectional allocation decision. The final result
contains a target weight for every stock plus an explicit cash weight; the
weights are capped and normalized to 100% in code rather than trusting the LLM's
arithmetic.

```python
from tradingagents.agents.schemas import render_portfolio_allocation
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

tickers = ["NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "JPM"]
ta = TradingAgentsGraph(debug=False, config=DEFAULT_CONFIG.copy())
states, allocation = ta.propagate_portfolio(
    tickers,
    "2026-08-07",
    max_position_percent=25,
)
print(render_portfolio_allocation(allocation))

# Optional: writes portfolio_allocation.md and portfolio_allocation.json
ta.save_portfolio_report(allocation)
```

The same mode is available from the CLI:

```bash
tradingagents portfolio "NVDA,AAPL,MSFT,GOOGL,AMZN,META,TSLA,JPM" \
  --date 2026-08-07 --max-position 25
```

Because this runs the full agent graph once per stock and then makes one final
allocation call, data/LLM work grows roughly with the number of supplied stocks.
For example, eight candidates require about eight single-stock graph runs plus
the allocation call. A 0% stock weight and a non-zero cash weight are valid outcomes.
Before any data or model work, portfolio mode also verifies that each selected
analyst's explicit vendor chain can serve every supplied market. For mixed A-share
and non-A-share portfolios, configure compatible chains such as
`tushare,yfinance`; an incompatible chain fails closed instead of silently adding
an unconfigured provider or analyzing a member without its required data.

You can also adjust the default configuration to set your own choice of LLMs, debate rounds, etc.

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"        # e.g. openai, google, anthropic, deepseek, groq, ollama; openai_compatible covers any OpenAI-compatible endpoint (vLLM, LM Studio, llama.cpp, ...)
config["deep_think_llm"] = "gpt-5.5"     # Model for complex reasoning
config["quick_think_llm"] = "gpt-5.4-mini" # Model for quick tasks
config["max_debate_rounds"] = 2

ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

See `tradingagents/default_config.py` for all configuration options.

## Persistence and Recovery

TradingAgents persists two kinds of state across runs.

### Decision log

The decision log is always on. Each completed run appends its decision to `~/.tradingagents/memory/trading_memory.md`. On the next run for the same ticker, TradingAgents fetches the realised return (raw and alpha vs SPY), generates a one-paragraph reflection, and injects the most recent same-ticker decisions plus recent cross-ticker lessons into the Portfolio Manager prompt, so each analysis carries forward what worked and what didn't.

Override the path with `TRADINGAGENTS_MEMORY_LOG_PATH`.

### Checkpoint resume

Checkpoint resume is opt-in via `--checkpoint`. When enabled, LangGraph saves state after each node so a crashed or interrupted run resumes from the last successful step instead of starting over. On a resume run you will see `Resuming from step N for <TICKER> on <date>` in the logs; on a new run you will see `Starting fresh`. Checkpoints are cleared automatically on successful completion.

Per-ticker SQLite databases live at `~/.tradingagents/cache/checkpoints/<TICKER>.db` (override the base with `TRADINGAGENTS_CACHE_DIR`). Use `--clear-checkpoints` to reset all of them before a run.

```bash
tradingagents analyze --checkpoint           # enable for this run
tradingagents analyze --clear-checkpoints    # reset before running
```

```python
config = DEFAULT_CONFIG.copy()
config["checkpoint_enabled"] = True
ta = TradingAgentsGraph(config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
```

## Reproducibility

TradingAgents is LLM-driven, so two runs of the same ticker and date can differ. This is expected for a research tool built on language models, not a defect. The variation comes from a few distinct sources, and it helps to separate them.

Language model sampling is non-deterministic. Even at a fixed temperature, providers do not guarantee byte-identical output across calls, and reasoning models (the default GPT-5.x family, and any thinking-mode model) vary the most because their internal reasoning is itself sampled.

Live data moves. News, StockTwits, and Reddit return different content as time passes, so a run today sees different inputs than a run last week even for the same historical trade date. Pin the analysis date to hold the price and indicator window fixed, but the social and news sources still reflect "now".

To reduce variation you can lower the sampling temperature. Set `temperature` in your config (or `TRADINGAGENTS_TEMPERATURE` in `.env`); lower values make models that honor it more repeatable. The current curated models are reasoning-first and largely ignore temperature, so for tighter reproducibility use a non-reasoning model, which you can set explicitly via the Custom model ID option.

```python
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"
config["temperature"] = 0.0
# Reasoning models ignore temperature. For tighter reproducibility, set a
# non-reasoning deep/quick model explicitly (e.g. via the Custom model ID option).
```

What does not vary anymore: the analyzed company identity is resolved deterministically from the ticker before any agent runs, and the market analyst grounds exact price and indicator claims in a verified data snapshot. Earlier reports of "different companies" or fabricated price levels across runs are addressed by these two mechanisms.

Backtest results are not guaranteed to match any published figure. Returns depend on the model, the temperature, the date range, data quality, and the sampling above. Treat the framework as a research scaffold for studying multi-agent analysis, not as a strategy with a fixed, replicable return.

## Contributing

Contributions are welcome: bug fixes, documentation, and feature ideas; past contributions are credited per release in [`CHANGELOG.md`](CHANGELOG.md).

## Citation

Please reference our work if you find *TradingAgents* provides you with some help :)

```
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework}, 
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138}, 
}
```
