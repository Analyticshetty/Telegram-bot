"""
Memory store — Redis-backed persistent log of everything the bot does.

Stores:
  - alerts:    every watcher alert fired (last 500)
  - checks:    every /check or pasted-CA rug check (last 200 per user)
  - scans:     every /scan command run (last 100)
  - seen sets: watcher's deduplication sets, persisted across restarts (24h TTL)

All saves are best-effort — never raise to caller, just log and move on.
A Redis hiccup must not break the bot.
"""

import json
import time
import logging
from redis_client import get_redis

log = logging.getLogger(__name__)
_redis = get_redis()

# ---------- KEY NAMESPACES ----------
K_ALERTS         = "mem:alerts"
K_SCANS          = "mem:scans"
K_CHECKS         = "mem:checks:{user_id}"
K_ALERT_BY_CA    = "mem:alert_by_ca:{ca}"
K_CHECK_BY_CA    = "mem:check_by_ca:{ca}"
K_SEEN_NARRATIVES = "mem:watcher_seen_narratives"
K_SEEN_TOKENS    = "mem:watcher_seen_tokens"

# ---------- CAPS (prevent unbounded growth) ----------
MAX_ALERTS_KEEP   = 500
MAX_SCANS_KEEP    = 100
MAX_CHECKS_KEEP   = 200
SEEN_TTL_SECONDS  = 86400  # 24h — after that a narrative could legitimately re-form


# ---------- LOW-LEVEL HELPERS ----------

def _push_capped_list(key: str, item: dict, max_keep: int):
    """LPUSH item, then LTRIM to max_keep. Atomic-ish."""
    try:
        pipe = _redis.pipeline()
        pipe.lpush(key, json.dumps(item))
        pipe.ltrim(key, 0, max_keep - 1)
        pipe.execute()
    except Exception as e:
        log.warning(f"memory_store push failed for {key}: {e}")


def _read_list(key: str, limit: int = 50) -> list:
    try:
        raw = _redis.lrange(key, 0, limit - 1)
        return [json.loads(x) for x in raw if x]
    except Exception as e:
        log.warning(f"memory_store read failed for {key}: {e}")
        return []


# ---------- ALERTS ----------

def save_alert(*, narrative: str, mint: str, symbol: str, verdict: str,
               mc=None, liq=None, twitter_ok=None, smart_wallets=None,
               cluster_size=None, full_text: str = None):
    """Persist a watcher alert. Call this every time the watcher fires."""
    entry = {
        "ts":            int(time.time()),
        "type":          "alert",
        "narrative":     narrative,
        "mint":          mint,
        "symbol":        symbol,
        "verdict":       verdict,
        "mc":            mc,
        "liq":           liq,
        "twitter_ok":    twitter_ok,
        "smart_wallets": smart_wallets,
        "cluster_size":  cluster_size,
        "full_text":     (full_text or "")[:4000],
    }
    _push_capped_list(K_ALERTS, entry, MAX_ALERTS_KEEP)
    # Quick lookup index by CA — overwrite (newest wins)
    try:
        _redis.set(K_ALERT_BY_CA.format(ca=mint), json.dumps(entry), ex=30 * 86400)
    except Exception as e:
        log.warning(f"memory_store alert_by_ca save failed: {e}")


def get_recent_alerts(limit: int = 20) -> list:
    return _read_list(K_ALERTS, limit)


def get_alert_by_ca(ca: str) -> dict | None:
    try:
        raw = _redis.get(K_ALERT_BY_CA.format(ca=ca))
        return json.loads(raw) if raw else None
    except Exception:
        return None


def search_alerts(needle: str, limit: int = 200) -> list:
    """Case-insensitive search across narrative/symbol/mint in recent alerts."""
    needle_l = (needle or "").lower()
    if not needle_l:
        return []
    out = []
    for a in _read_list(K_ALERTS, limit):
        hay = " ".join(str(a.get(k, "")) for k in ("narrative", "symbol", "mint")).lower()
        if needle_l in hay:
            out.append(a)
    return out


# ---------- CHECKS (per-user /check history) ----------

def save_check(*, user_id, mint: str, symbol: str, verdict: str,
               mc=None, liq=None, reasons_red=None, reasons_yellow=None):
    entry = {
        "ts":             int(time.time()),
        "type":           "check",
        "user_id":        user_id,
        "mint":           mint,
        "symbol":         symbol,
        "verdict":        verdict,
        "mc":             mc,
        "liq":            liq,
        "reasons_red":    (reasons_red or [])[:10],
        "reasons_yellow": (reasons_yellow or [])[:10],
    }
    _push_capped_list(K_CHECKS.format(user_id=user_id), entry, MAX_CHECKS_KEEP)
    try:
        _redis.set(K_CHECK_BY_CA.format(ca=mint), json.dumps(entry), ex=30 * 86400)
    except Exception as e:
        log.warning(f"memory_store check_by_ca save failed: {e}")


def get_recent_checks(user_id, limit: int = 20) -> list:
    return _read_list(K_CHECKS.format(user_id=user_id), limit)


def get_check_by_ca(ca: str) -> dict | None:
    try:
        raw = _redis.get(K_CHECK_BY_CA.format(ca=ca))
        return json.loads(raw) if raw else None
    except Exception:
        return None


# ---------- SCANS ----------

def save_scan(*, results_count: int, top_results: list = None):
    entry = {
        "ts":            int(time.time()),
        "type":          "scan",
        "results_count": results_count,
        "top_results":   [
            {"mint": r.get("mint"), "symbol": r.get("symbol"),
             "verdict": r.get("verdict"), "mc": r.get("market_cap_usd"),
             "liq": r.get("liquidity_usd")}
            for r in (top_results or [])[:10]
        ],
    }
    _push_capped_list(K_SCANS, entry, MAX_SCANS_KEEP)


def get_recent_scans(limit: int = 20) -> list:
    return _read_list(K_SCANS, limit)


# ---------- WATCHER SEEN SETS (persistent across restarts) ----------
# Stored as Redis hashes {key: timestamp}. Entries older than SEEN_TTL_SECONDS are pruned on read.

def _load_seen(key: str) -> set:
    try:
        raw = _redis.hgetall(key)
        if not raw:
            return set()
        now = time.time()
        alive = set()
        stale = []
        for k, ts in raw.items():
            try:
                if now - float(ts) < SEEN_TTL_SECONDS:
                    alive.add(k)
                else:
                    stale.append(k)
            except (TypeError, ValueError):
                stale.append(k)
        if stale:
            try:
                _redis.hdel(key, *stale)
            except Exception:
                pass
        return alive
    except Exception as e:
        log.warning(f"memory_store load_seen failed for {key}: {e}")
        return set()


def _mark_seen(key: str, value: str):
    try:
        _redis.hset(key, value, str(time.time()))
    except Exception as e:
        log.warning(f"memory_store mark_seen failed: {e}")


def load_seen_narratives() -> set:
    return _load_seen(K_SEEN_NARRATIVES)


def mark_narrative_seen(narrative: str):
    _mark_seen(K_SEEN_NARRATIVES, narrative)


def load_seen_tokens() -> set:
    return _load_seen(K_SEEN_TOKENS)


def mark_token_seen(mint: str):
    _mark_seen(K_SEEN_TOKENS, mint)


# ---------- SUMMARY ----------

def stats() -> dict:
    """Quick overview for /memstats command."""
    try:
        n_alerts = _redis.llen(K_ALERTS)
        n_scans  = _redis.llen(K_SCANS)
        n_seen_n = _redis.hlen(K_SEEN_NARRATIVES)
        n_seen_t = _redis.hlen(K_SEEN_TOKENS)
        return {
            "alerts":           n_alerts,
            "scans":            n_scans,
            "seen_narratives":  n_seen_n,
            "seen_tokens":      n_seen_t,
        }
    except Exception:
        return {"alerts": 0, "scans": 0, "seen_narratives": 0, "seen_tokens": 0}
