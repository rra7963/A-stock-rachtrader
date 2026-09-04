# TradingAgents 服务器 ACR 自动部署方案

> 状态：已确认；2026-08-13 事件交易计划 API 扩展已批准并实施中
> 编写日期：2026-08-06
> 目标仓库：`rra7963/A-stock-rachtrader`
> 目标服务器：与 `Rachel downstream execution service` 相同的 `<deployment-user>@<deployment-host>`
> 推荐部署目录：`/opt/a-stock-rachtrader`

> 2026-08-13 扩展说明：初版方案中“单个 CLI 工具箱、无 HTTP 端口”描述的是当时基线。
> 经用户批准，当前设计保留 CLI 并新增不发布宿主机端口的私网事件计划 API；协议、职责和安全
> 边界以 [`event-trade-plan-api.md`](event-trade-plan-api.md) 与
> [`event-portfolio-plan-api.md`](event-portfolio-plan-api.md) 为准，本文的部署与回滚合同同步扩展为
> 按各 release Compose 动态验证全部 services。

## 1. 目标

为 `a-stock-rachtrader` 建立与 `Rachel downstream execution service` 同类的交付链路：

1. 所有代码变更通过 Pull Request 进入 `main`。
2. PR 必须通过必要的测试、lint、Docker 和部署契约检查。
3. PR 至少需要一名有权限的其他用户 approve，才允许合并。
4. 合并后自动构建不可变 Docker 镜像，并推送到阿里云 ACR。
5. GitHub Actions 只上传少量非敏感部署文件，在服务器登记后台任务，并读取该次请求的精确终态回执。
6. launcher 确认后台任务成功启动后立即返回；workflow 随后在独立 SSH 步骤中有界等待结果，不把
   Docker mutation 或回滚职责移入 Actions。
7. ACR 拉取、部署互斥、健康检查、失败回滚、日志和结果落盘全部由服务器脚本完成；`rolled_back` 等
   失败终态必须使 workflow 失败，不能再把“已受理”显示成“已部署”。
8. 整个实施过程不得直接在本地 `main` 分支上修改代码。

## 2. 已确认的现状

### 2.1 仓库与分支保护

- 本地仓库：`C:\workspace\a-stock-rachtrader`
- GitHub 仓库：`rra7963/A-stock-rachtrader`，私有仓库。
- 当前 `main` 与 `origin/main` 指向同一提交 `8a225ba`。
- GitHub 已存在作用于 `main` 的 active ruleset：
  - 禁止删除分支；
  - 禁止 non-fast-forward；
  - 要求线性历史；
  - 必须通过 PR；
  - 至少一人 approve；
  - 当前还没有“required status check”，因此 CI 失败尚不能阻止合并。
- 仓库已有 PR/push CI，当前覆盖 Python 3.10–3.13、clean install 和 Ruff。
- 仓库尚未配置 Actions Secrets 和 Variables。

### 2.2 项目运行形态

- 原始人工入口仍是 `tradingagents` CLI；机器入口是独立 `event-plan-api` service。
- API 只通过 external Docker bridge network 暴露 8787，不发布宿主机端口。
- 服务器上的常驻容器包括已构建、已验证的 CLI 工具箱和事件计划 API；二者使用同一不可变镜像。
- 实际分析任务通过以下形式进入容器运行：

```bash
docker compose exec tradingagents tradingagents
```

- 部署验收不能只检查“容器进程存在”，至少还要检查：
  - 容器运行状态；
  - Docker healthcheck；
  - 容器实际使用的镜像是否为本次 SHA 镜像；
  - `tradingagents --help` 和 API import/readiness smoke 是否成功。

### 2.3 目标服务器

- 主机名：`fwq`
- 架构：`x86_64`
- Docker：`29.1.3`
- Docker Compose：`2.40.3`
- `/opt/downstream-execution` 已运行 `Rachel downstream execution service` 的 ACR 异步部署体系。
- 服务器已登录 ACR：`your-acr-registry.example.com`。
- 当前根分区约 99 GB，已使用约 80 GB，使用率 85%。新流程必须有项目级保留和清理策略，不能无限累积镜像与 release 目录。

### 2.4 当前 `main` 工作目录中的未提交改动

当前主工作目录已有以下改动，均视为需要保护的现有工作，不直接丢弃：

| 文件                               | 初步用途判断                                                              | 推荐归属                                                |
| ---------------------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------- |
| `.env.example`                   | 增加 A 股数据库、数据源和持久化路径配置；服务器需要环境契约，本地也会使用 | 服务器 PR 先纳入通用契约                                |
| `Dockerfile`                     | 修复非 root 用户对应用和报告目录的权限；生产镜像需要                      | 服务器部署 worktree                                     |
| `README.md`                      | 同时包含服务器与本地 Docker 说明                                          | 按段拆分，服务器说明进入服务器 PR，本地说明进入本地 PR  |
| `docker-compose.yml`             | 主要改善本地 build/run、命名卷和 Ollama 使用                              | 本地部署 worktree                                       |
| `docker-compose.server.yml`      | 常驻工具箱、健康检查和重启策略                                            | 重新整理为独立的服务器 Compose，进入服务器部署 worktree |
| `docs/docker-deployment.md`      | 当前混合本地初始化与服务器部署说明                                        | 拆成服务器 runbook 与本地 Docker 文档                   |
| `scripts/prepare-docker-env.ps1` | 从相邻项目安全生成本机 `.env`，仅适用于 Windows 本地开发                | 本地部署 worktree                                       |

迁移原则：先在目标 worktree 中确认内容和测试结果，再清理主工作目录。任何 `git restore` 或删除未跟踪文件的动作都要在迁移 diff 经确认后执行。

## 3. Worktree 与分支方案

### 3.1 服务器部署 worktree

- 路径：`C:\workspace\a-stock-rachtrader-server-deploy`
- 分支：`codex/server-acr-deploy`
- 基线：干净的 `origin/main`
- 用途：生产镜像、PR 部署检查、ACR workflow、服务器异步部署脚本、生产 runbook。

计划包含：

- `Dockerfile` 的生产可写目录和非 root 运行修复；
- `.env.example` 中服务器需要的应用配置契约；
- 独立的 `docker-compose.server.yml`；
- `.github/workflows/ci.yml` 的部署检查和统一 CI Gate；
- `.github/workflows/deploy-acr.yml`；
- `scripts/launch-acr-deploy.sh`；
- `scripts/deploy-acr-background.sh`；
- 部署契约校验脚本和测试；
- 服务器部署、观察、回滚、清理 runbook。

### 3.2 本地部署 worktree

- 路径：`C:\workspace\a-stock-rachtrader-local-deploy`
- 分支：`codex/local-docker-deploy`
- 基线：开始迁移时使用 `origin/main`；服务器 PR 合并后再 rebase 到最新 `main`。
- 用途：只保留本地 Docker 使用体验和 Windows 配置生成逻辑。

计划包含：

- `docker-compose.yml` 的本地 build/run、Ollama profile 和持久化卷；
- `scripts/prepare-docker-env.ps1`；
- 本地 Docker 文档和 README 中的本地说明；
- PowerShell 脚本的无密钥输出检查和 Docker 本地 smoke。

### 3.3 合并顺序

1. 先提交并合并服务器部署 PR。
2. ruleset 确认 `CI Gate` 已成为 required status check。
3. 本地部署分支 rebase 到新的 `main`。
4. 再提交本地 Docker PR。

这样可以避免 `Dockerfile`、README 和配置模板在两个独立 PR 中重复修改或产生无意义冲突。

### 3.4 当前提前产生的草稿

服务器 worktree 中目前存在一批未提交的实现草稿。这些草稿不视为已批准方案。评审通过后应先根据本文件逐项复核；不符合本方案的内容应在隔离 worktree 中调整或丢弃，不能因为文件已经存在就默认采用。

## 4. 推荐的交付架构

```text
开发分支
   │
   ├─ Pull Request
   │    ├─ Python 3.10–3.13 tests
   │    ├─ clean-install smoke
   │    ├─ Ruff
   │    ├─ Docker build
   │    ├─ Compose/deploy contract
   │    └─ CI Gate
   │
   ├─ 其他用户 approve
   │
   └─ squash/rebase merge 到 main
          │
          └─ GitHub Actions
               ├─ 等待服务器部署锁并校验 current state/runtime
               ├─ 构建 linux/amd64 SHA 镜像
               ├─ 推送 ACR 并验证 manifest
               ├─ SCP 非敏感 release bundle
               ├─ SSH 登记后台任务，launcher 立即返回
               ├─ 有界轮询该 run ID/attempt 的精确结果文件
               │      │
               │      └─ success/superseded 绿色；失败/回滚红色
               └─ 服务器后台脚本
                           ├─ 获取部署锁
                           ├─ 判断请求是否已被新版本取代
                           ├─ 登录 ACR 并重试拉取
                           ├─ 使用预构建镜像启动容器
                           ├─ 验证各 service health/image/CLI+API smoke
                           ├─ 成功后更新 current state
                           ├─ 失败则回滚上一成功 SHA
                           └─ 写日志、结果与服务器侧通知
```

## 5. PR 与 CI 设计

### 5.1 保留的现有检查

- Python 3.10、3.11、3.12、3.13 的 pytest；
- 无 dev 依赖的 clean-install/import smoke；
- 全仓库 Ruff。

### 5.2 新增的部署检查

新增独立 job，至少执行：

1. `docker build` 构建生产镜像。
2. `docker compose config` 校验本地和服务器 Compose。
3. `bash -n` 校验部署脚本语法。
4. `shellcheck` 校验服务器脚本。
5. `actionlint` 校验所有 GitHub Actions workflow。
6. 部署契约测试，阻止以下回归：
   - 服务器 Compose 出现 `build:`；
   - 服务器部署脚本出现 `git pull` 或服务端构建；
   - release bundle 包含 `.env`、密钥或源码运行时数据；
   - launcher 没有完全重定向 stdin/stdout/stderr；
   - launcher 等待完整部署而不是快速返回；
   - workflow 没有等待精确逐请求终态，或从全局 latest result 推断本次结果；
   - 容器没有健康检查、重启策略或非 root 运行；
   - workflow 使用可变镜像标签执行部署。

### 5.3 统一 required check

增加名为 `CI Gate` 的汇总 job，它依赖所有测试、lint 和部署检查。任何前置 job 失败、取消或跳过时，`CI Gate` 必须失败。

ruleset 只要求 `CI Gate`，而不是绑定每个矩阵 job 名称。这样以后调整 Python 矩阵或拆分 job 时，不需要频繁修改分支保护规则。

### 5.4 ruleset 调整

保留现有 PR 和一人 approve 要求，并新增：

- required status check：`CI Gate`；
- 要求分支在合并前与 `main` 保持最新，或至少要求 GitHub 对最终可合并树运行检查；
- 推荐 squash merge，保持当前线性历史要求。

首次设置顺序：先让服务器部署 PR 跑出一次 `CI Gate`，再把该 check 加入 ruleset，最后 approve 和合并，避免 required check 尚不存在造成配置误判。

## 6. ACR 镜像方案

### 6.1 镜像标识

推荐镜像格式：

```text
<ACR_REGISTRY>/<ACR_NAMESPACE>/a-stock-rachtrader:<40位Git SHA>
```

生产部署只使用完整 Git SHA 标签，不使用 `latest`。可以额外推送便于人工查看的标签，但服务器脚本不得以可变标签部署。

### 6.2 构建方式

- GitHub-hosted runner 使用 Buildx；
- 目标平台固定为服务器实际架构 `linux/amd64`；
- 首版不启用手写 `type=gha` Buildx 缓存，避免普通 `run` step 缺少 runtime token；
- push 后执行 `docker buildx imagetools inspect`，确认 ACR manifest 已可见后才上传部署脚本；
- 镜像内以 `appuser` 运行应用，不以 root 运行 CLI。

### 6.3 ACR 前置条件

- 复用已有、且当前账号具备 push/pull 权限的 namespace，并使用独立 repository 名 `a-stock-rachtrader`。

## 7. GitHub Actions 配置

### 7.1 Repository Variables

| 名称              | 含义                                                           |
| ----------------- | -------------------------------------------------------------- |
| `ACR_REGISTRY`  | 例如 `your-acr-registry.example.com` |
| `ACR_NAMESPACE` | 经确认存在且有权限的 namespace                                 |

### 7.2 Repository Secrets

| 名称                | 含义                                                |
| ------------------- | --------------------------------------------------- |
| `ACR_USERNAME`    | GitHub runner 推送镜像使用的 ACR 账号               |
| `ACR_PASSWORD`    | GitHub runner 推送镜像使用的 ACR密码                |
| `DEPLOY_HOST`     | `<deployment-host>`                               |
| `DEPLOY_USER`     | 初始可复用 `root`，长期建议改为受限部署用户       |
| `DEPLOY_PASSWORD` | 与参考项目一致的 SSH 密码；长期建议改为独立 SSH key |
| `DEPLOY_PATH`     | `/opt/a-stock-rachtrader`                     |

GitHub Secrets 无法从 `Rachel downstream execution service` 读取后复制，必须由有权限的人重新录入。

### 7.3 workflow 触发

采用方案 ：

- `CI` 在 `main` 上成功后通过 `workflow_run` 触发部署。优点是构建和部署的就是通过合并后 CI 的精确 SHA；缺点是部署要等待一次 main CI。
- workflow 在 Buildx/ACR 登录和镜像构建之前，通过只读 SSH 预检等待服务器全局部署锁并校验当前 state、release 和受管容器镜像身份。首次部署只在 state 与受管容器都不存在时放行。

## 8. Release bundle 设计

GitHub Actions 只向以下目录上传非敏感文件：

```text
/opt/a-stock-rachtrader/releases/<Git SHA>/
├── docker-compose.server.yml
└── scripts/
    └── deploy-acr-background.sh
```

禁止上传：

- `.env` 或 `.deploy.env`；
- API key、数据库密码、ACR 密码；
- 缓存、报告、记忆文件和运行日志；
- 整个 Git 仓库；
- 需要服务器现场构建的源码包。

GitHub Actions 通过仓库内的 `scripts/launch-acr-deploy.sh` 执行远程登记。该 launcher 只验证输入、
登记请求、启动后台进程并检查后台进程没有立即崩溃。launcher 返回后，workflow 通过仓库内的
`scripts/wait-acr-deploy-result.sh` 只读该次 `run_id.run_attempt` 的结果文件；等待器不进入 release
bundle、不执行 Docker 或进程控制，也不读取 `.deploy-last-result.acr` 冒充本次回执。

## 9. 服务器目录与配置

推荐目录结构：

```text
/opt/a-stock-rachtrader/
├── .env                         # 应用运行密钥，600
├── .event-plan-api.env          # API bearer token，600
├── .deploy.env                  # 服务器 ACR pull 凭据，600
├── .deploy-current              # 上一成功部署的 SHA/image/release
├── .deploy-current-sha          # 便于运维命令读取
├── .deploy-last-result.acr      # 最近有效请求的部署结果
├── releases/<sha>/              # 不可变 release bundle
├── logs/                        # 后台部署日志，700
└── run/
    ├── deploy-global.lock
    ├── deploy-acr.latest-request
    ├── deploy-acr.pid
    └── deploy-results/acr/
```

持久化数据使用显式命名的 Docker volumes，避免 release 目录变化导致新建卷：

- `a-stock-rachtrader-data`
- `a-stock-rachtrader-reports`
- `a-stock-rachtrader-api-data`

### 9.1 `.env`

保存应用本身需要的 LLM、数据库、Tushare 和运行参数。该文件不进入 Git，也不由 Actions 上传。

### 9.2 `.deploy.env`

只保存服务器拉取 ACR 镜像所需的最小凭据，例如：

```dotenv
ACR_PULL_USER=...
ACR_PULL_PASSWORD=...
```

可以在服务器内部从现有 `Rachel downstream execution service` 配置安全复制相同 ACR 凭据，但不得把值打印到终端、Actions 日志或方案文档中。不建议直接 source `/opt/downstream-execution/.env`，否则两个项目会形成不必要的配置耦合。

### 9.3 `.event-plan-api.env` 与共享网络

`.event-plan-api.env` 只保存 API 专属配置：独立高熵 `TRADINGAGENTS_API_BEARER_TOKEN`，以及可选、
闭合为 `1..4` 且默认 4 的 `TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY`。不得复用或包含 LLM、
数据库、ACR、SSH 凭据。CLI service 不加载该文件。`event-plan-api` 与调用方
`rachel_executor` 加入预先创建的 external bridge network `rachel-trading-internal`；Compose
不发布 host port。网络和两端匹配 token 必须在 activation/deploy 前由运维准备，GitHub Actions
不创建或修改它们。

## 10. 服务器异步部署协议

### 10.0 构建前服务器基线预检

Actions 在任何镜像构建或部署请求登记之前执行 `scripts/read-deploy-baseline.sh`：

1. 最长等待 1800 秒获取与后台部署相同的 `deploy-global.lock`，避免读取到部署中间态。
2. `.deploy-current` 与 `.deploy-current-sha` 必须同时存在或同时不存在，不接受符号链接、部分 state 或未知字段。
3. 已有 state 时，SHA、镜像 URI、release 目录、Compose 文件和 marker 必须一致。
4. 从 current release 自己的 Compose `config --services` 推导预期 service 集合；项目容器数必须
   精确匹配，每个 service 必须恰好一个容器，镜像 URI 与 `io.rachel.deploy.sha` 标签必须和 state
   一致。不以 running/healthy 作为构建前置条件，以允许用新版本修复故障容器。
5. 首次部署只有在没有 state 且没有受管容器时返回 `bootstrap`。

预检读取完成后释放锁，再开始镜像构建。它用于阻止在未知服务器基线上继续构建和登记请求，
不试图在整个远程构建期间占用服务器锁；预检之后的并发顺序仍由 latest-request 和后台全局锁保证。

### 10.1 launcher 阶段

Actions 通过 SSH 传入：

- `DEPLOY_DIR`
- `GIT_SHA`
- `DEPLOY_RUN_ID`
- `DEPLOY_RUN_ATTEMPT`
- 完整不可变 `IMAGE_URI`

launcher 必须：

1. 严格校验 SHA、run ID、路径和镜像 URI。
2. 确认 release bundle 存在。
3. 在 request lock 下原子写入“最新期望请求”。
4. 使用 `setsid` 启动后台脚本。
5. 将 stdin 指向 `/dev/null`，stdout/stderr 写入服务器日志文件。
6. 记录 PID 和初始 `queued` 结果。
7. 等待约 2 秒，只检查后台任务没有立即失败。
8. 打印 request ID、PID、日志和结果路径，然后返回 0。

launcher 到此返回。其返回 0 的语义只是“部署请求已被服务器接受”；GitHub Actions 还必须完成下一节的
精确结果等待，才能得出该 workflow 的最终结论。

### 10.2 Actions 精确结果等待阶段

Actions 在独立 SSH 步骤执行 `scripts/wait-acr-deploy-result.sh`：

1. 只读取 `run/deploy-results/acr/<run_id>.<attempt>.result`，不读取全局 latest result。
2. 校验结果是当前 SSH 用户拥有的 `0600` 普通文件，拒绝符号链接、重复/未知字段，以及不匹配的
   request ID、SHA、image URI 和 release 路径。
3. 每 5 秒轮询一次，默认最多 2700 秒。超时使 workflow 失败，但不会杀死或篡改服务器已经接管的
   detached deployment；服务器仍负责完成并留下终态。
4. `success`、`success_superseded`、`superseded` 是覆盖协议允许的非失败终态；`failed`、
   `rolled_back`、`rollback_failed`、`launch_failed` 必须返回非零。
5. 日志只输出经过身份校验和 shell escaping 的闭合部署元数据，不输出凭据、原始异常或响应。

### 10.3 background 阶段

后台脚本必须：

1. 使用全局 `flock`，防止两个部署同时替换容器。
2. 获取锁后再次检查自己是否仍是最新请求；旧请求直接标记 `superseded`。
3. 读取上一成功部署状态，验证其 SHA、镜像和 release 路径可用于回滚。
4. 从服务器本地 `.deploy.env` 登录 ACR。
5. 对精确 SHA 镜像执行有限次数、带退避的 pull。
6. 在改变容器前再次检查是否被更新请求取代。
7. 使用独立服务器 Compose 和 `--no-build` 启动容器。
8. 按目标 release Compose 验证每个 service 的容器健康、镜像 URI，并执行带闭合阶段名的 CLI/API
   smoke 和稳定运行窗口。配置/import/CLI 只检查一次；仅两个 HTTP health/readiness smoke 允许
   最多 3 次、间隔 5 秒的有界重试。
9. 成功后原子更新 current state，并执行项目级安全清理。
10. 失败时恢复上一成功 SHA；回滚也必须重新通过健康和镜像身份检查。
11. 写入结构化结果文件和完整日志。

### 10.4 请求覆盖

workflow concurrency 在正常运行期间串行化同一仓库的部署 workflow，并覆盖精确结果等待阶段；但它只是
Actions 侧调度，不是服务器后台任务的租约或取消机制。launcher 登记请求后，runner 取消、结果等待超时、
SSH 中断或经批准的运维登记都不会停止服务器已经接管的 detached task。此时更新请求仍可能在旧后台任务
运行时出现，所以服务器侧 `latest-request` 与全局锁仍是最终版本顺序的权威门禁，不能只依赖 workflow
concurrency。

服务器必须保存 latest request。旧任务在以下节点检查：

- 获取部署锁后；
- 完成镜像拉取后、容器变更前；
- 成功落盘前。

被新请求取代的旧任务不得覆盖最新结果文件，也不得在新任务之后把旧镜像重新部署为最终状态。

## 11. 健康检查、结果与通知

### 11.1 健康检查

对 release Compose 声明的全部 services 至少包含：

- `docker inspect` 显示容器为 running；
- health 状态为 healthy；
- `.Config.Image` 等于本次完整 SHA URI；
- `python -c "import tradingagents; import cli.main"` 成功；
- CLI `tradingagents --help` 非交互执行成功；若 release 含 `event-plan-api`，其 import 与本机
  `/health/ready` 成功；
- 每个 smoke 记录 `service-discovery`、`container-identity`、`cli-import`、`cli-help`、
  `agent-runtime-config`、`agent-runtime-health`、`dynamic-agent-help`、`event-plan-import` 或
  `event-plan-readiness` 等闭合阶段；只有两个 HTTP 阶段允许有界短重试，绝不重复 import，也不重放
  事件分析或交易；
- 连续稳定 10–30 秒后才算成功。

### 11.2 结果状态

建议统一以下状态：

- `queued`
- `running`
- `success`
- `success_superseded`
- `superseded`
- `failed`
- `rolled_back`
- `rollback_failed`
- `launch_failed`

GitHub workflow 必须等待自己的逐请求文件进入终态。前三个成功/覆盖状态返回绿色；后四个失败/回滚
状态返回红色。`queued` 与 `running` 只能作为过程状态，不能当作部署成功。`success_superseded` 表示
本次健康验证成功后已有更新请求，`superseded` 表示本次未做容器 mutation；两者都必须继续查看更新请求
终态，不能据此声称旧 SHA 是服务器最终版本。

### 11.3 可观测性

服务器必须可用以下命令查看状态：

```bash
cat /opt/a-stock-rachtrader/.deploy-last-result.acr
tail -n 200 /opt/a-stock-rachtrader/logs/deploy-acr.*.log
deploy_dir=/opt/a-stock-rachtrader
sha="$(cat "$deploy_dir/.deploy-current-sha")"
image_uri="$(sed -n 's/^image_uri=//p' "$deploy_dir/.deploy-current")"
TRADINGAGENTS_IMAGE="$image_uri" \
TRADINGAGENTS_ENV_FILE="$deploy_dir/.env" \
TRADINGAGENTS_API_ENV_FILE="$deploy_dir/.event-plan-api.env" \
AGENT_RUNTIME_ENV_FILE="/opt/etf-agent-runtime-tunnel/secrets/internal-agent-caller.env" \
GIT_SHA="$sha" \
  docker compose -p a-stock-rachtrader \
    --project-directory "$deploy_dir" \
    -f "$deploy_dir/releases/$sha/docker-compose.server.yml" \
    ps
```

推荐复用 `Rachel downstream execution service` 的服务器侧飞书通知思路：后台脚本在 success、rolled_back、failed、
rollback_failed 时通知。通知仍完全在服务器发送；GitHub 的结果等待不代替通知，也不接触 webhook。

## 12. 回滚与清理

### 12.1 自动回滚

只有上一成功 state 完整且对应 release bundle 仍存在时才允许自动回滚。回滚必须使用上一成功的精确镜像 URI，不使用 `latest`。

如果回滚失败：

- 状态写为 `rollback_failed`；
- 对 image pull、Compose up、health/image identity 和 smoke 分别形成闭合的恢复失败原因；smoke 原因包含
  `FAILED_SMOKE_STAGE` 的安全阶段名，不写底层异常、响应或凭据；
- 最终 message 先写 rollback failure、再写 primary failure，并沿用 500 字符上限，保证截断时优先保留
  真正需要人工介入的恢复失败位置；
- 保留失败日志和当前容器证据；
- 不伪造成功 state；
- 发出高优先级服务器侧通知；
- 等待人工处理。

### 12.2 手工回滚

runbook 应提供按 SHA 回滚的显式命令，但手工回滚也要使用相同锁和状态更新协议，不能直接绕过脚本执行任意 `docker compose up`。

## 13. 安全要求

- 镜像和 release bundle 不包含 `.env`。
- Secrets 不通过命令行参数、日志或结果文件回显。
- GitHub Actions 第三方 action 固定到完整 commit SHA。
- 服务器容器使用非 root `appuser`。
- Compose 启用 `no-new-privileges`。
- 部署脚本拒绝非 40 位 SHA 和不符合规则的镜像 URI。
- release 路径由固定部署根目录和 SHA 组成，不接受任意路径。
- `.env`、`.deploy.env`、logs、run/results 使用最小权限。
- 长期建议把 root/password SSH 替换为受限 deploy 用户和专用 SSH key。
- 服务器只从 ACR 拉取已由 CI 构建的镜像，不执行 `git pull`、`docker build` 或源码安装。

## 14. 实施步骤

### 阶段 0：方案确认

1. 审阅本文件。
2. 决定第 17 节中的待确认事项。
3. 明确是否保留或重做服务器 worktree 中的提前草稿。

### 阶段 1：迁移现有改动

1. 从主工作目录导出现有 diff，仅用于核对。
2. 按第 2.4 节逐文件、逐 hunk 迁移到两个 worktree。
3. 在两个 worktree 中分别运行测试并保存 diff。
4. 用户确认迁移完整后，再清理 `main` 工作目录中的对应未提交内容。

### 阶段 2：实现服务器部署分支

1. 完成生产 Dockerfile 和独立服务器 Compose。
2. 完成 CI Gate、部署契约、构建前服务器基线预检和 workflow。
3. 完成 launcher/background、回滚、状态和清理逻辑。
4. 完成服务器 runbook。
5. 本地执行静态验证、测试、Docker build 和容器 smoke。

### 阶段 3：准备 ACR、GitHub 与服务器

1. 确认或创建 ACR namespace/repository。
2. 配置 GitHub Variables 和 Secrets。
3. 在 `/opt/a-stock-rachtrader` 创建目录和最小权限文件。
4. 安全写入 `.env`、`.event-plan-api.env` 和 `.deploy.env`，创建/核验共享 external network。
5. 验证服务器 ACR pull、Docker Compose、flock、磁盘、共享网络和卷命名。
6. 不启动正式部署。

### 阶段 4：服务器部署 PR

1. 只 stage 服务器部署 worktree 中批准的文件。
2. commit、push 分支并创建 PR。
3. 等待全部 CI，包括 `CI Gate`。
4. 把 `CI Gate` 加入 main ruleset required checks。
5. 由另一账号 approve。
6. squash/rebase merge。

### 阶段 5：首次自动部署验收

1. 确认镜像已推送 ACR，标签等于合并 SHA。
2. 确认 launcher 在后台任务受理后快速返回，但 workflow 继续运行并有界等待本次精确结果。
3. 确认成功/覆盖终态为绿色，失败/回滚终态和等待超时为红色；超时不伪装成服务器任务已取消。
4. 在服务器独立核对逐请求 result/log 与 `.deploy-current`，不能只把 workflow 绿色当成最终 SHA 证据。
5. 验证全部 release services、镜像 URI、health、CLI/API smoke、卷、共享网络和配置。
6. 人工触发一个可控失败场景，验证回滚、workflow 失败和服务器通知。
7. 记录首个成功 SHA 和回滚证据。

### 阶段 6：本地部署 PR

1. 本地 worktree rebase 到最新 `main`。
2. 迁移本地 Compose、PowerShell 和本地文档。
3. 验证 Windows `.env` 生成不打印秘密且默认不覆盖现有文件。
4. 验证本地 Docker、Ollama profile 和持久化卷。
5. 独立 PR、CI、approve、merge。

## 15. 验收标准

### GitHub

- [ ] 不能直接 push 到 `main`。
- [ ] 没有一人 approve 时不能合并。
- [ ] `CI Gate` 失败时不能合并。
- [ ] 部署 action 只在批准的 `main` 提交上运行。
- [ ] workflow 不输出任何 secret。

### ACR

- [ ] 镜像为 `linux/amd64`。
- [ ] 每次部署使用完整 Git SHA 标签。
- [ ] manifest 可见后才进入服务器登记阶段。
- [ ] 服务器凭据只有 pull 权限时仍可部署。

### 服务器

- [ ] 代码和 runtime 目录位于 `/opt/a-stock-rachtrader`。
- [ ] 服务器不拉 Git、不构建镜像。
- [ ] 每次镜像构建前都等待部署锁并成功读取、校验服务器 current state；首次 bootstrap 不得已有受管容器。
- [ ] launcher 在约 2–10 秒的远程登记窗口后返回；GitHub workflow 随后有界等待本次精确终态。
- [ ] workflow 等待超时或读到失败/回滚终态时为红色，且不会终止或篡改 detached background。
- [ ] 后台脚本继续运行并能独立完成 pull/up/health。
- [ ] 同时到达的部署不会并发替换容器。
- [ ] 新请求最终不会被旧请求覆盖。
- [ ] 新版本失败时能恢复上一成功 SHA。
- [ ] 日志、结果和当前 state 可审计。
- [ ] 每个 release service 使用正确 SHA 镜像，CLI 与 API 对应 smoke 均成功。
- [ ] 持久化 volume 在升级和回滚后保持不变。
- [ ] 清理逻辑不会删除当前或回滚镜像。

### 本地工作区

- [ ] `main` 不包含实施过程中的直接编辑。
- [ ] 服务器和本地修改分别存在于两个 worktree。
- [ ] 清理主工作目录前已有可核对的迁移 diff。

## 16. 风险与对策

| 风险                             | 对策                                                    |
| -------------------------------- | ------------------------------------------------------- |
| workflow 把“已受理”误报为部署成功 | 精确逐请求结果等待；失败/回滚红灯；服务器日志和飞书通知闭环 |
| 多次 merge 导致旧任务覆盖新任务  | latest-request 协议、全局锁、容器变更前二次校验         |
| ACR 短暂不可见或网络抖动         | manifest 预检；服务器有限次数退避重试                   |
| 新镜像健康但 CLI/API 实际不可用  | 全 service health + 精确镜像身份 + CLI/API smoke       |
| 回滚指针损坏                     | 启动前严格验证 state；无可信上一版本时 fail closed      |
| 服务器磁盘持续增长               | 当前/上一版本 allowlist、release 保留策略、ACR 生命周期 |
| 两个 PR 修改同一文件产生冲突     | 服务器 PR 先合并，本地分支后 rebase；README 按段拆分    |
| 生产配置被打进镜像或上传         | release allowlist；`.dockerignore`；CI 扫描禁止路径   |
| root/password 权限过大           | 初期复用参考流程，后续迁移到受限 deploy 用户和 SSH key  |

## 17. 已确认的实施选择

2026-08-06 已确认以下选择：

1. ACR 复用现有 namespace 并创建独立 repository。
2. 部署触发采用推荐的“main CI 成功后的 `workflow_run`”。
3. GitHub SSH 首期继续使用 `DEPLOY_PASSWORD`。
4. 服务器 `.env` 以当前本地已验证配置为基线。
5. 在第一版就接入服务器侧飞书部署通知。
6. 本项目保留常驻 CLI 工具箱；2026-08-13 已批准并冻结私网事件计划 HTTP/API，详见独立设计文档。
7. 对提前产生的服务器 worktree 草稿，按本方案复核后修改。

## 18. 推荐结论

推荐采用以下组合：

- 两个 worktree、服务器 PR 先行、本地 PR 后 rebase；
- 独立 ACR repository、完整 SHA 镜像；
- `CI Gate` 作为唯一 required status check；
- main CI 成功后再触发构建部署；
- launcher 只登记并启动 detached task 后快速返回，GitHub workflow 再有界等待本次精确终态；
- 服务器使用 latest-request + `flock` + 精确 SHA 回滚；
- 第一版即加入服务器侧结果通知；
- 首次上线沿用现有 SSH 密码流程以降低迁移变量，稳定后再切换受限用户和 SSH key。

该设计保留了 `Rachel downstream execution service` 已验证的核心模式，同时支持同一不可变 release 内的 CLI 与私网
API services。服务器后台脚本独占部署真实性、动态 service 集合、容器 mutation、并发顺序和失败回滚；
GitHub workflow 只读本次精确结果并据终态给出绿色或红色结论，不接管服务器职责，也不再把“已受理”
冒充“已部署”。
