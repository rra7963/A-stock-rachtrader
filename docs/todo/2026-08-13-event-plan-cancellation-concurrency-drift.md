# Event Plan API 请求取消并发偏移

- **首次发现状态**：待讨论
- **当前状态**：已解决
- **决策日期/决策人**：2026-08-13 / 用户（要求修复 GitHub #4 且不得引入新问题）
- **发现日期**：2026-08-13
- **负责人**：trading-agents-adapted maintainer；修复后由严格 QA 复审
- **相关 PR/commit**：[trading-agents-adapted #4](https://github.com/BeixiHub/trading-agents-adapted/pull/4)
  `05e7025505c0e5707736fedc761031514480a44e`

## 原设计及位置

[`docs/event-trade-plan-api.md`](../event-trade-plan-api.md) 的 Idempotency, concurrency and failures
章节规定全进程同时最多运行一个分析；额外新请求返回 429。超时后 Python 不能安全终止 provider
线程，因此 late result 必须丢弃，且在该线程结束前不能让第二个分析进入。

## 实际实现与证据

`tradingagents/api/app.py` 为显式 timeout 实现了 worker done callback 持有 `analysis_lock` 的路径，
但请求协程被取消时会直接进入 `finally` 并释放锁。`asyncio.to_thread()` 中已经运行的 LLM/graph
线程不会随请求 task 取消而停止。

严格 QA 的最小异步反证让首请求进入 blocking planner 后取消客户端 task，再发送不同 request id。
第二请求返回 HTTP 200，观测为 `calls=2, max_active=2`；首 worker 此时仍未释放。取消还会绕过
`request_store.complete/fail`，使已 claim 的 request 继续显示 `running`，直到进程重启才被转换为
`process_interrupted`。

## 偏移原因

实现只把 `asyncio.wait_for` 的 timeout 当作 late-thread ownership transfer；客户端断开、ASGI task
取消或 worker shutdown 的 `CancelledError` 被当作普通函数退出处理。协程生命周期与不可取消的线程
生命周期因此脱钩。

## 影响与风险

- 两个 TradingAgents graph/provider 调用可在同一进程并发，违反成本、资源和隔离边界。
- 被取消请求的持久状态不立即终态化，同 request id 在当前进程内只能返回 in-progress，审计状态与
  实际 HTTP 结果不一致。
- TestingStrategies 依赖该单并发保证；因此本项阻塞 PR #4 及其下游 activation。

## 可选处理方案

1. **修改实现回归设计（推荐）**：显式捕获 cancellation；若 worker 已启动且未完成，把 lock
   ownership 转移给 worker done callback，丢弃 late result，并把 request 持久化为稳定 failed 状态；
   仅在 worker 已结束或尚未启动时由当前协程释放锁。增加 client cancellation/disconnect、后续不同
   request、同 request id 与 late exception 的反证。
2. **经确认后更新设计**：允许请求断开后启动并发分析，并增加多 worker 资源/成本/幂等设计。这会
   改变冻结的单并发安全合同，不推荐，也不能由当前 `finally` 行为静默形成新架构。

## 推荐、阻塞范围与解决条件

用户已选择修复现有 GitHub 问题，因此采用推荐方案 1；它与现有 timeout 路径一致，不改变外部 wire
schema。修复、对抗测试、Python 3.10–3.13/full CI 和严格 QA 复审均已完成，当前状态为 `已解决`。

## 落地证据、验证结果与剩余边界

- 修复 commit：`5743bc1570cc53efda253ef770f35837dbf2f9dc`。请求 task 取消后立即把已 claim
  request 持久化为 `analysis_cancelled`，并将 lock ownership 转交给 worker done callback；worker
  的迟到结果或异常均被丢弃，真实退出后才释放单分析锁。
- 回归测试覆盖取消后同 request id 永久失败、不同 request id 在 worker 存活时 429、迟到正常结果、
  迟到异常，以及 worker 退出后下一请求恢复；focused event-plan 测试 9 passed。
- Python 3.10、3.11、3.12、3.13 本地全仓分别为 `707 passed, 2 skipped, 69 subtests passed`；Ruff
  通过。GitHub Actions run
  [`31715588897`](https://github.com/BeixiHub/trading-agents-adapted/actions/runs/31715588897) 的四版本
  测试、clean-install、Ruff、deployment/image 和 CI Gate 共 8 项全部成功。
- 严格 QA discussion：
  [request cancellation 反证](https://github.com/BeixiHub/trading-agents-adapted/pull/4#discussion_r3776562429)。
- 剩余边界与既有设计一致：Python 不能强制终止已经进入 provider 的线程，所以取消后仍等待其真实
  退出；本修复不声称中止外部 LLM 调用。未调用真实 LLM、数据库或 broker，未 merge、deploy 或连接
  正式服。
