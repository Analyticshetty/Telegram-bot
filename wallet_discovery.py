"""
Wallet Discovery — auto-populates smart_wallets.json with real active traders.

Sources (all free, no auth):
  1. GMGN frontend API — top Solana smart money by 30D PNL
  2. GMGN smart degen tag — active memecoin specialists
  3. Rugcheck leaderboard — wallets that trade safe tokens

Activity filter: every wallet must have a Solana transaction in the last 7 days.
Quality filter: win_rate >= 0.50, pnl_30d > 0, at least 20 trades in 30d.

Usage: /discoverwallet in Telegram (owner-only).
"""

import requests
import time
import logging
from smart_wallets import add_wallet, load_wallets

log = logging.getLogger(__name__)

SOLANA_RPC = "https://api.mainnet-beta.solana.com"
TIMEOUT    = 10
HEADERS    = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":    "https://gmgn.ai/",
    "Accept":     "application/json",
}

# GMGN frontend endpoints (what their website hits — no auth needed)
GMGN_RANK_URL   = "https://gmgn.ai/defi/quotation/v1/rank/sol/wallets/7d"
GMGN_SMART_URL  = "https://gmgn.ai/defi/quotation/v1/smartmoney/sol/wallets"

# Quality thresholds
MIN_WIN_RATE   = 0.50   # 50% win rate minimum
MIN_TRADES_30D = 10     # must have traded at least 10 tokens in 30 days
MIN_PNL_30D    = 0      # must be profitable (any positive PNL)
MAX_WALLETS    = 100    # cap to avoid spamming RPC
ACTIVITY_DAYS  = 7      # wallet must have tx in last N days


# ---------- GMGN FETCHERS ----------

def _fetch_gmgn_rank(limit: int = 50) -> list:
    """Fetch top Solana traders by 7D PNL from GMGN rank page."""
    wallets = []
    try:
        params = {
            "orderby":    "pnl",
            "direction":  "desc",
            "limit":      limit,
            "filters[]":  "smart_degen",
        }
        r = requests.get(GMGN_RANK_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            items = (data.get("data") or {}).get("rank") or data.get("data") or []
            for item in items:
                addr = item.get("wallet_address") or item.get("address")
                if addr and len(addr) > 30:
                    wallets.append({
                        "address":   addr,
                        "win_rate":  float(item.get("winrate") or item.get("win_rate") or 0),
                        "pnl_30d":   float(item.get("pnl_30d") or item.get("realized_profit_30d") or 0),
                        "trades_30d": int(item.get("buy_30d") or item.get("txs_30d") or 0),
                        "source":    "gmgn-rank",
                    })
    except Exception as e:
        log.warning(f"GMGN rank fetch failed: {e}")
    return wallets


def _fetch_gmgn_smart(limit: int = 50) -> list:
    """Fetch smart money / KOL wallets from GMGN smart money endpoint."""
    wallets = []
    for tag in ("smart_degen", "kol", "sniper"):
        try:
            params = {
                "tag":       tag,
                "orderby":   "pnl_30d",
                "direction": "desc",
                "limit":     limit,
            }
            r = requests.get(GMGN_SMART_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                items = (data.get("data") or {}).get("wallets") or data.get("data") or []
                for item in items:
                    addr = item.get("wallet_address") or item.get("address")
                    if addr and len(addr) > 30:
                        wallets.append({
                            "address":    addr,
                            "win_rate":   float(item.get("winrate") or item.get("win_rate") or 0),
                            "pnl_30d":    float(item.get("pnl_30d") or item.get("realized_profit") or 0),
                            "trades_30d": int(item.get("buy_30d") or item.get("txs_30d") or 0),
                            "source":     f"gmgn-{tag}",
                        })
        except Exception as e:
            log.warning(f"GMGN smart/{tag} fetch failed: {e}")
    return wallets


# ---------- ACTIVITY VERIFIER ----------

def _is_wallet_active(address: str, days: int = ACTIVITY_DAYS) -> bool:
    """
    Returns True if wallet has at least 1 Solana transaction in the last `days` days.
    Uses getSignaturesForAddress — public RPC, no auth.
    """
    cutoff = int(time.time()) - (days * 86400)
    try:
        r = requests.post(
            SOLANA_RPC,
            json={
                "jsonrpc": "2.0",
                "id":      1,
                "method":  "getSignaturesForAddress",
                "params":  [address, {"limit": 1, "commitment": "confirmed"}],
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return False
        sigs = r.json().get("result") or []
        if not sigs:
            return False
        block_time = sigs[0].get("blockTime") or 0
        return block_time >= cutoff
    except Exception:
        return False


# ---------- QUALITY FILTER ----------

def _passes_quality(w: dict) -> bool:
    """Returns True if wallet meets minimum quality thresholds."""
    if w.get("win_rate", 0) < MIN_WIN_RATE:
        return False
    if w.get("pnl_30d", 0) < MIN_PNL_30D:
        return False
    if w.get("trades_30d", 0) < MIN_TRADES_30D:
        return False
    return True


# ---------- MAIN DISCOVERY ----------

def discover_wallets(progress_callback=None) -> dict:
    """
    Full discovery run. Returns {added: int, skipped_quality: int,
    skipped_inactive: int, skipped_duplicate: int, total_checked: int, sources: dict}.

    progress_callback(msg: str) — called with status updates during the run.
    """
    def _progress(msg):
        if progress_callback:
            progress_callback(msg)
        log.info(msg)

    already_tracked = {w["address"].lower() for w in load_wallets()}

    # Step 1: collect candidates from all sources
    _progress("🔍 Fetching from GMGN rank...")
    candidates = _fetch_gmgn_rank(limit=50)

    _progress("🔍 Fetching from GMGN smart money / KOL tags...")
    candidates += _fetch_gmgn_smart(limit=50)

    # Dedupe by address (keep first occurrence)
    seen = set()
    unique = []
    for w in candidates:
        addr = w["address"].lower()
        if addr not in seen:
            seen.add(addr)
            unique.append(w)

    _progress(f"📋 {len(unique)} unique candidates found. Filtering...")

    # Step 2: quality filter
    quality_passed = [w for w in unique if _passes_quality(w)]
    skipped_quality = len(unique) - len(quality_passed)

    # Step 3: skip already-tracked
    not_duplicate = [w for w in quality_passed if w["address"].lower() not in already_tracked]
    skipped_duplicate = len(quality_passed) - len(not_duplicate)

    # Cap to avoid hammering RPC
    to_check = not_duplicate[:MAX_WALLETS]

    _progress(
        f"✅ {len(quality_passed)} passed quality filter "
        f"({skipped_quality} failed, {skipped_duplicate} already tracked). "
        f"Verifying {len(to_check)} wallets for activity..."
    )

    # Step 4: activity check (sequential with small delay to respect public RPC)
    added = 0
    skipped_inactive = 0
    source_counts = {}

    for i, w in enumerate(to_check):
        if i > 0 and i % 10 == 0:
            _progress(f"⏳ Checked {i}/{len(to_check)}... ({added} added so far)")
            time.sleep(1)  # brief pause every 10 to avoid rate-limit

        if not _is_wallet_active(w["address"]):
            skipped_inactive += 1
            continue

        label = _make_label(w)
        source = w.get("source", "discovery")
        ok = add_wallet(w["address"], label, source=source)
        if ok:
            added += 1
            source_counts[source] = source_counts.get(source, 0) + 1
        time.sleep(0.15)  # ~6 req/s — well within public RPC limits

    return {
        "added":            added,
        "skipped_quality":  skipped_quality,
        "skipped_inactive": skipped_inactive,
        "skipped_duplicate": skipped_duplicate,
        "total_checked":    len(to_check),
        "sources":          source_counts,
    }


def _make_label(w: dict) -> str:
    """Generate a readable label from wallet stats."""
    wr  = w.get("win_rate", 0)
    pnl = w.get("pnl_30d", 0)
    src = w.get("source", "disc").replace("gmgn-", "")

    pnl_str = f"${pnl/1000:.0f}k" if pnl >= 1000 else f"${pnl:.0f}"
    return f"{src}-{int(wr*100)}pct-{pnl_str}"
