"""
Wallet Discovery — smart money finder using Helius + GeckoTerminal.

Strategy:
  1. Pull top 200 trending Solana tokens from GeckoTerminal (trending only, no new pools)
     No price/volume filters — if it's trending, it's in.
  2. For each token, get top 100 holders via Helius getTokenAccounts (returns owners directly)
  3. Wallets appearing in 2+ tokens = smart money candidate
  4. Verify wallet has on-chain activity in last 7 days
  5. Add to smart_wallets.json
"""

import os
import requests
import time
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from smart_wallets import add_wallet, load_wallets

log = logging.getLogger(__name__)

HELIUS_API_KEY  = os.environ.get("HELIUS_API_KEY", "")
GECKO_TRENDING  = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
TIMEOUT         = 12
GECKO_TIMEOUT   = 8
ACTIVITY_DAYS   = 7
MIN_TOKEN_HITS  = 2
MAX_WORKERS     = 5
TARGET_TOKENS   = 200
HOLDERS_PER_TOKEN = 100
SOLANA_MINT_RE  = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

SKIP_ADDRESSES = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bNX",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
}


def _helius_url():
    return f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"


# ---------- TOKEN DISCOVERY ----------

def _get_trending_tokens(limit: int = TARGET_TOKENS) -> list:
    """Pull top trending Solana tokens from GeckoTerminal. No filters — trending = in."""
    tokens = []
    seen   = set()
    page   = 1

    while len(tokens) < limit:
        try:
            r = requests.get(
                GECKO_TRENDING,
                params={"page": page},
                headers={"Accept": "application/json"},
                timeout=GECKO_TIMEOUT,
            )
            if r.status_code != 200:
                break
            pools = r.json().get("data") or []
            if not pools:
                break

            for pool in pools:
                rel   = pool.get("relationships") or {}
                bt_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
                mint  = bt_id.replace("solana_", "")
                attr  = pool.get("attributes") or {}

                if not mint or not SOLANA_MINT_RE.match(mint) or mint in seen:
                    continue
                seen.add(mint)

                tokens.append({
                    "mint":   mint,
                    "symbol": (attr.get("name") or mint[:6]).split("/")[0].strip(),
                })

                if len(tokens) >= limit:
                    break

            time.sleep(0.4)
            page += 1

        except Exception as e:
            log.warning(f"GeckoTerminal error page {page}: {e}")
            break

    return tokens[:limit]


# ---------- HOLDER LOOKUP via Helius ----------

def _get_holder_wallets(mint: str, debug_callback=None) -> list:
    """Returns up to HOLDERS_PER_TOKEN owner wallet addresses via Helius getTokenAccounts."""
    try:
        r = requests.post(
            _helius_url(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccounts",
                "params": {
                    "mint": mint,
                    "limit": HOLDERS_PER_TOKEN,
                    "page": 1,
                },
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            if debug_callback:
                debug_callback(f"🔬 {mint[:8]}: Helius HTTP {r.status_code}")
            return []

        body = r.json()
        if "error" in body:
            if debug_callback:
                debug_callback(f"🔬 {mint[:8]}: Helius error: {body['error']}")
            return []

        accounts = (body.get("result") or {}).get("token_accounts") or []
        owners = []
        for acct in accounts:
            owner  = acct.get("owner") or ""
            amount = acct.get("amount") or 0
            if owner and int(amount) > 0 and SOLANA_MINT_RE.match(owner) and owner not in SKIP_ADDRESSES:
                owners.append(owner)

        if debug_callback:
            debug_callback(f"🔬 {mint[:8]}: {len(owners)} holders")

        return owners

    except Exception as e:
        if debug_callback:
            debug_callback(f"🔬 {mint[:8]}: exception: {e}")
        return []


# ---------- ACTIVITY CHECK ----------

def _is_active(address: str) -> bool:
    cutoff = int(time.time()) - (ACTIVITY_DAYS * 86400)
    try:
        r = requests.post(
            _helius_url(),
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [address, {"limit": 1, "commitment": "confirmed"}],
            },
            timeout=TIMEOUT,
        )
        result = r.json().get("result")
        if not isinstance(result, list) or not result:
            return False
        return (result[0].get("blockTime") or 0) >= cutoff
    except Exception:
        return False


# ---------- MAIN ----------

def discover_wallets(progress_callback=None) -> dict:
    def _p(msg):
        if progress_callback:
            progress_callback(msg)

    if not HELIUS_API_KEY:
        _p("❌ HELIUS_API_KEY not set in Railway env vars. Add it and redeploy.")
        return {"added": 0, "skipped_quality": 0, "skipped_inactive": 0,
                "skipped_duplicate": 0, "total_checked": 0, "sources": {}}

    already = {w["address"].lower() for w in load_wallets()}

    _p(f"🔍 Fetching top {TARGET_TOKENS} trending Solana tokens from GeckoTerminal...")
    tokens = _get_trending_tokens(limit=TARGET_TOKENS)

    if not tokens:
        _p("⚠️ GeckoTerminal returned 0 tokens. Check Railway connectivity.")
        return {"added": 0, "skipped_quality": 0, "skipped_inactive": 0,
                "skipped_duplicate": len(already), "total_checked": 0, "sources": {}}

    _p(f"📋 {len(tokens)} trending tokens. Fetching top {HOLDERS_PER_TOKEN} holders each via Helius...")

    wallet_hits: dict = {}

    for i, tok in enumerate(tokens):
        dbg    = _p if i < 3 else None
        owners = _get_holder_wallets(tok["mint"], debug_callback=dbg)
        sym    = tok.get("symbol") or tok["mint"][:6]

        for addr in owners:
            if addr.lower() in already:
                continue
            if addr not in wallet_hits:
                wallet_hits[addr] = {"count": 0, "tokens": []}
            wallet_hits[addr]["count"]  += 1
            wallet_hits[addr]["tokens"].append(sym)

        if (i + 1) % 20 == 0:
            _p(f"⏳ {i+1}/{len(tokens)} tokens done — {len(wallet_hits)} unique wallets so far")

        time.sleep(0.2)

    total_unique = len(wallet_hits)
    _p(f"👥 {total_unique} unique wallets found across {len(tokens)} tokens.")

    if total_unique == 0:
        _p("⚠️ 0 wallets found. Helius may be down or tokens have no holders.")
        return {
            "added": 0, "skipped_quality": 0, "skipped_inactive": 0,
            "skipped_duplicate": len(already), "total_checked": 0, "sources": {"helius": 0},
        }

    # Wallets in 2+ tokens = smart money
    candidates = sorted(
        [(a, info) for a, info in wallet_hits.items() if info["count"] >= MIN_TOKEN_HITS],
        key=lambda x: x[1]["count"],
        reverse=True,
    )
    skipped_quality = total_unique - len(candidates)
    _p(f"⭐ {len(candidates)} wallets appeared in 2+ tokens (smart money candidates). Checking activity...")

    if not candidates:
        _p(f"⚠️ No wallet appeared in 2+ tokens out of {total_unique} found. Market may be fragmented — try again later.")
        return {
            "added": 0, "skipped_quality": skipped_quality, "skipped_inactive": 0,
            "skipped_duplicate": len(already), "total_checked": 0, "sources": {"helius": 0},
        }

    # Activity check
    added            = 0
    skipped_inactive = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_map = {ex.submit(_is_active, addr): (addr, info) for addr, info in candidates}
        for future in as_completed(future_map):
            addr, info = future_map[future]
            try:
                if not future.result():
                    skipped_inactive += 1
                    continue
                sym_str = "-".join(info["tokens"][:3])
                label   = f"disc-{info['count']}x-{sym_str}"[:40]
                if add_wallet(addr, label, source="auto-discovery"):
                    added += 1
            except Exception:
                skipped_inactive += 1

    return {
        "added":             added,
        "skipped_quality":   skipped_quality,
        "skipped_inactive":  skipped_inactive,
        "skipped_duplicate": len(already),
        "total_checked":     len(candidates),
        "sources":           {"helius": added},
    }
