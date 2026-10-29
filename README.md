# Risk-gated MCP Gateway (Tradeq I9)

AI agents reach the Tradeq platform through one Model Context Protocol (MCP) server. In research sessions every
data, feature, forecast and backtest call is answered as known at a session clock that the agent cannot move;
every backtest and order intent is logged as a trial in the research ledger (S6); orders pass pre-trade checks
and per-agent budgets before they reach the shadow exchange (I6) or the execution engine (I7). In 200 scripted
adversarial episodes the gateway blocked 1,225 of 1,225 policy violations (Wilson 95% lower bound 99.7%) with
0 of 2,686 false refusals, while an unguarded pass-through baseline blocked none and returned 11,620 rows from
after the session clock. Specification: `quant_platform_plan_full.md`, project I9.

Since 2026-10-29 the gateway targets MOEX and is not bound to any exchange. Every order and account call goes
through one broker-adapter protocol (contract 6.8), chosen per session. The default for paper sessions is the
built-in **mirror sandbox**: it reads the real MOEX order book and trade tape from I2 at the session clock and
matches the agent's orders against them (walking the book, queue position for resting orders, lots, ticks and
fees), so agents and RL policies trade a copy of the live market, or a replayed day, with no broker. The T-Bank
sandbox and the I6 shadow exchange are the other paper adapters, and I7 serves live. Two tools were added:
`market_slice` (contract 6.7, the same observation an RL policy and the terminal see) and `instrument` (the
broker's trading rules). The benchmark numbers below come from v0.1 sessions on the I6 fake, which the
benchmark still uses explicitly.

## Quickstart

```bash
make setup          # uv venv --python 3.13 .venv && uv sync
cp .env.example .env   # set TRADEQ_DATA_DIR and GATEWAY_ADMIN_API_KEY
make lint typecheck test
make reproduce      # offline demo against in-process fakes; prints the audit log
make report         # experiments behind docs/report.md (about 80 s)
make test-integration  # opt-in: real I2, I5 and S6 from the sibling repos' .venv (scratch data under tmp/)
make serve          # admin API + MCP over streamable HTTP on 127.0.0.1:8608
make serve-offline  # the same server wired to the in-process fakes (gateway serve --offline)
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
| `data` | I2 tape dataset template, row cap, `time_travel_policy` (refuse or clamp), feature spine entity and step |
| `pricing` | USD per million tokens per model, used by `report_usage` (see below) |

Prices are Anthropic first-party API rates as checked on 2026-10-23: Claude Opus 5.5 $4 / $20, Claude Sonnet 5.5
$2 / $10, Claude Haiku 5.5 $0.10 / $0.50 (prompts up to 100K tokens; $0.50 / $2.50 beyond, which the flat table
does not model), Claude Sonnet 4.6 $3 / $15, Claude Haiku 4.5 $1 / $5 (input / output per million tokens).
`claude-sonnet-4-5` was removed because its current rate could not be verified from the source used. Models that
are not listed are priced at the `default` row ($4 / $20, the Opus 5.5 rate), which is an assumption, or refused
with `unknown_model_policy: refuse`. `scripted-llm` is the deterministic test double, not a real model.

In paper and live sessions that have an operator clock (`known_at`), budgets and the order-rate window run on that
session clock, and the gateway sends it to I6/I7 in an `X-Tradeq-Known-At` header (an optional extension of
contract 6.4; a header because I6's `OrderRequest` forbids extra fields). The fake I6 fills at the last price
known at that time, which is the price the gateway's reference-price check reads from I2.

## Architecture

```
MCP client --stdio / streamable HTTP--> mcp_server.py --+
admin client --HTTP--> admin.py ------------------------+--> gateway.py --> clients/ --> S6 I2 S1 I5 S7
                                                          |    timetravel.py  risk.py  budgets.py  trials.py
                                                          |    brokers/router.py --> mirror (I2 book) | shadow (I6)
                                                          |                          tbank-sandbox    | execution (I7)
                                                          +--> sessions.py (SQLite)   audit.py (hash-chained JSONL)
```

Brokers (contract 6.8, `brokers/`): `base.py` defines `BrokerAdapter` (place, get, cancel, account, positions,
instrument) and `InstrumentRules`; `router.py` picks the session's adapter (`brokers.paper`, `brokers.live`,
`brokers.allowed_paper`; a session may name one at creation with `broker`). A new exchange or terminal is one new
adapter class and one config entry; the tools, budgets and risk checks do not change.

- `mirror.py`: the market-mirroring sandbox. Market and IOC/FOK orders walk the latest I2 book known at the clock
  (liquidity taken from one snapshot is remembered, so two orders cannot take the same shares); GTC limits rest
  behind the displayed size at their price and fill when later trades exhaust that queue or trade through the
  price; POST_ONLY that would trade is rejected. Rouble accounts with taker and maker fees; equity marked at the
  mid. State persists in `$TRADEQ_DATA_DIR/risk-gated-mcp-gateway/brokers/mirror-state.json`.
- `tbank_sandbox.py`: T-Bank Invest API `SandboxService` over the REST proxy (token from `TBANK_TOKEN`); opens and
  funds one sandbox account per gateway account, resolves symbols with `ShareBy`, converts shares to lots. The
  T-Bank sandbox fills by its own idealised rules, so it is an integration target, not a fill model.

Pre-trade checks gained the exchange rules (`risk.instrument_checks`): whole lots, price on the tick, inside the
daily price band, instrument tradable; `risk.require_instrument_rules` refuses instruments without rules.

`gateway.py` holds all policy and is transport-free. Every tool call is authenticated, serialised per session,
checked against the frozen state, and written to the audit log, where each entry carries the SHA-256 of the
previous one. Data written: `$TRADEQ_DATA_DIR/risk-gated-mcp-gateway/{sessions.sqlite3,audit/,demo/,reports/}`
and, only when S6 is unreachable, `$TRADEQ_DATA_DIR/research-ledger/inbox/i9-<yyyymmdd>.jsonl`.
`fakes/` holds in-process fakes of every downstream service; `bench/` holds the report experiments.

## Contracts

Produced: 6.6 (conventions section 6.6). Tools: `clock`, `market_data`, `features`, `forecast`, `run_backtest`,
`verdict`, `place_order`, `cancel_order`, `positions`, `account`, `budget`, `report_usage`, and since revision 2
`market_slice` and `instrument`. Contract 6.8 (broker adapter) is implemented here. No tool takes
`known_at`. Success results carry `structuredContent` that matches the tool's `outputSchema`; refusals come back
with `isError: true` and a structured body `{refused: true, code, message, details, session_status}`.

Consumed: 6.1 (S6 trials, verdicts, leakage events, inbox fallback), 6.3 (I2 queries, 409 leakage guard),
6.4 (I6 and I7 order API), 6.5 (S1 forecast, I5 backtests, S7 probe), 6.7 (I2 market slice), and the T-Bank
Invest API sandbox (`../docs/moex-sources.md`).

The opt-in test against the real I2, I5 and S6 (`make test-integration`, passed 2026-10-30) seeds I2 with a MOEX
tape (trades and one order-book snapshot), and checks that a paper order filled by the mirror at the best ask
stored in the real I2, and that `market_slice` returns the real I2 slice.

Checked against the shipped services (`make test-integration`, which passed on 2026-10-23 with I2, I5 and S6):

- I2: the gateway sends `POST /v1/query` with `"format": "json"` in the body and typed filters
  (`[{"column": "symbol", "op": "==", "value": ...}]`), and reads `{fingerprint, columns, rows, flags}` with
  ISO-8601 timestamps, `X-Dataset-Fingerprint` and `X-Leakage-Flag` (returned to the agent as `leakage_flags`).
  I2 orders rows by key columns, so the gateway sorts them by `valid_time` before truncating to the latest rows.
  The `features` tool uses `POST /v1/features/training-set` with a grid spine (`data.feature_entity_column`,
  `data.feature_step_seconds`).
- S6: trial records validate against `research_ledger/sdk/wire.py` (`returns: null`, `source: "i9"`). Each
  look-ahead attempt the gateway blocks itself is posted as a `LeakageEvent` with `source: "i9"`, severity
  `block` (`warn` when the clamp policy cut the window), with the S6 inbox as fallback; I2's own 409s are
  reported to S6 by I2. Ledger strategy ids are `<agent_id>:<strategy>`: S6's verdict route cannot match an
  id that contains `/`.
- I5: `POST /v1/backtests` carries I5's optional `strategy_id` (the agent-scoped ledger id), `actor`
  (`{"kind": "agent", "id": <agent_id>}`) and `seed` (taken out of `params`, because I5 strategies reject
  unknown parameters). I5 then logs the run in S6 under that id, so `verdict(strategy)` finds it; the gateway
  does not log a second record of the same run (`logged_to: "i5"`, `gateway_trial_id` is I5's trial id). Refused
  or failed backtests, which I5 never sees, are logged by the gateway in I5's family (`i5/<strategy>/<universe>`,
  setting `ledger.backtest_family_template`), because S6 counts `n_trials` per family.

Extensions (optional fields only):

| Where | Extension |
|---|---|
| `POST /admin/v1/sessions` | request: optional `account_id`; response: `api_key` (shown once, stored as SHA-256), `mcp_path`, limits, usage |
| admin API | extra routes: `GET /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/unfreeze`, `POST /sessions/{id}/close`, `GET /sessions/{id}/audit` |
| MCP auth | `Authorization: Bearer <session api_key>`, or admin key plus `X-Gateway-Session` |
| Order API (6.4) | `X-Tradeq-Known-At` request header on orders, positions and accounts when the session has an operator clock |
| `TrialRecord` from I9 | `source="i9"`, `returns: null`; only for runs I5 did not log itself (refused or failed), tagged `refused:<code>`; order intents in family `risk-gated-mcp-gateway/<agent>/order-intent/<strategy>` |
| `verdict` tool | adds the S7 leakage reports, `leakage_status` and `promotable` to the S6 verdict |
| `POST /admin/v1/sessions` | optional `broker` (one of `brokers.allowed_paper`); the session view reports it |
| `place_order` result | `routed_to` is the adapter label (`mirror`, `i6`, `i7`, `tbank-sandbox`); mirror and T-Bank orders add `filled_qty`, `avg_price`, `fee`, `account_id`, `broker` |

## Key results

From `make report` (synthetic fakes, laptop; details and method in [docs/report.md](docs/report.md)).

| Metric | Value (95% CI) |
|---|---|
| Violations blocked, guarded gateway | 1,225 / 1,225, 100% (99.7 to 100) |
| False refusals on benign controls | 0 / 2,686, 0% (0 to 0.14) |
| Violations blocked, pass-through baseline | 0 / 1,225, 0% (0 to 0.3) |
| MCP overhead per `market_data` call, stdio | 2.58 ms (2.47 to 2.71) |
| MCP overhead per `market_data` call, streamable HTTP | 3.33 ms (3.20 to 3.49) |
| Guard cost per `place_order`, vs pass-through | 4.04 ms (4.00 to 4.09) |

## Limitations and roadmap

Only I2, I5 and S6 have been exercised as real services (opt-in test); S1, S7 and I6 are fakes, and the report
experiments use the fakes throughout. The T-Bank sandbox adapter is tested against a mocked REST proxy only,
because no token exists yet. The mirror does not move the market (its own orders have no impact beyond the
liquidity they take from one snapshot), and AlgoPack books are polled snapshots, so queue position between two
snapshots is an estimate from the trade tape. I7 does not exist yet, so live routing is tested against a second fake with
the Order API. Session state is SQLite with per-process locks; two gateway
processes serving the same session can interleave updates.

Roadmap: run the integration test against I5, S7 and I6 once they ship; cache S6/S7 gate results per strategy; an approval step
(stage, approve, execute) for agent orders as I7 specifies; per-agent budgets that persist across sessions.
