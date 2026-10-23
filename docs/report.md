# Risk-gated MCP Gateway: research report

The present study belongs to the present project, which is project I9 of the Tradeq platform. All numbers below come from one run of make report on 2026-10-23, after the fake I2 and the fake I6 were aligned with the shipped services and the session clock, and that run wrote them to $TRADEQ_DATA_DIR/risk-gated-mcp-gateway/reports/metrics.json and tables.md.

## Question

The present project gives AI agents one Model Context Protocol (MCP) server, which is the open protocol that lets a large language model (LLM) client call tools on a server, and the study asks whether its guards are worth their cost. The study answers three questions, because each one decides a design choice. The first question is which transport the gateway should serve, stdio (the server runs as a child process and talks through its standard input and output) or streamable HTTP (the server listens on a port and answers HTTP requests), and what each one adds to a tool call. The second question is how often the gateway blocks look-ahead attempts and budget breaches from scripted adversarial agents, compared with an unguarded pass-through wrapper of the kind that laukikk/alpaca-mcp represents. The third question is what the documented Alpaca MCP server offers, because that analog cannot be run against the platform contracts.

## Data

All experiments run on synthetic data, so the results describe the behaviour of the software and are not market evidence. The in-process fakes of the downstream services, which live in src/mcp_gateway/fakes, generate a price for each symbol and each one-minute bar i as p0 times exp(a sin(2 pi i / P) + s z), where p0 is the base price, a is 0.02, P is 720 bars, s is 0.001 and z is a standard normal draw that a generator seeded with the symbol and the bar index produces. Each bar becomes knowable one second after its valid time, which gives the fake point-in-time store (I2) a knowledge time for every row. The fake shadow exchange (I6) fills market orders at the last synthetic price that is known at the order's session clock, and it keeps cash and positions per account, which starts with 100,000 USDT of cash. The fake research ledger (S6) stores every trial that it receives, and the fake leakage probe (S7) flags only the model id leaky-llm. All parameters sit in the fakes and demo sections of config/settings.yaml, and the report seed is 20261021.

## Methods

The latency experiment, which bench/latency.py implements, calls two tools 300 times per run after 20 warm-up calls, and it repeats the run three times with a fresh gateway, so each row of Table 1 holds 900 samples. The clock tool reads only the session, while the market_data tool also queries the fake I2 for one hour of trades. The experiment calls the gateway through five paths, which are a direct Python call to the gateway core, an in-process MCP client, an MCP client that talks to a stdio child process, an MCP client that talks streamable HTTP to a uvicorn server on the loopback interface, and a direct call to the pass-through baseline. We report the median of each sample with a percentile bootstrap interval of 2,000 resamples (Efron and Tibshirani 1993, chapter 13), and we report the overhead of each MCP path as the difference of its median from the direct median, with a bootstrap interval of that difference.

The pass-through baseline, which bench/baseline.py implements, reimplements the design that the specification describes for laukikk/alpaca-mcp, which is a lightweight wrapper with no risk management or validation. The baseline forwards each data query to I2 in live mode with a knowledge time equal to the wall clock, it forwards each backtest window as the agent sent it, and it sends each order straight to the shadow exchange. The guarded gateway, which is the present project, instead pins research sessions to the session clock, checks every order against the whitelist, the fat-finger price band of 200 basis points, the notional and position limits and the per-agent budgets, and it freezes a session when a token, spend, latency or drawdown budget is breached.

The adversarial experiment, which bench/adversary.py implements, runs 200 episodes against each system, and each episode draws its attack parameters from the same seeded generator for both systems. Each episode makes one attempt in each of six classes, which are a data query whose end lies between one day before and one day after the session clock, a backtest whose end lies between five days before and five days after it, a limit order whose distance from the reference price lies between 0 and 600 basis points, a market order whose notional lies between 0.5 and 2 times the order cap of 10,000 USDT, an order that is equally likely to use a whitelisted or a non-whitelisted instrument, and a burst of 10 to 15 orders inside one 60-second rate window. Each episode also opens two fresh sessions for budget attacks, because a breach freezes the session it happens in. One session reports between 0.5 and 1.5 times the token budget of 2,000,000 tokens and then asks for data, while the other builds a position of 45,000 USDT, applies a mark-price shock between 0.70 and 0.98 and then places another order. We label each attempt as a violation or a benign control from the configured thresholds, and we never use the gateway's decision for the label. For each class we count the blocked violations and the refused benign controls, and we give each rate with its Wilson score interval at 95% confidence (Wilson 1927).

## Experiments and results

The streamable HTTP transport adds about 0.75 ms more than stdio to a market_data call, but both overheads stay below 3.5 ms, so the transport choice does not limit an agent that thinks for seconds per decision. As Table 1 shows, the direct call takes a median of 2.702 ms with a 95% interval from 2.679 to 2.741 ms, the in-process MCP client adds 1.524 ms (1.451 to 1.611), stdio adds 2.578 ms (2.465 to 2.714), and streamable HTTP adds 3.326 ms (3.200 to 3.488). The per-run medians of streamable HTTP range from 4.98 to 7.38 ms, which is a spread of 2.40 ms, so the gap of 0.75 ms between the two transports is smaller than the variation between runs on this laptop.

Table 1 lists the per-call latency in milliseconds, which we measured over 900 calls per row in three runs of 300.

| Transport | Tool | n | Median | 95% CI of median | p95 | Per-run medians | Overhead vs direct (95% CI) |
|---|---|---|---|---|---|---|---|
| baseline_direct | market_data | 900 | 1.799 | 1.774 to 1.826 | 2.840 | 2.16, 1.78, 1.56 | -0.903 (-0.952 to -0.865) |
| direct | clock | 900 | 0.496 | 0.490 to 0.501 | 0.731 | 0.56, 0.47, 0.47 | |
| direct | market_data | 900 | 2.702 | 2.679 to 2.741 | 4.613 | 3.48, 2.61, 2.54 | |
| mcp_in_process | clock | 900 | 0.697 | 0.687 to 0.708 | 1.437 | 1.02, 0.65, 0.68 | |
| mcp_in_process | market_data | 900 | 4.226 | 4.163 to 4.308 | 8.270 | 5.00, 3.50, 4.13 | 1.524 (1.451 to 1.611) |
| mcp_stdio | clock | 900 | 1.633 | 1.613 to 1.664 | 2.608 | 1.89, 1.43, 1.66 | |
| mcp_stdio | market_data | 900 | 5.281 | 5.175 to 5.416 | 8.486 | 6.44, 4.41, 5.37 | 2.578 (2.465 to 2.714) |
| mcp_streamable_http | clock | 900 | 2.156 | 2.116 to 2.207 | 4.600 | 2.90, 1.72, 2.22 | |
| mcp_streamable_http | market_data | 900 | 6.028 | 5.908 to 6.185 | 10.755 | 6.31, 4.98, 7.38 | 3.326 (3.200 to 3.488) |

The guards cost about 0.9 ms on a data call and about 4 ms on an order, because the guarded gateway makes extra downstream calls and writes an audit entry with a file sync. As Table 1 shows, the pass-through baseline answers market_data in a median of 1.799 ms, while the guarded gateway needs 2.702 ms, which is a difference of 0.903 ms (0.865 to 0.952) that comes from the session lookup, the window check, the sort by valid time, the trial counters and the audit write. As Table 2 shows, the guarded place_order takes a median of 4.305 ms against 0.267 ms for the baseline, which is a difference of 4.038 ms (4.003 to 4.090), because each guarded order also asks I2 for the reference price, asks I6 for positions and equity, and logs the order intent as a trial in S6.

Table 2 lists the place_order latency in milliseconds, which we measured with direct calls against the same fake I6.

| Arm | n | Median | 95% CI of median | p95 |
|---|---|---|---|---|
| guarded | 900 | 4.305 | 4.268 to 4.362 | 7.246 |
| baseline | 900 | 0.267 | 0.265 to 0.269 | 0.461 |

The guarded gateway blocked all 1,225 violations and refused none of the 2,686 benign controls, while the pass-through baseline blocked none of the violations. As Table 3 shows, the pooled block rate of the guarded gateway is 100% with a Wilson lower bound of 99.7%, and its pooled false-refusal rate is 0% with a Wilson upper bound of 0.14%. The smallest class is the drawdown attack with 64 violations, so its lower bound of 94.3% is the weakest per-class guarantee in the table. The baseline returned 11,620 rows from after the session clock in the 100 look-ahead data attempts, which is 116.2 rows per attempt, while the guarded gateway returned none. The guarded gateway froze 163 sessions, which equals the 99 token overspends plus the 64 drawdown breaches, so every budget breach that is configured to freeze did freeze its session.

Table 3 lists the block rate on violations and the false-refusal rate on benign controls, which we computed per attack class over 200 episodes.

| Arm | Attack | Violations | Blocked | Block rate % (95% CI) | Benign | False refusals % (95% CI) | Leaked rows |
|---|---|---|---|---|---|---|---|
| guarded | lookahead_data | 100 | 100 | 100.0 (96.3 to 100.0) | 100 | 0.0 (0.0 to 3.7) | 0 |
| guarded | lookahead_backtest | 107 | 107 | 100.0 (96.5 to 100.0) | 93 | 0.0 (0.0 to 4.0) | 0 |
| guarded | fat_finger | 122 | 122 | 100.0 (96.9 to 100.0) | 78 | 0.0 (0.0 to 4.7) | 0 |
| guarded | oversize | 123 | 123 | 100.0 (97.0 to 100.0) | 77 | 0.0 (0.0 to 4.8) | 0 |
| guarded | non_whitelisted | 99 | 99 | 100.0 (96.3 to 100.0) | 101 | 0.0 (0.0 to 3.7) | 0 |
| guarded | order_burst | 511 | 511 | 100.0 (99.3 to 100.0) | 2000 | 0.0 (0.0 to 0.2) | 0 |
| guarded | token_overspend | 99 | 99 | 100.0 (96.3 to 100.0) | 101 | 0.0 (0.0 to 3.7) | 0 |
| guarded | drawdown | 64 | 64 | 100.0 (94.3 to 100.0) | 136 | 0.0 (0.0 to 2.7) | 0 |
| baseline | lookahead_data | 100 | 0 | 0.0 (0.0 to 3.7) | 100 | 0.0 (0.0 to 3.7) | 11620 |
| baseline | lookahead_backtest | 107 | 0 | 0.0 (0.0 to 3.5) | 93 | 0.0 (0.0 to 4.0) | 0 |
| baseline | fat_finger | 122 | 0 | 0.0 (0.0 to 3.1) | 78 | 0.0 (0.0 to 4.7) | 0 |
| baseline | oversize | 123 | 0 | 0.0 (0.0 to 3.0) | 77 | 0.0 (0.0 to 4.8) | 0 |
| baseline | non_whitelisted | 99 | 0 | 0.0 (0.0 to 3.7) | 101 | 0.0 (0.0 to 3.7) | 0 |
| baseline | order_burst | 511 | 0 | 0.0 (0.0 to 0.7) | 2000 | 0.0 (0.0 to 0.2) | 0 |
| baseline | token_overspend | 99 | 0 | 0.0 (0.0 to 3.7) | 101 | 0.0 (0.0 to 3.7) | 0 |
| baseline | drawdown | 64 | 0 | 0.0 (0.0 to 5.7) | 136 | 0.0 (0.0 to 2.7) | 0 |

The difference in block rates between the two systems is not random in any attack class. We tested the null hypothesis that both systems block violations at the same rate with Fisher's exact test for a 2 by 2 table (Fisher 1922), and as Table 4 shows, the largest two-sided p-value is 8.35e-38 for the drawdown class, which is far below the 0.05 level, while the pooled p-value underflows below 1e-300. The blocking decisions themselves are deterministic rules over configured thresholds, so these results show that the implementation follows its rules across the sampled parameter range, and they do not estimate the accuracy of a learned classifier.

Table 4 lists Fisher's exact test, which compares the blocked counts of the two systems on violations.

| Attack | Guarded blocked / n | Baseline blocked / n | Two-sided p |
|---|---|---|---|
| lookahead_data | 100 / 100 | 0 / 100 | 2.21e-59 |
| lookahead_backtest | 107 / 107 | 0 / 107 | 1.39e-63 |
| fat_finger | 122 / 122 | 0 / 122 | 1.39e-72 |
| oversize | 123 / 123 | 0 / 123 | 3.48e-73 |
| non_whitelisted | 99 / 99 | 0 / 99 | 8.79e-59 |
| order_burst | 511 / 511 | 0 / 511 | 1.78e-306 |
| token_overspend | 99 / 99 | 0 / 99 | 8.79e-59 |
| drawdown | 64 / 64 | 0 / 64 | 8.35e-38 |
| all | 1225 / 1225 | 0 / 1225 | < 1e-300 |

One parameter changed during the study, because a single run did not separate the two transports. The number of latency runs, which is demo.report_latency_repeats, went from 1 to 3 after two single runs of 300 calls ranked the transports in opposite orders, since the first run measured a market_data overhead of 6.27 ms for streamable HTTP and 5.30 ms for stdio, while the second run measured 4.67 ms and 5.98 ms. The pooled three-run result in Table 1 therefore replaces both single runs, and the per-run medians column shows how far the runs still differ. The fakes also changed before the final run, because the shipped I2 answers JSON with ISO-8601 timestamps and orders rows by key columns, and because the fake I6 now fills at the last price known at the session clock. Those changes moved the latency numbers, but every count in Tables 3 and 4 stayed the same.

## Comparison with analogs

The Alpaca MCP server covers more markets than the present project, but its public documentation describes no enforced limits, budgets or point-in-time data, so the present project fills the validity gap that the specification names. The Alpaca announcement (Alpaca 2025), section "One Interface for Many Markets", states that the server covers US equities, options and crypto, together with market data, order entry, change and cancel, and portfolio details, so we can conclude that its tool surface is wider than the twelve tools of contract 6.6. The same announcement, section "Important Considerations", states that users should review and confirm orders on their Alpaca dashboard and should link their keys to the right live or paper account, so we can conclude that the documented risk control is human review rather than a rule that the server enforces. The Alpaca agentic page (Alpaca 2026), section FAQ, states that paper trading uses 100,000 USD of simulated funds with real market data and that a switch to live trading needs only new API keys, so we can conclude that credentials choose the mode, while the present project opens live routing only when the S6 verdict passes and the S7 leakage report is clean. Neither Alpaca page documents per-agent token or latency budgets, rate limits or a session clock, and a direct measurement against Alpaca was not possible, because the server needs a broker account and does not speak the platform contracts.

The pass-through baseline stands for laukikk/alpaca-mcp, which the specification describes as a lightweight Alpaca wrapper without risk management or validation, and Table 3 shows the cost of that design, which is that every violation in every class reached the downstream service. The other analogs in the specification, CPZAI MCP and OpenQuant, could not be run either, because the specification lists CPZAI as a commercial backend and OpenQuant as a proof of concept.

## Decision

The present project serves both transports and makes streamable HTTP the default for the Agent Arena (S8), because the difference between them is about 0.75 ms and HTTP lets one server hold many sessions with per-session keys. As Table 1 shows, both transports stay under 3.5 ms of overhead on a data call, which is small against the latency of an LLM decision or an I5 backtest. Stdio stays available for desktop clients that start one server per agent. The guards stay on every call, because they cost 0.903 ms per data call and 4.038 ms per order, as Tables 1 and 2 show, while Table 3 shows that without them all 1,225 violations reach the services and 11,620 future rows reach the agents.

## Limitations

The results rest on synthetic fakes that run in the same process, so the latency numbers measure the gateway and the protocol stack, and they exclude network time to real I2, I5 and I6 services. The laptop also ran other development agents during the study, which explains part of the spread between runs that Table 1 shows. A separate opt-in test, which make test-integration runs, drives the gateway against the shipped I2 and S6 services, while the experiments in this report still use the fakes. The adversarial agents are scripted, which means that they test the stated rules at sampled parameters, but they do not search for rules that are missing. The trial records that the gateway sends to S6 carry no return series, because I5 keeps the returns, so S6 must join the gateway trial with the I5 trial through params.linked_trial_id. The I7 execution engine does not exist yet, so we tested live routing only against a second fake that speaks the Order API.

## References

Alpaca (2025). Introducing the official MCP server, enabling multi-market trading with AI interfaces. https://alpaca.markets/blog/introducing-official-mcp-server-enabling-multi-market-trading-with-ai-interfaces/

Alpaca (2026). Agentic trading. https://alpaca.markets/agentic

Efron, B., and Tibshirani, R. J. (1993). An Introduction to the Bootstrap. Chapman and Hall.

Enterprise DNA. laukikk/alpaca-mcp directory entry. https://enterprisedna.co/directories/mcp/laukikk-alpaca-mcp

Fisher, R. A. (1922). On the interpretation of chi-square from contingency tables, and the calculation of P. Journal of the Royal Statistical Society 85(1), 87-94.

Tradeq platform plan, project I9, quant_platform_plan_full.md.

Wilson, E. B. (1927). Probable inference, the law of succession, and statistical inference. Journal of the American Statistical Association 22(158), 209-212.
