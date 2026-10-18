"""USD prices per million tokens, used to enforce per-task and daily spending caps.

Values are Anthropic's published first-party list prices (input, output, 5-minute
cache write, cache read). Unknown cloud models are charged at the most expensive
known rate so caps err on the safe side; override with MODEL_INPUT_PRICE_PER_MTOK /
MODEL_OUTPUT_PRICE_PER_MTOK in .env.
"""

from __future__ import annotations

from .base import Usage

ANTHROPIC_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 12.5, 0.25),
    "claude-fable-5": (10.0, 50.0, 12.5, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 6.25, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 0.10),
}
CONSERVATIVE = (10.0, 50.0, 12.5, 1.0)


def anthropic_price(model: str) -> tuple[tuple[float, float, float, float], bool]:
    if model in ANTHROPIC_PRICES:
        return ANTHROPIC_PRICES[model], True
    for known, price in ANTHROPIC_PRICES.items():
        if model.startswith(known):
            return price, True
    return CONSERVATIVE, False


def apply_cost(usage: Usage, price: tuple[float, float, float, float]) -> Usage:
    in_p, out_p, write_p, read_p = price
    usage.cost_usd += (
        usage.input_tokens * in_p
        + usage.output_tokens * out_p
        + usage.cache_write_tokens * write_p
        + usage.cache_read_tokens * read_p
    ) / 1_000_000
    return usage


def override_price(input_price: float | None, output_price: float | None) -> tuple[float, float, float, float] | None:
    if input_price is None or output_price is None:
        return None
    return (input_price, output_price, input_price * 1.25, input_price * 0.1)
