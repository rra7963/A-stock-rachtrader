# 组合能力堆叠未传播到 main 的架构偏移

- **首次发现状态**：待讨论
- **当前状态**：已解决
- **发现/决策日期**：2026-08-18
- **决策人**：用户（选择独立恢复 PR）
- **负责人**：本会话负责恢复分支、验证和 PR；原 #10-#12 作者仍保留原实现归属
- **阻塞范围**：无；PR #13 已基于 `main` 完成重放和 exact-head CI，合并及后续发布仍是独立授权
- **相关 PR/commit**：恢复 PR #15；#10 `eb92f919a49074fb31e1b4a87698cdeefd37e295`、#11
  `baa868b6f5feb0904de453b8a08aba56a66f13b3`、#12
  `cd5503dd708a4718a208d113972a2a6bc9c4d67c`、#13
  `166e1dc57f48cee4e79cfe9a3fef2017fcb2ec48`

## 原设计及其位置

PR #10-#12 的组合编排、CLI、报告与文档是 PR #13 批量组合 API 的显式前置能力。该依赖记录在
PR #13 分支的 `docs/event-portfolio-plan-api.md` 与
`docs/todo/2026-08-17-event-portfolio-plan-api-drift.md`：#13 应在 #6-#12 全部进入
`main` 后只保留 API 增量，不能重复实现或接管前置堆叠。

## 实际实现与证据

GitHub 将 #6-#12 全部显示为 `MERGED`，但这些 PR 的 base 是逐层堆叠分支。#6 于
2026-08-18 03:45:09Z 合入 `main` 时，`main@882091ffe6e66cefaf0d36cff25c1c3fe3ea8fd2`
的文件树只等于 #9 最终树 `88968862a1e3f64a597d7edac0e5699ccee30ad2`。

#10、#11 分别在 04:01:13Z 和 04:01:47Z 才合入各自父分支，且之后没有再次传播到
`main`。因此 `main` 缺少 `propagate_portfolio`、`save_portfolio_report`、组合 CLI、报告和
相关回归。#11 合并后的汇总树 `c2bc1226b6524dbeb598aac36ec65875757435a7` 相对
`main` 涉及 12 个文件、1396 行新增和 153 行删除，并已包含更早合入其 head 的 #12 文档。

## 偏移原因

堆叠 PR 按中间 base 分别合并，但父层进入 `main` 的时间早于后续子层回传。平台的
`MERGED` 状态只证明变更进入当时的 base，不保证最终提交或文件树可从 `main` 到达。

## 影响与风险

- 机械地只把 #13 的四个 API 专属提交 rebase 到 `main` 会丢失运行依赖；
- 把缺失能力静默塞入 #13 会扩大其所有权和评审范围；
- 仅检查 PR 状态会错误声称多标的组合能力已经可以由生产镜像构建和部署；
- 当前生产服务不因本地恢复工作发生变化，本记录不授权部署、模型调用或交易。

## 可选方案、决定与理由

1. **独立恢复 PR（用户已选择）**：从最新 `main` 精确重放 #10 的有效合并增量，再重放
   #11 及其携带的 #12 文档；验证恢复后的受控文件树与原汇总树逐字一致。该 PR 合入后再
   rebase #13。
2. 将 #10-#12 一并塞入 #13：较快但会把 API PR 扩大为组合恢复 PR，未选择。
3. 等待原维护者再次传播：不改变所有权但继续阻塞链路，未选择。

独立恢复 PR 保留原作者和原设计，不重新发明组合算法，也不修改 #13 分支，是范围最小且
审计边界最清楚的方案。

## 解决条件

恢复分支必须满足以下全部条件后才可合并：

1. 除本审计记录外，受控文件树与 #11/#12 汇总树逐字一致；
2. 全仓 pytest、严格 Ruff、clean-install、部署合同和生产镜像门禁通过；
3. 恢复 PR exact-head GitHub CI 全绿，并完成实际 diff 回读；
4. 恢复 PR 进入 `main` 后，重新验证关键提交/文件树可达性；
5. 随后将 #13 的 API 专属增量 rebase 到新的 `main`，不得继续携带旧堆叠历史。

在第 4、5 项完成前，本记录保持 `已决策待落地`；分支绿色不等于 main 已修复。

## 恢复分支实现与本地验证

恢复分支 `fix/portfolio-stack-main-propagation` 从
`main@882091ffe6e66cefaf0d36cff25c1c3fe3ea8fd2` 创建：

- `9f3fe96d3c856d76ef8f0eaf86cadd41cdaa6fd3` 精确重放 #10 的第一父增量；
- `417d748145ddac2a562e630092b20bd46c571a96` 精确重放 #11 的第一父增量，并携带 #12；
- 在加入本审计文件前，恢复 head 与原汇总提交 `baa868b6f5feb0904de453b8a08aba56a66f13b3`
  具有同一文件树 `c2bc1226b6524dbeb598aac36ec65875757435a7`。

本地验证结果：全仓 `748 passed, 2 skipped, 19 warnings, 69 subtests passed`；两个 skip 分别是
未安装可选 Bedrock extra 与未配置 DeepSeek live key。严格全仓 Ruff、部署合同、部署脚本定向
`24 passed`、clean-install/import、server Compose、部署 shell 语法均通过。生产镜像成功构建，
用户为 `appuser`，镜像内 `tradingagents`、`tradingagents.api.app`、`cli.main` 导入以及 CLI help
smoke 通过。

本机 Docker 默认 bridge 元数据存在但 `docker0` 设备缺失，标准构建首次在创建 veth 时失败；未重启
Docker 或修改宿主网络，而是只为本地镜像构建使用 `--network=host`，镜像内 smoke 使用
`--network=none`。该环境绕行不替代 GitHub 干净 runner 的标准镜像门禁；GitHub exact-head CI、
review、merge/main 可达性和 #13 rebase 仍是独立门禁，因此状态不提前改为 `已解决`。

恢复 PR #15 已以实现/本地验证 head
`efba24ae550fa57de22d7f0fa6d5c7e1b5cccb5b` 创建并完成 base/head/files/body 回读。首轮
GitHub CI run `32112913804` 的全部 job 在分配 runner 前失败：每个 job 均为 `runner_id=0`、
`steps=[]`，GitHub annotation 明确说明近期账户付款失败或 Actions spending limit 需要提高。
该结果不是 pytest、Ruff、部署合同或镜像构建失败，但也不满足 exact-head CI 门禁；不得用原
#10/#11 的绿色 CI 或本地结果替代。修复 GitHub Billing & plans 后必须在 PR #15 的最终 head
重新运行全部 CI，通过 review/merge 并验证 main 可达性后，才能继续关闭本记录与 rebase #13。

## 2026-08-21 基于最新 main 的重放复核

PR #14 已先行合入 `main@c914a831c119fcced8df4e29952970dee39ee77d`。恢复分支先为旧
head `77582542e23f610c1e9bdeba5f0d5963ed0a0eab` 建立本地备份引用，再普通 rebase 到该
`main`；五个恢复/审计提交全部无冲突重放。`git range-diff` 对五组新旧提交均显示 `=`：

- #10 恢复提交重写为 `d19c2b1`；
- #11/#12 恢复提交重写为 `015fbaa`；
- 三个审计提交依次重写为 `d48cfd4`、`409b657`、`f969915`。

除 `README.md` 外，恢复 PR 的全部受控实现、测试和说明文件与原汇总提交
`baa868b6f5feb0904de453b8a08aba56a66f13b3` 逐字一致。`README.md` 的 38 行组合模式增量
也与原补丁逐字一致，同时保留了 #14 新增的 Dynamic Agent API 文档入口。恢复 diff 未接管
#14 的客户端、凭据注入、Compose 或部署脚本职责。

rebase 后本地验证结果：全仓 `769 passed, 2 skipped, 19 warnings, 69 subtests passed`；两个
skip 仍分别是未安装可选 Bedrock extra 与未配置 DeepSeek live key。严格全仓 Ruff、部署合同、
部署脚本定向 `26 passed`、Python 3.12 全新环境安装/导入和两个 CLI help 均通过。生产镜像使用
已记录的本机 `--network=host` 绕行成功构建；镜像用户为 `appuser`，并在 `--network=none`
下通过组合 CLI、Dynamic Agent 客户端导入及示例 help smoke。本机未安装 `shellcheck` 和
`actionlint`，这两项仍必须由 GitHub exact-head CI 在干净 runner 上证明。

当前 `deploy-acr.yml` 会在 `main` push 的 CI 成功后自动构建并发布到生产服务器。因此“合并
#15”本身会触发生产部署，不能与“不部署”同时承诺。在获得明确生产发布授权前，本轮只把 PR
推进到 merge 前门禁；不得通过停用 workflow 或跳过 CI 绕开该边界。原解决条件第 3 至 5 项
仍未满足，状态继续保持 `已决策待落地`。

## 2026-08-21 恢复忠实性后的正确性加固

基于 `main@c914a831c119fcced8df4e29952970dee39ee77d` 的恢复 head
`3e5f9d99a04712b05d514569e9eec595bf7f44d1` 已满足原解决条件第 1 项：五个重放提交的
`range-diff` 均为 `=`，除需同时保留 #14 文档的 `README.md` 外，受控恢复文件与原 #11/#12
汇总树逐字一致。随后严格 QA 在这个精确恢复 head 上发现旧实现会接受混合市场组合配合
Tushare-only 显式数据源链，并可能让非 A 股在核心数据缺失时继续运行。该问题与证据另见
`docs/todo/2026-08-21-pr15-mixed-market-tushare-provider-gap.md`。

用户明确选择在 #15 内落地最小 fail-closed 预检，并将该修复 ownership 转交当前会话。因此
`3e5f9d9` 之后对 CLI、图入口、测试和文档的变化是经决策的正确性加固，不再宣称最终文件树与
旧汇总树逐字一致；原逐字一致检查仍保留为“历史能力是否被忠实恢复”的证据。新实现不改变
组合算法、allocator、显式 provider 顺序或自动部署边界，只在任何数据、LLM 和交易相关工作前
拒绝不能覆盖候选市场及所选 analyst 方法的配置。聚焦、全仓、clean-install 和生产镜像本地
门禁已通过；最终 exact-head CI、独立 approval、merge/main 可达性、生产部署与 #13 rebase
仍按原顺序执行，故本记录继续保持 `已决策待落地`。

## 2026-08-21 main 传播与 #13 重放进展

PR #15 已获独立批准并以 `d8bd1896460420aecee26ec06e98cab437a01ac8` squash merge 到
`main`；main CI run `32461712153` 全绿。生产 ACR workflow run `32461848195` 成功，163
状态文件、不可变 release、CLI 与 API 两个健康容器的镜像 URI/部署标签均指向同一完整 SHA，
部署结果明确记录服务健康和 smoke 通过。`main` 上也已直接复核 `propagate_portfolio`、
`save_portfolio_report` 与 mixed-market fail-closed validator 可达，原解决条件第 3、4 项完成。

#13 旧 head `166e1dc57f48cee4e79cfe9a3fef2017fcb2ec48` 已保留本地备份；只把
`e261a8c4352a57e94f3ea087674d106409f5bbfa` 之后的四个 API/审计提交无冲突重放到上述
`main`，四组 `range-diff` 均为 `=`，没有重新携带旧组合堆叠历史。原解决条件第 5 项的本地
重放部分已满足，但新 head 的完整本地门禁、受保护远端更新、PR base 切换和 exact-head GitHub
CI 尚未完成，因此本记录暂时保持 `已决策待落地`，不提前标记解决。

新 head 的本地门禁已经完成：聚焦栈 `102 passed`；全仓
`806 passed, 2 skipped, 19 warnings, 69 subtests passed`；严格 Ruff、部署合同、部署脚本定向
`26 passed`、Compose、shell 语法、Python 3.12 全新安装/导入和 `git diff --check` 均通过。
生产镜像使用既有的本机 `--network=host` 绕行成功构建，用户为 `appuser`，断网容器内核心导入、
主 CLI 和 Dynamic Agent 示例 help 均通过。本机缺少的 `shellcheck`、`actionlint` 以及干净 runner
上的标准 Docker 网络构建仍由 GitHub exact-head CI 验证；在远端安全更新、base 切换和最终 CI
完成前，状态继续保持 `已决策待落地`。

远端主干与 #13 旧 head 再次锁定后，分支已用精确旧 SHA 的 `--force-with-lease` 更新至
`5698b4f`，PR base 也已切换为 `main`，GitHub 文件列表与本地预期一致。首次更新没有生成新的
Actions run；本记录的普通后续 push 将重新触发 `pull_request/synchronize`，只有其 exact-head
检查通过后才满足最后解决条件。

## 2026-08-21 解决证据

普通后续 push head `d72f503cdf8be79a291e6058999b81ea46bdae2f` 的 GitHub CI run
`32465739121` 已全绿：Python 3.10-3.13 测试矩阵、clean-install、严格全仓 Ruff、部署合同、
ShellCheck、Actionlint、Compose、标准网络生产镜像构建和 CI Gate 均成功。该结果与本地
`102 passed` 聚焦栈、`806 passed, 2 skipped, 19 warnings, 69 subtests passed` 全仓回归、
Python 3.12 全新安装和断网镜像 smoke 相互独立。

至此五项解决条件全部满足：#15 已进入 `main` 并完成 main/部署验证；#13 只重放四个专属提交，
四组 `range-diff` 为 `=`；PR base 已切到 `main`；实际文件列表、完整本地门禁和新 exact-head CI
全部复核。组合研究仍由 TA 拥有，broker/订单/成交事实仍由 Rachel downstream execution service 拥有，未新增外部
协议、安全门或数据所有权偏移，符合既有设计预期，因此状态更新为 `已解决`。

该状态只关闭“组合能力未传播到 main”及其对 #13 rebase 的阻塞。PR #13 仍为 OPEN 且需要独立
review；本任务不授权合并 #13、自动生产发布、live LLM、数据库、券商或交易调用。
