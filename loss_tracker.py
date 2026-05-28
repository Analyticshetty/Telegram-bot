"""
Loss Tracker (Module #3b) — data-only logging of real losses.

When a position closes with negative P&L, evaluate whether it was:
  - A "REAL" loss (mechanical breakdown of the token), OR
  - An "UNCONFIRMED" loss (could be a wick / temporary dip)

Real-loss definition (combines C + D + E from the design discussion):
  C. Fibonacci 61.8% retracement of (entry → peak) is broken
  D. Sell-side volume in last 5min ≥ 1.5x the 1h average sell volume
  E. (Approximated as) exit_price stayed below Fib threshold (1 confirmation)

If 2+ of (C, D) pass → "REAL_LOSS"
Otherwise → "UNCONFIRMED_LOSS"

NO lockouts, NO actions taken. Pure data collection.
Future modules will use this data to build trade-quality scoring.

Storage:
  losses:log   JSON list of {ts, mint, symbol, entry, peak, exit, pnl, pnl_pct,
                              fib_618, fib_broken, volume_ratio, classification, ...}
               capped at 500 entries
"""

import json
import time
import logging
import requests
from redis_client import get_redis

log = logging.getLogger(__name__)
_redis = get_redis()

K_LOSSES   = "losses:log"
MAX_LOSSES = 500
TIMEOUT    = 8
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"


def _fetch_volume_data(mint: str) -> dict:
    """Pull sell-side volume snapshots from DEXScreener. Returns dict with 5m / 1h sell counts + USD."""
    try:
        r = requests.get(DEXSCREENER_URL.format(mint=mint), timeout=TIMEOUT)
        if r.status_code != 200:
            return {}
        pairs = (r.json() or {}).get("pairs") or []
        if not pairs:
            return {}
        pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
        p = pairs[0]
        txns = p.get("txns") or {}
        vol  = p.get("volume") or {}
        return {
            "sells_5m":   (txns.get("m5")  or {}).get("sells")  or 0,
            "sells_1h":   (txns.get("h1")  or {}).get("sells")  or 0,
            "volume_5m":  vol.get("m5")  or 0,
            "volume_1h":  vol.get("h1")  or 0,
        }
    except Exception as e:
        log.warning(f"loss_tracker volume fetch failed: {e}")
        return {}


def evaluate(position: dict, exit_price: float) -> dict:
    """Evaluate a closed position. Returns classification dict.

    Inputs:
      position: dict with mint, symbol, entry_price, high_price (peak), size_usd
      exit_price: float

    Output dict fields:
      classification:  'REAL_LOSS' | 'UNCONFIRMED_LOSS' | 'GAIN' (if pnl>=0, skipped)
      fib_618:         the 61.8% retracement support level
      fib_broken:      bool — did exit price break below fib_618?
      volume_ratio:    sell_vol_5m / (sell_vol_1h / 12) — proxy for 1.5x avg test
      volume_confirmed: bool — ratio >= 1.5?
      reasoning:       human-readable string
    """
    entry = position.get("entry_price") or 0
    peak  = position.get("high_price") or entry
    size  = position.get("size_usd") or 0

    if entry <= 0:
        return {"classification": "INVALID", "reasoning": "no entry price"}

    pnl_pct = (exit_price / entry - 1) * 100
    if pnl_pct >= 0:
        return {"classification": "GAIN", "pnl_pct": pnl_pct, "reasoning": "exit at/above entry"}

    # --- C. Fibonacci 61.8% retracement of (entry → peak) ---
    # Standard formula: support = peak - 0.618 * (peak - entry)
    # If peak <= entry (price never went above entry), use entry as peak (no Fib meaningful)
    fib_618 = entry  # default
    fib_broken = False
    if peak > entry:
        fib_618 = peak - 0.618 * (peak - entry)
        fib_broken = exit_price < fib_618
    else:
        # Never rallied above entry; treat any meaningful drawdown as broken
        fib_broken = pnl_pct <= -20

    # --- D. Volume confirmation ---
    v = _fetch_volume_data(position.get("mint", ""))
    sells_5m  = v.get("sells_5m") or 0
    sells_1h  = v.get("sells_1h") or 0
    # Expected 5m sells if even distribution = sells_1h / 12
    expected_5m = max(sells_1h / 12, 1)
    volume_ratio = sells_5m / expected_5m
    volume_confirmed = volume_ratio >= 1.5

    # --- Classification ---
    passes = int(fib_broken) + int(volume_confirmed)
    if passes >= 2:
        classification = "REAL_LOSS"
        reasoning = f"Fib 61.8% broken + sell volume {volume_ratio:.1f}x avg — mechanical breakdown"
    elif passes == 1:
        classification = "UNCONFIRMED_LOSS"
        if fib_broken:
            reasoning = f"Fib broken but volume only {volume_ratio:.1f}x avg — could be wick"
        else:
            reasoning = f"Volume {volume_ratio:.1f}x avg but Fib held — could recover"
    else:
        classification = "UNCONFIRMED_LOSS"
        reasoning = f"Neither Fib nor volume confirmed ({volume_ratio:.1f}x) — likely wick or noise"

    return {
        "classification":   classification,
        "pnl_pct":          round(pnl_pct, 2),
        "fib_618":          fib_618,
        "fib_broken":       fib_broken,
        "volume_ratio":     round(volume_ratio, 2),
        "volume_confirmed": volume_confirmed,
        "sells_5m":         sells_5m,
        "sells_1h":         sells_1h,
        "reasoning":        reasoning,
    }


def log_loss(position: dict, exit_price: float, evaluation: dict):
    """Persist a loss event. Called from position_tracker when a position closes negative."""
    try:
        entry_price = position.get("entry_price") or 0
        entry = {
            "ts":             int(time.time()),
            "mint":           position.get("mint"),
            "symbol":         position.get("symbol"),
            "entry_price":    entry_price,
            "peak_price":     position.get("high_price"),
            "exit_price":     exit_price,
            "size_usd":       position.get("size_usd"),
            "pnl_usd":        round(position.get("size_usd", 0) * (exit_price / entry_price - 1), 2) if entry_price else 0,
            "pnl_pct":        evaluation.get("pnl_pct"),
            "close_reason":   position.get("close_reason"),
            "classification": evaluation.get("classification"),
            "fib_618":        evaluation.get("fib_618"),
            "fib_broken":     evaluation.get("fib_broken"),
            "volume_ratio":   evaluation.get("volume_ratio"),
            "volume_confirmed": evaluation.get("volume_confirmed"),
            "reasoning":      evaluation.get("reasoning"),
        }
        pipe = _redis.pipeline()
        pipe.lpush(K_LOSSES, json.dumps(entry))
        pipe.ltrim(K_LOSSES, 0, MAX_LOSSES - 1)
        pipe.execute()
    except Exception as e:
        log.warning(f"loss_tracker log_loss failed: {e}")


def get_recent_losses(limit: int = 20) -> list:
    try:
        raw = _redis.lrange(K_LOSSES, 0, limit - 1)
        return [json.loads(x) for x in raw if x]
    except Exception:
        return []


def stats() -> dict:
    losses = get_recent_losses(limit=500)
    real = sum(1 for l in losses if l.get("classification") == "REAL_LOSS")
    unconfirmed = sum(1 for l in losses if l.get("classification") == "UNCONFIRMED_LOSS")
    total_pnl = sum(l.get("pnl_usd", 0) or 0 for l in losses)
    return {
        "total":       len(losses),
        "real":        real,
        "unconfirmed": unconfirmed,
        "total_pnl":   round(total_pnl, 2),
    }


def format_losses_list(losses: list) -> str:
    if not losses:
        return "📊 No losses logged yet."
    lines = [f"📊 *Recent losses ({len(losses)})*", ""]
    for l in losses:
        sym = l.get("symbol") or "?"
        pnl = l.get("pnl_usd", 0)
        pct = l.get("pnl_pct", 0)
        cls = l.get("classification") or "?"
        icon = "🔴" if cls == "REAL_LOSS" else "🟡"
        reason = l.get("reasoning", "")
        lines.append(
            f"{icon} *{sym}*  ${pnl:+.2f} ({pct:+.1f}%)  [{cls}]\n"
            f"   _{reason}_"
        )
    return "\n".join(lines)
