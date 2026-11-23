"""Claude list prices, for the eval's cost numbers."""

# US dollars per 1M tokens (input, output), the list prices in cost.py from SHY-14
# (PR #17), checked 2026-11-22. Switch to cost.PRICES once that PR is merged.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def dollars(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost of one call; 0 for a model without a price here."""
    price_in, price_out = PRICES.get(model, (0, 0))
    return (input_tokens * price_in + output_tokens * price_out) / 1e6
