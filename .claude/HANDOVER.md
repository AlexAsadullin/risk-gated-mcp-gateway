# HANDOVER: risk-gated-mcp-gateway (Tradeq I9)

Last updated 2026-10-23 by the orchestrating session. It was written from the finishing agent's final report and
from the repository.

## Goal

The gateway gives AI agents one MCP door to the platform. In research mode every data call is pinned to the
session clock, so agents cannot see the future. Every idea and backtest becomes a trial in S6, each agent has
budgets, and orders go only through the shared risk path (I6 for paper, I7 for live). The brief is
`../prompts/08-risk-gated-mcp-gateway.md`, and the default port is 8608 (MCP at `/mcp`). The project produces
contract 6.6 and consumes 6.1, 6.3, 6.4 and 6.5.

## Stack

Python 3.13 with a uv `.venv` and pins from `../constraints.txt`. Libraries: `mcp` 2.3.0 (MCPServer over
streamable HTTP and stdio), FastAPI and uvicorn (the admin API), httpx and httpx2, pyarrow (I2 Arrow responses),
SQLite (sessions), pydantic-settings (`GATEWAY_*`, `config/settings.yaml`), typer (the `gateway` CLI) and
structlog. Nothing was added beyond the constraints.

## Architecture and data flow

`app.py` mounts the MCP server (`mcp_server.py`, which exposes exactly the 12 tools of contract 6.6) and the admin
API (`admin.py`) in one ASGI app. A tool call goes through `gateway.py`. That module loads the session
(`sessions.py`: SQLite, hashed API keys, a forward-only clock), applies `timetravel.py` (it pins every request to
the session clock and drops returned rows with a later knowledge_time), checks `budgets.py`, and for orders runs
`risk.py` (whitelist, limit price present, fat-finger band against the I2 reference price, notional and position
limits). It then calls the downstream clients in `clients/`, logs trials through `trials.py` to S6 with the inbox
fallback, and writes every decision to the hash-chained audit log in `audit.py`. Refusals are structured
(`refusals.py`) and come back with `isError: true`. `fakes/` holds in-process FastAPI fakes of all seven
downstream services, which the demo, the tests and the report use.

## Files

| Path | Purpose |
|---|---|
| src/mcp_gateway/mcp_server.py, contracts.py, outputs.py | MCP tools and their input and output schemas |
| src/mcp_gateway/gateway.py | Tool dispatch: session, time travel, budgets, risk, downstream, trials, audit |
| src/mcp_gateway/sessions.py, admin.py, admin_models.py | Sessions, modes, clock, admin API |
| src/mcp_gateway/timetravel.py, budgets.py, risk.py, refusals.py | Guards |
| src/mcp_gateway/audit.py | Append-only, hash-chained JSONL audit log and its verifier |
| src/mcp_gateway/clients/{base,pit,research,ledger,orders}.py, downstream.py | Typed clients for I2, S1, I5, S6, S7, I6, I7 |
| src/mcp_gateway/fakes/{services,world}.py, harness.py | In-process fakes and the test harness |
| src/mcp_gateway/bench/{adversary,baseline,latency,stats,report}.py | make report experiments |
| src/mcp_gateway/demo.py | make reproduce |

## State at handover

1. The repo is finished and green. Lint and `mypy --strict` are clean, 69 tests pass in about 11 s, and coverage is 98%. The tree is clean, with 9 commits.
2. `make reproduce` runs a scripted agent session: a time-travel refusal, a logged backtest, a paper order at the fake I6, a fat-finger reject, and a budget breach that freezes the session. `make report` takes about 80 s.
3. docs/report.md is committed, and the orchestrator checked it against the writing rules.
4. Headline results (synthetic): the gateway blocked all 1225 violations by adversarial agents, with 0 false refusals in 2686 valid calls. A pass-through baseline in the style of laukikk/alpaca-mcp blocked none and returned 11620 future rows. Fisher's exact test gives p below 8.35e-38 for every attack class. Transport overhead per call is 3.01 ms for stdio and 4.05 ms for streamable HTTP, and the guards add 1.03 ms per data call and 4.36 ms per order.
5. Contract extensions, all optional: `account_id` on session creation, which returns a one-time `api_key` and `mcp_path`; admin routes to list, get, unfreeze and close sessions and to read the audit log; MCP auth through `Authorization: Bearer <api_key>`, or the admin key plus `X-Gateway-Session`; and `leakage_status` and `promotable` on the verdict tool.

## Mistakes and lessons

1. Only the fakes have been exercised. I7 does not exist, so live routing was tested against a second fake that speaks the Order API.
2. The shape of I2's JSON response was an assumption when I9 was built. The client accepts row lists, `{"rows": ...}`, columnar JSON and Arrow IPC. I2 has since shipped with ISO-8601 timestamps in JSON, so verify the client against the real I2 next.
3. I9 trials carry empty returns and link to I5's trial through `params.linked_trial_id`. S6 now accepts this and counts them toward n_trials.
4. `LeakageEvent.source` had no `i9` value, so blocked look-ahead attempts stayed in the audit log. S6 now accepts `i9`, so the gateway can start posting them.
5. Session-state locks work per process, so two gateway processes that serve the same session could interleave updates. Run one process per session store.
6. The latency numbers were measured while nine other agents loaded the laptop. Rerun them on an idle machine before quoting them.
