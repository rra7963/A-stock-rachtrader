# Event Portfolio Plan API Architecture

## Status and ownership

- **Status:** schema 1.0 approved and verified on 2026-08-17; backward-compatible schema 1.1
  extension for A events and STAR Market instruments merged through PR #16; bounded independent-graph
  concurrency approved on 2026-09-01 and implemented in the current PR
- **Protocol owner:** `rra7963/A-stock-rachtrader`
- **Execution and broker owner:** `downstream execution system`
- **Dependency:** the portfolio-allocation stack is present on `main` through recovery PR #15;
  PR #13 now contains only this resource and its audit commits on top of that main
- **Consumer:** Rachel downstream execution service service `rachel_executor` (Rachel Executor)

This contract adds a batch portfolio resource without changing `POST /v1/event-trade-plans`.
TradingAgents researches every supplied instrument and chooses one long-only target portfolio.
It never owns an account, credential, order, fill, position reconciliation, stop loss, or take profit.

## Endpoint and transport

- `POST /v1/event-portfolio-plans`
- The existing bearer token, private Docker bridge, no-host-port boundary, process-wide analysis
  lock, persistent request-id idempotency, sanitized errors and no automatic retry remain in force.
- The endpoint has a separate bounded server timeout. The request also carries an absolute
  `decision_deadline`; the earlier boundary wins. Late worker results are discarded.
- `TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY` is a closed integer from 1 through 4 and defaults
  to 4, including on existing servers whose API env file predates the setting. It changes only
  in-request instrument research parallelism, never the process-wide request admission limit.
- On OpenRouter, only the final portfolio allocator uses native strict JSON Schema and
  `provider.require_parameters=true`. This keeps allocator requests away from routed providers that
  cannot honor structured outputs. It can reduce the eligible provider pool and change latency or
  price, but it does not change the selected model, call count, candidate universe, or any other
  graph/adapter request.
- The current single-event endpoint remains wire-compatible and keeps its 900-second ceiling.

## Request contract

Both versions use the same closed object shape. `schema_version="1.0"` preserves the original
contract; `schema_version="1.1"` is an explicit opt-in for the new event/market domain:

- a stable `request_id`, aware `requested_at`, and aware `decision_deadline`;
- one half-open `event_window` with `starts_at < ends_at`, `coverage_status` equal to `complete`
  or `partial`, and a non-negative `coverage_gap_count` consistent with that status;
- zero to 100 events. Version 1.0 accepts only exact `S`; version 1.1 accepts exact `S|A`. Event
  text is bounded, untrusted evidence. Rachel downstream execution service deterministically orders S before A when
  its event limit applies;
- one to 20 unique instruments. Version 1.0 accepts the original ordinary Shanghai/Shenzhen
  A-share prefixes; version 1.1 additionally accepts `688xxx/689xxx`. Every non-held instrument has
  at least one high-relevance link to a supplied event; a held instrument may have no current-window
  event. Each link points to an event in the same request;
- an account-free portfolio snapshot containing non-negative available cash and positive total
  equity. Each instrument carries its current quantity, optional sellable quantity and confirmed
  cost, reference/limit prices, quote time, and current weight.

The caller always places all existing holdings first. Remaining universe slots are deterministic
high-relevance enabled-event candidates, with S candidates taking precedence over A at the limit.
If more than 20 instruments exist, Rachel downstream execution service discloses
the truncated-candidate count; TradingAgents cannot select outside the supplied universe.

Per the user's 2026-08-17 decision, this resource has no 10-position, CNY 100,000, or fixed 25%
single-name limit. Those legacy limits remain unchanged on `/v1/event-trade-plans`. The batch
portfolio still forbids short weights and leverage.

## Response contract

The closed response echoes the exact request schema version and contains request/hash/decision
identity, aware `decided_at`, exact model
provenance, one allocation row for every request instrument in request order, an explicit cash
weight, a concise portfolio summary and a risk note. Each row contains:

- exact request `stock_code`;
- target weight from `0.00` to `100.00` as a decimal JSON string;
- the exact five-tier rating from that instrument's completed graph;
- a bounded relative-allocation rationale.

All stock weights plus cash must equal exactly `100.00`. A zero weight is an explicit exit target;
a higher/lower weight than the broker snapshot is an add/reduce target. There is no deterministic
rating fallback, free-text JSON fallback, invented member, silent omission, normalization of an
invalid total, or order payload. Any strict-output or membership mismatch fails closed.

The allocator's internal structured proposal expresses each stock and the explicit cash target as
integer basis points, where 100 basis points is 1.00% and all components must total exactly 10000.
This is a lossless internal representation of the two-decimal wire weights, not normalization or a
fallback. The proposal repeats each stock code in request order but does not repeat the graph's
rating: the planner deterministically binds the already validated per-stock rating after checking
exact proposal membership/order. The allocator therefore cannot silently change a completed graph
rating, while it still chooses every stock weight, cash weight, public rationale, summary and risk
note.

## Analysis pipeline

1. Validate and hash canonical closed JSON before any model call.
2. Provision exactly one mutable graph per possible in-flight instrument, up to the configured
   bound. The coordinator graph is one worker; every additional worker is a separately constructed
   graph with the exact same config. Reusing one graph object or mixing configs fails readiness.
3. A fixed worker loop claims instruments from the exact request universe. Each worker processes at
   most one instrument at a time with checkpoint reuse disabled. Its trigger context includes only
   that instrument's linked S/A events with their real signal levels, current holding facts, the
   batch window and any `partial` coverage warning. Event text remains delimited and escaped as
   untrusted evidence. Unique request stock codes keep graph result files disjoint; process-local
   memory-log reads and writes are serialized across worker graphs.
4. Check `decision_deadline` before and after every graph call. The first failure stops workers from
   claiming new instruments, but the planner waits for every already in-flight graph to finish before
   returning failure. The API therefore retains its process-wide analysis lock until external calls
   have actually ended.
5. Give the completed per-stock ratings/conclusions and current portfolio facts to one strict
   cross-sectional allocation model. On OpenRouter, bind the allocator alone to native strict JSON
   Schema and require a provider that supports the requested parameters.
6. Validate the closed basis-point proposal, exact membership/order and exact 10000 total; convert
   basis points losslessly to two-decimal percentages, bind the completed graph ratings by exact
   stock code, validate the unchanged public response schema, and compute the response decision
   hash.

The SQLite terminal record stores only an allowlisted stage code such as
`planner_failed_instrument_graph`, `planner_failed_instrument_rating`,
`planner_failed_allocator_invoke`, `planner_failed_allocator_schema`,
`planner_failed_allocator_binding`, `planner_failed_allocator_total`,
`planner_failed_deadline`, or `planner_failed_response_binding`. Legacy
`planner_failed_allocator` remains readable/allowlisted for already persisted rows and unexpected
allocator implementation failures. The `event_portfolio_analysis` progress records contain only
stage, completed count, total count, configured concurrency and elapsed milliseconds. Neither this
progress record nor the SQLite terminal code records raw event text, instrument identity, account
facts, credentials, provider responses or hidden reasoning. The HTTP response remains the existing
generic `planner_failed` contract.

## Execution boundary

Rachel downstream execution service independently binds the response (including schema version) to the exact request,
persists it, obtains new broker/quote snapshots, converts target weights to exchange-valid deltas,
and performs sell-confirm-before-
buy execution. It may lower or skip quantities for cash, price-limit, T+1, stale quote, unresolved
intent, hard -9% stop-loss or +20% take-profit safety, but never raises a TA target or claims an
accepted order was filled.

## Acceptance criteria

- closed schema tests cover 1.0 rejection and 1.1 acceptance of A/688/689, unknown signal levels,
  unknown fields, duplicate members/events/links, cross-reference errors, partial coverage
  consistency, aware times, decimal-string enforcement and exact 100% totals;
- planner tests prove per-instrument event and mutable-graph isolation, bounded concurrency 1 through
  4, failure waiting for all in-flight workers, stopped scheduling after failure, process-local
  memory-log serialization, held-without-event support, one-to-20 members, exact rating/membership
  preservation, maximum-universe strict native allocator binding, lossless basis-point conversion,
  closed allocator failure substages, real A-level prompt provenance, 688/689 Shanghai ticker
  routing, partial-coverage prompt visibility, deadline checks and sanitized failures;
- API tests cover shared authentication/analysis lock, separate idempotency table, cache/conflict,
  timeout/cancellation lock retention, safe persisted terminal stages, closed concurrency config,
  response binding and coexistence with v1;
- full existing tests, strict Ruff, deployment contract and immutable image checks stay green;
- no live LLM, event platform, account, broker, order, deployment, merge or production mutation is
  authorized by this PR.
