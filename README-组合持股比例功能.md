# 多股票持股比例功能代码包

此压缩包只包含“输入多支股票并输出每支股票目标持仓比例”功能的新增代码，以及该功能运行所必需的修改文件，不包含完整项目。

## 功能

- 输入 2–20 支股票。
- 全 A 股组合的行情、历史收益、基准和股票身份使用 Tushare；混合市场组合必须显式配置能覆盖
  每类标的的数据源链，不兼容配置会在任何数据或模型工作前失败。
- 输出每支股票的目标持仓比例、评级、理由和现金比例。
- 权重合计固定为 100%，支持 `--max-position` 限制单股最大比例。
- OpenRouter 结构化输出失败时自动改用 JSON；仍失败时使用确定性权重回退。

## 使用方式

将压缩包中的目录和文件覆盖到原项目根目录，然后配置 `.env`，运行：

```bash
uv run tradingagents portfolio \
  "600519.SH,000001.SZ,000333.SZ,300750.SZ,601318.SH,600036.SH,601899.SH,002594.SZ" \
  --date 2026-08-07 \
  --max-position 25
```

## 文件说明

- `tradingagents/agents/managers/portfolio_allocator.py`：新增，多股票横截面权重分配器。
- `tests/test_portfolio_allocation.py`：新增，组合模式测试。
- `cli/main.py`：增加 `portfolio` 命令。
- `tradingagents/agents/schemas.py`：增加组合权重数据结构及表格渲染。
- `tradingagents/graph/trading_graph.py`：增加多股票完整 Agent 分析、组合编排和报告保存。
- `tradingagents/reporting.py`：保存 Markdown/JSON 权重报告。
- `tradingagents/dataflows/tushare_stock.py`：Tushare 股票、指数、收盘价及身份数据。
- `tradingagents/agents/utils/agent_utils.py`：A 股身份改用 Tushare。
- `tradingagents/default_config.py`：A 股基准映射配置。
- `tests/test_tushare_stock.py`、`tests/test_memory_log.py`、`tests/test_dataflows_config.py`、`tests/test_instrument_identity.py`：相关回归测试。
- `main.py`、`README.md`、`.env.example`：运行示例和配置说明。

压缩包不包含 `.env`、API 密钥、虚拟环境、缓存或运行报告。
