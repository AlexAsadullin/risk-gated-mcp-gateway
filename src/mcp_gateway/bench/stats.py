"""Small, dependency-free statistics for the report: Wilson intervals and percentile bootstrap."""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence

Z_95 = 1.959963984540054  # standard normal quantile for a two-sided 95% interval


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (Wilson 1927, JASA 22:209-212).

    ``(p + z^2/2n -/+ z sqrt(p(1-p)/n + z^2/4n^2)) / (1 + z^2/n)``; well behaved at p = 0 and p = 1.
    """
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (type 7, the numpy default)."""
    if not values:
        raise ValueError("quantile of an empty sample")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def median(values: Sequence[float]) -> float:
    return quantile(values, 0.5)


def bootstrap_ci(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float],
    *,
    n_resamples: int,
    seed: int,
    level: float = 0.95,
) -> tuple[float, float]:
    """Percentile bootstrap interval of ``statistic`` (Efron and Tibshirani 1993, ch. 13), seeded."""
    rng = random.Random(seed)
    n = len(values)
    stats = [statistic([values[rng.randrange(n)] for _ in range(n)]) for _ in range(n_resamples)]
    alpha = (1 - level) / 2
    return (quantile(stats, alpha), quantile(stats, 1 - alpha))


def bootstrap_diff_ci(
    a: Sequence[float],
    b: Sequence[float],
    statistic: Callable[[Sequence[float]], float],
    *,
    n_resamples: int,
    seed: int,
    level: float = 0.95,
) -> tuple[float, float]:
    """Percentile bootstrap interval of ``statistic(a) - statistic(b)`` from independent resamples."""
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_resamples):
        ra = [a[rng.randrange(len(a))] for _ in range(len(a))]
        rb = [b[rng.randrange(len(b))] for _ in range(len(b))]
        diffs.append(statistic(ra) - statistic(rb))
    alpha = (1 - level) / 2
    return (quantile(diffs, alpha), quantile(diffs, 1 - alpha))
