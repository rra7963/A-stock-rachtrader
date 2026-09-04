# PR #15 混合市场组合的 Tushare-only 数据源偏移

- 状态：已解决
- 发现/决策日期：2026-08-21
- 决策人：用户（选择最小 fail-closed 预检）
- 负责人：当前会话（用户已将严格 QA 任务发现的偏移转交本会话落实）
- 相关 PR：<https://github.com/BeixiHub/trading-agents-adapted/pull/15>
- 锁定提交：`3e5f9d99a04712b05d514569e9eec595bf7f44d1`

## 原设计及位置

- `cli/main.py:1354-1447` 将 `portfolio` 暴露为 2–20 个股票代码的公开入口；其中
  `cli/main.py:1392-1403` 明确把混合市场组合描述为使用面向 Yahoo 的数据路径。
- `tradingagents/graph/trading_graph.py:570-679` 要求每个候选标的先完成完整单股图，再做一次横截面配置。
- `tradingagents/dataflows/interface.py:221-235` 规定显式 vendor 列表就是完整回退链，不得静默使用用户
  未配置的数据源。
- `README.md:41-45` 对同时使用 Tushare 与美股数据的配置建议是 `tushare,yfinance`。

## 实际实现与证据

当环境覆盖把 `data_vendors.core_stock_apis` 设为单一 `tushare` 时，公开 CLI 仍接受
`portfolio "600519,AAPL"`，把输入改为 `['600519.SS', 'AAPL']`，并原样保留 Tushare-only
配置。之后 `propagate_portfolio()` 按同一全局配置逐个运行单股图；Tushare 适配器会把 `AAPL`
原样送入 `daily` 接口，显式 vendor 契约又禁止未配置的 Yahoo 回退。因此美股候选无法获得其
核心行情，违反“每个候选完成完整单股图”的组合入口契约。

独立、无网络的 CLI 边界反证在锁定 head 上得到：`10 passed, 1 failed`；唯一失败断言记录为：

```text
mixed portfolio was accepted with a Tushare-only stock-data route:
tickers=['600519.SS', 'AAPL'], vendor='tushare'
```

现有 `tests/test_cli_command_topology.py:145-204` 只验证混合输入保留 `DEFAULT_CONFIG`，没有把
`DEFAULT_CONFIG` 覆盖成 Tushare-only，也没有继续验证每个 ticker 实际使用的 provider。

## 偏移原因

混合市场分支把“保留当前配置”与“当前配置一定包含 Yahoo-capable provider”视为同一件事；
环境变量允许用户合法地把 vendor 链覆盖为单一 Tushare，因此该前提并不成立。

## 影响与风险

- 美股候选可能在缺失核心行情、技术面和基本面的情况下继续进入后续 LLM 节点，结果完整性不可保证。
- 相同的公开输入会因进程环境不同而正常分析或静默退化，CI 目前无法发现这种配置相关行为。
- PR #15 是历史功能恢复；逐字恢复旧文件可证明传播忠实，但不能证明旧的 correctness blocker 已消失。

## 可选处理方案

1. 在 CLI/图启动任何模型或数据工作前，检测标的市场与显式 vendor 链是否兼容；不兼容时用清晰错误
   fail closed，并新增 Tushare-only + 混合输入的回归测试。
2. 设计按 ticker/市场选择 provider 的路由，同时保持 tool-level override、显式回退链和可审计 provenance；
   这会扩大数据流职责和测试矩阵。
3. 明确收窄产品契约：混合市场仅在配置包含各市场可用 provider 时受支持，并仍在入口做可操作的配置报错。

## 推荐及理由

本恢复 PR 优先采用方案 1：范围最小、fail closed，不会静默替用户改变显式 provider 配置，也不会在
缺少行情时继续产生看似完整的组合结果。若后续需要自动跨市场路由，再单独按方案 2 设计 ADR 和
provider provenance 测试。

## 用户决定与验收边界

用户于 2026-08-21 选择方案 1，并明确将现有 TODO 和 #15 分支上的最小修复 ownership 转交
当前会话。方案 2 的自动按市场路由不纳入本 PR；方案 3 不采用。

本次落地必须同时满足：

- CLI 在构造 `TradingAgentsGraph`、检查 LLM key、访问数据或调用模型前拒绝不兼容配置；
- `TradingAgentsGraph.propagate_portfolio()` 对程序化调用执行同一预检，不能只保护 CLI；
- 校验尊重 category/tool-level 显式 vendor 链，只判断已配置 provider 的市场覆盖，不添加回退；
- 全 A 股 Tushare、纯非 A 股兼容数据源及显式覆盖两个市场的组合链保持可用；
- 新增真实 CLI 边界、程序化入口和相关 category override 回归，文档给出可操作配置示例；
- 聚焦、全仓、严格 Ruff、clean-install、部署/镜像与最终 exact-head CI 全部通过。

完成实现、复审和验证前状态保持 `已决策待落地`。

## 落地实现与本地验证

当前恢复分支在审计提交 `38b4ac2` 与实现提交 `db53e76` 中完成了最小 fail-closed
方案：CLI 在 API key 检查和图构造前调用共享预检器，
`TradingAgentsGraph.propagate_portfolio()` 在程序化入口再次调用同一预检器。校验器只检查
当前所选 analyst 实际需要的数据方法，并按照既有 dataflow 契约解析 tool-level override、
category-level 显式链和默认链；它不会插入、重排或替换 provider。

回归覆盖混合市场 Tushare-only 的 CLI/程序化拒绝、技术面 category 缺口、tool-level
`get_indicators=tushare` 覆盖、显式 `tushare,yfinance` 混合市场成功、全 A 股与既有纯非 A 股
路径。实际本地结果为：聚焦回归 `30 passed`，相关更宽回归 `143 passed`，全仓
`774 passed, 2 skipped, 19 warnings, 69 subtests passed`；两个 skip 分别是未安装可选
Bedrock extra 与未配置 DeepSeek live key。严格全仓 Ruff、部署合同、部署脚本语法及部署定向
`26 passed` 均通过。

Python 3.12 全新临时环境完成 115 个依赖安装，并通过包、API、integrations、CLI 与预检器导入
以及两个 CLI help。生产 Dockerfile 镜像 `trading-agents-adapted:pr15-provider-fix` 构建成功，
镜像用户为 `appuser`；包/预检器导入、组合 CLI help 和 Dynamic Agent 示例 help 均在
`--network=none` 下通过。

实现/验证 head `11df833fb74e6cbc6c12e6fbb1a515ece59803cb` 的 GitHub CI run
`32451331938` 已完整通过 Python 3.10–3.13、strict Ruff、clean-install、部署合同与生产镜像以及
最终 CI Gate。代码、配置契约、回归与正式说明已经重新一致，因此本偏移更新为 `已解决`。
独立账号 approval、merge/main 可达性、生产部署与 #13 rebase 是尚未完成的交付门禁，继续由
`docs/todo/2026-08-18-portfolio-stack-main-propagation-drift.md` 跟踪；它们不被本状态转换绕过。

## 阻塞范围

- 阻塞 PR #15 以当前 head 传播混合市场公开入口到 `main`。
- 不阻塞全 A 股 Tushare 路径、纯 Yahoo 路径、恢复提交的 provenance 核验及其他无关工作。
- 用户已决定最小 fail-closed 契约；除本节列明的预检、回归与文档外，不扩展为自动跨市场路由。
