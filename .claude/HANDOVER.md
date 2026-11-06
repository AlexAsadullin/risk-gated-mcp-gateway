# HANDOVER: risk-gated-mcp-gateway (Tradeq I9)

Last updated 2026-11-05 (local-only trading and the T-Invest commission). Earlier: 2026-10-29/30 MOEX pivot,
2026-10-23 alignment with the shipped I2, I5 and S6.

## Goal

The gateway gives AI agents one MCP door to the platform. In research mode every data call is pinned to the
session clock, so agents cannot see the future. Every idea and backtest becomes a trial in S6, each agent has
budgets, and orders go only through the shared risk path to a broker adapter (contract 6.8): the local mirror
sandbox for paper by default, I7 for live. Brief: `../prompts/08-risk-gated-mcp-gateway.md` (revision 2); port
8608 (MCP at `/mcp`). Produces 6.6 and 6.8; consumes 6.1, 6.3, 6.4, 6.5 and 6.7.

User decisions (2026-11-05): trading runs only in our local sandbox, never through T-Bank (not even its sandbox);
the T-Invest token is for market-data download by I1 only. The mirror charges the real T-Invest commission:
0.25% of trade value (basic tariff), 0% for T-Bank's own funds (T-Capital ETFs).

## Stack

Python 3.13, uv `.venv`, pins from `../constraints.txt`: `mcp` 2.3.0 (streamable HTTP and stdio), FastAPI and
uvicorn (admin API), httpx, SQLite (sessions), pydantic-settings (`GATEWAY_*`, `config/settings.yaml`), typer,
structlog.

## Architecture and data flow

`app.py` mounts the MCP server (`mcp_server.py`) and the admin API (`admin.py`) in one ASGI app and builds the
`BrokerRouter`. A tool call goes through `gateway.py`: it loads the session (`sessions.py`: SQLite, hashed API keys,
forward-only clock), applies `timetravel.py`, checks `budgets.py`, and for orders runs `risk.py` (whitelist, limit
price, fat-finger band against the I2 reference price, notional and position limits, lot/tick/price-band rules).
Orders, cancels, positions and accounts go to the session's adapter (`brokers/router.py`): `mirror` (default for
paper; matches against the I2 book and trade tape at the session clock), `shadow` (I6), `execution` (I7, live),
`tbank-sandbox` (disabled). Trials go to S6 through `trials.py` with the inbox fallback; every decision goes to the
hash-chained audit log (`audit.py`). Refusals are structured (`refusals.py`, `isError: true`). `fakes/` holds
in-process fakes of all downstream services for the demo, the tests and the report.

## Files

| Path | Purpose |
|---|---|
| src/mcp_gateway/mcp_server.py, contracts.py, outputs.py | The 14 MCP tools of contract 6.6 rev. 2 (incl. `market_slice`, `instrument`) and their schemas |
| src/mcp_gateway/gateway.py | Tool dispatch: session, time travel, budgets, risk, broker, trials, audit |
| src/mcp_gateway/brokers/base.py, router.py | `BrokerAdapter` protocol, instrument rules, HTTP 6.4 adapter; per-session adapter choice |
| src/mcp_gateway/brokers/mirror.py, commission.py | Mirror sandbox (matching, accounts, state file); T-Invest commission model |
| src/mcp_gateway/brokers/tbank_sandbox.py | T-Bank sandbox adapter (kept, disabled by default) |
| src/mcp_gateway/sessions.py, admin.py, admin_models.py | Sessions, modes, clock, admin API |
| src/mcp_gateway/timetravel.py, budgets.py, risk.py, refusals.py | Guards |
| src/mcp_gateway/audit.py | Append-only, hash-chained JSONL audit log and its verifier |
| src/mcp_gateway/clients/{base,pit,research,ledger,orders}.py, downstream.py | Typed clients for I2, S1, I5, S6, S7, I6, I7 |
| src/mcp_gateway/fakes/{services,world}.py, harness.py | In-process fakes and the test harness |
| src/mcp_gateway/bench/, demo.py | `make report` experiments; `make reproduce` |
| tests/unit, tests/integration, tests/real | Unit tests; in-process tool tests; opt-in real-service test |

## State at handover (2026-11-05)

- `make lint typecheck test`: clean; 107 default tests pass in about 13 s, 97% coverage.
- `make test-integration` (marker `integration`, deselected by default) passed on 2026-11-05 in about 8 s. It starts
  `point-in-time-store/.venv/bin/pit serve`, `research-ledger/.venv/bin/ledger serve` and
  `backtest-orchestrator/.venv/bin/bt serve` (env `BT_PIT__MODE=http`, `BT_PIT__BARS_SOURCE=trades`,
  `BT_MARKET__VENUE=moex-tbank`, `BT_LEDGER__MODE=http`) on free ports, with scratch data under
  `tmp/integration-<id>/` (removed on success). It seeds a MOEX tape, runs a research session (data, a look-ahead
  refusal, an I5 backtest, a refused peek, a verdict with n_trials 2 and sources `{i5: 1, i9: 1}`) and a paper
  session (mirror fill at the best ask stored in the real I2, `market_slice` from the real I2). Ports 8601-8610
  should be free first; no process stays running afterwards.
- Local-only trading: `brokers.tbank_sandbox.enabled: false` (default in code and YAML), `tbank-sandbox` is not in
  `brokers.allowed_paper`. While disabled the adapter is not built; `POST /admin/v1/sessions` with that broker
  returns 403 `broker_disabled`; a stored session that names it is refused with `mode_forbidden`; a
  `brokers.paper`/`brokers.live` default naming it raises `BrokerDisabled` at start. `.env` has no `TBANK_TOKEN`.
- Commission: `brokers.mirror.commission` (`rate` 0.0025, `zero_fee_rate` 0, `rounding` 0.01 RUB half-up in
  Decimal, `zero_fee_instruments`, `zero_fee_reference_file`, `zero_fee_reference_key: funds`). Applied per mirror
  fill, both sides, taker and maker alike. The zero-fee set is the config list (tickers, symbols, FIGIs, uids)
  plus the `funds` of I1's `$TRADEQ_DATA_DIR/market-tape-recorder/reference/tbank_zero_fee_funds.json`, re-read on
  mtime change; missing file or missing key logs a warning and falls back to the config list. The old
  `taker_fee_bps`/`maker_fee_bps` keys were removed.
- Fee flow (optional fields only, backward compatible): mirror `Order` carries `fee` (total) and new `fills`
  (`ts_fill`, `price`, `qty`, `fee`, `liquidity`); mirror `Account` carries `fees` and new `pnl` (equity minus
  opening cash, net of fees); cash is net of fees, so equity and the drawdown budget are too; order-intent trials
  to S6 add `filled_qty`, `avg_price`, `fee` to `metrics` (values at placement; a resting order's later fills are
  not re-logged). I6 and I7 compute their own fees; the fake I6 charges none.
- `make report` was not rerun since the MOEX pivot: README and `docs/report.md` numbers are pre-MOEX (crypto-era
  fakes, I6 fake). `make reproduce` shows a mirror fill with the new fee.

## Open items

- I1's zero-fee reference file does not exist yet; the shipped `zero_fee_instruments` tickers (TMOS, TRUR, TGLD,
  TBRU, TMON, TPAY, TDIV, TITR, TOFZ, TLCB) are from memory and unverified. No fund is in `risk.whitelist` or
  `brokers.instruments`, so zero-fee fills are only unit-tested; add fund instruments (board TQTF) to trade them.
- Rerun `make report` on the MOEX fakes; a mirror mode that reads the live I1 stream directly; an I7 adapter.
- Run S8's `make integration` against this gateway: fees changed from 4 bps to 25 bps, which moves S8's PnL.

## Mistakes and lessons

1. Build clients from the producer's `models.py`/`api.py`, not guesses, and keep an opt-in real-service test: the
   first I2 client would have got 422 on every query.
2. Ledger strategy ids must not contain `/` (S6's verdict route is `{strategy_id}`); the fakes hid this.
3. Test every budget with a moving session clock: the order-rate window once ran on the wall clock.
4. Verify prices and tariffs against a current source and record the date and source in config.
5. Session locks are per process; run one gateway process per session store.
6. Changing an I9 default (broker, fees) changes what every consumer sees; run the consumers' integration tests
   (S8 `make integration`) afterwards. Old tests that inspect a specific fake should name their broker.
7. Compound shell commands (`cd && ...`) may be denied in some environments; `git -C` and `make -C` always work.

## Report rerun on the MOEX fakes (2026-11-06)

`make report` first crashed with `KeyError: 'YDEX-RUB-TQBR'`: the adversary probes a non-whitelisted symbol and
asks the fake world for its price. `fakes/world.py` now prices unlisted symbols from `UNLISTED_BASE_PRICE`
(100.0), so the gateway, not the fake, refuses them. Results: guarded 1,181/1,181 violations blocked, 0/2,730
false refusals; pass-through baseline 42/1,225 blocked (mirror-broker rejections) and 11,620 leaked rows.
README updated; docs/report.md still has the pre-MOEX numbers.

The real-service integration test failed after I5 moved to MOEX sessions: its world sat on Saturday night,
2025-05-31 22:00Z to 2025-06-01 00:00Z, when MOEX is closed, so I5 had no bars. It now uses Tuesday
2025-06-03 10:00-12:00Z, inside the main session, and passes again. Lesson: a test world on MOEX must sit
inside a trading session.
