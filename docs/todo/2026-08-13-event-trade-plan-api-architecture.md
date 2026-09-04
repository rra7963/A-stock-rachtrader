# 事件交易计划 API 架构扩展

- **状态**：已解决
- **发现日期**：2026-08-13
- **决策日期**：2026-08-13
- **决策人**：用户（产品与跨仓边界）
- **负责人**：本任务由当前 Codex 会话负责实现与验证；仓库 maintainer 负责 review/merge/deploy
- **基线**：`main` `f01c7c5d34d6824dc3748a709ef87ceffe90118a`
- **当前分支**：`feat/api-event-trade-plan`
- **相关 PR**：[#4](https://github.com/rra7963/A-stock-rachtrader/pull/4)
- **落地提交**：功能 `cbd4b5be231f0b6479a2605a32efc6db69482083`；严格合同修复
  `59e00eba3c8dbf13d2a240464d3ae8de9c0f72e5`

## 原设计及位置

1. [`../server-acr-deployment-plan.md`](../server-acr-deployment-plan.md) 第 2.2 节把当前运行形态定义为
   无 HTTP 端口的 CLI 工具箱；第 17 节同时记录“未来还需要一个 HTTP/API”，但没有冻结其职责、
   协议、安全和部署合同。
2. [`../docker-deployment.md`](../docker-deployment.md)、仓库 [`README.md`](../../README.md) 与
   `docker-compose.server.yml` 只部署一个常驻 `tradingagents` CLI 容器，运维通过
   `docker compose exec` 启动分析。
3. 当前部署前检与后台部署脚本只验证一个 `tradingagents` service 的镜像、health 和 CLI smoke；
   当前结构化 Portfolio/Trader 输出仍面向人类报告，不是可由策略执行的版本化交易计划协议。

## 已批准变化

为 Rachel downstream execution service 的“Rachel Executor”提供同步、版本化、受认证的事件交易计划 API。保留 CLI
service，新增独立 API service；输入限于 S 级事件、调用方筛出的高相关普通 A 股候选和不含账户
身份的组合摘要，输出最多一只股票的封闭买入计划或明确拒绝。API 不拥有账户、不调用券商、不卖出、
不保证执行；Rachel downstream execution service 是最终风险校验、幂等下单、持仓与 -9%/+20% 退出规则的 owner。

## 证据与偏移原因

- 当前包级入口只接受 ticker/date，不能接收外部事件 envelope 或多候选集合。
- 当前自由文本 position sizing、五档 rating 和 markdown 报告不能直接作为 broker payload。
- 当前 server Compose、health、回滚与基线前检只认识一个 CLI service。
- 产品需求把已规划但未设计的“未来 HTTP/API”变成 TS 自动决策依赖，因此必须补齐协议、认证、
  幂等、提示注入边界、迁移兼容和多 service 部署验证。

## 影响与风险

1. API 输入是可能包含恶意指令的事件文本，必须按不可信数据处理，限制字段和长度，且不得允许其
   改写系统规则或候选 allowlist。
2. LLM 输出非确定，必须使用严格结构化 schema；无 schema、候选越权、金额/价格越界和解析失败
   全部 fail closed，不使用自由文本兜底。
3. 相同 request id 必须绑定相同规范化输入并持久化结果；相同 id 的不同输入必须冲突，避免重投
   产生另一份计划。
4. 新旧 release 的 service 数量不同；部署前检与回滚必须从各 release Compose 推导预期 services，
   才能从单 CLI 版本迁移到 CLI+API，也能回滚到旧版。
5. external Docker network 和 bearer token 是 activation 前置条件；不得把 secret 写入镜像、release、
   Git 日志或 API 响应，本任务不在服务器创建它们。

## 已作决定

1. 两仓同步 API；总计四张未合并 PR，当前仓库负责第一张。
2. 独立 API 容器、共享 external Docker bridge network、bearer token、无宿主机端口。
3. 同步请求上限 15 分钟；TS 不自动重试 API。
4. 只接受 S + 高相关普通 A 股；每事件最多一只新股，禁止对持仓/待成交加仓；单股 10 万元、
   组合 10 仓是协议上限，TS 会再次确定性校验。
5. API 只产出买入计划；TS 独占订单、持仓和退出职责。

## 推荐落地与验收

- 新增独立、版本化设计文档和 Pydantic wire schema；请求使用稳定 id 与规范化 SHA-256。
- 对现有 TradingAgents graph 增加显式、不可信 trigger-event context，由严格结构化候选选择与最终
  计划适配层把研究结论收敛为 0/1 个计划。
- 使用持久 SQLite 状态缓存完成 request id 去重；并发、超时、鉴权、冲突与失败均有封闭状态。
- 更新 Compose、部署前检、health、smoke、回滚、CI 和 runbook，使两个 services 使用同一不可变
  SHA 镜像且整体通过健康验证。
- 使用 hermetic 单元、API contract、提示注入、幂等、部署迁移和容器配置测试；不得在测试中调用
  实际 LLM、数据库、网络或券商。

## 阻塞与状态转换

- **本任务始终禁止**：merge、部署、正式服配置和交易。
- **可以继续**：按已批准边界实现、测试、推送分支并创建未合并 PR。
- **落地证据（2026-08-13）**：
  - `docs/event-trade-plan-api.md` 冻结职责、wire schema、认证、幂等、提示注入、超时、部署迁移和
    非目标；README、server ACR 设计与运维 runbook 已同步。
  - `tradingagents/api/` 实现封闭 schema、候选选择、现有 graph 事件上下文、严格计划适配、
    deterministic post-validation、SQLite request-id 状态机和认证 HTTP transport。
  - `docker-compose.server.yml` 保留 CLI 并新增无 host port 的 API service、独立 token env、独立卷
    与 external network；部署前检、health/smoke、回滚和 CI 合同按 release 动态 service 集合验证。
  - 最终修复 head 在本地 Python 3.10、3.11、3.12、3.13 全仓各为 705 passed、2 skipped、69
    subtests passed；skip 为未安装的可选 Bedrock extra 与无真实 key 的既有 DeepSeek live test。
    `uv run --frozen ruff check .`、部署合同、`bash -n`、server Compose config 均通过。
  - 生产镜像成功构建为非 root `appuser`，CLI import/help 与 API import 通过；API 临时容器 readiness
    返回 200，Docker `PortBindings={}`，验证没有宿主机端口发布。临时容器随后已停止并自动删除。
- **PR CI 复核**：本机没有 `shellcheck` 二进制，因此没有声称本地 shellcheck 通过；GitHub Actions
  run `31704530014` 已对最终 head 完成 Python 3.10–3.13、clean-install、strict Ruff、deployment
  contract/image build（含 shellcheck）与 CI Gate，8 项全部成功。
- **后续严格 QA**：初始实现曾存在 prompt delimiter escape、Decimal wire 类型和 Python 3.10
  兼容偏移；用户选择回归设计后已由 `59e00eb` 修复并以
  [`2026-08-13-pr4-strict-qa-architecture-drift.md`](./2026-08-13-pr4-strict-qa-architecture-drift.md)
  留存完整发现、决策和解决证据。
- **结论**：实现符合仓库既有不可变镜像、PR-only、非 root、server-only secret、部署锁/回滚设计，
  并把原先已规划的 HTTP/API 扩展落为显式机器接口；未发现仍需用户决策的架构偏移。本文件保留为
  审计历史。
