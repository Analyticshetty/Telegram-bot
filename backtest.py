"""
Backtest — instant accuracy check on the MOMENTUM HALF of the signal.

Why this exists: /signal logs a call and waits 6h to grade itself. That's honest
but slow. This module instead replays history: take a coin that's already old,
pretend it's N hours ago, score it using ONLY data we can truthfully reconstruct
from the past (price candles + volume + age), then jump forward to see if the call
was right. Instant grade, no waiting.

THE HONEST LIMIT (say it on every output):
We can get historical PRICE for free (GeckoTerminal hourly candles). We canNOT get
the historical on-chain snapshot (buy/sell flow, whale concentration, liquidity at
that past moment) — free APIs only show those as they are NOW. Using "now" data to
score the past would be cheating (the model would half-see the answer). So this
backtest deliberately scores on momentum/volume/age only. A good result here proves
the price-trend logic has some edge — it does NOT prove the full /signal is accurate.
The rug-safety half is still validated only by the live 6h tracker in signal_engine.
"""

import logging
import requests

from signal_engine import MOVE_THRESHOLD, BULLISH_AT, BEARISH_AT, _lean, HORIZON_HRS

log = logging.getLogger(__name__)

TIMEOUT = 10
GT = "https://api.geckoterminal.com/api/v2"
GT_HEADERS = {"Accept": "application/json;version=20230302"}

CANDLE_LIMIT   = 72   # hours of history to pull (3 days)
MIN_LOOKBACK   = 4    # candles needed before a point to score momentum
SWEEP_COINS    = 6    # how many trending coins /backtest sweep uses


# ---------- DATA (historical price — the part we CAN replay) ----------

def get_pool_for_mint(mint: str):
    """Top GeckoTerminal pool address for a token mint. Returns address or None."""
    try:
        r = requests.get(f"{GT}/networks/solana/tokens/{mint}/pools",
                          headers=GT_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        data = (r.json() or {}).get("data") or []
        if not data:
            return None
        # GeckoTerminal returns pools ranked; take the first (deepest liquidity).
        return (data[0].get("attributes") or {}).get("address")
    except Exception as e:
        log.warning(f"get_pool_for_mint failed: {e}")
        return None


def fetch_ohlcv_hourly(pool: str, limit: int = CANDLE_LIMIT) -> list:
    """Return list of candles ascending by time: [{ts, close, vol}, ...]."""
    try:
        r = requests.get(
            f"{GT}/networks/solana/pools/{pool}/ohlcv/hour",
            params={"aggregate": 1, "limit": limit, "currency": "usd"},
            headers=GT_HEADERS, timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        rows = (((r.json() or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        out = []
        for row in rows:
            # [timestamp, open, high, low, close, volume]
            if not row or len(row) < 6:
                continue
            try:
                out.append({"ts": int(row[0]), "close": float(row[4]), "vol": float(row[5] or 0)})
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda c: c["ts"])  # oldest first
        return out
    except Exception as e:
        log.warning(f"fetch_ohlcv_hourly failed: {e}")
        return []


# ---------- MOMENTUM-ONLY SCORE (only what history can honestly provide) ----------

def momentum_score(closes: list, vols: list, i: int) -> int:
    """Score 0-100 at candle index i using ONLY price/volume up to i.
    Mirrors the spirit of signal_engine's momentum factors, nothing on-chain."""
    score = 50

    # 1h price change
    if i >= 1 and closes[i - 1] > 0:
        pc1 = (closes[i] - closes[i - 1]) / closes[i - 1] * 100
        if pc1 > 20:   score += 8
        elif pc1 > 5:  score += 4
        elif pc1 >= -5: score += 0
        elif pc1 >= -20: score -= 4
        else: score -= 8

    # 3h trend (confirmation)
    if i >= 3 and closes[i - 3] > 0:
        pc3 = (closes[i] - closes[i - 3]) / closes[i - 3] * 100
        if pc3 > 30:   score += 8
        elif pc3 > 10: score += 4
        elif pc3 >= -10: score += 0
        elif pc3 >= -30: score -= 4
        else: score -= 8

    # Volume trend: last 2 candles vs prior 4
    if i >= 5:
        recent = (vols[i] + vols[i - 1]) / 2
        prior = sum(vols[i - 5:i - 1]) / 4
        if prior > 0:
            ratio = recent / prior
            if ratio > 1.5:   score += 6
            elif ratio > 1.1: score += 3
            elif ratio < 0.7: score -= 4

    return max(0, min(100, score))


def _is_correct(lean: str, move_pct: float) -> bool:
    if lean == "BULLISH":
        return move_pct > MOVE_THRESHOLD
    if lean == "BEARISH":
        return move_pct < -MOVE_THRESHOLD
    return abs(move_pct) <= MOVE_THRESHOLD


# ---------- BACKTEST ENGINE ----------

def backtest_candles(candles: list, horizon: int = HORIZON_HRS) -> dict:
    """Walk every scorable point, grade against the actual `horizon`-hours-later move.
    Returns aggregate + per-lean counts. Windows overlap (not independent)."""
    closes = [c["close"] for c in candles]
    vols = [c["vol"] for c in candles]
    n = len(candles)

    by_lean = {"BULLISH": [0, 0], "NEUTRAL": [0, 0], "BEARISH": [0, 0]}  # [hits, total]
    samples = 0
    hits = 0

    for i in range(MIN_LOOKBACK, n - horizon):
        if closes[i] <= 0:
            continue
        s = momentum_score(closes, vols, i)
        lean = _lean(s)
        move = (closes[i + horizon] - closes[i]) / closes[i] * 100
        correct = _is_correct(lean, move)
        by_lean[lean][1] += 1
        if correct:
            by_lean[lean][0] += 1
            hits += 1
        samples += 1

    return {
        "samples": samples,
        "hits": hits,
        "rate": (hits / samples * 100) if samples else None,
        "by_lean": {k: {"hits": v[0], "total": v[1],
                        "rate": (v[0] / v[1] * 100) if v[1] else None}
                    for k, v in by_lean.items()},
        "horizon": horizon,
    }


def backtest_token(mint: str) -> dict:
    pool = get_pool_for_mint(mint)
    if not pool:
        return {"error": "No GeckoTerminal pool found for this CA."}
    candles = fetch_ohlcv_hourly(pool)
    if len(candles) < MIN_LOOKBACK + HORIZON_HRS + 1:
        return {"error": f"Not enough price history ({len(candles)} candles) to backtest."}
    res = backtest_candles(candles)
    res["mint"] = mint
    res["candles"] = len(candles)
    return res


def get_trending_pools(limit: int = SWEEP_COINS) -> list:
    """Return [{pool, symbol}] from GeckoTerminal Solana trending."""
    try:
        r = requests.get(f"{GT}/networks/solana/trending_pools",
                         params={"page": 1}, headers=GT_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        data = (r.json() or {}).get("data") or []
        out = []
        for p in data[:limit]:
            attrs = p.get("attributes") or {}
            addr = attrs.get("address")
            name = attrs.get("name") or "?"
            if addr:
                out.append({"pool": addr, "symbol": name})
        return out
    except Exception as e:
        log.warning(f"get_trending_pools failed: {e}")
        return []


def backtest_sweep(n_coins: int = SWEEP_COINS) -> dict:
    pools = get_trending_pools(n_coins)
    if not pools:
        return {"error": "Couldn't fetch trending coins to sweep."}

    by_lean = {"BULLISH": [0, 0], "NEUTRAL": [0, 0], "BEARISH": [0, 0]}
    total_samples = 0
    total_hits = 0
    coins_used = 0
    coin_rows = []

    for p in pools:
        candles = fetch_ohlcv_hourly(p["pool"])
        if len(candles) < MIN_LOOKBACK + HORIZON_HRS + 1:
            continue
        res = backtest_candles(candles)
        if res["samples"] == 0:
            continue
        coins_used += 1
        total_samples += res["samples"]
        total_hits += res["hits"]
        for k in by_lean:
            by_lean[k][0] += res["by_lean"][k]["hits"]
            by_lean[k][1] += res["by_lean"][k]["total"]
        coin_rows.append({"symbol": p["symbol"], "samples": res["samples"], "rate": res["rate"]})

    return {
        "coins_used": coins_used,
        "samples": total_samples,
        "hits": total_hits,
        "rate": (total_hits / total_samples * 100) if total_samples else None,
        "by_lean": {k: {"hits": v[0], "total": v[1],
                        "rate": (v[0] / v[1] * 100) if v[1] else None}
                    for k, v in by_lean.items()},
        "coins": coin_rows,
        "horizon": HORIZON_HRS,
    }


# ---------- FORMATTING ----------

_DISCLAIMER = (
    "_⚠️ Momentum-only backtest: scores past PRICE/VOLUME/age, the part we can honestly "
    "replay. It does NOT include the rug-safety half of /signal (whales, liquidity, flow "
    "weren't recordable for the past). A good number here ≠ /signal is accurate. Windows "
    "overlap, so samples aren't independent — treat it as a smell test, not proof._"
)


def _lean_lines(by_lean: dict, min_note: int = 30) -> list:
    lines = []
    for lean in ("BULLISH", "NEUTRAL", "BEARISH"):
        b = by_lean[lean]
        if b["total"] == 0:
            lines.append(f"   {lean}: no samples")
        else:
            lines.append(f"   {lean}: *{b['rate']:.0f}%* ({b['hits']}/{b['total']})")
    return lines


def format_token_backtest(res: dict) -> str:
    if res.get("error"):
        return f"⚠️ {res['error']}"
    lines = [
        f"⏪ *Backtest — momentum half only* ({res['horizon']}h horizon)",
        f"Coin: `{res['mint'][:8]}...{res['mint'][-4:]}`  ·  {res['candles']} hours of price history",
        "",
        f"*Hit rate: {res['rate']:.0f}%* over {res['samples']} replayed moments"
        if res["rate"] is not None else "No scorable moments.",
        "",
        "*By direction lean:*",
        *_lean_lines(res["by_lean"]),
        "",
    ]
    if res.get("rate") is not None:
        if res["rate"] < 55:
            lines.append("📉 *Below 55% = the momentum logic shows no edge here.* Don't lean on the score.")
        else:
            lines.append("📈 Above 55% on momentum alone — worth a closer look, but see the caveat.")
    lines.append("")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def format_sweep(res: dict) -> str:
    if res.get("error"):
        return f"⚠️ {res['error']}"
    lines = [
        f"⏪ *Backtest sweep — momentum half only* ({res['horizon']}h horizon)",
        f"Across *{res['coins_used']}* trending coins  ·  *{res['samples']}* replayed moments",
        "",
        f"*Overall hit rate: {res['rate']:.0f}%*"
        if res["rate"] is not None else "No scorable moments.",
        "",
        "*By direction lean:*",
        *_lean_lines(res["by_lean"]),
        "",
        "*Per coin:*",
    ]
    for c in res.get("coins", []):
        rate = f"{c['rate']:.0f}%" if c["rate"] is not None else "n/a"
        lines.append(f"   {c['symbol']}: {rate} ({c['samples']} pts)")
    lines.append("")
    if res.get("rate") is not None:
        if res["rate"] < 55:
            lines.append("📉 *Below 55% across the board = momentum logic has no demonstrated edge.* This is the honest read.")
        else:
            lines.append("📈 Above 55% — promising, but it's only the momentum half. Verify with the live tracker.")
    lines.append("")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)
