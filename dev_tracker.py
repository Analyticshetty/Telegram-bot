"""
Dev Sell Tracker — fires when a token creator sells on pump.fun.

How it works:
  Every 3 minutes, fetches the top 50 pump.fun coins by market cap.
  For each, pulls last 20 trades and checks if the creator wallet is selling.
  On detection → Telegram alert with coin, MC, SOL sold, and a clear warning.

Why this matters (from Plon Bot's feature):
  Dev sells are the earliest on-chain signal that the person who launched
  the token is exiting. Price often pumps briefly on FOMO (retail buys the dip
  the dev created), then crashes. Catching this fast gives you an exit window.

Storage (Redis):
  dev_tracker:alerted:{mint}   = "1", TTL 4h  (don't re-alert same coin)
  dev_tracker:status           = JSON status snapshot

Cost: $0 — pump.fun API is public. No API key needed.
"""

import json
import time
import logging
import threading
import requests
from redis_client import get_redis

log = logging.getLogger(__name__)
_redis = get_redis()

PUMP_API      = "https://frontend-api.pump.fun"
SCAN_INTERVAL = 180           # 3 minutes
TOP_COINS     = 50            # watch top 50 by market cap
TRADE_LIMIT   = 20            # last N trades to check per coin
MIN_MC        = 10_000        # ignore micro coins < $10k MC
MIN_SOL_SOLD  = 0.1           # ignore dust sells < 0.1 SOL
ALERT_TTL     = 4 * 3600      # 4h dedup window per coin
TIMEOUT       = 8

# ---------- STATE ----------
_running   = False
_thread    = None
_alert_fn  = None
_scans     = 0
_last_scan = None
_last_alerts = 0


# ---------- PUMP.FUN API ----------

def _get_top_coins(limit: int = TOP_COINS) -> list:
    """Fetch top pump.fun coins by market cap. Returns list of coin dicts."""
    try:
        r = requests.get(
            f"{PUMP_API}/coins",
            params={
                "offset": 0,
                "limit":  limit,
                "sort":   "market_cap",
                "order":  "DESC",
                "includeNsfw": "false",
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f"dev_tracker get_top_coins: {e}")
        return []


def _get_trades(mint: str, limit: int = TRADE_LIMIT) -> list:
    """Fetch recent trades for a token. Returns list of trade dicts."""
    try:
        r = requests.get(
            f"{PUMP_API}/coins/{mint}/trades",
            params={"offset": 0, "limit": limit},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f"dev_tracker get_trades {mint}: {e}")
        return []


def _get_coin_info(mint: str) -> dict:
    """Fetch full coin info including creator field."""
    try:
        r = requests.get(f"{PUMP_API}/coins/{mint}", timeout=TIMEOUT)
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except Exception as e:
        log.warning(f"dev_tracker get_coin_info {mint}: {e}")
        return {}


# ---------- DETECTION ----------

def _find_dev_sells(coin: dict, trades: list) -> list:
    """Return list of dev-sell trade dicts. Empty = no dev sell detected.

    A dev sell is: trade.user == coin.creator AND is_buy == False.
    We require at least MIN_SOL_SOLD SOL to filter out dust/fee transactions."""
    creator = (coin.get("creator") or "").strip()
    if not creator:
        return []

    sells = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        if (t.get("is_buy") is False or t.get("is_buy") == "false" or t.get("is_buy") == 0):
            user = (t.get("user") or "").strip()
            if user == creator:
                raw_sol = float(t.get("sol_amount") or 0)
                # pump.fun returns lamports (1 SOL = 1e9); normalise
                sol = raw_sol / 1e9 if raw_sol > 1000 else raw_sol
                if sol >= MIN_SOL_SOLD:
                    raw_tok = float(t.get("token_amount") or 0)
                    sells.append({
                        "user":       user,
                        "sol_amount": sol,
                        "token_amount": float(t.get("token_amount") or 0),
                        "ts":         t.get("timestamp") or int(time.time() * 1000),
                    })
    return sells


def _already_alerted(mint: str) -> bool:
    try:
        return bool(_redis.get(f"dev_tracker:alerted:{mint}"))
    except Exception:
        return False


def _mark_alerted(mint: str):
    try:
        _redis.set(f"dev_tracker:alerted:{mint}", "1", ex=ALERT_TTL)
    except Exception as e:
        log.warning(f"dev_tracker mark_alerted: {e}")


# ---------- ALERT FORMAT ----------

def _format_alert(coin: dict, sells: list) -> str:
    mint   = coin.get("mint") or "?"
    name   = coin.get("name") or "?"
    symbol = coin.get("symbol") or "?"
    mc     = float(coin.get("usd_market_cap") or 0)

    total_sol = sum(s["sol_amount"] for s in sells)
    n_sells   = len(sells)

    # Estimate % of supply sold (rough — token_amount / total_supply if available)
    supply = float(coin.get("total_supply") or 0)
    tokens_sold = sum(s["token_amount"] for s in sells)
    pct_str = ""
    if supply > 0 and tokens_sold > 0:
        pct = tokens_sold / supply * 100
        pct_str = f" ({pct:.1f}% of supply)"

    age_ms = coin.get("created_timestamp")
    age_str = "?"
    if age_ms:
        age_min = (time.time() * 1000 - age_ms) / 60000
        age_str = f"{age_min:.0f}m" if age_min < 60 else f"{age_min/60:.1f}h"

    lines = [
        f"🚨 *DEV SELL — {symbol}*",
        "",
        f"📋 CA: `{mint}`",
        f"💰 MC: ${mc:,.0f}  ·  Age: {age_str}",
        f"",
        f"🔴 Dev sold *{total_sol:.2f} SOL*{pct_str} across {n_sells} tx{'s' if n_sells>1 else ''}",
        "",
        "⚡ *What usually happens next:*",
        "   Retail FOMO buys the dip → brief pump → then hard dump as dev sells more.",
        "   If you're holding: consider exiting NOW, not after the bounce.",
        "",
        f"🔗 [Pump.fun](https://pump.fun/{mint})  ·  Run `/check {mint}` to re-check",
        "_Advisory only. Dev sells sometimes are partial, not full exits._",
    ]
    return "\n".join(lines)


# ---------- SCAN LOOP ----------

def _scan_once() -> int:
    """One full scan. Returns number of dev-sell alerts fired."""
    alerts = 0
    coins = _get_top_coins()
    if not coins:
        log.warning("dev_tracker: pump.fun returned empty")
        return 0

    for coin in coins:
        mint = (coin.get("mint") or "").strip()
        mc   = float(coin.get("usd_market_cap") or 0)

        if not mint or mc < MIN_MC:
            continue
        if _already_alerted(mint):
            continue

        # Creator might not be in the list response — fall back to full fetch
        if not coin.get("creator"):
            info = _get_coin_info(mint)
            coin = {**coin, **info}   # merge; full info wins
            time.sleep(0.5)

        if not coin.get("creator"):
            continue   # can't detect without knowing the creator

        trades = _get_trades(mint)
        if not trades:
            time.sleep(0.3)
            continue

        dev_sells = _find_dev_sells(coin, trades)
        if not dev_sells:
            time.sleep(0.3)
            continue

        # Fire alert
        alert_text = _format_alert(coin, dev_sells)
        try:
            _alert_fn(alert_text)
        except Exception as e:
            log.warning(f"dev_tracker alert send failed: {e}")

        _mark_alerted(mint)
        alerts += 1
        log.info(f"dev_tracker: dev sell alert fired for {coin.get('symbol')} ({mint})")
        time.sleep(1)

    return alerts


def _loop():
    global _running, _scans, _last_scan, _last_alerts
    log.info("Dev tracker started.")
    _save_status()
    while _running:
        try:
            _last_alerts = _scan_once()
            _last_scan   = time.time()
            _scans      += 1
            _save_status()
            log.info(f"Dev tracker scan #{_scans} — {_last_alerts} dev sell alerts")
        except Exception as e:
            log.warning(f"Dev tracker scan error: {e}")
        for _ in range(SCAN_INTERVAL):
            if not _running:
                break
            time.sleep(1)
    log.info("Dev tracker stopped.")
    _save_status()


def _save_status():
    try:
        _redis.set("dev_tracker:status", json.dumps({
            "running":      _running,
            "scans":        _scans,
            "last_scan":    _last_scan,
            "last_alerts":  _last_alerts,
        }))
    except Exception:
        pass


# ---------- PUBLIC API ----------

def start(alert_fn):
    """Start background thread. alert_fn(text) sends Telegram message."""
    global _running, _thread, _alert_fn
    if _running:
        return False
    _alert_fn = alert_fn
    _running  = True
    _thread   = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    return True


def stop():
    global _running
    _running = False


def is_running() -> bool:
    return _running


def get_status() -> dict:
    mins_ago = round((time.time() - _last_scan) / 60, 1) if _last_scan else None
    return {
        "running":     _running,
        "scans":       _scans,
        "mins_ago":    mins_ago,
        "last_alerts": _last_alerts,
        "interval_m":  SCAN_INTERVAL // 60,
        "watching":    TOP_COINS,
    }
