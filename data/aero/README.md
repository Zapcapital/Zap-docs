# Aero Daily Fee & Allocation Snapshot (Base)

Published by [Zap Capital](https://zap.capital). A free, machine-readable daily snapshot of the top 50 Aero gauges on Base, ranked by Allocation Power (AP).

## Endpoints (stable URLs)

| File | URL | Purpose |
|---|---|---|
| Latest JSON | `https://docs.zap.capital/data/aero/latest.json` | Newest daily snapshot (JSON) |
| Latest CSV | `https://docs.zap.capital/data/aero/latest.csv` | Flat table, one row per pool |
| Dated JSON | `https://docs.zap.capital/data/aero/YYYY-MM-DD.json` | Immutable daily archive (e.g. `2026-11-25.json`) |
| Dated CSV | `https://docs.zap.capital/data/aero/YYYY-MM-DD.csv` | Immutable daily CSV archive |
| JSON Schema | `https://docs.zap.capital/data/aero/schema-0.2.json` | JSON Schema (Draft 2020-12); also at `schema.json` |
| Dataset Index | `https://docs.zap.capital/data/aero/index.json` | Machine-readable index of latest snapshot and dated archives |

For reproducible citations, use the dated `YYYY-MM-DD.json` URL rather than `latest.json`.

## Update cadence

- **Target schedule:** Daily at approximately **06:30 UTC** (Base mainnet snapshot).
- **Window:** True 24-hour lookback resolved by on-chain block timestamps (`snapshot_ts - 86,400s`), recorded in `snapshot.window`.

## Fields & sources

All on-chain data is read directly from official Aero contracts on Base (chain ID `8453`), paired with reputable market aggregators:

- **Gauge & Allocation Power (`allocation_power`, `allocation_power_permanent`, `allocation_pct`):** `LeafVoter` and `FactoryRegistry` on Base. `total_allocation_power` sums AP across all Base gauges.
- **Pool metadata (`pool_name`, `pair`, `pool_type`, `fee_tier`, `tick_spacing`, `pool_address`, `gauge_address`, `voting_rewards_manager`, `token0`, `token1`, `gauge_activated`):** On-chain gauge and pool contract calls.
- **Swap fee (`fee_pct`, `fee_pct_spot`):** For Slipstream CL pools, `fee_pct` is the time-weighted mean of 24 hourly historical `pool.fee()` calls across the 24-hour window (`fee_pct_spot` is at the snapshot block). For volatile/stable pools, `PoolFactory.getFee()` probe value.
- **24h volume & liquidity (`volume_24h_usd`, `tvl_usd`):** GeckoTerminal API v2 (`/networks/base/pools/multi/`).
- **Estimated gross swap fees (`gross_swap_fees_24h_usd_est`):** `volume_24h_usd * fee_pct / 100`. Represents estimated gross swap fees across all LPs, not allocator payouts.
- **Emission cap (`emission_cap_aero_per_week`):** `FactoryRegistry.emissionCap(gauge) * 604,800`. This is a weekly ceiling, not realised emissions.
- **Protocol totals (`protocol_fees_last_utc_day`, `aero_price_usd`):** DefiLlama protocol fees summary (`aerodrome` / Aero) and `coins.llama.fi` AERO token price.
- **Incentives (`incentives_usd_per_week`):** Currently `null` in schema `0.2.0`.
- **Per-pool flags (`notes`):** Automatically flags incomplete fee samples, missing aggregator rows, or statistical outliers (`outlier_high_fee_pct` when `fee_pct >= 1.0%`; `outlier_low_turnover_vol_lt_100k_on_gt500k_tvl` when 24h volume is under `$100k` on `>= $500k` TVL).

## Free-use terms (placeholder pending legal review)

> Free to use with attribution to Zap Capital (`zap.capital`). Informational data only, not financial advice; yields and fees are not guaranteed, and data may be incomplete, delayed, or wrong. Aero is a third-party protocol not affiliated with Zap Capital.

*(Formal open-data licence / terms wording pending review.)*

## Contact

- Website: https://zap.capital
- Agent brief: https://docs.zap.capital/llms.txt
- Email: info@zap.capital
- X: https://x.com/zap_capital
