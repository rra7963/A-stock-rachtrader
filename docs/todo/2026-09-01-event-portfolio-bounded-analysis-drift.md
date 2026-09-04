# Event Portfolio API 有界并发与可观测性架构偏移

- **状态**：已解决
- **发现日期**：2026-09-01
- **负责人**：Trading Agent protocol owner、Supporting Lobster integration owner、生产发布负责人
- **相关分支 / PR / commit**：`fix/event-portfolio-bounded-analysis`；PR #17；实现 commit
  `1c38e53bf57e48a86c07dfce910c76eac4b9d9a2`；基线
  `cd1bcff588ea34fbac4bf78669c7e30914ad89d3`
- **阻塞范围**：用户已于 2026-09-01 解除实现与 PR 阻塞；merge、部署、live LLM、历史批次重试和
  任何交易仍不在授权范围

## 原设计及位置

[`docs/event-portfolio-plan-api.md`](../event-portfolio-plan-api.md) 第 72–84 行明确冻结以下行为：

1. 对请求中的每个标的逐个、串行运行既有 single-stock graph；
2. 每个标的都必须完成研究，不能静默省略、使用评级 fallback 或返回部分组合；
3. 每个 graph 调用前后检查绝对 `decision_deadline`；
4. 所有标的完成后才允许进入一次严格的横截面仓位分配；
5. 整个 API 继续受进程级 analysis lock、持久化幂等、无自动重试和失败关闭约束。

实现与设计一致：`tradingagents/api/planner.py::plan_portfolio()` 当前在一个 `for` 循环内复用同一个
`TradingAgentsGraph`，依次执行一至二十个标的；`tradingagents/api/app.py` 对整个请求只占用一个分析槽。

## 实际运行事实与证据

2026-09-01 生产只读核验确认，Supporting Lobster 08:30 批次向 TA 提交了 20 个标的。该请求从约
08:30 运行到 12:28，约 3 小时 58 分后以 HTTP 503 / `planner_failed` 失败关闭；没有返回目标仓位，
调用方没有生成交易 intent 或订单，也不会自动重试该失败 request id。

现有安全边界只向调用方和 SQLite 留下通用 `planner_failed`。容器日志中可见一次 Research Manager
严格结构化输出失败后转为自由文本的旁证，但它不能证明最终失败发生在哪个标的或阶段；FRED 缺失日志
被实现明确标为 optional，也不能当作终因。因此当前不能从证据中声称已知精确根因。

代码复核还确认，同一个 `TradingAgentsGraph` 实例不能安全地直接放入线程池：它会修改 `ticker`、
`curr_state`、`graph`、`_checkpointer_ctx` 和 `log_states_dict`，并写共享 memory log 与结果目录。

## 拟议偏移及产生原因

为了让最多 20 个标的在绝对 deadline 内有现实的完成余量，拟把“同一 graph 实例逐个串行”改为
“固定上限的并发 worker，每个在途标的使用独立 graph 实例”。所有标的仍必须完成，最终 allocator、
严格成员/顺序/评级绑定、整个请求的进程级 admission lock、幂等、无自动重试和失败关闭均保持不变。

这不是单纯的性能参数调整：它改变模型和数据源调用的时间重叠、瞬时配额压力、graph/记忆/文件状态
所有权以及失败后的在途任务收尾方式，所以必须由用户确认，不能根据当前慢运行事实静默落地。

## 影响与风险

- **时延**：并发上限 4 时，20 个独立标的在理想情况下可把 graph 阶段墙钟时间压到约串行的四分之一；
  但外部模型、数据源或单个卡住的调用仍可能使请求超时，不能承诺固定完成时间。
- **成本与限流**：总 graph 数不变，但最多四组 LLM/数据调用重叠，瞬时并发和峰值带宽约提高到四倍，
  可能触发 provider 429、数据库连接压力或第三方限流。
- **状态隔离**：每个 worker 必须拥有独立 graph 可变状态；共享 memory log 的读取/追加与公共文件写入
  必须串行化或改为明确的请求级隔离，不能让不同标的互相覆盖。
- **失败语义**：任何一个标的失败时整批仍失败关闭；已经发出的外部调用通常无法强制取消，API 必须
  保持 analysis lock，直到所有在途 worker 真正结束，不能提前接纳下一笔分析。
- **可观测性**：只记录安全的阶段码、完成数/总数和耗时，不记录原始事件、模型原文、凭据、账户数据
  或私有思维过程。更细的失败阶段有助于区分 graph、评级解析、allocator 与 deadline 问题。

## 可选处理方案

1. **推荐：可配置的独立 graph 有界并发，生产默认 4。** 允许值 `1..4`，默认 4；测试证明最大在途数、
   独立状态、全成员严格绑定、失败等待在途任务、deadline 与无重试语义。增加安全进度日志和持久化终端
   阶段码。该方案最有机会让 20 标的规模在现有窗口内完成，但需要接受峰值调用压力。
2. **保留串行，只补安全阶段码和进度。** 架构与峰值成本最稳定，也能在下一次失败时获得更准确证据；
   但不解决 20 标的约四小时墙钟时延，不能据此宣称配角已恢复正常可用。
3. **调用方降低标的上限或预先只挑一部分。** 会改变用户已选择的“高相关候选 + 上限”以及“TA 研究
   每个已提交标的”的跨仓合同，需要另开 Rachel downstream execution service 设计与 PR，不推荐。
4. **返回部分组合或给失败标的做评级 fallback。** 违反严格全成员与失败关闭设计，会让仓位决策建立在
   不完整研究上，不推荐。

## 我的推荐、待决策项与验收边界

推荐方案 1，生产默认并发 4，并保留所有既有安全门。理由是它不缩小研究 universe、不伪造部分成功，
同时针对当前最明确的容量证据提供有界改善。用户需要明确接受最多四倍瞬时 LLM/数据源并发及相应限流
风险；在确认前，本分支不修改实现、不创建 PR、不部署，也不重试 2026-09-01 的失败批次。

若获批准，验收至少包括：架构文档同步更新、独立 graph/thread-safety 测试、并发上限与 `1` 回退测试、
全成员 all-or-nothing 测试、失败/超时后锁保留测试、安全日志与 SQLite 阶段码测试、全仓测试和 CI。
PR 绿色只证明实现合同，不代表 live LLM、自然事件、模拟券商或生产部署已经验收。

## 决策

- **决定**：采用方案 1；并发允许值为 `1..4`，生产默认 4，每个在途标的使用独立 graph；接受最多
  四倍瞬时 LLM、数据库与数据源调用压力。
- **决策人**：用户。
- **决策日期**：2026-09-01。
- **保持不变的门禁**：全部请求成员必须完成才可分配；任何成员失败整批失败；等待所有在途 worker
  真正结束后才释放进程级 analysis lock；不自动重试；不返回部分组合或 fallback 评级；不改变 wire
  schema、账户与交易职责。
- **授权边界**：允许在本分支实现、验证、push 并创建 PR；不授权 merge、部署、live LLM/数据库业务
  调用、重试 2026-09-01 批次或访问 broker。

## 落地进度与实施后架构一致性检查

截至 2026-09-01，本分支已经完成以下落地：

- API 配置新增闭合的 `TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY=1..4`，默认 4；
- planner readiness 预创建并验证相同配置、相同 graph shape 且对象身份互异的 worker graph；
- 固定 worker/graph 使用共享队列完成精确请求 universe，任何成员失败后停止启动新 graph 调用，并等待
  所有已在途 worker 结束；只在全员完成后进入一次 allocator；
- 进程级 API admission lock、持久化 request-id 幂等、绝对 deadline、无重试、无 partial/fallback、
  wire schema 与 Rachel downstream execution service 的账户/订单职责边界均未改变；
- 跨 graph 共享的 Markdown memory log 读写使用进程内可重入锁串行化；正式 API 仍固定单个
  uvicorn worker。若未来改为多进程，必须先补跨进程文件同步并重新评审；
- SQLite 终态只接受 allowlist 阶段码；HTTP 保持通用 `planner_failed`；新增进度记录不包含标的、事件
  原文、账户事实、凭据、provider 原文或隐藏推理。

实施后再次核对了 [`docs/event-portfolio-plan-api.md`](../event-portfolio-plan-api.md)、
[`docs/event-trade-plan-api.md`](../event-trade-plan-api.md)、
[`docs/server-acr-deployment-plan.md`](../server-acr-deployment-plan.md) 与
[`docs/docker-deployment.md`](../docker-deployment.md)。正式组合 API 设计已经同步为用户批准的新并发
预期，其余跨仓、交易和部署不变量均符合既有设计；没有发现新的职责、协议、安全门禁、数据所有权或
不可逆迁移偏移。

本地验证证据：严格 Ruff 全仓通过；Python 3.13 全仓 `833 passed, 2 skipped, 69 subtests passed`；
Python 3.10、3.11、3.12 的组合 planner/API/store/memory 聚焦栈各 `113 passed`；包含部署脚本的聚焦
栈 `140 passed`；部署合同 `27 passed` 且独立 validator 与 Bash 语法通过；并发上限及失败等待测试连续
10 轮通过。两个 skip 分别是未安装可选 `langchain_aws` 与未配置 live DeepSeek key，均不属于本次
回归失败。

## 解决证据

- PR #17 的实现 exact head `1c38e53bf57e48a86c07dfce910c76eac4b9d9a2` 已通过 GitHub Actions
  run `33481760249`：Python 3.10、3.11、3.12、3.13 全仓测试、严格 Ruff、clean-install smoke、
  deployment contract/image build 与汇总 CI Gate 全部成功；
- GitHub issue comments、review comments 与 review threads 在状态转换前均为空；PR 为 open、非 draft、
  mergeable，仍等待仓库要求的人工 review；
- 实际 diff 已再次与四份正式设计核对，代码、配置、测试和正式设计已对齐用户批准的有界并发预期，
  未发现仍未记录的架构偏移；
- 本记录的状态转换会形成纯文档 final head，并再次运行 exact-head CI。若该最终 CI 或后续 review
  发现问题，必须继续修复并更新本记录，不能把本状态当作绕过门禁的依据；
- “已解决”只表示该架构偏移已在 PR 中落地并验证，不授权 merge、部署、live LLM/数据库调用、历史
  批次重试或真实交易。
