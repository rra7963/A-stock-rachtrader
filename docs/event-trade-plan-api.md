# Event Trade Plan API Architecture

## Status and ownership

- **Status:** approved for implementation on 2026-08-13
- **Protocol owner:** `BeixiHub/trading-agents-adapted`
- **Execution and portfolio owner:** `BeixiHub/TestingStrategies`
- **First consumer:** TestingStrategies service `supporting_lobster` (display name: 配角小龙虾)

This document freezes the first machine-to-machine TradingAgents interface. It extends the
server from one persistent CLI toolbox to two independently healthy services built from the
same immutable image: the existing CLI toolbox and a dedicated event-plan API. The API is a
research and decision component. It never owns a broker account and never submits, cancels, or
reconciles orders.

## Responsibilities

The API owns:

1. strict validation of a versioned S-event envelope, high-relevance A-share allowlist, and an
   account-free portfolio summary;
2. selecting zero or one candidate from that exact allowlist;
3. running the existing TradingAgents graph for the selected instrument with the supplied event
   represented as explicitly untrusted evidence;
4. reducing the graph conclusion to a strict, bounded buy plan or a decline;
5. persistent request-id idempotency and response provenance.

TestingStrategies owns:

1. event provenance, the S/high-relevance filter, event freshness, trading-session calendar and
   the plan expiry window;
2. live quote, limit-up, lot-size, cash, current-holding and pending-order revalidation;
3. the dedicated paper-account identity and all broker calls;
4. intent journaling, confirmed-fill accounting, restart recovery and duplicate-order prevention;
5. deterministic full-position exits at -9% and +20% from confirmed entry cost.

The API output is never a broker payload. A syntactically valid response may still be rejected by
TestingStrategies when execution-time state has changed.

## Transport and deployment boundary

- HTTP/JSON endpoint: `POST /v1/event-trade-plans`.
- Liveness and readiness: `GET /health/live` and `GET /health/ready`.
- Authentication: `Authorization: Bearer <token>`; the token is read only from
  `TRADINGAGENTS_API_BEARER_TOKEN` and compared in constant time.
- The API container is attached to an externally managed Docker bridge network named by
  `TRADINGAGENTS_SHARED_NETWORK` (default `beixi-trading-internal`). It has the stable network
  alias `trading-agents-event-plan-api` and exposes port 8787 only to that Docker network.
- No host port is published. The existing CLI service remains present and does not receive the API
  token.
- Common LLM/data configuration remains in `/opt/trading-agents-adapted/.env`. The API bearer token
  is stored separately in `/opt/trading-agents-adapted/.event-plan-api.env`, mode 0600, and is never
  part of an image or release bundle.
- The caller deadline and server processing deadline are 900 seconds. TestingStrategies does not
  automatically retry a timeout, transport error, 429, 5xx response, or previously failed request.

The external network and both repositories' matching token files are deployment preconditions.
This PR documents them but does not create or modify server resources.

## Version 1 request

Every model rejects unknown fields. Monetary and price values are JSON decimal strings so hashing
and validation do not depend on binary floating-point behavior.

```json
{
  "schema_version": "1.0",
  "request_id": "supporting_lobster:event-123:2026-08-13",
  "requested_at": "2026-08-13T10:02:00+08:00",
  "event": {
    "event_id": "event-123",
    "signal_level": "S",
    "title": "Example material event",
    "summary": "Bounded event summary",
    "content": "Optional bounded source content",
    "source": "raw_events",
    "published_at": "2026-08-13T09:51:00+08:00",
    "received_at": "2026-08-13T09:52:00+08:00"
  },
  "candidates": [
    {
      "stock_code": "600000",
      "stock_name": "Example",
      "relevance": "high",
      "relevance_reason": "Directly named by the event",
      "reference_price": "10.20",
      "upper_limit_price": "11.22",
      "quote_at": "2026-08-13T10:01:30+08:00"
    }
  ],
  "portfolio": {
    "available_cash_cny": "500000.00",
    "current_position_count": 2,
    "max_position_count": 10,
    "per_stock_cap_cny": "100000.00",
    "held_stock_codes": ["000001", "600519"],
    "pending_buy_stock_codes": []
  }
}
```

Version 1 invariants:

- `signal_level` is exactly `S`; candidate relevance is exactly `high`.
- Candidate codes are six-digit ordinary Shanghai/Shenzhen A-share codes. STAR Market prefixes
  `688`/`689`, funds/ETFs, and unknown prefixes are rejected.
- There are 1–20 unique candidates. None may already be held or pending.
- The event must have been first received no more than 15 minutes after publication. The API also
  rejects a request timestamp materially different from server time; TestingStrategies performs
  the authoritative session/expiry check.
- `max_position_count` is at most 10 and `per_stock_cap_cny` is at most CNY 100,000. Pending buys
  reserve slots. No account id, broker token, database credential, or person identity is accepted.

The 20-candidate wire cap is a transport and prompt-size safeguard, not permission to buy more than
one stock. TestingStrategies may apply a lower candidate cap.

## Version 1 response

The success response is returned for both buy and decline decisions:

```json
{
  "schema_version": "1.0",
  "request_id": "supporting_lobster:event-123:2026-08-13",
  "request_sha256": "<64 lowercase hex characters>",
  "decision_id": "<64 lowercase hex characters>",
  "decided_at": "2026-08-13T10:08:00+08:00",
  "outcome": "buy",
  "selected_stock_code": "600000",
  "graph_rating": "Buy",
  "reason_code": "approved",
  "rationale": "Concise decision rationale, not private chain of thought.",
  "provenance": {
    "llm_provider": "openai",
    "deep_model": "gpt-5.5",
    "quick_model": "gpt-5.4-mini"
  },
  "plan": {
    "stock_code": "600000",
    "cash_amount_cny": "80000.00",
    "max_entry_price": "10.35",
    "order_type": "limit",
    "time_in_force": "DAY"
  }
}
```

A decline has `plan: null`; `selected_stock_code` and `graph_rating` may be null when the candidate
selector declines before graph execution. The response contains a concise rationale only. Agent
messages, hidden reasoning, credentials, raw database rows and full graph reports are not returned.

The API validates that a buy plan:

- names the selected allowlisted stock and no other stock;
- does not exceed the caller's cash or per-stock cap and never exceeds CNY 100,000;
- has a positive maximum entry price no higher than the supplied upper-limit price;
- can fund at least one 100-share board lot at that maximum price;
- appears only when the graph's exact final rating is `Buy`.

TestingStrategies independently rechecks all of these conditions against current broker and market
state. It may submit a smaller board-lot quantity but may never raise the API's amount or price.

## Decision pipeline and prompt-injection boundary

1. Pydantic validates and normalizes the wire request. The server hashes canonical JSON with
   SHA-256 before any LLM call.
2. A strict structured candidate selector sees the untrusted event and exact candidate allowlist.
   It returns `decline` or exactly one allowlisted code. Free-text fallback is forbidden.
3. The existing TradingAgents graph runs once for that code with the trigger event added to graph
   state. Every consumer sees a delimiter and the rule that event text is evidence, never an
   instruction. Checkpoint reuse is disabled for API requests; API idempotency is authoritative.
4. If the Portfolio Manager's exact parsed rating is not `Buy`, the API declines deterministically.
5. A strict structured plan adapter sees bounded graph conclusions, the selected snapshot and the
   portfolio caps. It returns a decline or one bounded limit plan. Free-text fallback is forbidden.
6. Deterministic post-validation checks membership, amount, price and board-lot feasibility. Any
   mismatch fails closed and no plan is returned.

The model never chooses a ticker outside the request, changes portfolio caps, changes event grade,
submits an order, or defines sell rules.

## Idempotency, concurrency and failures

- A SQLite database under the API-only named volume stores request id, request SHA-256, state and
  the final response. The raw request and bearer token are not stored.
- First receipt claims the id as `running`. The same id and same hash returns the cached completed
  response. The same id with a different hash returns HTTP 409.
- A duplicate while running, a previous failure, or a process-interrupted request returns a closed
  error and is not automatically re-executed. A human must investigate and deliberately issue a new
  request id if re-analysis is desired.
- One process-wide analysis may run at a time. Additional new requests return HTTP 429 and are
  recorded failed; clients do not retry automatically.
- Authentication/validation failures occur before the idempotency claim. Planner failures are
  sanitized into stable error codes; exception text and secrets are not returned.
- The server deadline is best-effort for the HTTP result: Python cannot safely kill an in-flight
  provider thread. On deadline the request is permanently failed and any late result is discarded;
  no order side effect exists in this repository.

## Immutable deployment and migration

The current deployed release contains one service while the new release contains two. Deployment
preflight and background verification therefore derive the expected service list from each
release's own Compose model. For every expected service, they require exactly one running, healthy
container with the requested immutable image URI and `io.beixi.deploy.sha`. Unexpected project
services fail verification.

New-release smoke checks cover CLI import/help plus API import and local readiness. Rollback uses the
previous release's own Compose model, so it removes the API container when returning to a CLI-only
release. Named volumes are never deleted.

## Non-goals

- accepting A/B events, low/medium relevance candidates, STAR Market securities, ETFs or arbitrary
  tickers;
- producing sell, rotation, stop-loss or take-profit instructions;
- reading an account, submitting an order or claiming a plan was executed;
- exposing a public/host HTTP port, cross-host API, queue, Docker socket or SSH execution path;
- retrying a failed API request or guaranteeing deterministic LLM output;
- changing the interactive CLI behavior.

## Acceptance criteria

1. Hermetic tests cover authentication, schema closure, S/high-only filtering, A-share allowlists,
   event freshness, candidate membership, strict LLM output, caps, board lots, idempotency,
   concurrency, timeout and sanitized errors.
2. Trigger-event tests prove the graph receives delimited untrusted evidence and checkpoint identity
   cannot cross event contexts.
3. Compose publishes no host port, isolates the API token from the CLI and uses the external network.
4. Deployment tests cover current one-service baseline, new two-service verification and rollback to
   a one-service release.
5. `uv run --frozen pytest -q`, strict Ruff, deployment-contract checks, Compose config and production
   image build/import smokes pass without live LLM, database, broker or internet calls from tests.
