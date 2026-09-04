# Dynamic Agent API 开发与调用

TradingAgents 已内置 ETF Platform Dynamic Agent API 客户端。163 服务器上的生产 Compose
会自动注入网络地址和内部调用令牌；后续开发者在业务代码中只需导入
`call_dynamic_agent`，不需要编写 SSH、HTTP 鉴权或服务器配置代码。OpenRouter key、模型、
temperature 等调用信息则由业务代码在每次调用时显式传入。

本能力是可选的独立调用，不会自动改变现有 TradingAgents graph、CLI 或 event-plan API 的执行
流程。只有业务代码显式调用时才会产生一次 Dynamic Agent 运行及对应的模型费用。

## 最小调用

```python
import os

from tradingagents.integrations import call_dynamic_agent

result = call_dynamic_agent(
    openrouter_api_key=os.environ["OPENROUTER_API_KEY"],
    model="mistralai/ministral-8b-2512",
    prompt="复核下面的交易信号，说明主要依据和风险。",
    content="600519.SH 出现示例信号；这只是研究输入，不执行交易。",
    temperature=0.2,
    max_tokens=600,
    # 可选：稳定、非敏感的 key 标签，不要填真实 API key。
    key_alias="my-strategy",
    # 可选：本次业务请求的唯一追踪 ID；不填则自动生成。
    # 该值不是幂等键，重复值仍可能执行和计费多次。
    caller_request_id="my-strategy-signal-20260818-001",
)

print(result.run_id)
print(result.content)
print(result.usage.model_dump())
```

`model` 完全由每次调用动态指定，可以换成调用方 OpenRouter key 有权使用的任意合法模型，
包括 `openrouter/auto`。不要在模型名前重复添加 LiteLLM provider 前缀。

可直接运行的完整代码见
[`tradingagents/examples/dynamic_agent_api.py`](../tradingagents/examples/dynamic_agent_api.py)。

## 在长生命周期服务中复用连接

偶发调用使用上面的便捷函数即可。一个进程会连续调用多次时，应复用客户端连接：

```python
import os

from tradingagents.integrations import DynamicAgentClient

openrouter_api_key = os.environ["OPENROUTER_API_KEY"]
client = DynamicAgentClient.from_env()
try:
    first = client.run(
        openrouter_api_key=openrouter_api_key,
        model="openrouter/auto",
        prompt="总结信号。",
        content="第一条信号",
        temperature=0.1,
        caller_request_id="strategy-run-001",
    )
    second = client.run(
        openrouter_api_key=openrouter_api_key,
        model="mistralai/ministral-8b-2512",
        prompt="总结信号。",
        content="第二条信号",
        temperature=0.3,
        caller_request_id="strategy-run-002",
    )
finally:
    client.close()
```

如果把结果接入自动交易流程，Dynamic Agent 的自由文本只能作为不可信分析依据，不能直接成为
订单。候选标的、资金上限、价格、手数和最终结构化输出仍须经过原有的确定性校验。

## 参数和返回值

调用参数：

- `openrouter_api_key`：必填；是本次调用要使用的 OpenRouter key。客户端只把它放在
  `X-OpenRouter-Api-Key` 请求头，不放入 JSON payload、日志或异常文本。
- `model`、`prompt`、`content`：必填；接口内部固定使用通用内容分析，调用方不传 `general`、
  `content_type` 或 `analysis_mode`。
- `temperature`、`max_tokens`：可选；受 ETF Platform 服务端上限约束。
- `key_alias`：可选的可读 key 标识，不得填写真实 API key。
- `caller_request_id`：可选，最长 128 字符，用于跨服务追踪。

成功返回 `DynamicAgentRunResult`，常用字段是 `run_id`、`model`、`content`、`usage` 和
`duration_ms`。客户端会校验成功响应契约，异常响应不会被当作正常文本使用。

`caller_request_id is not an idempotency key`：当前同步接口在调用方超时或连接中断时可能已经
执行并计费，因此 `DynamicAgentResultUncertain` 不能自动重试。显式 HTTP 错误会抛出
`DynamicAgentApiError`，其中保留 `status_code`、`error_code` 和可能存在的 `run_id`，但不会
记录两个鉴权值，也不会把服务端返回的任意 `message` 拼入异常文本。

客户端自建的 HTTP Session 会忽略 `HTTP_PROXY`、`HTTPS_PROXY` 等环境代理，防止两枚
鉴权值被交给进程环境中的代理。客户端也不跟随 HTTP 3xx 重定向；任何 3xx 都会作为
`unexpected_redirect` 协议错误返回。如果高级调用方向 `DynamicAgentClient` 注入自己的
`requests.Session`，客户端不会擅自修改该 Session；注入方必须自行禁用环境代理或
保证配置的代理可信，因为两枚凭据都会通过该 Session 发送。

## 配置来源

`DynamicAgentClient.from_env()` 只自动读取两个基础设施环境变量：

```dotenv
AGENT_RUNTIME_BASE_URL=http://etf-agent-runtime-tunnel:18001
INTERNAL_AGENT_TOKEN=<Rachel downstream execution service 内部调用令牌>
```

生产服务器不需要开发者修改这两个值：

- `AGENT_RUNTIME_BASE_URL` 和 `INTERNAL_AGENT_TOKEN` 由 root-only 文件
  `/opt/etf-agent-runtime-tunnel/secrets/internal-agent-caller.env` 注入；
- `docker-compose.server.yml` 已把两份配置自动提供给 `tradingagents` 和 `event-plan-api`；
- SSH 隧道容器已加入这两个服务使用的 Docker 网络，业务代码只访问
  `http://etf-agent-runtime-tunnel:18001`。

OpenRouter key 不是 `DynamicAgentClient` 的固定配置，必须通过每次调用的
`openrouter_api_key=...` 显式传入。服务器现有 `/opt/a-stock-rachtrader/.env` 已为容器
提供 `OPENROUTER_API_KEY`，所以示例代码直接传
`openrouter_api_key=os.environ["OPENROUTER_API_KEY"]`。业务代码也可以根据请求、租户或策略动态选择
其他 key，再把选中的值传给同一个参数，无需修改客户端或部署配置。

真实令牌不能放进 Git、镜像、示例、日志或异常。其他服务器或本地开发机若要调用真实接口，
仍必须由运维安全下发自己的内部令牌并建立批准的网络路径；这项安全边界不能通过示例代码绕过。

## 测试

单元测试不访问真实服务，也不消耗模型额度：

```bash
uv run --frozen pytest -q tests/test_dynamic_agent_api.py
uv run --frozen ruff check tradingagents/integrations tradingagents/examples/dynamic_agent_api.py tests/test_dynamic_agent_api.py
```

163 服务器部署后可执行真实冒烟。该命令会产生一次 OpenRouter 调用和费用：

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
    exec -T tradingagents \
      tradingagents-dynamic-agent-example \
        --caller-request-id "manual-smoke-$(date +%Y%m%d%H%M%S)"
```

服务器自动验收只执行客户端导入、配置加载、示例命令 `--help` 和 `/health` 连通检查，不产生
模型费用。上面的命令会执行一次付费真实调用，应使用唯一 `caller_request_id` 并保存返回的
`run_id`。
