"""
Signal engine — the honest version of "prediction as a service".

What the paid tools (Token Metrics, the Solana signal bots) actually do:
collapse a pile of on-chain/flow data into one score and a direction lean.
Their best is ~55-65% directional accuracy on LIQUID coins; worse on memecoins.
And they NEVER show you their real hit rate — that's the whole business model.

This module copies the useful part (multi-signal scoring on REAL data we already
compute in rug_check) and adds the part they leave out: every signal is logged with
its entry price, then resolved against what the token actually did 6h later. So
`/signal stats` shows Shashi his OWN tracked accuracy. If it's a coinflip, he'll know.

NO candle-reading from chart images. NO invented confidence. NO trade execution
(handover rule #2 — 12 risk rules still unconfirmed). RED tokens get no trade card.
"""

import json
import time
import logging

from redis_client import get_redis
from rug_check import check_token, get_geckoterminal_price, get_dexscreener
from trade_card import compute_trade_card, format_trade_card

log = logging.getLogger(__name__)
_redis = get_redis()

# ---------- KEYS ----------
K_STORE  = "signals:store"     # HASH  id -> json(signal)
K_BY_TS  = "signals:by_ts"     # ZSET  id scored by created ts (prune + due iteration)
K_SEQ    = "signals:seq"       # INCR  monotonic id counter

MAX_KEEP        = 500
HORIZON_HRS     = 6            # resolve a signal 6h after it fired
HORIZON_SECS    = HORIZON_HRS * 3600
MOVE_THRESHOLD  = 10.0         # ±% dead-band: smaller move counts as "flat" (NEUTRAL)
MIN_CALIBRATION = 20          # resolved signals per lean before we trust the hit rate

# Lean thresholds on the 0-100 score
BULLISH_AT = 62
BEARISH_AT = 45


# ---------- SCORING ----------

def _score_from_details(verdict: str, d: dict) -> tuple[int, list[str]]:
    """Transparent additive score. Returns (0-100 score, list of factor strings).
    Every point is attributable to a named real-data factor — no black box."""
    score = 50
    factors = []

    def adj(points: int, label: str):
        nonlocal score
        score += points
        sign = "+" if points >= 0 else ""
        factors.append(f"{sign}{points}  {label}")

    # --- Momentum: buy/sell flow across windows ---
    flow = d.get("flow_windows") or []
    ratios = [r[3] for r in flow if r[3] is not None]
    if ratios:
        avg = sum(ratios) / len(ratios)
        if avg >= 60:
            adj(12, f"strong buy flow (avg {avg:.0f}% buys)")
        elif avg >= 55:
            adj(6, f"mild buy flow (avg {avg:.0f}% buys)")
        elif avg >= 45:
            adj(0, f"balanced flow (avg {avg:.0f}% buys)")
        elif avg >= 40:
            adj(-6, f"mild sell flow (avg {avg:.0f}% buys)")
        else:
            adj(-12, f"sell-dominant flow (avg {avg:.0f}% buys)")

    # --- Price momentum (1h) ---
    pc1h = d.get("price_change_1h")
    if pc1h is not None:
        try:
            pc1h = float(pc1h)
            if pc1h > 20:
                adj(8, f"price +{pc1h:.0f}% 1h")
            elif pc1h > 5:
                adj(4, f"price +{pc1h:.0f}% 1h")
            elif pc1h >= -5:
                adj(0, f"price flat ({pc1h:+.0f}% 1h)")
            elif pc1h >= -20:
                adj(-4, f"price {pc1h:.0f}% 1h")
            else:
                adj(-8, f"price {pc1h:.0f}% 1h")
        except (TypeError, ValueError):
            pass

    # --- Exit safety: liquidity vs market cap ---
    liq_mc = d.get("liq_to_mc_pct")
    if liq_mc is not None:
        if liq_mc >= 5:
            adj(8, f"healthy liq:MC ({liq_mc:.1f}%)")
        elif liq_mc >= 3:
            adj(4, f"ok liq:MC ({liq_mc:.1f}%)")
        elif liq_mc >= 1:
            adj(-4, f"thin liq:MC ({liq_mc:.1f}%) — slippage on exit")
        else:
            adj(-10, f"dangerous liq:MC ({liq_mc:.1f}%) — exit crashes price")

    # --- Holder concentration ---
    top10 = d.get("top10_holders_pct")
    if top10 is not None:
        if top10 < 50:
            adj(5, f"distributed (top10 {top10:.0f}%)")
        elif top10 <= 70:
            adj(-5, f"concentrated (top10 {top10:.0f}%)")
        else:
            adj(-12, f"whale-heavy (top10 {top10:.0f}%) — dump risk")

    sniper = d.get("sniper_level")
    if sniper == "HIGH":
        adj(-10, "sniper concentration HIGH")
    elif sniper == "MEDIUM":
        adj(-4, "sniper concentration medium")

    # --- Lifecycle sweet spot (post-grad survivor is Shashi's zone) ---
    stage = d.get("lifecycle_stage")
    if stage == "post_grad":
        adj(6, "post-grad survivor (the sweet spot)")
    elif stage == "established":
        adj(2, "established token")
    elif stage == "just_grad":
        adj(-3, "just-graduated (most volatile)")
    elif stage == "pre_grad":
        adj(-5, "pre-graduation pump.fun")

    # --- Volume interest ---
    vol24 = d.get("volume_24h")
    if vol24 is not None:
        try:
            vol24 = float(vol24)
            if vol24 > 50_000:
                adj(4, f"active (${vol24:,.0f} 24h vol)")
            elif vol24 > 10_000:
                adj(2, f"some volume (${vol24:,.0f} 24h)")
            elif vol24 < 2_000:
                adj(-4, f"near-dead (${vol24:,.0f} 24h vol)")
        except (TypeError, ValueError):
            pass

    # Clamp
    score = max(0, min(100, score))
    return score, factors


def _lean(score: int) -> str:
    if score >= BULLISH_AT:
        return "BULLISH"
    if score < BEARISH_AT:
        return "BEARISH"
    return "NEUTRAL"


# ---------- LOGGING ----------

def _next_id() -> str:
    try:
        return str(_redis.incr(K_SEQ))
    except Exception:
        return str(int(time.time() * 1000))

def _log_signal(entry: dict):
    try:
        sid = entry["id"]
        pipe = _redis.pipeline()
        pipe.hset(K_STORE, sid, json.dumps(entry))
        pipe.zadd(K_BY_TS, {sid: entry["ts"]})
        pipe.execute()
        # Prune oldest beyond MAX_KEEP
        excess = _redis.zcard(K_BY_TS) - MAX_KEEP
        if excess > 0:
            old_ids = _redis.zrange(K_BY_TS, 0, excess - 1)
            if old_ids:
                p2 = _redis.pipeline()
                p2.hdel(K_STORE, *old_ids)
                p2.zrem(K_BY_TS, *old_ids)
                p2.execute()
    except Exception as e:
        log.warning(f"signal log failed: {e}")


# ---------- RESOLUTION (lazy — no background thread, $0) ----------

def _current_price(mint: str):
    """Freshest price for resolution. GeckoTerminal first, DEXScreener fallback."""
    p = get_geckoterminal_price(mint)
    if p:
        return p
    pair = get_dexscreener(mint)
    if pair and pair.get("priceUsd"):
        try:
            return float(pair["priceUsd"])
        except (TypeError, ValueError):
            return None
    return None


def resolve_due_signals(limit: int = 25) -> int:
    """Resolve any unresolved signal whose 6h horizon has passed. Called lazily
    from /signal and /signal stats so we never need a background thread.
    Returns count resolved this pass."""
    now = time.time()
    cutoff = now - HORIZON_SECS
    resolved = 0
    try:
        due_ids = _redis.zrangebyscore(K_BY_TS, 0, cutoff)
    except Exception as e:
        log.warning(f"resolve fetch failed: {e}")
        return 0

    for sid in due_ids:
        if resolved >= limit:
            break
        try:
            raw = _redis.hget(K_STORE, sid)
            if not raw:
                continue
            sig = json.loads(raw)
            if sig.get("resolved"):
                continue
            entry_price = sig.get("entry_price")
            if not entry_price or entry_price <= 0:
                sig["resolved"] = True
                sig["outcome"] = "NO_ENTRY_PRICE"
                _redis.hset(K_STORE, sid, json.dumps(sig))
                continue

            cur = _current_price(sig["mint"])
            sig["resolved"] = True
            sig["resolve_ts"] = int(now)
            if cur is None:
                # Can't price it — almost always means it died / delisted.
                sig["outcome"] = "DEAD"
                sig["move_pct"] = -100.0
                move = -100.0
            else:
                move = (cur - entry_price) / entry_price * 100
                sig["exit_price"] = cur
                sig["move_pct"] = round(move, 1)
                sig["outcome"] = "PRICED"

            lean = sig.get("lean")
            if lean == "BULLISH":
                sig["correct"] = move > MOVE_THRESHOLD
            elif lean == "BEARISH":
                sig["correct"] = move < -MOVE_THRESHOLD
            else:  # NEUTRAL
                sig["correct"] = abs(move) <= MOVE_THRESHOLD

            _redis.hset(K_STORE, sid, json.dumps(sig))
            resolved += 1
        except Exception as e:
            log.warning(f"resolve signal {sid} failed: {e}")
            continue
    return resolved


# ---------- PUBLIC: compute + persist a signal ----------

def generate_signal(mint: str) -> dict:
    """Run the full check, score it, persist for later accuracy tracking.
    Returns a dict the caller formats. Resolves due signals as a side effect."""
    resolve_due_signals()

    result = check_token(mint)
    verdict = result.get("verdict")
    if verdict == "INVALID":
        return {"verdict": "INVALID"}

    d = result.get("details") or {}
    score, factors = _score_from_details(verdict, d)
    lean = _lean(score)

    entry_price = d.get("fresh_price") or d.get("price_usd")
    try:
        entry_price = float(entry_price) if entry_price else None
    except (TypeError, ValueError):
        entry_price = None

    sid = _next_id()
    entry = {
        "id":          sid,
        "ts":          int(time.time()),
        "mint":        mint,
        "symbol":      d.get("symbol"),
        "verdict":     verdict,
        "score":       score,
        "lean":        lean,
        "entry_price": entry_price,
        "horizon_hrs": HORIZON_HRS,
        "resolved":    False,
    }
    # Only persist tradeable-direction signals where we have a price to track against.
    if entry_price:
        _log_signal(entry)

    # Trade card only for GREEN/YELLOW (rule #3 — never size RED).
    card = compute_trade_card(verdict, entry_price) if verdict in ("GREEN", "YELLOW") else None

    return {
        "verdict":     verdict,
        "score":       score,
        "lean":        lean,
        "factors":     factors,
        "details":     d,
        "entry_price": entry_price,
        "calibration": _lean_calibration(lean),
        "trade_card":  card,
        "logged":      bool(entry_price),
    }


# ---------- STATS ----------

def _all_resolved() -> list:
    try:
        raw = _redis.hgetall(K_STORE)
    except Exception:
        return []
    out = []
    for v in raw.values():
        try:
            s = json.loads(v)
            if s.get("resolved") and s.get("outcome") != "NO_ENTRY_PRICE":
                out.append(s)
        except Exception:
            continue
    return out


def _lean_calibration(lean: str) -> dict:
    """Tracked hit rate for signals that shared this lean. The honest confidence."""
    resolved = [s for s in _all_resolved() if s.get("lean") == lean]
    n = len(resolved)
    hits = sum(1 for s in resolved if s.get("correct"))
    rate = (hits / n * 100) if n else None
    return {
        "n":           n,
        "hits":        hits,
        "rate":        rate,
        "calibrated":  n >= MIN_CALIBRATION,
        "need":        MIN_CALIBRATION,
    }


def overall_stats() -> dict:
    resolve_due_signals()
    resolved = _all_resolved()
    try:
        total_logged = _redis.zcard(K_BY_TS)
    except Exception:
        total_logged = len(resolved)
    pending = max(0, total_logged - len(resolved))

    by_lean = {}
    for lean in ("BULLISH", "NEUTRAL", "BEARISH"):
        subset = [s for s in resolved if s.get("lean") == lean]
        n = len(subset)
        hits = sum(1 for s in subset if s.get("correct"))
        by_lean[lean] = {
            "n": n,
            "hits": hits,
            "rate": (hits / n * 100) if n else None,
        }

    n_all = len(resolved)
    hits_all = sum(1 for s in resolved if s.get("correct"))
    return {
        "total_logged": total_logged,
        "resolved":     n_all,
        "pending":      pending,
        "overall_rate": (hits_all / n_all * 100) if n_all else None,
        "by_lean":      by_lean,
        "horizon_hrs":  HORIZON_HRS,
        "min_calib":    MIN_CALIBRATION,
    }


# ---------- FORMATTING ----------

def _fmt_price(p) -> str:
    if not p:
        return "?"
    if p >= 1:
        return f"${p:,.4f}"
    if p >= 0.0001:
        return f"${p:.6f}"
    return f"${p:.9f}"


def format_signal(sig: dict) -> str:
    if sig.get("verdict") == "INVALID":
        return "❌ *Invalid address.* Send a valid Solana mint."

    d = sig["details"]
    score = sig["score"]
    lean = sig["lean"]
    verdict = sig["verdict"]
    lean_icon = {"BULLISH": "📈", "NEUTRAL": "➖", "BEARISH": "📉"}[lean]
    v_icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(verdict, "")

    lines = [
        f"{lean_icon} *SIGNAL: {lean}*  —  score *{score}/100*  {v_icon}{verdict}",
        f"*{d.get('symbol') or '?'}*  ·  MC ${(d.get('market_cap') or 0):,.0f}  ·  liq ${(d.get('liquidity_usd') or 0):,.0f}",
        "",
        "*Why (every point is real data):*",
    ]
    for f in sig["factors"]:
        lines.append(f"   {f}")

    # The honest confidence layer — this is what the paid tools hide.
    cal = sig["calibration"]
    lines.append("")
    if cal["calibrated"]:
        lines.append(
            f"📊 *Tracked accuracy* for past *{lean}* calls: "
            f"*{cal['rate']:.0f}%* ({cal['hits']}/{cal['n']} resolved, {HORIZON_HRS}h horizon)"
        )
        if cal["rate"] is not None and cal["rate"] < 55:
            lines.append("   ⚠️ That's at/below a coinflip. This score has shown NO edge yet — don't trust it.")
    else:
        lines.append(
            f"📊 *Confidence: UNKNOWN.* Only {cal['n']}/{cal['need']} *{lean}* signals have "
            f"resolved so far. Not enough to claim any accuracy. Treat this as a data point, not a call."
        )

    if verdict == "RED":
        lines.append("\n🔴 *RED — no trade card. Never buy a RED token (rule).* Signal logged for tracking only.")
    elif sig.get("trade_card"):
        lines.append(format_trade_card(sig["trade_card"]))

    if sig.get("logged"):
        lines.append(f"\n_Logged. I'll score this against the real {HORIZON_HRS}h move. Check `/signal stats`._")
    else:
        lines.append("\n_No live price — not logged for tracking._")

    lines.append("_Score = on-chain + flow only. It does NOT predict the future. Your exit discipline is the edge._")
    return "\n".join(lines)


def format_stats(s: dict) -> str:
    lines = [
        "📊 *Signal accuracy — your tracked hit rate*",
        "",
        f"Logged: *{s['total_logged']}*  ·  resolved: *{s['resolved']}*  ·  pending: *{s['pending']}*",
        f"_Horizon: {s['horizon_hrs']}h. A call is 'correct' if direction matched a >±{MOVE_THRESHOLD:.0f}% move._",
        "",
    ]
    if s["resolved"] == 0:
        lines.append("No signals have resolved yet. Run `/signal <CA>` and come back after a few hours.")
        return "\n".join(lines)

    if s["overall_rate"] is not None:
        lines.append(f"*Overall: {s['overall_rate']:.0f}%* across {s['resolved']} resolved signals.")
    lines.append("")
    lines.append("*By direction lean:*")
    for lean in ("BULLISH", "NEUTRAL", "BEARISH"):
        b = s["by_lean"][lean]
        if b["n"] == 0:
            lines.append(f"   {lean}: no resolved signals yet")
        else:
            flag = "" if b["n"] >= s["min_calib"] else f"  _(only {b['n']}/{s['min_calib']} — not yet reliable)_"
            lines.append(f"   {lean}: *{b['rate']:.0f}%* ({b['hits']}/{b['n']}){flag}")

    lines.append("")
    if s["overall_rate"] is not None and s["resolved"] >= s["min_calib"] and s["overall_rate"] < 55:
        lines.append("⚠️ *Below 55% = no demonstrated edge.* The paid tools claim 55-65% on liquid coins. "
                     "If yours sits here, the score is noise — trust your rules, not the number.")
    else:
        lines.append("_Keep logging. Below 55% means no edge. This is the number the paid services never show you._")
    return "\n".join(lines)
