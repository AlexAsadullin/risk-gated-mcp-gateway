"""``make report``: run every experiment cited in docs/report.md and write the metric tables.

Outputs go to ``$TRADEQ_DATA_DIR/risk-gated-mcp-gateway/reports/``: ``metrics.json`` (all numbers) and
``tables.md`` (the tables the report quotes). All randomness is seeded from ``demo.report_seed``.
"""

from __future__ import annotations

import json
import random
import shutil
from typing import Any

from mcp_gateway.bench.adversary import (
    ALL_ATTACKS,
    ArmResult,
    BaselineArm,
    GuardedArm,
    run_budget_episode,
    run_episode,
)
from mcp_gateway.bench.latency import measure_order_overhead, measure_transports
from mcp_gateway.bench.stats import bootstrap_ci, bootstrap_diff_ci, median, quantile, wilson_interval
from mcp_gateway.harness import Harness
from mcp_gateway.settings import Settings

REPORT_ADMIN_KEY = "report-admin-key-not-a-secret"
WARMUP_CALLS = 20
P95 = 0.95


def report_settings(settings: Settings, arm: str) -> Settings:
    base = f"{settings.storage.reports_dir}/runs/{arm}"
    storage = settings.storage.model_copy(
        update={"sessions_db": f"{base}/sessions.sqlite3", "audit_dir": f"{base}/audit"}
    )
    ledger = settings.ledger.model_copy(update={"inbox_relpath": f"{settings.storage.subdir}/{base}/inbox"})
    return settings.model_copy(update={"storage": storage, "ledger": ledger})


def _p95(values: list[float]) -> float:
    return quantile(values, P95)


def latency_table(
    samples: dict[str, dict[str, list[float]]], direct: list[float], b: int, seed: int
) -> list[dict[str, Any]]:
    rows = []
    for transport, tools in samples.items():
        for tool, values in tools.items():
            lo, hi = bootstrap_ci(values, median, n_resamples=b, seed=seed)
            row: dict[str, Any] = {
                "transport": transport,
                "tool": tool,
                "n": len(values),
                "median_ms": median(values),
                "median_ci95": [lo, hi],
                "p95_ms": _p95(values),
            }
            if tool == "market_data" and transport != "direct":
                dlo, dhi = bootstrap_diff_ci(values, direct, median, n_resamples=b, seed=seed)
                row["overhead_vs_direct_ms"] = median(values) - median(direct)
                row["overhead_ci95"] = [dlo, dhi]
            rows.append(row)
    return rows


def attack_table(results: dict[str, ArmResult]) -> list[dict[str, Any]]:
    rows = []
    for arm, result in results.items():
        counts = result.counts()
        for attack in ALL_ATTACKS:
            c = counts.get(attack, {"tp": 0, "fn": 0, "fp": 0, "tn": 0, "leaked_rows": 0})
            n_viol, n_benign = c["tp"] + c["fn"], c["fp"] + c["tn"]
            rows.append(
                {
                    "arm": arm,
                    "attack": attack,
                    "violations": n_viol,
                    "blocked": c["tp"],
                    "block_rate": c["tp"] / n_viol if n_viol else None,
                    "block_rate_ci95": list(wilson_interval(c["tp"], n_viol)),
                    "benign": n_benign,
                    "false_refusals": c["fp"],
                    "false_refusal_rate": c["fp"] / n_benign if n_benign else None,
                    "false_refusal_ci95": list(wilson_interval(c["fp"], n_benign)),
                    "leaked_future_rows": c["leaked_rows"],
                }
            )
    return rows


async def run_attacks(settings: Settings) -> tuple[dict[str, ArmResult], dict[str, int]]:
    results: dict[str, ArmResult] = {}
    frozen: dict[str, int] = {}
    for name in ("guarded", "baseline"):
        harness = Harness.create(report_settings(settings, name), admin_key=REPORT_ADMIN_KEY)
        arm = GuardedArm(harness) if name == "guarded" else BaselineArm(harness)
        rng = random.Random(settings.demo.report_seed)
        result = ArmResult()
        for i in range(settings.demo.report_episodes):
            await run_episode(arm, harness, rng, i, result)
            await run_budget_episode(arm, harness, rng, i, result)
        frozen[name] = arm.frozen if isinstance(arm, GuardedArm) else 0
        await harness.gateway.downstream.aclose()
        results[name] = result
    return results, frozen


def _fmt_ci(ci: list[float], pct: bool) -> str:
    if pct:
        return f"{100 * ci[0]:.1f} to {100 * ci[1]:.1f}"
    return f"{ci[0]:.3f} to {ci[1]:.3f}"


def markdown_tables(metrics: dict[str, Any]) -> str:
    lines = ["Table 1. Per-call latency by transport (ms)", ""]
    lines.append("| Transport | Tool | n | Median | 95% CI of median | p95 | Overhead vs direct (95% CI) |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in metrics["latency"]:
        overhead = ""
        if "overhead_vs_direct_ms" in r:
            overhead = f"{r['overhead_vs_direct_ms']:.3f} ({_fmt_ci(r['overhead_ci95'], False)})"
        lines.append(
            f"| {r['transport']} | {r['tool']} | {r['n']} | {r['median_ms']:.3f} | "
            f"{_fmt_ci(r['median_ci95'], False)} | {r['p95_ms']:.3f} | {overhead} |"
        )
    o = metrics["order_overhead"]
    lines += ["", "Table 2. place_order latency, guarded vs pass-through (ms, direct calls)", ""]
    lines.append("| Arm | n | Median | 95% CI of median | p95 |")
    lines.append("|---|---|---|---|---|")
    for arm in ("guarded", "baseline"):
        r = o[arm]
        lines.append(
            f"| {arm} | {r['n']} | {r['median_ms']:.3f} | {_fmt_ci(r['median_ci95'], False)} | {r['p95_ms']:.3f} |"
        )
    lines.append(
        f"\nMedian difference guarded minus baseline: {o['diff_ms']:.3f} ms (95% CI {_fmt_ci(o['diff_ci95'], False)})"
    )
    lines += [
        "",
        "Table 3. Adversarial attempts: block rate on violations and false-refusal rate on benign controls",
        "",
    ]
    lines.append(
        "| Arm | Attack | Violations | Blocked | Block rate % (95% CI) | Benign | False refusals % (95% CI) "
        "| Leaked rows |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in metrics["attacks"]:
        br = (
            "n/a" if r["block_rate"] is None else f"{100 * r['block_rate']:.1f} ({_fmt_ci(r['block_rate_ci95'], True)})"
        )
        fr = (
            "n/a"
            if r["false_refusal_rate"] is None
            else f"{100 * r['false_refusal_rate']:.1f} ({_fmt_ci(r['false_refusal_ci95'], True)})"
        )
        lines.append(
            f"| {r['arm']} | {r['attack']} | {r['violations']} | {r['blocked']} | {br} | {r['benign']} | {fr} | "
            f"{r['leaked_future_rows']} |"
        )
    lines.append(f"\nSessions frozen by budget breaches (guarded arm): {metrics['frozen_sessions']['guarded']}")
    return "\n".join(lines) + "\n"


async def run_report(settings: Settings) -> dict[str, Any]:
    out_dir = settings.reports_dir
    shutil.rmtree(out_dir / "runs", ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    d = settings.demo
    b, seed = d.report_bootstrap, d.report_seed

    harness = Harness.create(report_settings(settings, "latency"), admin_key=REPORT_ADMIN_KEY)
    samples = await measure_transports(harness, d.report_latency_calls, WARMUP_CALLS)
    orders = await measure_order_overhead(harness, d.report_latency_calls, WARMUP_CALLS)
    await harness.gateway.downstream.aclose()

    order_rows: dict[str, Any] = {}
    for arm, values in orders.items():
        order_rows[arm] = {
            "n": len(values),
            "median_ms": median(values),
            "median_ci95": list(bootstrap_ci(values, median, n_resamples=b, seed=seed)),
            "p95_ms": _p95(values),
        }
    order_rows["diff_ms"] = median(orders["guarded"]) - median(orders["baseline"])
    order_rows["diff_ci95"] = list(
        bootstrap_diff_ci(orders["guarded"], orders["baseline"], median, n_resamples=b, seed=seed)
    )

    attacks, frozen = await run_attacks(settings)
    metrics = {
        "schema_version": "1",
        "config": {
            "seed": seed,
            "episodes": d.report_episodes,
            "latency_calls": d.report_latency_calls,
            "warmup_calls": WARMUP_CALLS,
            "bootstrap_resamples": b,
        },
        "latency": latency_table(samples, samples["direct"]["market_data"], b, seed),
        "order_overhead": order_rows,
        "attacks": attack_table(attacks),
        "frozen_sessions": frozen,
    }
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    (out_dir / "tables.md").write_text(markdown_tables(metrics))
    return metrics
