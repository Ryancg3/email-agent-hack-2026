"""Claude list prices, for the eval's cost numbers."""

# US dollars per 1M tokens (input, output), the list prices in cost.py from SHY-14
# (PR #17), checked 2026-11-22. Switch to cost.PRICES once that PR is merged.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def require_prices(models) -> None:
    """Stop before spending if a model has no price here, so its cost can't show as $0."""
    missing = sorted({m for m in models if m and m != "none" and m not in PRICES})
    if missing:
        raise SystemExit(f"No price for {', '.join(missing)}: add it to PRICES in evals/prices.py first.")


def dollars(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost of one call. A model without a price raises rather than costing $0."""
    if model not in PRICES:
        raise KeyError(f"No price for {model}: add it to PRICES in evals/prices.py")
    price_in, price_out = PRICES[model]
    return (input_tokens * price_in + output_tokens * price_out) / 1e6
