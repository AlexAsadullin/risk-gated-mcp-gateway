# Risk-gated MCP Gateway (Tradeq I9)

AI agents reach the Tradeq platform through one Model Context Protocol (MCP) server. In research sessions every
data, feature, forecast and backtest call is answered as known at a session clock that the agent cannot move;
every backtest and order intent is logged as a trial in the research ledger (S6); orders pass pre-trade checks
and per-agent budgets before they reach the shadow exchange (I6) or the execution engine (I7). In 200 scripted
adversarial episodes the gateway blocked 1,225 of 1,225 policy violations (Wilson 95% lower bound 99.7%) with
0 of 2,686 false refusals, while an unguarded pass-through baseline blocked none and returned 11,620 rows from
after the session clock. Specification: `quant_platform_plan_full.md`, project I9.

## Quickstart

```bash
make setup          # uv venv --python 3.13 .venv && uv sync
cp .env.example .env   # set TRADEQ_DATA_DIR and GATEWAY_ADMIN_API_KEY
make lint typecheck test
make reproduce      # offline demo against in-process fakes; prints the audit log
make report         # experiments behind docs/report.md (about 80 s)
make serve          # admin API + MCP over streamable HTTP on 127.0.0.1:8608
```

Connect an agent over HTTP:

1. `POST /admin/v1/sessions` with `Authorization: Bearer $GATEWAY_ADMIN_API_KEY` and
   `{"agent_id": "a1", "mode": "research", "known_at": "2025-06-01T00:00:00Z", "budgets": {}}`.
   The response contains `session_id` and a one-time `api_key`.
2. Point the MCP client at `http://127.0.0.1:8608/mcp` with `Authorization: Bearer <api_key>`.
   An orchestrator holding the admin key can instead send the admin key plus `X-Gateway-Session: <session_id>`.
3. Move the clock with `POST /admin/v1/sessions/{session_id}/clock` and `{"known_at": ...}` (forward only).

For stdio: `gateway session-create --agent-id a1 --mode research --known-at 2025-06-01T00:00:00Z`, put the printed
key in `GATEWAY_AGENT_API_KEY`, and run `gateway stdio` (`--offline` uses the fakes).

## Configuration

Defaults live in `config/settings.yaml`. Any key is overridden by an env variable with prefix `GATEWAY_` and
nested delimiter `__`, e.g. `GATEWAY_RISK__PRICE_BAND_BPS=150`. Secrets come only from env:
`GATEWAY_ADMIN_API_KEY` (admin API returns 503 without it) and `GATEWAY_AGENT_API_KEY` (stdio).
`TRADEQ_DATA_DIR` is the data root. The sections that matter most:

| Section | Controls |
|---|---|
| `services` | base URLs and timeouts of S6, I2, S1, I5, S7, I6, I7 |
| `budgets` | default per-agent limits; `on_breach` (refuse or freeze) per budget; tools allowed while frozen |
| `risk` | instrument whitelist, max order and position notional, fat-finger band in basis points |
| `modes` | `live_enabled` (off by default) and the S6/S7 conditions for live orders |
| `data` | I2 dataset names, row cap, `time_travel_policy` (refuse or clamp) |
| `pricing` | USD per million tokens per model, used by `report_usage` |

## Architecture

```
MCP client --stdio / streamable HTTP--> mcp_server.py --+
admin client --HTTP--> admin.py ------------------------+--> gateway.py --> clients/ --> S6 I2 S1 I5 S7 I6 I7
                                                          |    timetravel.py  risk.py  budgets.py  trials.py
                                                          +--> sessions.py (SQLite)   audit.py (hash-chained JSONL)
```

`gateway.py` holds all policy and is transport-free. Every tool call is authenticated, serialised per session,
checked against the frozen state, and written to the audit log, where each entry carries the SHA-256 of the
previous one. Data written: `$TRADEQ_DATA_DIR/risk-gated-mcp-gateway/{sessions.sqlite3,audit/,demo/,reports/}`
and, only when S6 is unreachable, `$TRADEQ_DATA_DIR/research-ledger/inbox/i9-<yyyymmdd>.jsonl`.
`fakes/` holds in-process fakes of every downstream service; `bench/` holds the report experiments.

## Contracts

Produced: 6.6 (conventions section 6.6). Tools: `clock`, `market_data`, `features`, `forecast`, `run_backtest`,
`verdict`, `place_order`, `cancel_order`, `positions`, `account`, `budget`, `report_usage`. No tool takes
`known_at`. Success results carry `structuredContent` that matches the tool's `outputSchema`; refusals come back
with `isError: true` and a structured body `{refused: true, code, message, details, session_status}`.

Consumed: 6.1 (S6 trials, verdicts, leakage events, inbox fallback), 6.3 (I2 queries, 409 leakage guard),
6.4 (I6 and I7 order API), 6.5 (S1 forecast, I5 backtests, S7 probe).

Extensions (optional fields only):

| Where | Extension |
|---|---|
| `POST /admin/v1/sessions` | request: optional `account_id`; response: `api_key` (shown once, stored as SHA-256), `mcp_path`, limits, usage |
| admin API | extra routes: `GET /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/unfreeze`, `POST /sessions/{id}/close`, `GET /sessions/{id}/audit` |
| MCP auth | `Authorization: Bearer <session api_key>`, or admin key plus `X-Gateway-Session` |
| `TrialRecord` from I9 | `source="i9"`, empty `returns`; `params.linked_trial_id` and tag `linked:i5:<id>` point at I5's own trial; refused attempts are logged with tag `refused:<code>` |
| `verdict` tool | adds the S7 leakage reports, `leakage_status` and `promotable` to the S6 verdict |
| I2 queries | `format=json` as a query parameter; filters `{"venue", "symbol"}` for tape and `{"symbol": [...]}` for features |

## Key results

From `make report` (synthetic fakes, laptop; details and method in [docs/report.md](docs/report.md)).

| Metric | Value (95% CI) |
|---|---|
| Violations blocked, guarded gateway | 1,225 / 1,225, 100% (99.7 to 100) |
| False refusals on benign controls | 0 / 2,686, 0% (0 to 0.14) |
| Violations blocked, pass-through baseline | 0 / 1,225, 0% (0 to 0.3) |
| MCP overhead per `market_data` call, stdio | 3.01 ms (2.96 to 3.06) |
| MCP overhead per `market_data` call, streamable HTTP | 4.05 ms (3.96 to 4.14) |
| Guard cost per `place_order`, vs pass-through | 4.36 ms (4.32 to 4.40) |

## Limitations and roadmap

All downstream services are fakes in v0.1; real I2, I5, S6, S7, I6 and I7 have not been exercised. I7 does not
exist yet, so live routing is tested against a second fake with the Order API. The I2 query JSON shape and filter
format are assumptions until I2 publishes them; the client accepts row lists, `{"rows": ...}`, columnar JSON and
Arrow IPC. Contract 6.1 `LeakageEvent.source` has no `i9` value, so look-ahead attempts that the gateway blocks
itself stay in the gateway audit log instead of S6. Session state is SQLite with per-process locks; two gateway
processes serving the same session can interleave updates.

Roadmap: propose `i9` as a `LeakageEvent` source; cache S6/S7 gate results per strategy; an approval step
(stage, approve, execute) for agent orders as I7 specifies; per-agent budgets that persist across sessions.
