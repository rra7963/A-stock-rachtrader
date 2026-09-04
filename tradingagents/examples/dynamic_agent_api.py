"""Minimal runnable example for ETF Platform's Dynamic Agent API."""

from __future__ import annotations

import argparse
import json
import os

from tradingagents.integrations import call_dynamic_agent


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one paid Dynamic Agent API example")
    parser.add_argument(
        "--model",
        default="mistralai/ministral-8b-2512",
        help="OpenRouter model ID selected for this request",
    )
    parser.add_argument(
        "--caller-request-id",
        default="tradingagents-example-001",
        help="Trace ID for this non-idempotent synchronous request",
    )
    args = parser.parse_args()

    result = call_dynamic_agent(
        model=args.model,
        openrouter_api_key=os.environ["OPENROUTER_API_KEY"],
        prompt=(
            "You are an independent trading-risk reviewer. Summarize the evidence, "
            "identify the main risk, and give a concise review conclusion."
        ),
        content=json.dumps(
            {
                "ticker": "600519.SH",
                "strategy_signal": "The strategy produced a demonstration buy signal.",
                "position_limit": "This is an example only; do not place an order.",
            },
            ensure_ascii=False,
        ),
        temperature=0.2,
        max_tokens=600,
        # 可选：填稳定、非敏感的 key 标签，例如 tradingagents-default。
        # 不要填真实 OpenRouter API key；省略不影响鉴权。
        key_alias="tradingagents-example",
        # 可选：填本次业务请求的唯一追踪 ID，最长 128 字符。
        # 省略时客户端自动生成 UUID；该值不是幂等键。
        caller_request_id=args.caller_request_id,
    )

    print(f"run_id={result.run_id}")
    print(f"model={result.model}")
    print(f"usage={result.usage.model_dump()}")
    print(result.content)


if __name__ == "__main__":
    main()
