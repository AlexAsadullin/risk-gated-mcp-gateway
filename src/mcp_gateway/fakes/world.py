"""A deterministic synthetic market shared by the in-process fakes.

Prices are a closed-form function of (symbol, bar index): a slow sine cycle plus seeded Gaussian noise,
``p_i = p0 * exp(a * sin(2 pi i / P) + s * z_i)`` with ``z_i ~ N(0, 1)`` drawn from ``Random(f"{seed}:{symbol}:{i}")``.
This is not market evidence; it only gives the fakes stable, reproducible numbers.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime

from mcp_gateway.settings import FakeSettings
from mcp_gateway.timeutil import NS_PER_SECOND, to_ns


@dataclass
class SyntheticWorld:
    base_prices: dict[str, float]
    seed: int
    bar_seconds: int
    knowledge_delay_s: int
    cycle_amplitude: float
    cycle_bars: int
    noise_scale: float

    def bar_index(self, ts_ns: int) -> int:
        return ts_ns // (self.bar_seconds * NS_PER_SECOND)

    def price_at_index(self, symbol: str, index: int) -> float:
        base = self.base_prices[symbol]
        z = random.Random(f"{self.seed}:{symbol}:{index}").gauss(0.0, 1.0)
        cycle = self.cycle_amplitude * math.sin(2 * math.pi * index / self.cycle_bars)
        return base * math.exp(cycle + self.noise_scale * z)

    def price(self, symbol: str, at: datetime) -> float:
        return self.price_at_index(symbol, self.bar_index(to_ns(at)))

    def bars(self, start_ns: int, end_ns: int) -> range:
        """Bar indices whose valid time lies in ``[start, end]``."""
        step = self.bar_seconds * NS_PER_SECOND
        first = -(-start_ns // step)
        last = end_ns // step
        return range(first, last + 1)

    def valid_ns(self, index: int) -> int:
        return index * self.bar_seconds * NS_PER_SECOND

    def knowledge_ns(self, index: int) -> int:
        return self.valid_ns(index) + self.knowledge_delay_s * NS_PER_SECOND


def world_from_settings(cfg: FakeSettings) -> SyntheticWorld:
    return SyntheticWorld(
        base_prices=dict(cfg.base_prices),
        seed=cfg.seed,
        bar_seconds=cfg.bar_seconds,
        knowledge_delay_s=cfg.knowledge_delay_s,
        cycle_amplitude=cfg.cycle_amplitude,
        cycle_bars=cfg.cycle_bars,
        noise_scale=cfg.noise_scale,
    )
