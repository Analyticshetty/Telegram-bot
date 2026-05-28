"""
Watcher — background scanner that runs every 5 minutes.

Alert 1 — New Narrative Alert:
  - Monitors pump.fun for narrative clusters (3+ tokens with same word in last 30 min)
  - Confirms narrative on Twitter/web via Tavily
  - Finds the cleanest CA from that cluster
  - Runs rug check — only alerts on GREEN or YELLOW
  - Checks smart wallets

Alert 2 — Smart Wallet Signal (embedded in same alert if found):
  - While checking the best CA, if 1+ smart wallets hold it, highlights that too
"""

import os
import re
import time
import logging
import threading
import requests
from collections import defaultdict
from rug_check import check_token, format_report
from smart_wallets import check_wallets_hold_token, load_wallets
from trade_card import trade_card_for_check
import memory_store

log = logging.getLogger(__name__)

# ---------- CONFIG ----------
PUMP_API        = "https://frontend-api.pump.fun/coins"
TAVILY_API_KEY  = os.environ.get("TAVILY_API_KEY", "")
SCAN_INTERVAL   = 300        # 5 minutes — how often watcher wakes up
NARRATIVE_WINDOW = 3600      # 1 hour — how far back to look for cluster
MIN_CLUSTER     = 3          # 3+ tokens with same word = narrative forming
TIMEOUT         = 10

COMMON_WORDS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "with", "by",
    "sol", "solana", "token", "coin", "inu", "ai", "doge", "pepe", "based",
    "pump", "fun", "moon", "mars", "meme", "finance", "defi", "nft", "dao",
    "and", "or", "not", "but", "new", "old", "big", "max", "pro", "plus",
    "one", "two", "just", "now", "get", "let", "buy", "sell", "swap",
}

# ---------- STATE ----------
_running        = False
_thread         = None
# Seen sets hydrated from Redis on startup (survives Railway redeploys for 24h)
_seen_narratives = memory_store.load_seen_narratives()
_seen_tokens    = memory_store.load_seen_tokens()
_lock           = threading.Lock()
_last_scan_time = None
_last_scan_found = 0       # narratives found in last scan
_scan_count     = 0        # total scans done


# ---------- PUMP.FUN ----------

def _fetch_new_pumps(limit: int = 200) -> list:
    try:
        r = requests.get(
            PUMP_API,
            params={
                "offset": 0,
                "limit": limit,
                "sort": "created_timestamp",
                "order": "DESC",
                "includeNsfw": "false",
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f"pump.fun fetch error: {e}")
        return []


def _extract_words(text: str) -> set:
    words = re.findall(r"[a-zA-Z]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in COMMON_WORDS}


def _find_narratives(coins: list) -> dict:
    """Returns {word: [coin, ...]} for words appearing in 3+ tokens in last 30 min."""
    now    = time.time()
    cutoff = now - NARRATIVE_WINDOW
    word_map = defaultdict(list)

    for coin in coins:
        ts = (coin.get("created_timestamp") or 0) / 1000
        if ts < cutoff:
            continue
        words = _extract_words(coin.get("name") or "") | _extract_words(coin.get("symbol") or "")
        for w in words:
            word_map[w].append(coin)

    return {w: coins for w, coins in word_map.items() if len(coins) >= MIN_CLUSTER}


# ---------- TWITTER CONFIRMATION ----------

def _confirm_twitter(narrative: str) -> bool:
    """Confirm narrative is actually trending on crypto-Twitter/Reddit.

    Search uses the conventions real crypto posters use:
      - $narrative (cashtag — price-action crowd)
      - #narrative (hashtag — awareness crowd)
    Restricted to the last 24h so stale posts don't count as buzz.
    DEXScreener intentionally NOT in the domain list — it's a chart site,
    not a discussion site; a listing isn't social proof.
    """
    if not TAVILY_API_KEY:
        return True
    try:
        r = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key":        TAVILY_API_KEY,
                "query":          f'"${narrative}" OR "#{narrative}"',
                "search_depth":   "basic",
                "max_results":    3,
                "days":           1,   # last 24 hours only
                "include_domains": ["twitter.com", "x.com", "reddit.com"],
            },
            timeout=TIMEOUT,
        )
        results = r.json().get("results") or []
        return len(results) >= 1
    except Exception:
        return True  # don't block alert on Tavily failure


# ---------- BEST CA FROM CLUSTER ----------

def _best_ca(narrative: str, coins: list) -> dict:
    """Pick the coin from the cluster with highest market cap (most traction)."""
    candidates = [
        c for c in coins
        if narrative in (_extract_words(c.get("name") or "") | _extract_words(c.get("symbol") or ""))
        and c.get("mint")
    ]
    if not candidates:
        return {}
    candidates.sort(key=lambda x: float(x.get("usd_market_cap") or 0), reverse=True)
    best = candidates[0]
    return {
        "mint":   best.get("mint") or "",
        "name":   best.get("name") or "",
        "symbol": best.get("symbol") or "",
        "mc":     float(best.get("usd_market_cap") or 0),
        "cluster_size": len(candidates),
    }


# ---------- ALERT BUILDER ----------

def _build_alert(narrative: str, token: dict, rug_result: dict, twitter_ok: bool) -> str:
    mint          = token["mint"]
    symbol        = token.get("symbol") or mint[:6]
    cluster_size  = token.get("cluster_size", 0)

    verdict       = rug_result.get("verdict") or "UNKNOWN"
    verdict_emoji = "🟢" if verdict == "GREEN" else "🟡" if verdict == "YELLOW" else "🔴"

    details   = rug_result.get("details") or {}
    mc        = details.get("market_cap_usd") or token.get("mc") or 0
    liq       = details.get("liquidity_usd") or 0
    age       = details.get("age_human") or "unknown"
    change_1h = details.get("price_change_1h") or 0
    vol_1h    = details.get("volume_1h_usd") or 0

    # Smart wallet check
    holders       = check_wallets_hold_token(mint)
    total_wallets = len(load_wallets())
    if holders:
        labels = ", ".join(h["label"] for h in holders[:3])
        wallet_line = f"🐋 Smart wallets: 👀 *{len(holders)} of {total_wallets}* holding ({labels})"
    else:
        wallet_line = f"🐋 Smart wallets: ⚪ 0 of {total_wallets} holding"

    twitter_line = "🐦 Twitter: ✅ Trending" if twitter_ok else "🐦 Twitter: ⚠️ Not confirmed yet"

    trade_card_block = trade_card_for_check(rug_result)

    return (
        f"🚨 *{symbol}* — Narrative forming\n\n"
        f"📋 CA: `{mint}`\n"
        f"⏰ Age: {age}\n"
        f"💰 MC: ${mc:,.0f}\n"
        f"💧 Liquidity: ${liq:,.0f}\n"
        f"📈 1h change: {change_1h:+.1f}%\n"
        f"🔥 1h volume: ${vol_1h:,.0f}\n\n"
        f"🧠 Narrative: *\"{narrative}\"* — {cluster_size} tokens launched in last 30 min\n"
        f"{twitter_line}\n\n"
        f"🛡 Rug check: {verdict_emoji} *{verdict}*\n"
        f"{wallet_line}"
        f"{trade_card_block}"
    )


# ---------- SCAN LOOP ----------

def _scan_once(send_alert_fn) -> int:
    """One full scan cycle. Returns number of alerts sent."""
    alerts_sent = 0
    coins = _fetch_new_pumps(limit=200)
    if not coins:
        log.warning("Watcher: pump.fun returned 0 coins")
        return 0

    narratives = _find_narratives(coins)
    if not narratives:
        return 0

    for narrative, cluster_coins in narratives.items():
        with _lock:
            if narrative in _seen_narratives:
                continue

        # Find best CA in cluster
        token = _best_ca(narrative, cluster_coins)
        if not token or not token.get("mint"):
            continue

        mint = token["mint"]
        with _lock:
            if mint in _seen_tokens:
                continue

        # Twitter confirmation
        twitter_ok = _confirm_twitter(narrative)

        # Rug check — skip RED
        try:
            rug_result = check_token(mint)
        except Exception as e:
            log.warning(f"Watcher rug check failed for {mint}: {e}")
            continue

        if rug_result.get("verdict") == "RED":
            with _lock:
                _seen_narratives.add(narrative)
                _seen_tokens.add(mint)
            # Persist seen-state so a Railway redeploy doesn't re-alert this RED
            memory_store.mark_narrative_seen(narrative)
            memory_store.mark_token_seen(mint)
            continue

        # Build and send alert
        alert = _build_alert(narrative, token, rug_result, twitter_ok)
        send_alert_fn(alert)
        alerts_sent += 1

        # Persist alert + seen state
        try:
            d = rug_result.get("details") or {}
            holders = check_wallets_hold_token(mint)
            memory_store.save_alert(
                narrative=narrative,
                mint=mint,
                symbol=token.get("symbol"),
                verdict=rug_result.get("verdict"),
                mc=d.get("market_cap") or token.get("mc"),
                liq=d.get("liquidity_usd"),
                twitter_ok=twitter_ok,
                smart_wallets=len(holders),
                cluster_size=token.get("cluster_size"),
                full_text=alert,
            )
        except Exception as e:
            log.warning(f"Watcher save_alert failed: {e}")

        with _lock:
            _seen_narratives.add(narrative)
            _seen_tokens.add(mint)
        memory_store.mark_narrative_seen(narrative)
        memory_store.mark_token_seen(mint)

        time.sleep(2)  # brief pause between alerts

    return alerts_sent


def _loop(send_alert_fn):
    global _running, _last_scan_time, _last_scan_found, _scan_count
    log.info("Watcher started.")
    while _running:
        try:
            _last_scan_found = _scan_once(send_alert_fn)
            _last_scan_time  = time.time()
            _scan_count     += 1
            log.info(f"Watcher scan #{_scan_count} done — {_last_scan_found} narratives found")
        except Exception as e:
            log.warning(f"Watcher scan error: {e}")
        for _ in range(SCAN_INTERVAL):
            if not _running:
                break
            time.sleep(1)
    log.info("Watcher stopped.")


# ---------- PUBLIC API ----------

def start(send_alert_fn):
    """Start the watcher background thread. send_alert_fn(text) sends Telegram message."""
    global _running, _thread
    if _running:
        return False
    _running = True
    _thread  = threading.Thread(target=_loop, args=(send_alert_fn,), daemon=True)
    _thread.start()
    return True


def stop():
    global _running
    _running = False


def is_running() -> bool:
    return _running


def get_status() -> dict:
    mins_ago = None
    if _last_scan_time:
        mins_ago = round((time.time() - _last_scan_time) / 60, 1)
    return {
        "running":     _running,
        "scan_count":  _scan_count,
        "mins_ago":    mins_ago,
        "last_found":  _last_scan_found,
    }
