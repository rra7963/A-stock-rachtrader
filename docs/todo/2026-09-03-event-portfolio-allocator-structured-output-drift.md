# Event Portfolio allocator 最大规模结构化输出可靠性

- **首次发现状态**：待讨论
- **当前状态**：已解决
- **发现/决策日期**：2026-09-03
- **决策人**：用户
- **负责人**：Trading Agent protocol owner；仓库 maintainer review/merge/deploy
- **相关分支 / PR / commit**：`fix/event-portfolio-allocator-max-universe`；PR #19；实现 commit
  `42b6cc99aaba3246fc3bbadc0153e45f640f4117`
- **阻塞范围**：阻塞 Supporting Lobster 的生产业务可用性；不阻塞事件被动接收、前端账户同步或安全告警

## 原设计及位置

[`docs/event-portfolio-plan-api.md`](../event-portfolio-plan-api.md) 要求 Trading Agent 对请求中的一至二十个
标的全部完成研究，再由一个严格横截面 allocator 选择长仓目标权重和显式现金权重。响应必须保留精确
请求成员顺序和每个个股 graph 已得出的五档评级，股票与现金权重合计必须精确为 100.00%。任何不完整、
成员漂移、评级漂移或非法权重都必须失败关闭；不得返回部分组合、确定性评级 fallback、自由文本 JSON
fallback 或自动重试。

现有实现把完整 `AgentEventPortfolioAllocation` 作为 function tool schema 交给模型，要求模型在最多二十
行中重复输出 `stock_code`、`rating`、十进制目标权重和理由，再由 planner 重复验证成员、顺序、评级及
权重总和。OpenRouter 路由没有显式要求所选后端支持原生 structured outputs。

## 生产证据与问题

2026-09-03 生产自然批次在 08:30 收到 88 个 A/S 事件，从 63 个高相关候选中按既定上限提交 20 个标的。
有界并发 graph 在 09:42 前完成 20/20 个标的；随后 allocator 失败，API 安全日志记录
`failure_stage=allocator completed=20 total=20`，持久终态为 `planner_failed_allocator`，HTTP 为通用 503。
Supporting Lobster 因而记录 `analysis_failed / agent_portfolio_failed`，没有目标仓位、订单 intent 或成交，
并成功发送安全分类飞书告警。

现有安全日志按设计不保存 provider 原文，因此不能把这次失败进一步断言为解析、成员、评级或权重总和
中的某一种。当前单一 `allocator` 码也使下一次自然批次仍无法安全区分这些原因。

## 选择、决定与理由

1. **采用：allocator-only 原生严格结构化输出 + 精简冗余字段。** OpenRouter allocator 使用原生 JSON
   Schema，并要求路由到支持请求参数的 provider；模型继续输出精确股票/现金 basis points、股票身份和
   公开理由，planner 将已验证的个股评级确定性绑定回同一股票。basis points 只是内部无损表示，最终 wire
   仍为两位小数百分比，且总和不合法时仍失败关闭。
2. 只补安全子阶段码、不改变 allocator 请求：能诊断但不能解决已经出现的最大规模可靠性问题，未选择。
3. 缩小 20 标的 universe、返回部分组合、自动重试或用评级生成 fallback 权重：分别违反用户选择的候选
   上限、全员研究、无自动重试和 TA 自主仓位合同，禁止采用。

用户在获知推荐修复范围后于 2026-09-03 指示“按推荐”。具体实现还保持以下边界：原生严格路由只作用于
allocator，不改变个股 graph；非 OpenRouter provider 保留其既有 structured-output 机制；不记录原始
provider 响应；不把 allocator 失败重试成第二次 LLM 调用。

## 影响与风险

- **可靠性**：原生 JSON Schema 可约束闭合字段与基础类型；程序不再要求模型重复决定 graph 已拥有的评级，
  减少二十行输出的无意义漂移面。
- **路由/成本**：`require_parameters` 会排除不支持 structured outputs 的 OpenRouter provider，可用池可能
  变小，延迟或单次价格可能变化；模型、总调用次数和分析 universe 不变。
- **严格性**：模型仍逐股票选择 0..10000 basis points 和显式现金 basis points，总和必须精确 10000；
  planner 不归一化、不补齐、不接受遗漏/增补/乱序成员。
- **兼容性**：HTTP request/response schema、canonical request hash、持久 completed response、调用方绑定和
  broker/订单职责均不变；新增的持久错误码只细分既有失败终态。

## 落地与验收标准

- 安全子阶段至少区分 provider/invoke、closed schema、成员/顺序绑定和权重总和；HTTP 对调用方仍只返回
  通用 `planner_failed`，日志和 SQLite 不包含标的、事件、账户、provider 原文或隐藏推理。
- 最大 20 标的测试必须通过原生严格 allocator 绑定，证明精确成员顺序、评级程序绑定、basis-points 无损
  转换和 100.00% 总和；遗漏、额外、乱序、非法 schema、非法总和分别失败关闭。
- OpenRouter 请求测试必须证明只在 allocator 使用 `json_schema`、`strict=true` 和
  `provider.require_parameters=true`；不得改变 candidate selector、plan adapter 或 worker graph。
- 聚焦 planner/API/store/LLM-client 测试、全仓测试、严格 Ruff、部署合同、镜像构建和 exact-head CI 通过。
- 实施后重新核对正式设计和实际 diff，再把本记录更新为 `已解决`；PR 绿色不代表 merge、部署、live LLM、
  自然批次、broker、飞书或前端已验收。

## 落地证据与验证结果

实现 commit `42b6cc99aaba3246fc3bbadc0153e45f640f4117` 已完成本记录中的代码、closed schema、
安全阶段、测试和正式设计更新。GitHub Actions run `33706737585` 在该精确 head 上通过 Python 3.10、
3.11、3.12、3.13 全仓测试、clean-install smoke、全仓严格 Ruff、部署合同、Compose 模型、生产镜像
构建/非 root 用户及镜像入口导入检查，最终 `CI Gate` 为通过。本地补充回执为：聚焦 67 passed；全仓
868 passed、2 个既有条件跳过、69 subtests passed；部署合同测试 52 passed；锁文件中的真实 LangChain
绑定生成 `response_format=json_schema`、`strict=true` 与 `provider.require_parameters=true`。

实施后已重新核对 [`docs/event-portfolio-plan-api.md`](../event-portfolio-plan-api.md)、本记录、
[`2026-08-17-event-portfolio-plan-api-drift.md`](2026-08-17-event-portfolio-plan-api-drift.md) 和
[`2026-09-01-event-portfolio-bounded-analysis-drift.md`](2026-09-01-event-portfolio-bounded-analysis-drift.md)。
结论是实现符合既有设计预期：Trading Agent 仍只拥有研究与目标组合输出，调用方仍拥有账户、broker、订单、
成交与风控执行；完整请求 universe 和 graph 评级得到保留；任何 provider、schema、成员/顺序或精确总和失败
仍关闭；没有新增跨仓协议字段或不可逆迁移。

本状态只表示代码、测试和正式设计在 PR 分支重新一致。PR #19 尚待 maintainer review/merge，且未部署、未
调用真实 OpenRouter、未重试原失败请求、未经过生产自然批次，也未验收 broker、飞书或前端的新报告链路。
