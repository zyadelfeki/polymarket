# Roadmap — Next-Level Upgrades

**Status as of 2026-02-21.**  
Baseline is stable: OrderStore removed, all tracking in AsyncLedger, sweep
runs cleanly, multi-asset support added.  Real `arbitrage_opportunity_detected`
events now flow once `paper_exploration.yaml` is used.

---

## Design review — weaknesses and concrete fixes

### 1. Data & signals

| Weakness | Concrete fix (builds on current code) |
|----------|---------------------------------------|
| Single edge model: `abs(true_prob – market_price)`. No feature enrichment. | Add `signals/edge_enricher.py` that computes additional features per market (implied vol, cross-market correlation, book imbalance) and includes them in the log as `edge_features`. No model change yet — just better observability. |
| Neutrality cut-off is a fixed 0.02% BTC movement. | Replace with rolling volatility-normalised threshold: `0.02% × (σ_30min / σ_baseline)`. Reuse `price_history` already wired into the strategy. |
| Fees are a synthetic 3% peak model. | Fetch actual Polymarket fee tier from the API or hard-code the documented 2% taker fee. This alone improves net-edge accuracy by ~1%. |
| No treatment of funding or carry costs. | For multi-day markets, add a simple linear time-decay penalty to the edge so far-expiry markets are not over-rewarded. |

---

### 2. Execution & microstructure

| Weakness | Concrete fix |
|----------|--------------|
| No slippage attribution. Replay uses a static `slippage_bps` constant. | Add `execution_analytics` table in `AsyncLedger`: log expected price vs fill price, time-to-fill, and queue-position estimate. Feed realized slippage back into `calculate_dynamic_fee` as a rolling estimate. |
| Partial fills not explicitly handled — order assumed fully filled. | `ExecutionServiceV2` already tracks fill status. Expand `order_settled` logging to include `fill_pct` and `slippage_bps_realized` so replay can compute realistic costs. |
| No cancellation or timeout logic surface in PnL. | Log `order_cancelled` and `order_expired` events with reason codes. Replay engine should account for these as zero-PnL closed positions. |

---

### 3. Risk & capital allocation

| Weakness | Concrete fix |
|----------|--------------|
| Each market sized independently; correlated positions ignored. | Add `services/risk_budget.py`: tag markets by theme (`crypto`, `politics`, `sports`). Enforce per-category exposure caps read from a new `risk_budget.yaml`. Wire into `_execute_opportunity` before order placement. |
| Kelly fraction and `min_edge_required` are static. | See **bank-grade feature A** below. |
| No regime awareness; Kelly doesn't adapt to drawdown. | `REGIME_RISK_OVERRIDES` exists but isn't wired into `KellySizer`. Connect it: when `PerformanceTracker.rolling_drawdown > threshold`, multiply `fractional_kelly` by the regime override factor. |
| `STARTING_CAPITAL` hardcoded to $13.98 in `config_production.py`. | Replace with `await ledger.get_equity()` at startup (already done for `PortfolioState`, but `KellySizer` still receives `get_real_balance()` from execution service — verify both paths use the same number). |

---

### 4. Monitoring & observability

| Weakness | Concrete fix |
|----------|--------------|
| No per-asset or per-timeframe PnL breakdown. | Add `reports/pnl_attribution.py`: query `AsyncLedger.order_tracking` and group by `market_id`, `asset`, `timeframe`. Write a CLI: `python reports/pnl_attribution.py --since 7d`. |
| `edge_candidate_computed` exists but sweep doesn't read it. | Update `experiments/sweep_kelly_and_edge.py` to optionally replay `edge_candidate_computed` events in addition to `order_settled`. This lets you estimate "how many trades would I have done" at various thresholds without waiting for real fills. |
| No per-model accuracy surfaced in logs. | `PerformanceTracker` stores per-model accuracy internally. Emit a `model_accuracy_snapshot` log event every N minutes so it appears in JSON logs and can be swept. |

---

### 5. Reliability & infra

| Weakness | Concrete fix |
|----------|--------------|
| Strategy scan re-runs on every ETH/SOL/XRP tick — up to 4× load increase after multi-asset fix. | Add per-symbol cooldown in `_run_strategy_scan`: track `last_scan_at` per symbol, not globally. |
| `_market_cache_ttl_seconds=10` means up to 10-second-stale market data. | This is fine for latency-arb at 15-min granularity. Document explicitly in the YAML so reviewers don't mistake it for a bug. |
| Database WAL not compacted during long runs. | Existing `vacuum_interval_hours=24` handles this; verify it fires correctly in the production event loop. |

---

### 6. Research tooling

| Weakness | Concrete fix |
|----------|--------------|
| 16-combo Kelly sweep is a fixed grid. | Extend `experiments/sweep_kelly_and_edge.py` with a `--rolling-window-days` argument: slice the log into non-overlapping windows and run the sweep on each, then plot how optimal params drift over time. |
| No scenario / stress test. | See **bank-grade feature D** below. |
| Sweep outputs only CSV. | Add `--save-metadata` flag that writes a JSON sidecar with log window, git commit hash, and the winning config so sweeps are reproducible. |

---

## Incremental PR-sized tasks

### PR-01: Fix per-symbol scan cooldown after multi-asset change
**Acceptance criteria:**
- `TradingSystem.last_strategy_scan_at` is replaced with a `Dict[str, float]` keyed by symbol.
- A new scan does NOT fire if the same symbol triggered one less than `strategy_scan_min_interval_seconds` ago.
- A BTC tick does NOT suppress an ETH scan if ETH's cooldown has expired.
- Unit test in `tests/test_trading_system.py` covers the multi-symbol case.

### PR-02: Realized slippage column in AsyncLedger
**Acceptance criteria:**
- `order_tracking` table gains a `slippage_bps_realized` nullable column (schema migration in `database/schema.sql`).
- `ExecutionServiceV2.settle_order` computes `(fill_price – expected_price) / expected_price * 10000` and writes it.
- `replay/engine.py` reads `slippage_bps_realized` when available; falls back to configured `slippage_bps`.
- Tests updated.

### PR-03: Per-category risk budget
**Acceptance criteria:**
- New file `config/risk_budget.yaml` with `max_exposure_pct` per category (`crypto`, `politics`, `sports`, `other`).
- New `services/risk_budget.py` module with a `RiskBudget.check(market_id, side, size) -> (allowed: bool, reason: str)` method.
- Wire into `TradingSystem._execute_opportunity` before order placement.
- `opportunity_skipped` event gains a `reason=risk_budget_exceeded` variant.

### PR-04: Rolling Kelly sweep
**Acceptance criteria:**
- `experiments/sweep_kelly_and_edge.py` gains `--rolling-window-days N` flag.
- When set, it slices logs into N-day windows and runs the grid on each, appending window start/end to CSV output.
- A separate `results/rolling_sweep_plot.py` script generates a line chart of optimal `fractional_kelly` and `min_edge_required` over time.

### PR-05: `edge_candidate_computed` in sweep replay
**Acceptance criteria:**
- `replay/engine.py` can optionally process `edge_candidate_computed` events as "would-have-traded" entries with zero fill cost.
- New CLI flag: `python experiments/sweep_kelly_and_edge.py --include-candidates`.
- Produces a second table: "how many trades would each config have taken?" alongside the existing PnL table.

---

## Bank-grade features (3–5 week scope each)

### A. Regime-adaptive Kelly

**What it does:** Every night, run the Kelly sweep on the last 30/60/90-day
rolling window.  Fit a piecewise mapping from regime indicators (realized
hit-rate, rolling drawdown, realized vol) to `fractional_kelly` and
`min_edge_required`.  Write the result to a dated YAML snapshot; `main.py`
loads the latest snapshot at startup.

**Why it's valuable:** In low-hit-rate or high-vol regimes, Kelly should be
cut aggressively; in stable, high-hit-rate periods it can be raised.  Static
parameters leave money on the table in good regimes and over-risk in bad ones.

**Reuse:** `experiments/sweep_kelly_and_edge.py`, `replay/engine.py`,
existing JSON logs, `config_production.py` plumbing.

**Implementation plan:**
1. Add `--rolling-window-days` to sweep (PR-04 above).
2. Add a `scripts/nightly_kelly_fit.py` script that runs the rolling sweep and writes `config/kelly_config_snapshot_{YYYY-MM-DD}.yaml`.
3. Add regime indicator computation: rolling hit-rate, 30-day realized vol, drawdown from peak.
4. Fit a simple piecewise lookup table: `regime_indicators → (fractional_kelly, min_edge)`.
5. Modify `config_production.py` to load the latest snapshot at import time; fall back to hard-coded defaults if no snapshot exists.
6. Add a `CRON`/`schedule` trigger (or a GitHub Actions nightly job) to run `nightly_kelly_fit.py`.

---

### B. Full PnL attribution dashboard

**What it does:** A CLI (and optional HTML report) that queries `AsyncLedger`
and breaks down PnL by asset, timeframe, trade direction, and signal family.
Shows contribution from edge vs execution vs slippage.

**Why it's valuable:** This is how desks debug systematically — you see that
ETH hourly markets make money but ETH daily markets lose it, so you cut the
losers and size up the winners.

**Reuse:** `AsyncLedger.order_tracking`, `performance_tracker.py`,
`replay/engine.py` PnL calculations.

**Implementation plan:**
1. Standardise `order_tracking` columns: ensure `asset`, `timeframe`, `edge` at entry, `slippage_bps_realized` are all populated (PR-02 prerequisite).
2. Create `reports/pnl_attribution.py` with functions: `by_asset()`, `by_timeframe()`, `by_direction()`, `edge_vs_execution_split()`.
3. Add a `reports/pnl_attribution_cli.py` CLI accepting `--since`, `--asset`, `--format table|csv|json`.
4. (Optional) Generate an HTML report using Jinja2 + simple Chart.js charts.

---

### C. Portfolio-aware sizing with cross-market correlation

**What it does:** Before placing an order, check existing open positions for
correlated markets (same asset, same event, same expiry window).  Apply a
correlation penalty: treat markets on the same event as 100% correlated for
exposure purposes.

**Why it's valuable:** Without this, the bot can double-up on the same
directional bet across two ETH 15-min markets that expire 15 minutes apart —
equivalent to betting 2× with no diversification benefit.

**Reuse:** `services/portfolio_state.py`, `AsyncLedger`, `services/risk_budget.py` (PR-03).

**Implementation plan:**
1. Add `market_group_id` tag to `order_tracking`: markets on the same event share a group_id (use `event_id` from Polymarket API response, already in normalized market dict).
2. In `PortfolioState.refresh()`, compute `exposure_by_group` in addition to `exposure_by_market`.
3. Add `group_exposure_cap_pct` to `risk_budget.yaml`.
4. Wire the group-cap check into `RiskBudget.check()`.
5. Add `kelly_correlation_discount(n_same_event_positions) → float` to `KellySizer`: each additional correlated position reduces the Kelly fraction by a configurable factor.

---

### D. Stress testing / scenario engine

**What it does:** Uses the existing replay infrastructure to simulate adverse
scenarios (sudden 20% implied-probability shock, 90% spread widening, liquidity
dry-up) across the current open book.  Reports worst-case PnL and VaR-style
metrics before deploying a new config.

**Why it's valuable:** Lets you quantify "what happens if the market moves 30%
against all open positions simultaneously?" before you run live.  This is
standard pre-deployment risk management at any serious shop.

**Reuse:** `replay/engine.py`, `AsyncLedger`, `experiments/sweep_kelly_and_edge.py` infrastructure.

**Implementation plan:**
1. Add `replay/scenario.py`: a `ScenarioEngine` that takes a list of open positions + a shock spec (`{asset: delta_prob, spread_multiplier, liquidity_multiplier}`) and applies them to the replay state.
2. Implement PnL calculation under each scenario using the same logic as the live replay engine.
3. Add CLI: `python scripts/stress_test.py --scenario scenarios/shock_50pct.yaml` that reads open positions from `AsyncLedger` and outputs a scenario PnL table.
4. Write 2-3 standard shock scenarios as YAML files (moderate, severe, extreme).
5. Integrate into `nightly_kelly_fit.py`: if any scenario produces drawdown > 20%, reduce `fractional_kelly` by an additional margin before writing the snapshot.
