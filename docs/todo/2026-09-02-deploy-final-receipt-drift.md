# ACR 部署最终回执与 smoke 可观测性架构偏移

- **状态**：已解决
- **发现日期**：2026-09-02
- **决策日期**：2026-09-02
- **决策人**：用户
- **负责人**：Trading Agent 生产部署负责人
- **相关分支 / PR / commit**：`fix/deploy-smoke-final-receipt`；PR #18；实现 commit
  `65fe8a84e660c4dbb284c9776cc2cb2cf85d70ed`；Strict QA 修复 commit
  `7bb29d9927e650d30f8a26daa3b5f08770ac7a08`；rollback 失败归因修复 commit
  `6e47461a0f7ff7e469813c4e9508d3599e4cda22`；基线
  `7628b7ea0f34e4a388f78a0d6b44fba9a74c6b18`
- **阻塞范围**：用户已授权实现、测试、push 和创建 PR；merge、生产部署、live 依赖调用、历史批次
  重试和任何交易仍未授权

## 原设计及位置

[`docs/server-acr-deployment-plan.md`](../server-acr-deployment-plan.md) 第 1、4、5.2、8、10.1 与 11.3 节，
以及 [`docs/docker-deployment.md`](../docker-deployment.md) 的“部署成功语义与观察”明确规定：GitHub
Actions 只登记 detached server deployment，launcher 约两秒后返回；workflow 绿色仅表示服务器已受理，
不等待镜像拉取、容器替换、smoke 或回滚终态。服务器后台脚本独占容器变更、回滚、结果和通知职责。

既有不变量还包括：只部署完整 SHA 镜像；按逐请求结果落盘；latest-request 覆盖；全局部署锁；失败关闭；
上一成功版本自动回滚；launcher 不执行 Docker 操作；服务器不拉源码或现场构建。

## 实际偏移与证据

2026-09-02 对已合并 PR #17 的生产发布进行只读核验：GitHub deploy run `33499479900` 为绿色，服务器
精确逐请求结果却是 `status=rolled_back`，生产 current SHA 仍是旧版本
`cd1bcff588ea34fbac4bf78669c7e30914ad89d3`。新版本容器曾进入 healthy，随后一个部署 smoke 出现短暂
连接断开并触发回滚；现有日志把 Dynamic Agent `/health`、event-plan `/health/ready`、import 和 CLI help
串成无阶段标签的布尔链，无法从回执确定失败端点。

因此当前实现同时存在两个验收缺口：GitHub 绿色不能证明发布落地，且合法回滚会被外部误读为成功；
单次、无阶段标签的 HTTP smoke 既容易受启动边缘抖动影响，也无法给出安全、闭合的失败阶段。

## 用户批准的新设计

用户于 2026-09-02 选择推荐方案：

1. 保留服务器后台任务和 launcher 快速返回；GitHub 在随后独立步骤中轮询本次
   `run_id.run_attempt` 的精确结果文件，等待有界终态。
2. `rolled_back`、`failed`、`rollback_failed`、`launch_failed` 必须使 workflow 失败；覆盖协议的
   `success_superseded` 与 `superseded` 继续作为非失败终态。
3. 每个 smoke 使用闭合阶段名记录开始、尝试与结果；只对 Dynamic Agent health 和 event-plan readiness
   增加有界短重试，不重试配置/import/CLI，更不重试任何业务分析或交易。
4. 结果等待器只读精确逐请求文件，校验权限、owner、字段、request/SHA/image/release 身份；不得读取
   `.deploy-last-result.acr` 冒充本次回执，也不得输出凭据、原始异常或响应。

## 影响与风险

- **Actions 成本与时延**：workflow 将等待实际终态，最长等待值会占用 runner；超时只代表本次 GitHub
  验收失败，不能假装已经取消服务器后台任务。
- **发布语义**：绿色由“服务器已受理”提升为“本次请求已进入允许的非失败终态”；回滚会立即体现为红色。
- **瞬态容忍**：HTTP smoke 的短重试可吸收容器健康刚稳定时的单次连接抖动；上限必须闭合，不能把持续
  故障拖成无限等待。
- **并发覆盖**：被更新请求取代仍是正常协议终态；逐请求文件和 identity 校验防止读到另一次部署结果。
- **安全**：等待器不持有 ACR 应用密钥，不改变服务器状态；输出只包含经过校验和 shell escaping 的闭合
  部署元数据。

## 可选方案与推荐理由

1. **已批准：detached server deployment + GitHub 精确终态等待。** 保留服务器故障恢复能力，同时让
   CI 结论与真实发布终态一致，改动范围最小。
2. 让 launcher 同步等待整个部署。实现更少，但 SSH 断开会混淆任务 owner，也破坏 launcher 快速返回
   和服务器后台接管边界，不推荐。
3. 保持 workflow 快速绿色，仅靠飞书/人工看服务器结果。不会修复误报成功，已被本次事故证明不足。
4. 取消 smoke 或无限重试。前者削弱发布门禁，后者可能掩盖持续故障并长期占锁，均不接受。

## 验收标准

- 回执等待器对 exact request 的成功、回滚、失败、覆盖、身份错配、权限异常与超时均有测试；
- workflow 明确调用等待器，且 `rolled_back` 返回非零；
- smoke 日志能定位闭合阶段，HTTP smoke 的尝试次数/间隔有上限，确定性检查不重试；
- launcher 仍在启动窗口后快速返回，服务器后台仍独占 Docker mutation 和 rollback；
- 部署合同、Bash syntax、ShellCheck、Actionlint、聚焦 pytest 与全仓 CI 通过；
- 实施后重新核对正式设计并把本记录更新为 `已解决`，保留 exact-head CI 和 review 证据。

## 落地结果与实施后架构一致性检查

截至 2026-09-02，PR #18 已完成以下落地：

- launcher 仍在约 2 秒启动窗口后返回，Docker pull/up、smoke、状态持久化、回滚和通知仍由服务器
  detached background 独占；
- workflow 新增独立结果等待步骤，只读本次逐请求文件，最长 2700 秒、每 5 秒轮询；等待器校验
  `0600` mode、owner、闭合字段和 request/SHA/image/release 身份，拒绝符号链接、未知/重复字段与
  identity mismatch；
- 三个成功/覆盖终态返回 0；四个失败/回滚终态返回非零，尤其 `rolled_back` 不再被 workflow 误报绿色；
- smoke 拆成闭合阶段，最终失败回执记录安全阶段名；Dynamic Agent health 与 event-plan readiness
  最多重试 3 次、间隔 5 秒，event-plan import 被拆为单次独立检查；
- 配置、import、CLI、LLM 分析、事件批次和交易请求均不自动重试；不可变 SHA、全局锁、latest-request
  覆盖、失败关闭、上一成功版本回滚、无服务端源码构建和 secrets 边界保持不变。

实施后再次核对了 [`docs/server-acr-deployment-plan.md`](../server-acr-deployment-plan.md) 与
[`docs/docker-deployment.md`](../docker-deployment.md)，正式设计已经同步为用户批准的新最终回执合同；
实现与设计的职责所有权、依赖边界、数据流、外部状态协议、不变量、非目标和验收标准一致。未发现新的
仓库职责、跨仓协议、安全门禁、数据所有权或不可逆迁移偏移。

## 解决证据

- 本地 Python 3.10、3.11、3.12、3.13 部署聚焦栈各 `44 passed`；Python 3.13 全仓
  `850 passed, 2 skipped, 69 subtests passed`；严格 Ruff、部署合同、Bash syntax、ShellCheck 与
  Actionlint 均通过。两个 skip 分别是未安装的可选 `langchain_aws` 和未配置 live DeepSeek key，
  均不是本次回归失败；
- PR #18 实现 exact head `65fe8a84e660c4dbb284c9776cc2cb2cf85d70ed` 已通过 GitHub Actions
  run `33604915329`：Python 3.10–3.13、严格 Ruff、clean-install、deployment contract/image build
  与最终 `CI Gate` 全部成功；
- GitHub issue comments、review comments 和 review threads 在状态转换前均为空；PR 为 open、非 draft、
  mergeable，仍按 ruleset 等待人工 approve；
- 本次状态转换会形成纯文档 final head 并重新运行 exact-head CI。若最终 CI 或后续 review 发现问题，
  必须继续修复并更新本记录，不能用此前 head 的绿色绕过门禁；
- “已解决”只表示代码、测试和正式设计已重新一致，不授权 merge、生产部署、live TA/数据库/券商调用、
  历史批次重试或任何交易。

## 2026-09-02 Strict QA 重新打开

PR #18 head `a082dd390492dd2a9499973467f5bb642f7acbcd` 的 Strict QA 在
[`issuecomment-5506687572`](https://github.com/rra7963/A-stock-rachtrader/pull/18#issuecomment-5506687572)
指出：正式方案前半部已经同步为“launcher 快速返回、workflow 有界等待精确终态”，但第 10.4、14–16、
18 节仍保留“Actions 提前结束”“只登记任务并立即返回”“不让 Actions 等待”等旧结论。因此上面的
“正式设计已经同步、未发现新偏移”结论并不完整，状态从 `已解决` 恢复为 `已决策待落地`。

用户明确要求修复 PR #18 的 GitHub 问题。落地范围是：统一同一份正式方案的并发、首次部署验收、
验收清单、风险表与推荐总结，并让部署合同 validator/test 拒绝旧反向语义。运行代码、服务器状态、
部署、live 依赖、历史批次和交易均不在这次补丁范围。完成设计、合同测试、本地验证与新 exact-head CI
后，才可再次把本记录更新为 `已解决`。

## Strict QA 解决证据

- 正式方案第 10.4 节现已明确：workflow concurrency 覆盖正常的精确结果等待，但不是服务器后台任务的
  租约或取消机制；runner 取消、等待超时、SSH 中断或经批准的运维登记后，server-side
  `latest-request` 与全局锁仍负责最终版本顺序；
- 首次部署验收、服务器验收清单、风险表、第 18 节和总结均已统一为“launcher 快速返回，workflow
  有界等待精确终态，服务器后台独占 Docker mutation/rollback”；正式方案中已不存在 Strict QA 列出的
  七种旧反向结论；
- deployment validator 现在把正式方案纳入 required files，同时要求新的并发/最终回执语义，并对七种
  旧表述失败关闭；新增的七项参数化合同测试证明每一种回退都会被拒绝；
- 本地 Python 3.10、3.11、3.12、3.13 部署聚焦栈各 `51 passed`；Python 3.13 全仓
  `857 passed, 2 skipped, 69 subtests passed`；严格 Ruff、部署合同、Bash syntax 和 diff whitespace
  均通过。两个 skip 仍是可选 `langchain_aws` 和未配置 live DeepSeek key；
- PR #18 修复 exact head `7bb29d9927e650d30f8a26daa3b5f08770ac7a08` 已通过 GitHub Actions
  run `33612264252`：Python 3.10–3.13、严格 Ruff、clean-install、ShellCheck、Actionlint、Compose、
  deployment contract/image build 与最终 `CI Gate` 全部成功；
- 实施后重新核对正式设计、validator、测试与实际 diff，职责所有权、依赖边界、并发数据流、终态协议、
  安全门禁、非目标和验收标准已经一致；没有新的跨仓、外部协议、数据所有权或不可逆迁移偏移；
- 本状态转换形成纯文档 final head 并再次触发 exact-head CI。若最终 CI 或后续 review 发现问题，必须继续
  修复并更新本记录；`已解决` 不授权 merge、部署、live 依赖、历史批次重试或交易。

## 2026-09-02 Strict QA rollback 失败归因重新打开

PR #18 head `888062d9586695575895b49b2cd26bdd55fec6cb` 的 Strict QA 在
[`issuecomment-5508746427`](https://github.com/rra7963/A-stock-rachtrader/pull/18#issuecomment-5508746427)
指出：原设计和上面的落地结论要求最终失败回执记录安全、闭合的失败阶段，且
`rollback_failed` 必须为人工恢复提供准确证据；实际 `rollback()` 虽能从
`run_service_smokes()` 得到 `FAILED_SMOKE_STAGE`，但 pull、compose-up、health 或 smoke 任一步恢复失败后，
仍把最初触发回滚的原因原样写入最终回执。由此回执只能解释为什么开始回滚，不能解释回滚本身为什么失败。

直接证据是：新版本 compose-up 后注入受控失败，再让上一成功 release 的确定性 `cli-import` smoke 失败；
脚本正确返回非零且状态为 `rollback_failed`，但 message 仍为
`controlled failure injected after compose up`，完全缺少 `stage=cli-import`。原因是 rollback 使用单个
`&&` 链执行四步恢复，链失败后没有保存失败步骤，随后统一复用了 primary reason。

影响是最需要人工介入的 `rollback_failed` 终态会误导恢复定位，并与最终回执可观测性合同冲突。可选处理：

1. **推荐且已由用户的修复指令批准**：对 rollback pull、compose-up、health、smoke 分别形成闭合的
   rollback failure reason；最终 message 明确区分 primary failure 与 rollback failure，继续复用 500 字符
   截断，绝不写原始异常、响应或凭据。
2. 只把 message 替换为 rollback failure。能定位恢复步骤，但会丢失触发回滚的 primary failure，信息较少。
3. 写入底层命令 stderr。可能泄露镜像仓库、响应或凭据，也破坏闭合集合，禁止采用。

用户已再次明确要求修复 PR #18 的 GitHub 问题，因此状态记为 `已决策待落地`。阻塞范围仅为 PR #18
合并门禁；不授权 merge、生产部署、live TA/数据库/券商调用、历史批次重试或交易。完成实现、反例回归、
本地全栈和新 exact-head CI 后，才可重新标记为 `已解决`。

## Strict QA rollback 失败归因解决证据

- `rollback()` 已把上一成功 release 的 image pull、Compose up、health/image identity 和 smoke 恢复步骤
  拆成独立失败分支；每个分支只形成闭合原因，不把底层 stderr、响应或凭据写入结果；
- `rollback_failed` message 先记录 rollback failure、再记录 primary failure，继续由统一
  `sanitize_message` 限制为 500 字符；即使发生截断，也优先保留真正需要人工介入的恢复失败位置；
- 新增的集成反例先在旧实现上稳定失败，证明最终 message 只有 primary reason；修复后同一场景得到
  `rollback failure: rollback smoke failed at stage=cli-import; primary failure: controlled failure injected after compose up`，
  且相邻的成功回滚场景继续通过；
- 正式部署方案和服务器运维文档已同步上述 rollback failure 安全归因与消息顺序；状态机、全局锁、
  latest-request、launcher/background ownership、有限 HTTP 重试和 Docker mutation/rollback 边界没有改变；
- 本地 Python 3.10、3.11、3.12、3.13 部署聚焦栈各 `52 passed`；Python 3.13 全仓
  `858 passed, 2 skipped, 19 warnings, 69 subtests passed`；deployment contract、全仓 Ruff、Bash syntax、
  ShellCheck 和 diff whitespace 均通过。两个 skip 仍是可选 `langchain_aws` 与未配置 live DeepSeek key；
- PR #18 实现 exact head `6e47461a0f7ff7e469813c4e9508d3599e4cda22` 已通过 GitHub Actions run
  `33628166424`：Python 3.10–3.13、strict Ruff、clean-install、ShellCheck、Actionlint、Compose、生产镜像
  构建与最终 `CI Gate` 全部成功；
- 实施后重新核对 `docs/server-acr-deployment-plan.md`、`docs/docker-deployment.md`、实际 shell diff 与回归
  输出，实现、测试和正式设计已重新一致，没有新的仓库职责、跨仓协议、安全门禁、数据所有权或不可逆
  迁移偏移。状态因此恢复为 `已解决`，但仍不授权 merge、部署、live 依赖、历史批次重试或交易；
- 本状态转换形成纯文档 final head 并重新触发 exact-head CI。若最终 CI 或后续 review 再发现问题，必须继续
  重新打开并修复，不能用实现 head 的绿色绕过门禁。
