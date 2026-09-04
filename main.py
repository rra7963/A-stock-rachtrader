from tradingagents.agents.schemas import render_portfolio_allocation
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# DEFAULT_CONFIG already applies TRADINGAGENTS_* env-var overrides
# (llm_provider, deep_think_llm, quick_think_llm, backend_url, etc.),
# so users can switch models or endpoints purely via .env without
# editing this script. Override individual keys here only when you
# want a hard-coded value that should ignore the environment.
config = DEFAULT_CONFIG.copy()

# Initialize with custom config
ta = TradingAgentsGraph(debug=False, config=config)

# Analyze 2–20 stocks, then compare them as one portfolio. The result
# contains target stock weights plus cash and always totals 100%.
tickers = ["NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "JPM"]
_, allocation = ta.propagate_portfolio(tickers, "2026-08-07")
print(render_portfolio_allocation(allocation))

# Memorize mistakes and reflect
# ta.reflect_and_remember(1000) # parameter is the position returns
