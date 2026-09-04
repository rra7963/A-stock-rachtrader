# 批量事件组合 API A 级与科创板兼容扩展

- **首次发现状态**：待讨论
- **当前状态**：已解决
- **发现/决策日期**：2026-08-27
- **决策人**：用户
- **负责人**：当前 Codex 会话负责实现与验证；仓库 maintainer 负责 review/merge/deploy
- **基线**：`main@8470c9e89624e62f16b68c5f4cd491d8eb6ee280`
- **当前分支**：`feat/api-event-portfolio-signal-levels`
- **相关 PR/commit**：PR #16；实现提交 `6f2f601095ec85fe72365d434e29a49cb34f3437`

## 原设计及位置

[`docs/event-portfolio-plan-api.md`](../event-portfolio-plan-api.md) 与
[`tradingagents/api/portfolio_schemas.py`](../../tradingagents/api/portfolio_schemas.py) 将批量组合请求
冻结为 schema 1.0：事件等级只能是精确 `S`，股票只能是普通沪深 A 股。单事件
`POST /v1/event-trade-plans` 也保持自身独立的 S-only、普通 A 股 1.0 合同。

## 实际变化与证据

Rachel downstream execution service 的Rachel Executor需要把“接收 A 级事件”和“允许科创板候选”作为两个独立开关，并在
生产版本化配置中开启；TA 必须收到真实 `S|A` provenance，并能研究 `688xxx/689xxx`。当前 closed
schema 会在模型调用前拒绝上述输入，planner 还会把事件等级硬编码为 S，并把 688/689 错误映射到
深圳后缀，构成跨仓外部协议偏移。

## 用户决定

用户选择最小跨仓兼容方案：增加本仓 PR 与 Rachel downstream execution service PR，不修改前端；TA 必须先
merge/deploy，TS 才能 merge/deploy。本任务只授权代码、测试与未合并 PR，不授权 merge、部署、
正式服务调用或交易。

## 影响与风险

- 静默放宽 1.0 会让既有版本号失去含义，并可能让旧 consumer 错误接受其不支持的输入；
- 修改共享 `StockCode` 会意外扩展旧单事件接口，因此科创板代码必须限定在 portfolio resource；
- A 级必须原样进入逐标的触发上下文，不能伪装成 S；
- 688/689 必须映射上海市场，TA 仍只输出账户无关目标权重，不拥有委托数量规则或 broker 职责；
- TS 先于 TA 发布会导致含新能力的整批请求确定性失败。

## 处理方案与推荐

采用显式、向后兼容的 portfolio schema 1.1：

1. 1.0 保持只接受 S 与普通 A 股；
2. 1.1 接受精确 `S|A` 和普通 A 股加 `688/689`；
3. 使用 portfolio 专属股票代码类型，不修改 legacy `/v1/event-trade-plans`；
4. response 回显 request schema version，API binding 强制两者一致；
5. planner 传递真实事件等级并正确使用 `.SS` 后缀。

该方案既保持旧 wire 合同，又让新 producer 明确 opt in，且不扩大账户、券商、订单或成交职责。

## 阻塞范围与解决条件

本仓实现、文档、聚焦与全量测试、exact-head CI 全部通过后才可把状态更新为 `已解决`。发布顺序仍是
硬门：TA 兼容版本先发布，TS 再启用 1.1；本记录解决不等于任一 PR 已 merge/deploy，也不证明 live
LLM、自然事件、模拟盘或真实订单链路可用。

## 落地证据与验证结果

- schema 1.0 的 S-only/普通 A 股约束与 legacy single-event 合同保持不变；schema 1.1 显式接受
  `S|A` 与 `688/689`，response version 绑定 request version；
- planner 向逐标的上下文传递真实等级，并把 `688/689` 路由为 `.SS`；
- 聚焦 schema/planner/API 43 项通过；全仓 819 passed、2 skipped、69 subtests passed；严格 Ruff、
  clean-install、部署合同、Compose、shell 与生产镜像构建均通过；
- 实现 head `6f2f601095ec85fe72365d434e29a49cb34f3437` 的 GitHub exact-head CI run
  `33050576860` 全绿，且 PR 创建后无 review/inline comment；
- 架构一致性结论：符合 [`docs/event-portfolio-plan-api.md`](../event-portfolio-plan-api.md) 的既有职责
  所有权。TA 仍只输出账户无关目标权重，没有获得账户、券商、委托、成交或硬退出职责。

仍未覆盖的边界：PR 尚未 merge/deploy，未运行 live LLM、自然事件或任何券商链路。发布硬门保持
TA PR #16 先于 Rachel downstream execution service PR #154。
