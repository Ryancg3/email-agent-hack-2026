"""Claude tokens and cost, for evals.

Every Claude call goes through agent.ask or agent.act, and both report the call's
tokens here. Nothing is stored or shown on the dashboard (SHY-14 decision: cost is
measured in evals only). Outside `track()` the tokens are dropped.

Usage, e.g. in an eval:
    with cost.track() as tally:
        agent.run(email, label_names)
    print(tally.input_tokens, tally.output_tokens, tally.dollars())
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

# US dollars per 1M tokens, Anthropic API list prices, checked 2026-11-22.
# Haiku 5.5 costs more above 100K input tokens per request; one email stays far below that.
# The agent doesn't use prompt caching, so cache reads and writes have no price here.
PRICES = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
}


def dollars(model: str, input_tokens: int, output_tokens: int) -> float:
    if model not in PRICES:
        raise KeyError(f"No price for {model}: add it to cost.PRICES")
    price = PRICES[model]
    return (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000


@dataclass
class Tally:
    """Tokens of the Claude calls made inside one `track()`, per model."""

    calls: int = 0
    tokens: dict[str, dict[str, int]] = field(default_factory=dict)  # model -> input, output

    @property
    def input_tokens(self) -> int:
        return sum(t["input"] for t in self.tokens.values())

    @property
    def output_tokens(self) -> int:
        return sum(t["output"] for t in self.tokens.values())

    def dollars(self) -> float:
        return sum(dollars(model, t["input"], t["output"]) for model, t in self.tokens.items())


_current: ContextVar[Tally | None] = ContextVar("cost_tally", default=None)


@contextmanager
def track() -> Iterator[Tally]:
    tally = Tally()
    token = _current.set(tally)
    try:
        yield tally
    finally:
        _current.reset(token)


def record(model: str, usage_metadata: dict | None) -> None:
    """Add one Claude call's tokens, from LangChain's AIMessage.usage_metadata."""
    tally = _current.get()
    if tally is None or not usage_metadata:
        return
    tokens = tally.tokens.setdefault(model, {"input": 0, "output": 0})
    tokens["input"] += usage_metadata.get("input_tokens", 0)
    tokens["output"] += usage_metadata.get("output_tokens", 0)
    tally.calls += 1
