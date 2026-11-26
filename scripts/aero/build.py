#!/usr/bin/env python3
"""Zap Capital - free daily Aero pool and allocation snapshot (public builder, schema 0.2.0).

Builds the public daily snapshot served at https://docs.zap.capital/data/aero/ : Allocation Power (AP)
per gauge read from Aero's contracts on Base, CL fee rates averaged over a true 24h window, plus 24h
volume / liquidity from GeckoTerminal and protocol daily fees / AERO price from DefiLlama.
Read-only, free public endpoints, no API keys, no wallets. Writes only the public files.

Usage (from the repo root):
  python scripts/aero/build.py --public-only --out-dir data/aero
  python scripts/aero/build.py --public-only --dry-run          # fetch + validate, write nothing
Options: --top N (default 50), --schema PATH (default <out-dir>/schema-0.2.json),
         --max-ap-change 0.10 (abort if total AP moves more than this vs the current latest.json).
Optional env: BASE_RPC_URL (state reads), BASE_LOGS_RPC_URL (historical state). Public RPCs are used
as fallbacks, so no secret is required.

Output (in --out-dir): YYYY-MM-DD.json/.csv (never overwritten), latest.json/.csv, index.json.
Exit codes: 0 = written or already published for that UTC date, 1 = build/validation/sanity failure.

Sources (official Aero contracts/repo and public aggregators only):
  - Aero contract addresses: github.com/Dromos-Labs/metadex-public deployment-addresses
  - FactoryRegistry (Base) 0x7b2A2c12dC5Ea2a31455952a05031C3E4BB28F3E: gaugeFactories(), factoryToGauges(), emissionCap()
  - LeafVoter (Base)       0x382409fad8503FbCdbB27A4211551fa03e884078: gaugeStates(gauge).point -> AP
  - Pools: pool.fee()/tickSpacing() (CL, 24 hourly historical samples), PoolFactory.getFee() (volatile/stable)
  - GeckoTerminal API v2 networks/base/pools/multi: volume_usd.h24, reserve_in_usd
  - DefiLlama api.llama.fi/summary/fees/aerodrome: protocol fees per completed UTC day
  - DefiLlama coins.llama.fi: AERO price
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

SCHEMA_VERSION = "0.2.0"
DUBLIN = ZoneInfo("Europe/Dublin")
UA = "ZapCapitalAeroData/0.2 (read-only; zap.capital)"
BASE_URL = "https://docs.zap.capital/data/aero/"


def _rpc_list(env_name: str, defaults: list[str]) -> list[str]:
    first = os.environ.get(env_name, "").strip()
    out = [first] if first else []
    out += [u for u in defaults if u not in out]
    return out


# State reads at the latest block. mainnet.base.org is the official public endpoint.
RPCS = _rpc_list("BASE_RPC_URL", ["https://mainnet.base.org", "https://base-rpc.publicnode.com",
                                  "https://base.drpc.org", "https://1rpc.io/base"])
# Historical state (eth_call at past blocks) needs a node that keeps recent state; publicnode does.
HIST_RPCS = _rpc_list("BASE_LOGS_RPC_URL", ["https://base-rpc.publicnode.com", "https://base.drpc.org",
                                            "https://mainnet.base.org", "https://1rpc.io/base"])
# Windows are timestamp-based. Never assume a block time (Base runs 200 ms blocks since Denim, 5 Nov 2026).
WINDOW_SECONDS = 86_400
FEE_SAMPLES = 24      # CL fee() samples at snapshot_ts - k*3600s, k = 0..23
CHAIN_ID = 8453
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
FACTORY_REGISTRY = "0x7b2A2c12dC5Ea2a31455952a05031C3E4BB28F3E"
LEAF_VOTER = "0x382409fad8503FbCdbB27A4211551fa03e884078"
AERO_TOKEN = "0x3f5bF3EdAc59d650D8fDf9657a71F2f904A3a4Ee"
GAUGE_FACTORY_TYPE = {
    "0x9fbcf2df0dc553ce40ef63963bff465b76792b7e": "volatile",
    "0x1e0f776e88649285f99e65fa599ad6e0f9d97800": "stable",
    "0xf437f62053acb121665b8fdbfea8f214fb8e589d": "cl",
}

URL_GT_MULTI = "https://api.geckoterminal.com/api/v2/networks/base/pools/multi/{}"
URL_LLAMA_FEES = "https://api.llama.fi/summary/fees/aerodrome?dataType=dailyFees"
URL_LLAMA_PRICE = f"https://coins.llama.fi/prices/current/base:{AERO_TOKEN}"
REPO_ADDRESSES = "https://github.com/Dromos-Labs/metadex-public/tree/main/deployment-addresses"

LICENSE_LINE = ("Free to use with attribution to Zap Capital (zap.capital). Informational data only, "
                "not financial advice; may be incomplete, delayed or wrong. Aero is a third-party protocol "
                "not affiliated with Zap Capital.")

SRC = {
    "onchain_factory_registry": f"Base FactoryRegistry {FACTORY_REGISTRY} (Aero official deployment, {REPO_ADDRESSES})",
    "onchain_leaf_voter": f"Base LeafVoter {LEAF_VOTER} gaugeStates(gauge).point (Aero official deployment)",
    "onchain_pool": "Base pool contract token0()/token1()/tickSpacing() + ERC-20 symbol()",
    "geckoterminal": "GeckoTerminal API v2 /networks/base/pools/multi",
    "defillama_fees": URL_LLAMA_FEES,
    "defillama_price": URL_LLAMA_PRICE,
}

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "application/json"})


def log(msg: str):
    print(msg, flush=True)


# ------------------------------------------------------------------ helpers
def sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def _host(u: str) -> str:
    return u.split("/")[2] if "//" in u else u  # never log full URLs (a keyed RPC URL may hold a key)


def rpc(method: str, params: list, urls: list[str] | None = None, tries_per_url: int = 3):
    """JSON-RPC with retry + fallback across endpoints. Pins the last good endpoint first."""
    urls = urls if urls is not None else RPCS
    last = None
    for u in list(urls):
        for i in range(tries_per_url):
            try:
                r = session.post(u, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=45)
                if r.status_code in (403, 429):
                    raise RuntimeError(f"HTTP {r.status_code}")
                r.raise_for_status()
                j = r.json()
                if "error" in j:
                    raise RuntimeError(str(j["error"])[:200])
                if urls and urls[0] != u:  # promote the working endpoint
                    urls.remove(u)
                    urls.insert(0, u)
                return j["result"]
            except Exception as e:
                last = f"{_host(u)}: {e}"
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"rpc {method} failed on all endpoints: {last}")


def get_json(url: str, tries: int = 5):
    last = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=60)
            if r.status_code == 429:
                time.sleep(15 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"fetch failed: {url}: {last}")


def multicall(calls: list[tuple[str, bytes]], block: str, chunk: int = 150, urls: list[str] | None = None) -> list[bytes | None]:
    """Multicall3.aggregate3 with allowFailure=True. Returns raw return data or None per call."""
    out: list[bytes | None] = []
    s = sel("aggregate3((address,bool,bytes)[])")
    for i in range(0, len(calls), chunk):
        part = [(to_checksum_address(t), True, d) for t, d in calls[i:i + chunk]]
        data = s + encode(["(address,bool,bytes)[]"], [part])
        res = rpc("eth_call", [{"to": MULTICALL3, "data": "0x" + data.hex()}, block], urls=urls)
        (results,) = decode(["(bool,bytes)[]"], bytes.fromhex(res[2:]))
        out.extend(rd if ok and rd else None for ok, rd in results)
    return out


def dec(types, raw):
    if raw is None:
        return None
    try:
        v = decode(types, raw)
        return v[0] if len(v) == 1 else v
    except Exception:
        return None


def dec_symbol(raw):
    s = dec(["string"], raw)
    if s is None and raw and len(raw) == 32:  # bytes32 symbols
        s = raw.rstrip(b"\x00").decode(errors="ignore")
    return s


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(x, nd=None):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if nd is not None else f


# ------------------------------------------------------------------ fetchers
def fetch_onchain(block_tag: str):
    """All gauges on Base with AP, emission cap, pool address, type and symbols."""
    gf_raw = multicall([(FACTORY_REGISTRY, sel("gaugeFactories()"))], block_tag)[0]
    factories = list(dec(["address[]"], gf_raw))
    lists = multicall([(FACTORY_REGISTRY, sel("factoryToGauges(address)") + encode(["address"], [f]))
                       for f in factories], block_tag)
    gauges = []
    for f, raw in zip(factories, lists):
        for g in dec(["address[]"], raw) or []:
            gauges.append({"gauge": to_checksum_address(g), "gauge_factory": to_checksum_address(f),
                           "type": GAUGE_FACTORY_TYPE.get(f.lower(), "unknown")})

    calls = []
    for g in gauges:
        a = g["gauge"]
        calls += [
            (LEAF_VOTER, sel("gaugeStates(address)") + encode(["address"], [a])),
            (FACTORY_REGISTRY, sel("emissionCap(address)") + encode(["address"], [a])),
            (a, sel("pool()")),
            (a, sel("stakingToken()")),
            (a, sel("votingRewardsManager()")),
        ]
    res = multicall(calls, block_tag)
    gs_types = ["uint128", "uint128", "uint48", "bool", "bool", "uint128", "uint256", "uint256",
                "(int128,int128,uint48,uint128)"]
    for i, g in enumerate(gauges):
        st, cap, pool, stk, mgr = res[5 * i:5 * i + 5]
        m = dec(["address"], mgr)
        g["manager"] = to_checksum_address(m) if m else None
        g["state"] = dec(gs_types, st)
        g["emission_cap_per_sec"] = dec(["uint128"], cap)
        p = dec(["address"], pool) or dec(["address"], stk)
        g["pool"] = to_checksum_address(p) if p else None

    pcalls = []
    for g in gauges:
        p = g["pool"] or "0x0000000000000000000000000000000000000000"
        pcalls += [(p, sel("token0()")), (p, sel("token1()")), (p, sel("tickSpacing()")),
                   (p, sel("fee()")), (p, sel("factory()")), (p, sel("getReserves()"))]
    pres = multicall(pcalls, block_tag)
    toks = set()
    v2_fee_calls, v2_idx = [], []
    for i, g in enumerate(gauges):
        t0, t1, ts, fee, fac, rsv = pres[6 * i:6 * i + 6]
        g["fee_pct"] = None
        if g["type"] == "cl" and dec(["uint24"], fee) is not None:
            g["fee_pct"] = dec(["uint24"], fee) / 1e4  # pips: 1e6 = 100%
        elif g["type"] in ("volatile", "stable") and fac and rsv:
            r = dec(["uint256", "uint256", "uint256"], rsv)
            f = dec(["address"], fac)
            if r and f:
                # factory.getFee(pool, sender, amount0In, amount1In, reserve0, reserve1) -> (fee bps, mevFee bps, toxic)
                amt = max(r[0] // 10000, 1)  # small probe swap: 0.01% of reserve0
                v2_fee_calls.append((f, sel("getFee(address,address,uint256,uint256,uint256,uint256)") + encode(
                    ["address", "address", "uint256", "uint256", "uint256", "uint256"],
                    [g["pool"], "0x0000000000000000000000000000000000000000", amt, 0, r[0], r[1]])))
                v2_idx.append(i)
        g["token0"] = to_checksum_address(dec(["address"], t0)) if dec(["address"], t0) else None
        g["token1"] = to_checksum_address(dec(["address"], t1)) if dec(["address"], t1) else None
        g["tick_spacing"] = dec(["int24"], ts)
        toks.update(t for t in (g["token0"], g["token1"]) if t)
    if v2_fee_calls:
        for i, raw in zip(v2_idx, multicall(v2_fee_calls, block_tag)):
            v = dec(["uint256", "uint256", "bool"], raw)
            if v:
                gauges[i]["fee_pct"] = v[0] / 100
    toks = sorted(toks)
    sres = multicall([(t, sel("symbol()")) for t in toks], block_tag)
    syms = {t: dec_symbol(r) for t, r in zip(toks, sres)}
    for g in gauges:
        g["symbol0"] = syms.get(g["token0"])
        g["symbol1"] = syms.get(g["token1"])
    return gauges


def ap_now(state, ts_now: int):
    """AP = bias - slope*(t - point.ts) + permanentStakeBalance, in AERO (18 decimals)."""
    if not state:
        return None, None, None
    bias, slope, pts, perm = state[8]
    decaying = max(bias - slope * max(ts_now - pts, 0), 0)
    return (decaying + perm) / 1e18, perm / 1e18, int(pts)


def fetch_geckoterminal(addresses: list[str]):
    out, notes = {}, []
    for i in range(0, len(addresses), 30):
        part = addresses[i:i + 30]
        try:
            d = get_json(URL_GT_MULTI.format(",".join(part)))
            for p in d.get("data", []):
                a = p["attributes"]
                out[a["address"].lower()] = a
        except Exception as e:
            notes.append(f"geckoterminal batch {i // 30} failed: {e}")
        time.sleep(2.5)  # free tier ~30 calls/min
    return out, notes


_block_ts_cache: dict[int, int] = {}


def block_ts(n: int) -> int:
    if n not in _block_ts_cache:
        b = rpc("eth_getBlockByNumber", [hex(n), False])
        _block_ts_cache[n] = int(b["timestamp"], 16)
    return _block_ts_cache[n]


def block_at_or_before(target_ts: int, hi: int, hi_ts: int) -> int:
    """Largest block number with timestamp <= target_ts, from block headers (no assumed block time).
    Gallops back from `hi` to bracket the target, then alternates interpolation and bisection."""
    if hi_ts <= target_ts:
        return hi
    step, lo = 1000, max(hi - 1000, 0)
    while block_ts(lo) > target_ts:
        if lo == 0:
            raise RuntimeError(f"no block at or before ts {target_ts}")
        hi, step = lo, step * 4
        lo = max(hi - step, 0)
    it = 0
    while hi - lo > 1:  # invariant: ts(lo) <= target < ts(hi)
        t_lo, t_hi = block_ts(lo), block_ts(hi)
        if it % 2 == 0:
            guess = lo + (target_ts - t_lo) * (hi - lo) // (t_hi - t_lo) + 1
        else:
            guess = (lo + hi) // 2
        guess = min(max(guess, lo + 1), hi - 1)
        if block_ts(guess) <= target_ts:
            lo = guess
        else:
            hi = guess
        it += 1
    return lo


def fetch_cl_fee_avg(sel_g, sample_blocks: list[int]):
    """Mean of CL pool fee() at true hourly timestamps over the last 24h (historical eth_call)."""
    cl = [g for g in sel_g if g["type"] == "cl" and g["pool"]]
    samples = {g["pool"]: [] for g in cl}
    errors = 0
    for blk in sample_blocks:
        try:
            res = multicall([(g["pool"], sel("fee()")) for g in cl], hex(blk), urls=HIST_RPCS)
        except Exception:
            errors += 1
            continue
        time.sleep(1.0)  # stay polite on shared public nodes
        for g, raw in zip(cl, res):
            v = dec(["uint24"], raw)
            if v is not None:
                samples[g["pool"]].append(v / 1e4)
    out = {p: (sum(v) / len(v), len(v)) for p, v in samples.items() if v}
    return out, errors


def fetch_llama_fees():
    d = get_json(URL_LLAMA_FEES)
    today = datetime.now(timezone.utc).date()
    rows = []
    bd = {int(ts): b for ts, b in d.get("totalDataChartBreakdown", [])}
    for ts, v in d.get("totalDataChart", []):
        day = datetime.fromtimestamp(int(ts), timezone.utc).date()
        if day < today:
            base = bd.get(int(ts), {}).get("Base")
            rows.append((day, float(v), sum(float(x) for x in base.values()) if base else None))
    if not rows:
        return None
    day, all_c, base = rows[-1]
    last7 = rows[-7:]
    return {
        "date_utc": day.isoformat(),
        "all_chains_usd": round(all_c, 2),
        "base_usd": round(base, 2) if base is not None else None,
        "all_chains_7d_usd": round(sum(r[1] for r in last7), 2) if len(last7) == 7 else None,
        "protocol_name": d.get("name"),
    }


def fetch_aero_price():
    c = get_json(URL_LLAMA_PRICE)["coins"][f"base:{AERO_TOKEN}"]
    return float(c["price"]), int(c["timestamp"])


# ------------------------------------------------------------------ build
def pool_label(g):
    pair = f"{g['symbol0'] or '?'}/{g['symbol1'] or '?'}"
    if g["type"] == "cl" and g["tick_spacing"] is not None:
        return pair, f"CL{g['tick_spacing']}"
    return pair, {"volatile": "volatile", "stable": "stable"}.get(g["type"], "unknown")


def build(top: int):
    notes: list[str] = [
        "incentives_usd_per_week is null for every pool: incentive programs are not read yet",
        "gross_swap_fees_24h_usd_est = rolling 24h volume x time-weighted 24h mean fee (all LPs, not the amount paid to allocators)",
        "emission_cap_aero_per_week is the FactoryRegistry cap (a ceiling), not realised emissions",
        "Pools with fee_pct >= 1.0% or <$100k 24h volume on >=$500k TVL are flagged in pool.notes (e.g. AERO/USDC CL200 at 1.05% fee)",
    ]
    blk = rpc("eth_getBlockByNumber", ["latest", False])
    block_number = int(blk["number"], 16)
    snap_ts = int(blk["timestamp"], 16)
    _block_ts_cache[block_number] = snap_ts
    block_tag = hex(block_number)
    snap = datetime.fromtimestamp(snap_ts, timezone.utc)
    log(f"snapshot block {block_number} at {iso_utc(snap)}")

    start_block = block_at_or_before(snap_ts - WINDOW_SECONDS, block_number, snap_ts)
    sample_blocks = [block_number] + [block_at_or_before(snap_ts - k * 3600, block_number, snap_ts)
                                      for k in range(1, FEE_SAMPLES)]
    window = {
        "seconds": WINDOW_SECONDS,
        "start_block": start_block,
        "start_timestamp_utc": iso_utc(datetime.fromtimestamp(block_ts(start_block), timezone.utc)),
        "end_block": block_number,
        "end_timestamp_utc": iso_utc(snap),
        "event_blocks": [start_block + 1, block_number],
        "blocks_in_window": block_number - start_block,
        "fee_sample_count": FEE_SAMPLES,
        "fee_sample_spacing_seconds": 3600,
    }
    log(f"window blocks {start_block}..{block_number} ({block_number - start_block:,} blocks)")

    gauges = fetch_onchain(block_tag)
    for g in gauges:
        g["ap"], g["ap_permanent"], g["point_ts"] = ap_now(g["state"], snap_ts)
        g["activated"] = bool(g["state"][4]) if g["state"] else None
    total_ap = sum(g["ap"] or 0 for g in gauges)
    ranked = sorted((g for g in gauges if g["ap"]), key=lambda g: -g["ap"])
    sel_g = ranked[:top]
    log(f"gauges {len(gauges)}, with AP {len(ranked)}, total AP {total_ap:,.0f}")

    gt, gt_notes = fetch_geckoterminal([g["pool"] for g in sel_g if g["pool"]])
    notes += gt_notes

    try:
        fee_avg, fee_err = fetch_cl_fee_avg(sel_g, sample_blocks)
        if fee_err:
            notes.append(f"{fee_err} of {FEE_SAMPLES} hourly CL fee samples failed")
    except Exception as e:
        fee_avg = {}
        notes.append(f"24h CL fee averaging unavailable, spot fee used: {e}")
    try:
        price, price_ts = fetch_aero_price()
    except Exception as e:
        price, price_ts = None, None
        notes.append(f"AERO price unavailable: {e}")
    try:
        llama = fetch_llama_fees()
    except Exception as e:
        llama = None
        notes.append(f"DefiLlama fees unavailable: {e}")

    pools = []
    for rank, g in enumerate(sel_g, 1):
        pair, tier = pool_label(g)
        a = gt.get((g["pool"] or "").lower())
        pnotes = []
        vol = num(a["volume_usd"]["h24"]) if a else None
        tvl = num(a["reserve_in_usd"]) if a else None
        spot_fee = g["fee_pct"]
        fee_pct = spot_fee
        fa = fee_avg.get(g["pool"])
        if fa:
            fee_pct, n_samples = fa
            if n_samples < FEE_SAMPLES:
                pnotes.append(f"fee_pct_from_{n_samples}_of_{FEE_SAMPLES}_samples")
        elif g["type"] == "cl":
            pnotes.append("fee_pct_24h_avg_unavailable_spot_used")
        if fee_pct is None and a and num(a.get("pool_fee_percentage")) is not None:
            fee_pct = num(a.get("pool_fee_percentage"))
            pnotes.append("fee_pct_from_geckoterminal_fallback")
        if a is None:
            pnotes.append("not_found_on_geckoterminal")
        if fee_pct is None:
            pnotes.append("fee_pct_unavailable")
        if fee_pct is not None and fee_pct >= 1.0:
            pnotes.append(f"outlier_high_fee_pct:{round(fee_pct, 4)}pct")
        if vol is not None and tvl is not None and tvl >= 500_000 and vol < 100_000:
            pnotes.append("outlier_low_turnover_vol_lt_100k_on_gt500k_tvl")
        gross = round(vol * fee_pct / 100, 2) if vol is not None and fee_pct is not None else None
        cap_w = g["emission_cap_per_sec"] * 604800 / 1e18 if g["emission_cap_per_sec"] is not None else None
        pools.append({
            "rank": rank,
            "pool_name": f"{pair} {tier}",
            "pair": pair,
            "pool_type": g["type"],
            "fee_tier": tier,
            "tick_spacing": g["tick_spacing"],
            "fee_pct": round(fee_pct, 6) if fee_pct is not None else None,
            "fee_pct_spot": round(spot_fee, 6) if spot_fee is not None else None,
            "pool_address": g["pool"],
            "gauge_address": g["gauge"],
            "voting_rewards_manager": g["manager"],
            "token0": {"address": g["token0"], "symbol": g["symbol0"]},
            "token1": {"address": g["token1"], "symbol": g["symbol1"]},
            "gauge_activated": g["activated"],
            "allocation_power": round(g["ap"], 2),
            "allocation_power_permanent": round(g["ap_permanent"], 2) if g["ap_permanent"] is not None else None,
            "allocation_pct": round(g["ap"] / total_ap * 100, 4) if total_ap else None,
            "volume_24h_usd": round(vol, 2) if vol is not None else None,
            "gross_swap_fees_24h_usd_est": gross,
            "tvl_usd": round(tvl, 2) if tvl is not None else None,
            "emission_cap_aero_per_week": round(cap_w, 2) if cap_w is not None else None,
            "incentives_usd_per_week": None,
            "notes": pnotes,
        })

    snapshot = {
        "timestamp_utc": iso_utc(snap),
        "timestamp_dublin": snap.astimezone(DUBLIN).isoformat(timespec="seconds"),
        "date_utc": snap.date().isoformat(),
        "chain": "base",
        "chain_id": CHAIN_ID,
        "block_number": block_number,
        "generated_at_utc": iso_utc(datetime.now(timezone.utc)),
        "window": window,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": "zap-capital/aero-daily",
        "publisher": "Zap Capital (zap.capital)",
        "license": LICENSE_LINE,
        "snapshot": snapshot,
        "totals": {
            "gauge_count": len(gauges),
            "gauges_with_allocation": len(ranked),
            "pools_in_file": len(pools),
            "total_allocation_power": round(total_ap, 2),
            "allocation_pct_in_file": round(sum(p["allocation_pct"] or 0 for p in pools), 4),
            "gross_swap_fees_24h_usd_est_in_file": round(sum(p["gross_swap_fees_24h_usd_est"] or 0 for p in pools), 2),
            "tvl_usd_in_file": round(sum(p["tvl_usd"] or 0 for p in pools), 2),
            "protocol_fees_last_utc_day": llama,
            "aero_price_usd": round(price, 6) if price else None,
            "aero_price_timestamp_utc": iso_utc(datetime.fromtimestamp(price_ts, timezone.utc)) if price_ts else None,
        },
        "field_sources": {
            "allocation_power": SRC["onchain_leaf_voter"],
            "allocation_power_permanent": SRC["onchain_leaf_voter"],
            "allocation_pct": "allocation_power / totals.total_allocation_power (all Base gauges)",
            "total_allocation_power": SRC["onchain_leaf_voter"] + "; summed over every gauge in " + SRC["onchain_factory_registry"],
            "gauge_address": SRC["onchain_factory_registry"] + " factoryToGauges()",
            "gauge_activated": SRC["onchain_leaf_voter"],
            "voting_rewards_manager": "gauge.votingRewardsManager() on Base",
            "pool_address": "gauge.pool() / gauge.stakingToken() on Base",
            "pool_type": "gauge factory (Aero gaugestack-base.json): volatile | stable | cl",
            "fee_tier": "cl: 'CL' + pool.tickSpacing(); else pool_type",
            "pair": SRC["onchain_pool"],
            "fee_pct": "CL: time-weighted mean of 24 pool.fee() reads (pips/1e4) at snapshot timestamp minus 0..23 hours, blocks resolved by timestamp (historical eth_call); volatile/stable: spot value",
            "fee_pct_spot": "onchain at snapshot block: CL pool.fee() pips/1e4; volatile/stable PoolFactory.getFee() for a small probe swap, bps/100. Aero fees are dynamic (fee modules, MEV tax)",
            "volume_24h_usd": SRC["geckoterminal"] + " volume_usd.h24 (rolling 24h)",
            "tvl_usd": SRC["geckoterminal"] + " reserve_in_usd",
            "gross_swap_fees_24h_usd_est": "volume_24h_usd * fee_pct / 100. Gross swap fees, all LPs; estimate; not the amount paid to allocators",
            "emission_cap_aero_per_week": SRC["onchain_factory_registry"] + " emissionCap(gauge) * 604800 (a cap, not actual emissions)",
            "incentives_usd_per_week": "not yet collected (null); planned: IncentiveStreaming programs per gauge",
            "protocol_fees_last_utc_day": SRC["defillama_fees"],
            "aero_price_usd": SRC["defillama_price"],
        },
        "pools": pools,
        "notes": notes,
    }


CSV_COLS = ["snapshot_utc", "rank", "pool_name", "pair", "pool_type", "fee_tier", "fee_pct", "fee_pct_spot",
            "pool_address", "gauge_address", "token0_symbol", "token1_symbol", "allocation_power", "allocation_pct",
            "volume_24h_usd", "gross_swap_fees_24h_usd_est", "tvl_usd", "emission_cap_aero_per_week", "notes"]


def _write_json(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _write_csv(path: Path, doc):
    tmp = path.with_suffix(path.suffix + ".tmp")
    snap_utc = doc["snapshot"]["timestamp_utc"]
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for p in doc["pools"]:
            w.writerow({"snapshot_utc": snap_utc, **{k: p[k] for k in CSV_COLS if k in p},
                        "token0_symbol": p["token0"]["symbol"], "token1_symbol": p["token1"]["symbol"],
                        "notes": ";".join(p["notes"])})
    os.replace(tmp, path)


def validate(doc, schema_path: Path):
    import jsonschema
    schema = json.loads(schema_path.read_text())
    errs = sorted(jsonschema.Draft202012Validator(schema).iter_errors(doc), key=lambda e: list(e.path))
    if errs:
        for e in errs[:20]:
            log(f"schema error at {'/'.join(map(str, e.path))}: {e.message[:200]}")
        raise SystemExit(f"schema validation failed ({len(errs)} errors)")
    log(f"schema OK ({schema_path.name})")


def sanity(doc, out_dir: Path, max_change: float):
    t = doc["totals"]
    if t["pools_in_file"] < 10 or not t["total_allocation_power"]:
        raise SystemExit(f"sanity failed: pools_in_file={t['pools_in_file']} total_ap={t['total_allocation_power']}")
    prev_path = out_dir / "latest.json"
    if prev_path.exists():
        prev = json.loads(prev_path.read_text())
        pa = prev.get("totals", {}).get("total_allocation_power")
        if pa:
            chg = t["total_allocation_power"] / pa - 1
            log(f"total AP change vs previous latest.json ({prev['snapshot']['date_utc']}): {chg:+.2%}")
            if abs(chg) > max_change:
                raise SystemExit(f"sanity failed: total AP moved {chg:+.2%} (> {max_change:.0%}); not publishing")


def update_index(out_dir: Path, doc):
    path = out_dir / "index.json"
    idx = json.loads(path.read_text()) if path.exists() else {
        "dataset": "zap-capital/aero-daily",
        "schema_version": SCHEMA_VERSION,
        "publisher": "Zap Capital (https://zap.capital)",
        "base_url": BASE_URL,
        "license": LICENSE_LINE,
    }
    s, t = doc["snapshot"], doc["totals"]
    day = s["date_utc"]
    idx["latest_snapshot"] = {
        "date_utc": day,
        "timestamp_utc": s["timestamp_utc"],
        "block_number": s["block_number"],
        "pools_in_file": t["pools_in_file"],
        "gauge_count": t["gauge_count"],
        "total_allocation_power": t["total_allocation_power"],
        "json_url": f"{BASE_URL}{day}.json",
        "csv_url": f"{BASE_URL}{day}.csv",
    }
    arch = [a for a in idx.get("archives", []) if a.get("date_utc") != day]
    arch.append({"date_utc": day, "json": f"{day}.json", "csv": f"{day}.csv"})
    idx["archives"] = sorted(arch, key=lambda a: a["date_utc"], reverse=True)
    # keep key order stable: move agent_headers_optional (if present) to the end
    if "agent_headers_optional" in idx:
        idx["agent_headers_optional"] = idx.pop("agent_headers_optional")
    _write_json(path, idx)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--public-only", action="store_true",
                    help="write only the public files (this builder has no other mode; flag kept explicit for CI)")
    ap.add_argument("--out-dir", default="data/aero")
    ap.add_argument("--schema", default=None)
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--max-ap-change", type=float, default=0.10)
    ap.add_argument("--dry-run", action="store_true", help="fetch + validate + sanity check, write nothing")
    a = ap.parse_args()
    if not a.public_only:
        log("note: --public-only is implied; this builder only produces public files")
    out_dir = Path(a.out_dir)
    schema_path = Path(a.schema) if a.schema else out_dir / "schema-0.2.json"

    today = datetime.now(timezone.utc).date().isoformat()
    if not a.dry_run and (out_dir / f"{today}.json").exists():
        log(f"{today}.json already published; nothing to do (dated files are never overwritten)")
        return 0

    t0 = time.time()
    doc = build(a.top)
    t = doc["totals"]
    log(f"top {t['pools_in_file']}: {t['allocation_pct_in_file']:.2f}% of AP, gross est ${t['gross_swap_fees_24h_usd_est_in_file']:,.2f}/24h, "
        f"TVL ${t['tvl_usd_in_file']:,.0f}, AERO ${t['aero_price_usd']}  ({time.time() - t0:.0f}s)")
    for n in doc["notes"]:
        log(f"  note: {n}")
    validate(doc, schema_path)
    sanity(doc, out_dir, a.max_ap_change)
    day = doc["snapshot"]["date_utc"]
    if a.dry_run:
        log(f"dry run: would write {day}.json/.csv, latest.json/.csv, index.json in {out_dir}")
        return 0
    if (out_dir / f"{day}.json").exists():
        log(f"{day}.json already exists; not overwriting")
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / f"{day}.json", doc)
    _write_csv(out_dir / f"{day}.csv", doc)
    _write_json(out_dir / "latest.json", doc)
    _write_csv(out_dir / "latest.csv", doc)
    update_index(out_dir, doc)
    log(f"wrote {out_dir}/{day}.json/.csv, latest.json/.csv, index.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
