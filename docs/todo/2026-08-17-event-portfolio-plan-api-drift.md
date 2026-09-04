# 批量事件组合 API 架构偏移

- **首次发现状态**：待讨论
- **当前状态**：已解决
- **发现/决策日期**：2026-08-17
- **决策人**：用户
- **负责人**：本会话实现与验证；Rachel 保持 PR #6-#12 原任务所有权；仓库 maintainer review/merge/deploy
- **基线**：`main@d8bd1896460420aecee26ec06e98cab437a01ac8`
- **当前分支**：`feat/api-event-portfolio-plan`
- **相关 PR/commit**：[a-stock-rachtrader#13](https://github.com/rra7963/A-stock-rachtrader/pull/13) / `7eb388c8f5c6fa0e524007fc9e375f253944e996`

## 原设计及位置

`docs/event-trade-plan-api.md` 把 API 冻结为每个 S 事件最多选择一只新股，明确禁止加仓、腾仓和卖出；
`README-组合持股比例功能.md` 与 `PortfolioAllocator` 只提供 2-20 ticker 的 CLI/Python 组合研究，未接收
事件集合、当前持仓或提供机器 HTTP 合同。

## 实际变化、证据和原因

用户要求配角在交易日 08:30 汇总上一窗口的全部 S 事件及已有持仓，由 TA 在多个事件、多个标的间
选择完整目标组合。现有 v1 不能表达持仓减仓/卖出、多个事件 provenance 或目标权重；开放组合堆栈
又尚未提供服务端接口，因此构成跨仓外部协议和职责变化。

## 影响与风险

- 长耗时多图分析需要独立 deadline，不能冒充 v1 的 15 分钟已被验证；
- 事件文本仍可能提示注入，必须逐标的隔离、转义和 strict structured output；
- 用户明确取消旧 10 仓/10 万限制，集中度由 TA 决定，但仍必须长仓、无杠杆、总权重 100%；
- `partial` 事件覆盖仍可交易，必须把不完整 provenance 传给 TA 和报告；
- API 不能获得 broker credential 或把目标权重描述成委托/成交。

## 方案与决定

1. **在 #12 head 上新增独立批量资源（已选择）**：不修改/接管 #6-#12，复用完整单票研究与组合分配概念。
2. 等 #6-#12 合并后再开工：更简单但阻塞当前三仓交付，未选择。
3. 从 main 重写组合引擎：重复实现和冲突风险高，否决。

协议、失败边界与验收见 `docs/event-portfolio-plan-api.md`。14:00 Asia/Shanghai 是用户明确选择的
当日结果截止；本任务只提交 PR，不授权 merge、部署或交易。

## 解决条件

TA schema/planner/API/store、Rachel downstream execution service producer/executor、downstream data consumer consumer、三仓文档与测试
全部重新一致，并有最终 diff、完整回归与 exact-head CI 证据后，才能更新为 `已解决`。

## 解决证据与仍未覆盖边界

TA 实现 commit `5f04d2ef14ae150934f610f142bc13373db86385` 已完成 closed schema、逐标的隔离
图分析、严格组合分配、共享单分析锁、独立持久幂等和部署配置。全仓本地测试为 770 passed、2 个
环境性 skip；严格全仓 Ruff、部署合同、Compose、生产镜像和镜像内导入均通过。PR #13 exact-head
CI run `32009973622` 的 Python 3.10/3.11/3.12/3.13、clean-install、strict Ruff、部署合同、镜像构建和
CI Gate 全部通过。

跨仓闭环也已取得独立精确 head 证据：Rachel downstream execution service PR #152 commit
`93e40fbac43d771e86c39dd3db9d9f4f757f16bf` 与 CI run `32010072246` 验证 canonical request hash、
response binding、执行和 v2 producer；downstream data consumer PR #57 implementation/CI head
`c76141d265d9a40528caea06018c9f1e2f01e2a7` 与 CI run `32022698447` 验证 v1/v2 consumer。结合最终
diff 复核，TA 仍只拥有账户无关的目标组合研究，TS 独占 broker/订单/成交事实，事件、deadline、
partial provenance、长仓无杠杆和精确 100% 协议均符合批准后的设计，因此状态更新为 `已解决`。

本状态不代表 PR #6-#12 或 #13 已 merge/deploy，也不证明 live LLM、自然事件、TA 服务、券商、飞书、
页面或订单链路可用；这些仍是按既定发布顺序完成后的独立受控验收，本任务未执行。

## 2026-08-17 跨仓 strict QA 重开

TA API 自身的测试与 review 结论仍为 PASS，但 Rachel downstream execution service #152 后续 strict QA 在实现 head 发现
组合 intent 与 legacy 硬退出 intent 未按股票统一仲裁，以及不可卖时过早写入日内 marker。该问题不
改变本仓 API 代码，却使本 TODO 的“三仓 producer/executor/consumer 全部重新一致”解决条件暂时不再
成立。因此本记录重开为 `已决策待落地`；在 #152 专项偏移完成用户决策、修复和复审前，不以本仓
绿色 CI 推导整条链路安全。

## 2026-08-17 跨仓 strict QA 解决证据

Rachel downstream execution service #152 已按用户决定以 fix
`341c893ee0ad2b5ded58b5118c6f166d4f041d78` 完成 legacy hard-exit 与 portfolio intent 的按股票原子
仲裁，并把持仓风险 trigger 与实际 order intent 分离。accepted/unknown、未决 buy/sell、部分成交、
被拒绝的买/卖、同日重启、真正 T+1 与新旧 position 边界均有回归；TS 本地全仓为 2803 passed、
confirmed-fill 为 36 passed，exact-head CI run `32025852363` 全绿。MiTuan-SmoAerosol strict QA
rereview `4951182313` 精确绑定该 fix head 并给出 `COMMENTED + PASS`，原两项反证均已消失。

本仓重开记录 head `03b71567ddc8d4f81fed0012cc2b29a0347774c1` 的 CI run `32023788960` 全绿；TA
实现仍是已审查的 `5f04d2ef14ae150934f610f142bc13373db86385`，外部 schema、canonical hash 和账户无关
职责未改变。downstream data consumer #57 final head `5b206610e7ae7c07a23ed5e55210f1d3a7678d29` 的 exact check job
`95366108471` 为 SUCCESS，v1/v2 consumer 仍向后兼容。三仓代码、配置、测试与批准后的协议再次一致，
因此本记录恢复为 `已解决`。

该状态不表示任一 PR 已 formal approved、merge 或 deploy，也不证明 live LLM、自然 S 事件、模拟券商、
飞书或页面端到端可用；它只关闭已版本化的架构偏移和代码级 strict-QA blocker。

## 2026-08-21 基于恢复后 main 的精确重放

恢复 PR #15 已获独立批准并以 squash commit
`d8bd1896460420aecee26ec06e98cab437a01ac8` 合入 `main`。该提交的 main CI run
`32461712153` 全绿，ACR workflow run `32461848195` 成功登记后台发布；163 正式服的
`.deploy-current-sha`、release、两个受管容器镜像 URI 与 `io.rachel.deploy.sha` 均精确指向该
提交，两个服务 healthy，部署结果记录全部 smoke 通过。因此原组合依赖不再停留在中间堆叠分支。

在改写前为旧 #13 head `166e1dc57f48cee4e79cfe9a3fef2017fcb2ec48` 保留本地备份引用。
随后只把基线 `e261a8c4352a57e94f3ea087674d106409f5bbfa` 之后的四个 API/审计提交重放到
`main@d8bd1896460420aecee26ec06e98cab437a01ac8`：

- `5f04d2e` → `7eb388c`：批量组合 API 实现；
- `608db37` → `ab669d8`：关闭本仓架构偏移；
- `03b7156` → `d6f16b7`：记录跨仓 strict-QA 重开；
- `166e1dc` → `5aac68e`：记录跨仓 strict-QA 解决证据。

四组 `git range-diff` 均为 `=`，rebase 无冲突；相对新 `main` 的实际产品增量仍仅为 16 个
API/schema/store/test/部署说明文件，没有重新携带 #6-#12 历史，也没有修改 Rachel downstream execution service、
downstream data consumer、账户、券商或交易职责。正式设计的事件、deadline、partial provenance、严格输出、
长仓无杠杆、精确 100% 与账户无关边界未发生变化。最终本地门禁、远端安全更新和新 exact-head
GitHub CI 仍须完成，不能用旧 head 的绿色结果替代。

重放后的本地验证已经完成：批量 API、legacy event-plan、组合编排和 CLI 聚焦栈为
`102 passed`；全仓为 `806 passed, 2 skipped, 19 warnings, 69 subtests passed`，两个 skip 仍是
未安装可选 Bedrock extra 和未配置 DeepSeek live key。严格 Ruff、部署合同、部署脚本定向
`26 passed`、Compose 配置、shell 语法、Python 3.12 全新安装/导入、两个 CLI help 和
`git diff --check` 均通过。生产镜像使用本机已记录的 `--network=host` 绕行成功构建，运行用户为
`appuser`，并在 `--network=none` 下通过 API/CLI 核心导入与两个 CLI help smoke。本机仍未安装
`shellcheck` 和 `actionlint`，二者及标准 Docker 网络构建必须由新 head 的 GitHub CI 独立证明。

远端分支在再次确认 `main@d8bd189` 与旧 PR head `166e1dc` 均未漂移后，以绑定旧 SHA 的
`--force-with-lease` 安全更新到 `5698b4f`，PR #13 base 已从中间堆叠分支切换为 `main`；GitHub
回读的 17 个文件等于 16 个产品/测试/部署增量加 1 个传播审计文件。该 force-push/base 更新尚未
生成新 Actions run，因此继续以新普通增量 push 触发 exact-head CI，绝不沿用旧 head 的绿灯。

普通审计增量 head `d72f503cdf8be79a291e6058999b81ea46bdae2f` 已触发 GitHub CI run
`32465739121`。Python 3.10/3.11/3.12/3.13、全仓测试、clean-install、严格 Ruff、部署合同、
ShellCheck、Actionlint、Compose、标准生产镜像构建和 CI Gate 全部成功。唯一注解是仓库既有
checkout/setup-python action 的 Node 20 弃用提示，不是失败，也不是本 PR 引入的代码或工作流变化。
结合本地 diff、重放等价性和正式设计复核，rebase 后实现仍符合既有设计预期。
