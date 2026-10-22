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

Last task (2026-10-23): align with the shipped I2 and S6.

1. The repo is green: lint and `mypy --strict` are clean, and 69 default tests pass in about 10 s with 97% coverage.
   One opt-in test (`make test-integration`, marker `integration`) is deselected by default. It passed when run on
   2026-10-23, taking 3 s. It starts `point-in-time-store/.venv/bin/pit serve` and
   `research-ledger/.venv/bin/ledger serve` on free ports. Its scratch `TRADEQ_DATA_DIR` is
   `tmp/integration-<id>/data` (gitignored, removed on success). It seeds I2 with a contract-6.2 tape file through
   `pit ingest tape.trades` and runs a research session (data, a look-ahead refusal, a logged backtest, a refused
   peeking backtest, a verdict) and a paper session (the reference price from real I2, an order to the fake I6).
   It asserts that S6's verdict lists the gateway's `i9` leakage flag.
2. I2 client (`clients/pit.py`): it sends `format: "json"` in the body and typed filter lists, and parses
   `{fingerprint, columns, rows, flags}` and `X-Leakage-Flag`. It no longer guesses Arrow, row-list or columnar
   shapes, and pyarrow was removed as a dependency. `features` uses `POST /v1/features/training-set` (grid spine).
   Rows are sorted by `valid_time`, because I2 orders them by key columns. The fake I2 now mimics all of this.
3. S6: `TrialRecord.returns` is now null. Every look-ahead the gateway blocks itself is posted as a `LeakageEvent`
   (`source: "i9"`, `block`, or `warn` on clamp) with the inbox fallback (`trials.TrialLogger.report_leakage`).
   Ledger strategy ids changed from `agent/strategy` to `agent:strategy`, because S6's verdict route is
   `{strategy_id}`, not `{strategy_id:path}`.
4. `make report` was rerun after the fake-I2 change. Tables 3 and 4 (block rates, Fisher tests) are unchanged; the
   latency numbers changed (stdio overhead 1.85 ms, HTTP 2.38 ms, guard cost per order 3.28 ms). The README cites
   the new numbers. docs/report.md still has the old ones: as instructed, the corrected text went to the
   orchestrator (scratchpad file report-corrected.md) and was not written into the repo.
5. Contract extensions are unchanged, except that the I2 query-shape assumptions were removed from the README
   table; the README now documents what was checked against the real services.

## Mistakes and lessons

1. I first built the I2 client from guesses: a `filters` dict, `format` as a query parameter, and several response
   shapes. The shipped I2 forbids extra fields and takes a filter list, so the real service would have rejected
   every gateway query with 422. Lesson: read the producer's `models.py` and `api.py` before writing a client, and
   keep an opt-in test against the real service.
2. The ledger strategy-id template used `/`. Starlette decodes `%2F` before routing, so S6's verdict route would
   have answered 404 for every agent strategy. Only the real-service test can catch a mistake like this; the
   fakes used `{strategy_id:path}` and hid it.
3. Trials used to carry `returns: {ts: [], values: []}`. S6 accepts that, but `null` is the documented "no
   returns" value.
4. Only I2 and S6 are real in any test. I5, S1, S7 and I6 are fakes, and I7 does not exist, so live routing is
   tested against a second fake.
5. Session-state locks work per process, so two gateway processes that serve the same session could interleave
   updates. Run one process per session store.
6. Compound shell commands (`cd && ...`) are denied in this environment; use `git -C` and `make -C`.
