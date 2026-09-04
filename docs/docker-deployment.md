# TradingAgents ACR 服务器部署与运维

TradingAgents 的服务器 release 同时包含原有 CLI 工具箱和一个专供受信容器调用的事件交易计划
API。API 不发布宿主机端口、不持有券商账户也不下单，完整协议见
[`event-trade-plan-api.md`](event-trade-plan-api.md) 与兼容的批量组合资源
[`event-portfolio-plan-api.md`](event-portfolio-plan-api.md)。`main` 的 CI 成功后，部署 workflow 先等待
服务器部署锁并读取当前发布状态；只有状态文件、release 和该 release 声明的全部受管服务镜像身份一致时，才构建
`linux/amd64` 的完整 Git SHA 镜像并推送到 ACR。GitHub Actions 随后只上传两项非敏感
release 文件并登记服务器后台任务；launcher 约 2 秒后返回，workflow 再有界等待该 run ID/attempt
的精确结果文件。镜像拉取、容器替换、健康检查、CLI/API smoke、回滚和清理仍全部由服务器后台
脚本完成；服务器返回回滚或失败终态时 workflow 必须失败。

## GitHub 配置

Repository Variables：

- `ACR_REGISTRY=your-acr-registry.example.com`
- `ACR_NAMESPACE=rachel`

Repository Secrets：

- `ACR_USERNAME`、`ACR_PASSWORD`：GitHub runner 推送 ACR 镜像；
- `DEPLOY_HOST=<deployment-host>`、`DEPLOY_USER=<deployment-user>`、`DEPLOY_PASSWORD`：SSH；
- `DEPLOY_PATH=/opt/a-stock-rachtrader`。

Secrets 不得写入 workflow、release bundle、日志或结果文件。部署 workflow 正常情况下只
由 `main` 分支成功结束的 `CI` workflow 触发；`workflow_dispatch` 只能从 `main` 发起，
且必须填写 `existing_sha`，只用于人工重新部署、回滚已有 SHA 或受控回滚演练。

`main` ruleset 必须保留一人 approve，并把 GitHub Actions 的 `CI Gate` 配成 required status
check。在该设置生效前，即使部署 workflow 会等待合并后的 main CI，GitHub 仍可能允许 PR
在 CI 未通过时合并，因此不能把“合并后等待 CI”当作合并保护。

## 服务器首次准备

以下命令在 `<deployment-user>@<deployment-host>` 执行：

```bash
install -d -m 700 \
  /opt/a-stock-rachtrader/logs \
  /opt/a-stock-rachtrader/run/deploy-results/acr \
  /opt/a-stock-rachtrader/releases

install -m 600 /dev/null /opt/a-stock-rachtrader/.env
install -m 600 /dev/null /opt/a-stock-rachtrader/.event-plan-api.env
install -m 600 /dev/null /opt/a-stock-rachtrader/.deploy.env
docker network inspect rachel-trading-internal >/dev/null 2>&1 \
  || docker network create rachel-trading-internal
test -s /opt/etf-agent-runtime-tunnel/secrets/internal-agent-caller.env
```

`.env` 保存应用运行配置。至少配置一个 LLM provider，并按实际使用情况配置 Tushare、
ETF stock/event PostgreSQL 及以下持久化路径：

```dotenv
TRADINGAGENTS_RESULTS_DIR=/home/appuser/.tradingagents/logs
TRADINGAGENTS_CACHE_DIR=/home/appuser/.tradingagents/cache
TRADINGAGENTS_MEMORY_LOG_PATH=/home/appuser/.tradingagents/memory/trading_memory.md
```

`.deploy.env` 只保存服务器部署控制凭据：

```dotenv
ACR_PULL_USER=...
ACR_PULL_PASSWORD=...
DEPLOY_FEISHU_WEBHOOK_URL=...
```

`.event-plan-api.env` 只保存 API 专属配置：独立 bearer token，以及可选的组合分析并发上限。token
必须使用独立高熵值，并在 Rachel downstream execution service 的 `rachel_executor` server-only env 中配置同一值；
不得复用 LLM、数据库、ACR 或 SSH 凭据。并发值只接受 `1..4`；旧服务器缺少该行时默认 4：

```dotenv
TRADINGAGENTS_API_BEARER_TOKEN=...
TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY=4
```

Dynamic Agent API 的地址和内部调用令牌不复制到本项目目录。服务器 Compose 固定读取已经由
ETF Platform 接入流程维护的 root-only 文件：

```text
/opt/etf-agent-runtime-tunnel/secrets/internal-agent-caller.env
```

该文件必须包含 `AGENT_RUNTIME_BASE_URL` 和 `INTERNAL_AGENT_TOKEN`；应用 `.env` 中必须已有
调用方自己的 `OPENROUTER_API_KEY`。部署会把两份环境配置自动注入 `tradingagents` 和
`event-plan-api`，并在新版本 smoke 中检查配置可加载且隧道 `/health` 可达。两个 HTTP smoke
允许最多 3 次、间隔 5 秒的短重试；配置、import、CLI help、事件分析和交易请求不会被重试。真实令牌不能复制
到 release、镜像、部署日志或 Git。调用方法见
[`dynamic-agent-api.md`](dynamic-agent-api.md)。

共享 external network 的创建与 token 写入都是首次激活前的人工运维前置条件。上面的 network
命令只用于 runbook；代码 PR 和 GitHub Actions 都不会自行修改服务器网络。

飞书 webhook 可留空；ACR 两项不能为空。写入时关闭 shell tracing，不得在终端或日志打印
值。完成后再次执行：

```bash
chmod 600 /opt/a-stock-rachtrader/.env \
  /opt/a-stock-rachtrader/.event-plan-api.env \
  /opt/a-stock-rachtrader/.deploy.env
```

首次部署允许没有 `.deploy-current` 和 `.deploy-current-sha`，但此时不得已有 Compose 项目
`a-stock-rachtrader` 的 `tradingagents` 容器。后续构建前预检会等待全局部署锁（最长
1800 秒），要求两个状态文件同时存在且一致，并核对 current release Compose 文件和容器的
镜像 URI/SHA 标签。预检不要求容器当前 healthy，以便故障版本仍可通过新部署修复；但状态
缺失、部分写入或与容器身份不一致时会在构建镜像之前失败。

## 部署成功语义与观察

GitHub Actions 先登记 detached deployment，再每 5 秒读取本次逐请求结果，默认最多等待 2700 秒。
`success`、`success_superseded` 或 `superseded` 才会绿色；`failed`、`rolled_back`、
`rollback_failed`、`launch_failed` 都会使 workflow 失败。等待超时也失败，但不会终止服务器已接管的
后台任务。服务器结果和阶段日志仍可独立查看：

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

`success` 表示该 release Compose 声明的每个 service 都恰有一个 `healthy` 容器、每个
`.Config.Image` 均为本次完整 SHA 镜像、容器标签 `io.rachel.deploy.sha` 匹配，且 CLI import/help
与 API import/readiness smoke 均成功。`success_superseded` 表示上述验证完成后出现了更新请求；
`superseded` 表示本请求未执行容器 mutation、由更新请求接管。后两者不会制造 workflow 假失败，
但也不能单独证明该 SHA 仍是服务器最终版本，必须以更新请求的终态和 `.deploy-current` 为准。

进入工具箱运行分析：

```bash
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
    exec tradingagents tradingagents
```

## 人工回滚与受控失败测试

不要直接执行任意 `docker compose up` 绕过部署锁和状态协议。确认目标 SHA 的 release 目录、
ACR 镜像仍存在后，从 GitHub 手工触发 `deploy-acr.yml`，把 `existing_sha` 设为完整 40 位
SHA。workflow 不重建或覆盖该镜像，只验证 manifest 并登记同一后台协议：

```bash
gh workflow run deploy-acr.yml \
  --repo rra7963/A-stock-rachtrader \
  --ref main \
  -f existing_sha=<40位已成功SHA>
```

验证自动回滚时，必须先确保 `.deploy-current` 指向一个已成功版本。再手工触发一个不同的
已有 SHA，并设置 `force_failure_after_up=true`。后台脚本会在新容器启动后注入失败，随后
使用上一成功 release 自己的 Compose service 集合恢复并重新通过 health、镜像身份和相应 smoke；
这保证可从 CLI+API 版本回滚到旧的 CLI-only 版本。预期请求结果为
`rolled_back`，workflow/飞书应明确报告失败与回滚，不能显示部署成功。

## 并发、保留和故障处理

- launcher 用 run ID/attempt 拒绝更旧请求；后台任务在拿锁后和容器变更前再次检查最新请求。
- Actions 在构建镜像前先持有同一全局锁读取一致的服务器状态；读完即释放锁，后续并发仍由
  latest-request 协议决定最终版本。
- 被覆盖任务只更新自己的结果文件，不覆盖 `.deploy-last-result.acr`。
- Actions 结果等待器只读自己的逐请求文件并校验 owner、`0600` mode、request/SHA/image/release 身份；
  不从 `.deploy-last-result.acr` 猜测本次结果。
- 全局 `flock` 保证一次只有一个任务改变容器。
- 成功后默认保留至少 5 个最新 release，并始终保护当前与上一成功 SHA。
- 镜像清理只处理本项目 repository 下的 40 位 SHA 标签，保护当前、上一成功及所有容器引用。
- 部署日志和逐请求结果默认保留 30 天；命名卷永不由部署脚本删除。
- `failed` 表示变更前失败或没有可信上一版本；`rolled_back` 表示新版本失败但已恢复；
  `rollback_failed` 需要立即人工介入，保留所有容器和日志证据。其结果 message 会先记录闭合的
  rollback failure（pull、Compose up、health/image identity 或带安全阶段名的 smoke），再记录
  primary failure；总长最多 500 字符，不包含底层异常、响应或凭据。

磁盘或异常排查：

```bash
df -h /
docker system df
docker ps --filter name=a-stock-rachtrader --no-trunc
cat /opt/a-stock-rachtrader/.deploy-current
cat /opt/a-stock-rachtrader/.deploy-last-result.acr
```

禁止使用 `docker system prune -a`、`docker image prune -a`、`docker volume prune`，也禁止
删除 `/var/lib/docker`。这些操作可能移除回滚镜像、其他项目数据或持久化卷。
