"""
Position Tracker (Module #2 in roadmap).

Watches your open positions in the background and alerts when TP1 / TP2 / SL hit.
You execute the actual trade on Bitget — bot only enforces exit discipline.

Workflow:
  /buy <CA> [size_usd] [entry_price]   → open position (auto-fills from live price)
  /positions                            → list open
  /closed                               → list closed
  /sell <CA>                            → mark closed manually
  Bot polls every 60s and pings TP1 / TP2 / SL.

Storage:
  positions:open    → list of dicts (one per open position)
  positions:closed  → list of last 100 closed positions (history)

Rules (from trade_card.py):
  TP1 = 2x entry, sell 50% (recover cost basis)
  TP2 = 3x entry, sell rest (free ride)
  SL  = -30% (cut loss)
"""

import json
import time
import logging
import threading
import requests
import loss_tracker
from redis_client import get_redis

log = logging.getLogger(__name__)
_redis = get_redis()

# ---------- CONFIG ----------
POLL_INTERVAL_SECS = 60        # check prices every minute
TP1_MULT = 2.0
TP1_SELL_PCT = 0.50
TP2_MULT = 3.0
SL_PCT   = -0.30
MAX_CLOSED_KEEP = 100
TIMEOUT = 8

K_OPEN   = "positions:open"
K_CLOSED = "positions:closed"

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
GECKO_URL       = "https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}"

# ---------- STATE ----------
_running = False
_thread  = None
_lock    = threading.Lock()


# ---------- PRICE FETCH ----------

def get_live_price(mint: str):
    """Fresh price: try DEXScreener first, fall back to GeckoTerminal. Returns float or None."""
    try:
        r = requests.get(DEXSCREENER_URL.format(mint=mint), timeout=TIMEOUT)
        if r.status_code == 200:
            pairs = (r.json() or {}).get("pairs") or []
            if pairs:
                pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
                px = pairs[0].get("priceUsd")
                if px:
                    return float(px)
    except Exception:
        pass
    try:
        r = requests.get(
            GECKO_URL.format(mint=mint),
            timeout=TIMEOUT,
            headers={"Accept": "application/json;version=20230302"},
        )
        if r.status_code == 200:
            attrs = ((r.json() or {}).get("data") or {}).get("attributes") or {}
            px = attrs.get("price_usd")
            if px:
                return float(px)
    except Exception:
        pass
    return None


def get_price_and_mc(mint: str):
    """Return (price_usd, market_cap_usd) from the top-liquidity DEXScreener pair.
    market_cap falls back to fdv. Either value may be None if unavailable."""
    try:
        r = requests.get(DEXSCREENER_URL.format(mint=mint), timeout=TIMEOUT)
        if r.status_code == 200:
            pairs = (r.json() or {}).get("pairs") or []
            if pairs:
                pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
                top = pairs[0]
                px = top.get("priceUsd")
                mc = top.get("marketCap") or top.get("fdv")
                return (float(px) if px else None, float(mc) if mc else None)
    except Exception:
        pass
    return (None, None)


def get_token_symbol(mint: str):
    """Best-effort symbol lookup."""
    try:
        r = requests.get(DEXSCREENER_URL.format(mint=mint), timeout=TIMEOUT)
        if r.status_code == 200:
            pairs = (r.json() or {}).get("pairs") or []
            if pairs:
                return (pairs[0].get("baseToken") or {}).get("symbol") or "?"
    except Exception:
        pass
    return "?"


# ---------- STORAGE ----------

def _load_open() -> list:
    try:
        raw = _redis.get(K_OPEN)
        return json.loads(raw) if raw else []
    except Exception as e:
        log.warning(f"positions load_open failed: {e}")
        return []


def _save_open(positions: list):
    try:
        _redis.set(K_OPEN, json.dumps(positions))
    except Exception as e:
        log.warning(f"positions save_open failed: {e}")


def _load_closed() -> list:
    try:
        raw = _redis.get(K_CLOSED)
        return json.loads(raw) if raw else []
    except Exception:
        return []


def _push_closed(position: dict):
    try:
        closed = _load_closed()
        closed.insert(0, position)
        closed = closed[:MAX_CLOSED_KEEP]
        _redis.set(K_CLOSED, json.dumps(closed))
    except Exception as e:
        log.warning(f"positions push_closed failed: {e}")


# ---------- POSITION CRUD ----------

def open_position(mint: str, size_usd: float, entry_price: float = None) -> dict:
    """Open a position. If entry_price omitted, uses current live price."""
    mint = mint.strip()
    if entry_price is None:
        entry_price = get_live_price(mint)
    if not entry_price or entry_price <= 0:
        return {"ok": False, "error": "Could not fetch live price for this CA"}

    symbol = get_token_symbol(mint)
    tokens = size_usd / entry_price

    position = {
        "mint":         mint,
        "symbol":       symbol,
        "size_usd":     round(float(size_usd), 4),
        "entry_price":  float(entry_price),
        "tokens":       tokens,
        "tp1_price":    entry_price * TP1_MULT,
        "tp2_price":    entry_price * TP2_MULT,
        "sl_price":     entry_price * (1 + SL_PCT),
        "tp1_hit":      False,
        "opened_at":    int(time.time()),
        "status":       "OPEN",
        "high_price":   entry_price,    # track peak for trailing context
    }

    with _lock:
        positions = _load_open()
        # Replace any existing open position on same CA
        positions = [p for p in positions if p.get("mint") != mint]
        positions.append(position)
        _save_open(positions)

    return {"ok": True, "position": position}


def close_position(mint: str, reason: str = "manual", exit_price: float = None) -> dict:
    """Mark a position closed and move to history."""
    with _lock:
        positions = _load_open()
        target = next((p for p in positions if p.get("mint") == mint), None)
        if not target:
            return {"ok": False, "error": "No open position with that CA"}
        positions = [p for p in positions if p.get("mint") != mint]
        _save_open(positions)

        if exit_price is None:
            exit_price = get_live_price(mint) or target.get("entry_price")
        target["status"]      = "CLOSED"
        target["close_reason"] = reason
        target["closed_at"]   = int(time.time())
        target["exit_price"]  = float(exit_price)
        # Realized P&L assuming TP1 partial already happened if marked
        entry = target["entry_price"]
        size  = target["size_usd"]
        if target.get("tp1_hit"):
            # 50% sold at 2x, 50% sold at exit_price
            pnl = (size * 0.5 * TP1_MULT) + (size * 0.5 * (exit_price / entry)) - size
        else:
            pnl = size * (exit_price / entry) - size
        target["pnl_usd"] = round(pnl, 2)
        target["pnl_pct"] = round((pnl / size) * 100, 1) if size else 0
        _push_closed(target)

    # Loss tracker — evaluate every closed position with negative P&L
    # Pure data collection, no actions taken (per design)
    try:
        if target.get("pnl_usd", 0) < 0:
            evaluation = loss_tracker.evaluate(target, exit_price)
            loss_tracker.log_loss(target, exit_price, evaluation)
    except Exception as e:
        log.warning(f"loss_tracker evaluation failed: {e}")

    return {"ok": True, "position": target}


def list_open() -> list:
    return _load_open()


def list_closed(limit: int = 20) -> list:
    return _load_closed()[:limit]


def get_position(mint: str) -> dict | None:
    for p in _load_open():
        if p.get("mint") == mint:
            return p
    return None


# ---------- MONITOR LOOP ----------

def _check_position(p: dict, send_alert_fn) -> tuple[dict, bool]:
    """Returns (updated_position, should_close)."""
    mint  = p["mint"]
    sym   = p.get("symbol") or mint[:6]
    entry = p["entry_price"]
    price = get_live_price(mint)
    if not price:
        return (p, False)

    # Track peak
    if price > (p.get("high_price") or entry):
        p["high_price"] = price

    pct = (price / entry - 1) * 100
    should_close = False

    # SL — always fires regardless of TP1 state
    if price <= p["sl_price"]:
        send_alert_fn(
            f"🛑 *SL HIT — {sym}*\n\n"
            f"📋 CA: `{mint}`\n"
            f"📉 Price: ${price:.8f}  ({pct:+.1f}%)\n"
            f"🛑 SL was: ${p['sl_price']:.8f} (-30%)\n"
            f"💸 Size: ${p['size_usd']:.2f}\n\n"
            f"_Exit on Bitget NOW. Discipline > hope._"
        )
        should_close = True
        p["close_reason"] = "SL"
        return (p, should_close)

    # TP1 — first time crossing 2x
    if not p.get("tp1_hit") and price >= p["tp1_price"]:
        p["tp1_hit"] = True
        send_alert_fn(
            f"🎯 *TP1 HIT — {sym}*\n\n"
            f"📋 CA: `{mint}`\n"
            f"📈 Price: ${price:.8f}  ({pct:+.1f}%)\n"
            f"🎯 TP1 was: ${p['tp1_price']:.8f} (2x)\n\n"
            f"*ACTION: Sell 50% on Bitget NOW*\n"
            f"   → Recovers cost basis. Rest rides free toward 3x.\n"
            f"_Your edge. Don't get greedy. Take it._"
        )
        # Keep position open — TP2 still pending
        return (p, False)

    # TP2 — second target
    if p.get("tp1_hit") and price >= p["tp2_price"]:
        send_alert_fn(
            f"🎯 *TP2 HIT — {sym}*\n\n"
            f"📋 CA: `{mint}`\n"
            f"📈 Price: ${price:.8f}  ({pct:+.1f}%)\n"
            f"🎯 TP2 was: ${p['tp2_price']:.8f} (3x)\n\n"
            f"*ACTION: Sell the rest on Bitget NOW*\n"
            f"_Full exit. Closing position in tracker._"
        )
        should_close = True
        p["close_reason"] = "TP2"
        return (p, should_close)

    return (p, False)


def _loop(send_alert_fn):
    global _running
    log.info("Position tracker loop started.")
    while _running:
        try:
            positions = _load_open()
            if positions:
                updated_open = []
                for p in positions:
                    new_p, should_close = _check_position(p, send_alert_fn)
                    if should_close:
                        # Move to closed
                        close_position(new_p["mint"], reason=new_p.get("close_reason", "auto"))
                    else:
                        updated_open.append(new_p)
                # Persist any in-place updates (tp1_hit, high_price) for non-closed
                if updated_open:
                    # Reload fresh (close_position may have mutated)
                    current = {p["mint"]: p for p in _load_open()}
                    for u in updated_open:
                        current[u["mint"]] = u
                    _save_open(list(current.values()))
        except Exception as e:
            log.warning(f"Position tracker scan error: {e}")

        # Sleep in 1s chunks so stop() responds quickly
        for _ in range(POLL_INTERVAL_SECS):
            if not _running:
                break
            time.sleep(1)
    log.info("Position tracker loop stopped.")


def start(send_alert_fn):
    global _running, _thread
    if _running:
        return False
    _running = True
    _thread = threading.Thread(target=_loop, args=(send_alert_fn,), daemon=True)
    _thread.start()
    return True


def stop():
    global _running
    _running = False


def is_running() -> bool:
    return _running


# ---------- FORMATTERS ----------

def _fmt_price(p):
    if p is None: return "?"
    if p >= 1: return f"${p:,.4f}"
    if p >= 0.01: return f"${p:.4f}"
    if p >= 0.0001: return f"${p:.6f}"
    return f"${p:.9f}"


def _ago(ts):
    if not ts: return "?"
    secs = max(0, int(time.time() - ts))
    if secs < 60: return f"{secs}s"
    if secs < 3600: return f"{secs // 60}m"
    if secs < 86400: return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def format_position(p: dict, live_price: float = None) -> str:
    """Render a single open position."""
    sym = p.get("symbol") or "?"
    entry = p["entry_price"]
    size  = p["size_usd"]
    if live_price is None:
        live_price = get_live_price(p["mint"]) or entry
    pct = (live_price / entry - 1) * 100
    tp1_status = "✅ HIT" if p.get("tp1_hit") else "⏳"
    tp2_status = "⏳"
    sl_status  = "⏳"
    mood = "🟢" if pct > 0 else "🔴" if pct < -10 else "⚪"
    return (
        f"{mood} *{sym}* ({_ago(p.get('opened_at'))} ago)\n"
        f"   `{p['mint']}`\n"
        f"   💸 Size: ${size:.2f} | Entry: {_fmt_price(entry)}\n"
        f"   📈 Now:  {_fmt_price(live_price)}  ({pct:+.1f}%)\n"
        f"   🎯 TP1 {_fmt_price(p['tp1_price'])} {tp1_status}\n"
        f"   🎯 TP2 {_fmt_price(p['tp2_price'])} {tp2_status}\n"
        f"   🛑 SL  {_fmt_price(p['sl_price'])} {sl_status}"
    )


def format_open_list(positions: list) -> str:
    if not positions:
        return "📂 No open positions.\n\nOpen with: `/buy <CA> [size_usd]`"
    lines = [f"📂 *Open positions* ({len(positions)})", ""]
    for p in positions:
        lines.append(format_position(p))
        lines.append("")
    return "\n".join(lines).rstrip()


def format_closed_list(positions: list) -> str:
    if not positions:
        return "📁 No closed positions yet."
    lines = [f"📁 *Closed positions* ({len(positions)})", ""]
    for p in positions:
        sym = p.get("symbol") or "?"
        pnl = p.get("pnl_usd", 0)
        pnl_pct = p.get("pnl_pct", 0)
        reason = p.get("close_reason", "?")
        icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        lines.append(
            f"{icon} *{sym}* — {reason} | P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)"
        )
    return "\n".join(lines)
