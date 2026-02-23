# Edge Diagnostics — Root Cause Analysis of "0 Opportunities"

**Date diagnosed:** 2026-02-21  
**Baseline commit:** `main` branch (OrderStore removed; all tracking via AsyncLedger)

---

## UPDATE 2026-02-21 — Replay engine trades=0 root cause (1933 opportunities, 0 trades)

### Root cause: `min_position_size: None` triggers `InvalidOperation` on every event (BUG 1)

`CHARLIE_CONFIG["min_position_size"]` is **present in the config dict but set to `None`**.

The old guard in `replay/engine.py _handle_opportunity`:

```python
min_pos = Decimal(str(CHARLIE_CONFIG.get("min_position_size", Decimal("1.00"))))
```

`dict.get(key, default)` returns the default only when the key is **absent**, not when it
exists with a `None` value.  So this called `Decimal(str(None))` → `Decimal("None")` →
`decimal.InvalidOperation`.  The exception propagated up to the outer `_process_events`
try/except, which logged it at DEBUG level and silently dropped **every** event.

Even if `None` were safe, the arithmetic would still block every trade at this capital level:

| Parameter | Value |
|-----------|-------|
| `STARTING_CAPITAL` | $13.98 |
| `KELLY_CONFIG["max_bet_pct"]` | 5.0% |
| `KELLY_CONFIG["fractional_kelly"]` | 0.25 |
| `CHARLIE_CONFIG["min_position_size"]` | `None` ← root cause |
| Actual order_value range | ~$0.03–$0.07 |
| Hard floor (default $1.00) | **$1.00 > $0.07 → filtered** |

**Fix applied** (`replay/engine.py`, `_handle_opportunity`):
- Use `or "1.00"` to safely handle `None`
- Scale floor to `min(configured_min, 0.5% × equity)` so replay is never blocked
  by an absolute dollar floor calibrated for a larger live account
- Hard floor of `$0.01` preserved to keep sizing sane

### BUG 2 — Field name mismatch: NOT present in this log

Sampled one real `arbitrage_opportunity_detected` from `logs/production.log`:

```json
{
  "market_id": "1399606",
  "side": "NO",
  "market_price": "0.35",
  "edge": "0.1190",
  "event": "arbitrage_opportunity_detected",
  "timestamp": "2026-02-20T22:16:23.450691Z"
}
```

Fields `market_price` and `edge` match exactly what `_handle_opportunity` reads.
No `charlie_p_win`, no `token_id`, no `technical_regime` — the engine handles all
three absent fields correctly via fallbacks.

### BUG 3 — Settlement event name: forward-compat fix applied

Production bot logs `order_settled_live` (already handled) and
`ledger_order_settled_offline` (already handled).  **No `order_settled` events
exist in `logs/production.log`** — no markets have resolved yet.

The bare `"order_settled"` was added to the tuple as a forward-compatibility guard.
The `pnl` field name in `_handle_settlement` is correct (matches `main.py:1500`).

### Changes made to `replay/engine.py`

| Location | Change |
|----------|--------|
| `_handle_opportunity` line ~689 | None-safe `or "1.00"` + capital-scaled `replay_min_pos` |
| `_process_events` line ~600 | Added `"order_settled"` to settlement event tuple |

**No production configs, YAML files, or live-trading code were modified.**

---

---

## Executive summary

The live bot was completing scans but never emitting `arbitrage_opportunity_detected`.
Three independent causes stacked:

| # | Cause | Severity | Fix |
|---|-------|----------|-----|
| 1 | **No active BTC 15-min markets** on Polymarket | **Blocking** | Extend strategy to ETH/SOL/XRP |
| 2 | `_min_required_net_edge` **hardcoded 6% floor** for mid-market prices | **Blocking** (if BTC existed) | Lower floor to `base_min_edge + 1%` |
| 3 | `_on_price_update` **ignores ETH/SOL/XRP ticks** | Aggravating | Allow all four symbols to trigger scans |

---

## Cause 1 — Missing BTC 15-min markets (blocking)

### Evidence

From `logs/production.log`:
```
{"asset": "ETH", "slug": "eth-updown-15m-1771624800", ...  "event": "15min_market_found"}
{"asset": "SOL", "slug": "sol-updown-15m-1771624800", ...  "event": "15min_market_found"}
{"asset": "XRP", "slug": "xrp-updown-15m-1771608600", ...  "event": "15min_market_found"}
# NO btc-updown-15m-* entries whatsoever
```

`get_crypto_15min_markets()` in `data_feeds/polymarket_client_v2.py` probes
`https://gamma-api.polymarket.com/events/slug/{asset}-updown-15m-{ts}` for all
four assets.  ETH, SOL, and XRP return HTTP 200; all BTC slugs return HTTP 404.
**Polymarket has no active BTC 15-min updown markets right now.**

### Decision path

```
scan_opportunities()
  └─ btc_markets = [m for m in all_markets if "btc" in question]  → []
  └─ prioritized_markets = _select_markets_for_all_timeframes("BTC", [])  → []
  └─ returns None        ← no arbitrage_opportunity_detected ever emitted
```

The strategy then emits `strategy_scan_complete(opportunity_found=False)` and
loops back.  No error, no warning — just silent no-op.

### Fix applied

`scan_opportunities()` now iterates over **BTC, ETH, SOL, XRP** in priority
order.  If BTC has no markets but ETH does, the bot trades ETH.  Each market
entry is tagged with its asset, and the correct spot price is fetched from the
Binance feed before calling `_check_market_arbitrage`.

---

## Cause 2 — Hardcoded 6% midpoint floor (blocking when BTC markets exist)

### Math

For a BTC market priced near 50/50 (YES ≈ 0.50), without Charlie:

```
true_prob_up   = 0.60         (fallback for 0.02–0.15% BTC movement)
raw_edge_up    = 0.60 – 0.50 = 0.10
calculate_dynamic_fee(0.50)  = 0.03  (base 3% × fee_multiplier=1.0 at midpoint)
slippage_buffer              = 0.01
net_edge_up    = 0.10 – 0.03 – 0.01 = 0.06

_min_required_net_edge(0.50, "hourly"):
  base_min_edge  = edge_thresholds["hourly"] = 0.025
  midpoint_min   = max(0.025, 0.06) = 0.06   ← hardcoded floor!
  required       = 0.06

gate: net_edge_up > required  →  0.06 > 0.06  →  FALSE (strict >)
```

**Off-by-epsilon**: the achievable maximum exactly equals the hardcoded floor.
Any market remotely close to 50/50 is permanently gated out regardless of the
configured `min_edge`.

### Fix applied

`_min_required_net_edge` now adds a **1% midpoint premium** on top of
`base_min_edge`, so a user-configured threshold of 0.025 becomes 0.035 at
midpoint — not a 6% override that eliminates all mid-market opportunities.

```python
# Before
midpoint_min = max(base_min_edge, Decimal("0.06"))   # always 6% floor

# After
midpoint_min = max(base_min_edge, base_min_edge + Decimal("0.01"))  # +1% premium
```

---

## Cause 3 — `_on_price_update` ignores ETH/SOL/XRP ticks

### Code

```python
# main.py — before fix
async def _on_price_update(self, symbol: str, price_data) -> None:
    if symbol != "BTC":
        return                    # ETH/SOL/XRP ticks silently dropped
    ...
    await self._run_strategy_scan(trigger="price_tick")
```

The WebSocket subscribes to `['BTC', 'ETH', 'SOL']` per the YAML, delivers
ticks for all three, but the strategy scan was only triggered on BTC.  Even
after fixing cause 1, an ETH market would only be scanned on the next BTC tick
(which may never come if BTC is stale), or on the periodic loop tick.

### Fix applied

`_on_price_update` now checks `symbol not in {"BTC", "ETH", "SOL", "XRP"}`
so every live asset tick drives a fresh strategy scan.

---

## New diagnostic events added

| Event | Where | What it tells you |
|-------|-------|-------------------|
| `diagnostic_asset_prices` | `scan_opportunities` start | BTC/ETH/SOL/XRP prices from Binance; `None` = disconnected |
| `asset_markets_filtered` | per-asset filter | how many Polymarket candidates per symbol |
| `edge_candidate_computed` | `_check_market_arbitrage` | **always emitted** with `passes_up / passes_down` so you can see why a trade was rejected even when no opportunity is returned |
| `edge_candidate_rejected` | `_check_market_arbitrage` | emitted when `abs(price_change_pct) < 0.02%` (too neutral); previously silently returned `None` |

---

## How to verify the fix

```bash
# Start a 15-minute exploration paper session
python main.py --config config/paper_exploration.yaml --mode paper

# In another terminal, watch the log
jq 'select(.event | test("edge_candidate_computed|arbitrage_opportunity_detected|asset_markets_filtered"))' \
    -c logs/production.log

# Expected output within 2 minutes:
# {"event":"asset_markets_filtered","asset":"ETH","count":4,...}
# {"event":"edge_candidate_computed","asset":"ETH","passes_up":false,"net_edge_up":"0.023",...}
# ...eventually...
# {"event":"arbitrage_opportunity_detected","market_id":"0x...","asset":"ETH","edge":"0.031",...}
```

---

## Remaining unknowns

1. **Charlie confidence gate**: `CHARLIE_CONFIG.min_edge=0.05` and
   `min_confidence=0.60` in `config_production.py` can block trades that pass
   the strategy-level gate.  Review `integrations/charlie_booster.py` to
   confirm it doesn't double-gate with the same threshold.

2. **Actual Polymarket fees**: `calculate_dynamic_fee` uses a synthetic 3%
   peak fee model.  Verify against real Polymarket taker fee schedule (currently
   ~2%) to avoid over-deducting edge.

3. **`price_change_pct < 0.02` neutrality filter**: this fires frequently when
   BTC/ETH are flat.  Consider raising the sensitivity or using volatility-
   adjusted thresholds rather than a fixed % cutoff.
