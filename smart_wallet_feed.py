"""
Smart Wallet Real-Time Feed (Module #6) — the goldmine signal.

Background thread continuously polls all tracked smart wallets.
When 2+ wallets have bought the same CA (at any time, any token age) → fire alert.

Architecture (designed for $0/mo on free tiers):
  - Signature polling: PUBLIC Solana RPC (free, rate-limited).
    Round-robin through all wallets, ~3s/wallet. Full cycle ~17 min.
  - Transaction parsing: Helius enhanced endpoint (100k credits/mo).
    Only called when wallet has NEW signatures since last check —
    quiet wallets cost zero credits.

Storage (Redis):
  sw_feed:cursor:{wallet}        = JSON {last_sig, last_check_ts}
  sw_feed:alerted:{ca}           = "1", TTL 24h (dedupe alert)
  sw_accum:holders:{ca}          = JSON list of {wallet, label, ts}, TTL 30 days
  sw_accum:entry:{ca}:{wallet}   = "1", TTL 7 days (per-wallet dedup)
"""

import os
import json
import time
import logging
import threading
import requests
from redis_client import get_redis

log = logging.getLogger(__name__)
_redis = get_redis()

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")

# ---------- CONFIG ----------
WALLET_POLL_DELAY = 3            # seconds between wallet checks (public RPC kindness)
SIG_LIMIT         = 10           # latest 10 sigs per wallet
ALERT_DEDUPE_TTL  = 86400        # 24h — one alert per token per day max
ACCUM_HOLDERS_TTL = 30 * 86400   # 30 days — remember all wallet holders per CA
ACCUM_ENTRY_TTL   = 7 * 86400    # 7 days — dedup per (mint+wallet) entry
TIMEOUT           = 8

SOLANA_RPC = "https://api.mainnet-beta.solana.com"
HELIUS_TX  = "https://api.helius.xyz/v0/transactions"

# Mints to ignore (SOL, common stables) — buying these isn't a memecoin signal
SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

# ---------- STATE ----------
_running    = False
_thread     = None
_alert_fn   = None  # set on start
_last_cycle_alerts = 0
_last_cycle_end_ts = None
_cycles_completed  = 0


# ---------- RPC HELPERS ----------

def _rpc_get_signatures(wallet: str, limit: int = SIG_LIMIT) -> list:
    """Public RPC — getSignaturesForAddress. Returns list of {signature, blockTime, ...}."""
    try:
        r = requests.post(
            SOLANA_RPC,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [wallet, {"limit": limit}],
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("result") or []
    except Exception as e:
        log.warning(f"sw_feed sig fetch failed for {wallet[:8]}: {e}")
        return []


def _helius_parse(signatures: list) -> list:
    """Batch-parse signatures via Helius. Returns enriched tx list."""
    if not HELIUS_API_KEY or not signatures:
        return []
    try:
        r = requests.post(
            f"{HELIUS_TX}?api-key={HELIUS_API_KEY}",
            json={"transactions": signatures},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning(f"Helius parse returned {r.status_code}")
            return []
        return r.json() or []
    except Exception as e:
        log.warning(f"sw_feed Helius parse failed: {e}")
        return []


def _extract_buys_for_wallet(parsed_txs: list, wallet: str) -> list:
    """From parsed Helius txs, extract memecoin BUYS by this wallet.
    A buy = wallet's SPL token balance increased for a non-skip mint."""
    buys = []
    for tx in parsed_txs:
        try:
            ts = tx.get("timestamp") or 0
            # Helius enhanced format has tokenTransfers array
            transfers = tx.get("tokenTransfers") or []
            for t in transfers:
                if (t.get("toUserAccount") or "").lower() != wallet.lower():
                    continue
                mint = t.get("mint")
                if not mint or mint in SKIP_MINTS:
                    continue
                # Skip dust amounts
                amount = float(t.get("tokenAmount") or 0)
                if amount <= 0:
                    continue
                buys.append({"mint": mint, "amount": amount, "ts": ts, "sig": tx.get("signature")})
        except Exception:
            continue
    return buys


def _has_been_alerted(mint: str) -> bool:
    try:
        return _redis.get(f"sw_feed:alerted:{mint}") == "1"
    except Exception:
        return False


def _mark_alerted(mint: str):
    try:
        _redis.set(f"sw_feed:alerted:{mint}", "1", ex=ALERT_DEDUPE_TTL)
    except Exception:
        pass


# ---------- ACCUMULATION TRACKING (no time window, no age gate) ----------

def _record_accumulation(mint: str, wallet_addr: str, wallet_label: str, ts: int):
    """Track every wallet that has ever bought this token.
    Returns (prior_holders, is_new_entry).
    prior_holders = wallets that were already in before this one.
    is_new_entry  = True if this wallet hadn't been recorded yet."""
    holders_key = f"sw_accum:holders:{mint}"
    dedup_key   = f"sw_accum:entry:{mint}:{wallet_addr}"

    # Already processed this wallet+token combo recently?
    try:
        if _redis.get(dedup_key):
            return [], False
    except Exception:
        pass

    # Load existing holders
    try:
        raw     = _redis.get(holders_key)
        holders = json.loads(raw) if raw else []
    except Exception:
        holders = []

    prior = [h for h in holders if h.get("wallet") != wallet_addr]

    # Add this wallet to holders list
    if not any(h.get("wallet") == wallet_addr for h in holders):
        holders.append({"wallet": wallet_addr, "label": wallet_label, "ts": ts})
        try:
            _redis.set(holders_key, json.dumps(holders), ex=ACCUM_HOLDERS_TTL)
        except Exception:
            pass

    # Mark as processed — won't re-fire for this wallet+token for 7 days
    try:
        _redis.set(dedup_key, "1", ex=ACCUM_ENTRY_TTL)
    except Exception:
        pass

    return prior, len(prior) >= 1


def _build_accum_alert(mint: str, prior_holders: list, new_buyer: dict,
                       age_min, rug_result) -> str:
    """Format the accumulation alert — distinct from convergence."""
    new_label = new_buyer.get("label") or "?"
    prior_labels = ", ".join(h.get("label", "?") for h in prior_holders[:5])

    age_str = "unknown"
    if age_min is not None:
        age_str = f"{age_min:.0f}min" if age_min < 60 else f"{age_min/60:.1f}h"

    verdict_icon = "⚪"
    extras = ""
    if rug_result:
        verdict = rug_result.get("verdict") or "UNCHECKED"
        verdict_icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(verdict, "⚪")
        d   = rug_result.get("details") or {}
        mc  = d.get("market_cap") or 0
        liq = d.get("liquidity_usd") or 0
        sym = d.get("symbol") or "?"
        extras = (
            f"\n📊 Symbol: *{sym}*\n"
            f"💰 MC: ${mc:,.0f}  |  💧 Liq: ${liq:,.0f}"
        )

    # How long ago did the first holder buy?
    oldest_ts = min((h.get("ts") or 0) for h in prior_holders) if prior_holders else 0
    if oldest_ts:
        first_ago = (time.time() - oldest_ts) / 3600
        first_str = f"{first_ago:.1f}h ago" if first_ago >= 1 else f"{int(first_ago*60)}min ago"
    else:
        first_str = "unknown"

    return (
        f"🐋🐋 *SMART WALLET SIGNAL*\n\n"
        f"📋 CA: `{mint}`\n"
        f"⏱ Token age: {age_str}\n\n"
        f"🔵 *{new_label}* just bought\n"
        f"Also holding: *{prior_labels}*\n"
        f"   (first wallet in: {first_str})\n"
        f"Total smart wallets: {len(prior_holders) + 1}{extras}\n"
        f"🛡 Rug check: {verdict_icon}\n\n"
        f"_Multiple independent smart wallets in the same token._"
    )


def _trigger_accumulation_alert(mint: str, prior_holders: list, new_buyer: dict):
    """Rug check + send accumulation alert. Skips RED."""
    # Don't fire if convergence already sent an alert on this mint recently
    if _has_been_alerted(mint):
        log.info(f"sw_feed accum skipped {mint} — convergence already alerted")
        return

    age_min    = _token_age_minutes(mint)
    rug_result = None
    try:
        from rug_check import check_token
        rug_result = check_token(mint)
        if rug_result.get("verdict") == "RED":
            log.info(f"sw_feed accum on {mint} but RED — suppressed")
            _mark_alerted(mint)
            return
    except Exception as e:
        log.warning(f"sw_feed accum rug_check failed for {mint}: {e}")

    alert = _build_accum_alert(mint, prior_holders, new_buyer, age_min, rug_result)
    if _alert_fn:
        try:
            _alert_fn(alert)
        except Exception as e:
            log.warning(f"sw_feed accum alert send failed: {e}")

    _mark_alerted(mint)

    try:
        import memory_store
        d = (rug_result or {}).get("details") or {}
        memory_store.save_alert(
            narrative="smart_wallet_accumulation",
            mint=mint,
            symbol=d.get("symbol"),
            verdict=(rug_result or {}).get("verdict") or "UNCHECKED",
            mc=d.get("market_cap"),
            liq=d.get("liquidity_usd"),
            twitter_ok=None,
            smart_wallets=len(prior_holders) + 1,
            cluster_size=len(prior_holders) + 1,
            full_text=alert,
        )
    except Exception as e:
        log.warning(f"sw_feed accum memory_store save failed: {e}")

    global _last_cycle_alerts
    _last_cycle_alerts += 1


def _get_cursor(wallet: str) -> dict:
    try:
        raw = _redis.get(f"sw_feed:cursor:{wallet}")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _save_cursor(wallet: str, last_sig: str):
    try:
        _redis.set(
            f"sw_feed:cursor:{wallet}",
            json.dumps({"last_sig": last_sig, "last_check_ts": int(time.time())}),
        )
    except Exception:
        pass


# ---------- TOKEN AGE ----------

def _token_age_minutes(mint: str) -> float | None:
    """Quick DEXScreener fetch for token age — informational only, not a gate."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None
        pairs = (r.json() or {}).get("pairs") or []
        if not pairs:
            return None
        pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
        created_ms = pairs[0].get("pairCreatedAt")
        if not created_ms:
            return None
        return (time.time() * 1000 - created_ms) / 60000
    except Exception:
        return None


# ---------- SCAN ONE WALLET ----------

def _scan_wallet(wallet: dict) -> int:
    """Returns number of new buys recorded for this wallet."""
    addr = wallet.get("address")
    label = wallet.get("label") or addr[:6]
    if not addr or addr.startswith("TODO"):
        return 0

    cursor = _get_cursor(addr)
    last_sig = cursor.get("last_sig")

    sigs = _rpc_get_signatures(addr, limit=SIG_LIMIT)
    if not sigs:
        return 0

    # Find sigs newer than cursor
    new_sigs = []
    for s in sigs:
        if last_sig and s.get("signature") == last_sig:
            break
        # Skip very old sigs (>20 min) to avoid replaying history on first run
        bt = s.get("blockTime") or 0
        if bt and (time.time() - bt) > 1200:
            continue
        new_sigs.append(s.get("signature"))

    if not new_sigs:
        # Update cursor so we don't keep re-checking the same head
        if sigs:
            _save_cursor(addr, sigs[0].get("signature"))
        return 0

    # Parse via Helius
    parsed = _helius_parse(new_sigs[:5])  # cap to limit Helius spend
    buys = _extract_buys_for_wallet(parsed, addr)

    # Record buy + check if any other wallet already holds this token
    recorded = 0
    for b in buys:
        mint = b["mint"]
        ts   = b["ts"] or int(time.time())
        recorded += 1

        prior, is_accum = _record_accumulation(mint, addr, label, ts)
        if is_accum:
            _trigger_accumulation_alert(mint, prior, {"wallet": addr, "label": label, "ts": ts})

    # Update cursor to newest sig
    _save_cursor(addr, sigs[0].get("signature"))
    return recorded


# ---------- MAIN LOOP ----------

def _loop():
    global _running, _last_cycle_alerts, _last_cycle_end_ts, _cycles_completed
    log.info("Smart wallet feed started.")
    while _running:
        try:
            # Defer import to break circular ref at import-time
            from smart_wallets import load_wallets
            wallets = load_wallets()
            if not wallets:
                time.sleep(60)
                continue

            _last_cycle_alerts = 0
            for w in wallets:
                if not _running:
                    break
                try:
                    _scan_wallet(w)
                except Exception as e:
                    log.warning(f"sw_feed scan_wallet error: {e}")
                # Polite pause between wallets
                for _ in range(WALLET_POLL_DELAY):
                    if not _running:
                        break
                    time.sleep(1)

            _last_cycle_end_ts = time.time()
            _cycles_completed += 1
            log.info(f"sw_feed cycle #{_cycles_completed} done — {_last_cycle_alerts} convergence alerts")

        except Exception as e:
            log.warning(f"sw_feed loop error: {e}")
            time.sleep(30)
    log.info("Smart wallet feed stopped.")


# ---------- PUBLIC API ----------

def start(alert_fn):
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
    mins_ago = None
    if _last_cycle_end_ts:
        mins_ago = round((time.time() - _last_cycle_end_ts) / 60, 1)
    return {
        "running":           _running,
        "cycles_completed":  _cycles_completed,
        "last_cycle_alerts": _last_cycle_alerts,
        "mins_since_cycle":  mins_ago,
    }
