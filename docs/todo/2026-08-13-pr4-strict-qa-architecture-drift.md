# PR #4 严格 QA 发现的架构偏移

- **状态**：已解决
- **发现日期**：2026-08-13
- **决策日期**：2026-08-13
- **决策人**：用户
- **负责人**：当前 Codex 会话负责按既有设计修复与离线验证；仓库 maintainer 负责 review/merge/deploy
- **相关 PR**：[#4](https://github.com/BeixiHub/trading-agents-adapted/pull/4)
- **相关提交**：发现记录 `9849d9043f54f71250aa4c40be0c1fc177e2c8a1`；修复
  `59e00eba3c8dbf13d2a240464d3ae8de9c0f72e5`

## 原设计及其位置

1. `docs/event-trade-plan-api.md` 第 163–179 行要求外部事件始终作为显式分隔的未信任证据，不能改变系统规则、候选 allowlist 或输出格式；第 220–231 行要求用提示注入测试证明该边界。
2. `docs/event-trade-plan-api.md` 第 60–63 行规定金额和价格在 wire request 中必须是 JSON decimal strings，以避免二进制浮点影响哈希与验证。
3. `docs/server-acr-deployment-plan.md` 第 5 节和 `.github/workflows/ci.yml` 要求 Python 3.10–3.13 全部通过；`pyproject.toml` 仍声明 `requires-python = ">=3.10"`。

## 实际实现与证据

1. `tradingagents/api/planner.py` 直接把 `_canonical_json(...)` 插入 XML 风格分隔符。JSON 字符串不会转义 `<`/`>`，因此事件或候选自由文本可包含 `</untrusted_event>`、`</candidate_allowlist>` 或 `</untrusted_trigger_event>`，提前闭合边界。对抗性复现观察到同一 prompt 中出现两个 closing tag。
2. `tradingagents/api/schemas.py` 的 `Decimal` 字段没有 wire-type 前置校验；本地复现表明 `available_cash_cny`、`per_stock_cap_cny`、`reference_price` 和 `upper_limit_price` 均接受 JSON number。
3. `tradingagents/api/store.py`、`tradingagents/api/planner.py` 及三组新测试导入 Python 3.11 才加入的 `datetime.UTC`。Python 3.10 的生产模块 import 和 pytest collection 均失败；PR 的 `tests (py3.10)` 与 `CI Gate` 当前为红色。
4. 按用户决定建立独立 Python 3.10 环境后进一步发现：`asyncio.wait_for` 在 3.10 抛出的
   `asyncio.TimeoutError` 未被只写内建 `TimeoutError` 的分支捕获，预期 504 `analysis_timeout`
   被错误降为 503 `planner_failed`；现有跨版本 timeout 测试直接复现该偏移。

## 偏移原因

实现采用了可读的 XML 风格标签、Pydantic 默认类型转换、`datetime.UTC` 简写及 3.11 后统一的
timeout exception 形态，但没有同时实现标签内容编码、wire string 类型门禁和完整 Python 3.10
兼容层。

## 影响与风险

- 恶意或被污染的事件文本可逃逸“未信任证据”标签并以标签外文本影响候选选择、graph 结论和机器计划，破坏本 PR 最关键的提示注入安全边界。
- JSON number 会扩大已经冻结的外部协议，令不同客户端的浮点序列化参与幂等哈希，并与文档及后续 TestingStrategies 实现产生契约漂移。
- 修复前 Python 3.10 用户无法导入新 API，required CI Gate 阻止合并；当时架构 TODO 中“全量验证
  通过、偏移已解决”的结论不成立。

## 可选处理方案

1. **修改实现回归设计（推荐）**：对所有嵌入 prompt 的未信任 JSON 采用不会生成原始 closing tag 的编码，并新增精确 closing-tag 对抗测试；对 wire 金额/价格先验证原始 JSON 值必须为字符串；把 `UTC` 改为 Python 3.10 可用的 `timezone.utc`，然后重跑完整矩阵。
2. **经用户确认后更新设计**：把最低 Python 版本提高到 3.11，并显式放宽 wire number 与提示边界。但该方案会改变已批准的外部协议、安全门禁和兼容范围，不应由实现静默决定。

## 推荐及理由

推荐方案 1。它不改变已批准的跨仓协议，修复范围有限，并能让现有 required checks 与安全验收直接证明实现重新符合设计。

## 用户决定

用户于 2026-08-13 在获知三项偏移、required Python 3.10/CI Gate 失败及两种方案影响后，明确选择
推荐方案 1：修改实现回归既有设计。具体为：所有进入 prompt 分隔区的 JSON 都不得包含原始
`<`、`>`、`&`；金额和价格的外部 request wire 值必须在 Decimal 解析前证明为 JSON string；
生产代码与测试恢复 Python 3.10 兼容。不得借修复提高最低 Python 版本、放宽 wire schema 或把当前
提示边界偏移写成新设计。

## 阻塞范围

在修复、对抗测试和 Python 3.10–3.13/full CI 验证完成前，继续阻塞 PR #4 合并、后续
TestingStrategies 对接及任何部署/activation。与本 PR 无关的开发不受影响。验证完成后把本记录
更新为“已解决”，保留原发现、用户决定、落地提交和验证证据。

## 落地进度

- 实现已改为：进入 prompt 的 JSON 对原始 `<`、`>`、`&` 使用稳定 JSON Unicode escape；四个
  request Decimal wire 字段用前置 validator 拒绝 JSON number；生产与测试代码使用
  `timezone.utc`，timeout 分支显式捕获跨版本 `asyncio.TimeoutError`。
- 对抗测试证明 selector、allowlist、trigger、limits 和 graph conclusions 每个 closing tag 只剩
  代码拥有的一处；API 对 numeric decimal 在 planner 前返回 422。
- 本地 Python 3.10、3.11、3.12、3.13 全仓各为 `705 passed, 2 skipped, 69 subtests passed`；两个
  skip 是既有可选 Bedrock extra 与无真实 key 的 DeepSeek live test。
- 全仓 Ruff、8 个本次文件 format、部署合同、24 个部署测试、Bash syntax、server Compose 渲染、
  生产镜像非 root/import/CLI smoke 均通过；镜像 smoke 使用 `--network none`，没有调用真实 LLM、
  数据库、券商或服务器。本机仍无 `shellcheck`，由 required CI 复核。
- 修复提交 `59e00eba3c8dbf13d2a240464d3ae8de9c0f72e5` 已进入 PR #4。GitHub Actions
  run `31704530014` 对该 head 的 Python 3.10/3.11/3.12/3.13、clean-install smoke、strict full-repo
  Ruff、deployment contract and image build 及最终 CI Gate 共 8 项全部成功。

## 解决结论

实现、wire schema、提示边界、最低 Python 版本与原设计重新一致；本记录更新为“已解决”并继续保留
为审计历史。PR #4 仍为 OPEN，未 merge、未部署；外部 network、secret 与 TestingStrategies
activation 仍由各自后续 PR/运维流程拥有。
