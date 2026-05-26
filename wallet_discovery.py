"""
Wallet Discovery — finds active profitable wallets using only public Solana RPC + GeckoTerminal.

Strategy:
  1. Pull up to 60 Solana tokens from GeckoTerminal (trending + new pools, loose filters)
  2. For each token, get top holders via Solana RPC getTokenLargestAccounts
     (resolves each token account to its owner wallet address)
  3. Wallets appearing as top holder in 2+ different tokens = smart money candidate
  4. Verify activity: must have tx in last 7 days
  5. Add to smart_wallets.json

100% free, no auth, no Cloudflare. All public RPC.
"""

import requests
import time
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from smart_wallets import add_wallet, load_wallets

log = logging.getLogger(__name__)

SOLANA_RPC     = "https://api.mainnet-beta.solana.com"
GECKO_TRENDING = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
GECKO_NEW      = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
TIMEOUT        = 10
ACTIVITY_DAYS  = 7
MIN_TOKEN_HITS = 2
MAX_WORKERS    = 6
SOLANA_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Known non-wallet addresses to skip
SKIP_ADDRESSES = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bNX",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
    "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",  # serum dex
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # raydium v4
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # raydium authority
}


# ---------- TOKEN DISCOVERY ----------

def _get_tokens(limit: int = 60) -> list:
    """
    Returns Solana token mints from GeckoTerminal trending + new pools.
    Filters loosened: any token with liq > $5K and vol > $500 and positive price.
    """
    tokens = []
    seen   = set()

    for url, page_count in ((GECKO_TRENDING, 2), (GECKO_NEW, 3)):
        for page in range(1, page_count + 1):
            try:
                r = requests.get(
                    url,
                    params={"page": page},
                    headers={"Accept": "application/json"},
                    timeout=TIMEOUT,
                )
                if r.status_code != 200:
                    break
                pools = r.json().get("data") or []
                if not pools:
                    break

                for pool in pools:
                    attr  = pool.get("attributes") or {}
                    rel   = pool.get("relationships") or {}
                    bt_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
                    mint  = bt_id.replace("solana_", "")

                    if not mint or not SOLANA_MINT_RE.match(mint) or mint in seen:
                        continue
                    seen.add(mint)

                    liq = float(attr.get("reserve_in_usd") or 0)
                    vol = float((attr.get("volume_usd") or {}).get("h24") or 0)
                    pc  = float((attr.get("price_change_percentage") or {}).get("h24") or 0)

                    # Very loose filters — just needs to be alive
                    if liq < 5_000:
                        continue
                    if vol < 500:
                        continue
                    if pc <= 0:
                        continue

                    tokens.append({
                        "mint":   mint,
                        "symbol": (attr.get("name") or mint[:6]).split("/")[0].strip(),
                        "liq":    liq,
                        "vol":    vol,
                    })

                    if len(tokens) >= limit:
                        break

                time.sleep(0.5)   # GeckoTerminal: 30 req/min free

            except Exception as e:
                log.warning(f"GeckoTerminal {url} page {page}: {e}")
                break

            if len(tokens) >= limit:
                break

        if len(tokens) >= limit:
            break

    return tokens[:limit]


# ---------- HOLDER RESOLUTION ----------

def _rpc(method, params):
    r = requests.post(
        SOLANA_RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=TIMEOUT,
    )
    return r.json().get("result") if r.status_code == 200 else None


def _get_holder_wallets(mint: str) -> list:
    """
    Returns top holder wallet addresses for a token mint using Solana RPC.
    getTokenLargestAccounts → list of ATAs → resolve each ATA to owner wallet.
    """
    try:
        result = _rpc("getTokenLargestAccounts", [mint, "confirmed"])
        if not result:
            return []
        accounts = result.get("value") or []
        if not accounts:
            return []

        # Filter to accounts with meaningful balance
        atas = [
            a["address"] for a in accounts[:10]
            if a.get("uiAmount") and float(a.get("uiAmount") or 0) > 0
        ]
        if not atas:
            return []

        # Batch resolve ATAs to owner wallets via getMultipleAccounts
        batch_result = _rpc("getMultipleAccounts", [atas, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        if not batch_result:
            return []

        owners = []
        for acct in (batch_result.get("value") or []):
            if not acct:
                continue
            try:
                owner = (
                    acct.get("data", {})
                        .get("parsed", {})
                        .get("info", {})
                        .get("owner") or ""
                )
                if (owner
                        and SOLANA_MINT_RE.match(owner)
                        and owner not in SKIP_ADDRESSES
                        and len(owner) >= 32):
                    owners.append(owner)
            except Exception:
                continue
        return owners

    except Exception as e:
        log.warning(f"Holder resolution failed for {mint[:8]}: {e}")
        return []


# ---------- ACTIVITY CHECK ----------

def _is_active(address: str) -> bool:
    cutoff = int(time.time()) - (ACTIVITY_DAYS * 86400)
    try:
        result = _rpc("getSignaturesForAddress", [address, {"limit": 1, "commitment": "confirmed"}])
        sigs   = result if isinstance(result, list) else []
        return bool(sigs) and (sigs[0].get("blockTime") or 0) >= cutoff
    except Exception:
        return False


# ---------- MAIN ----------

def discover_wallets(progress_callback=None) -> dict:
    def _p(msg):
        if progress_callback:
            progress_callback(msg)

    already = {w["address"].lower() for w in load_wallets()}

    # Step 1: tokens
    _p("🔍 Fetching Solana tokens from GeckoTerminal (trending + new pools)...")
    tokens = _get_tokens(limit=60)

    if not tokens:
        _p("⚠️ GeckoTerminal returned nothing. Check internet/Railway connectivity.")
        return {"added": 0, "skipped_quality": 0, "skipped_inactive": 0,
                "skipped_duplicate": len(already), "total_checked": 0, "sources": {}}

    _p(f"📋 {len(tokens)} tokens found. Resolving top holders via Solana RPC...")

    # Step 2: collect wallet hits
    wallet_hits: dict = {}

    for i, tok in enumerate(tokens):
        owners = _get_holder_wallets(tok["mint"])
        sym    = tok.get("symbol") or tok["mint"][:6]
        for addr in owners:
            if addr.lower() in already:
                continue
            if addr not in wallet_hits:
                wallet_hits[addr] = {"count": 0, "tokens": []}
            wallet_hits[addr]["count"]  += 1
            wallet_hits[addr]["tokens"].append(sym)

        if (i + 1) % 10 == 0:
            _p(f"⏳ {i+1}/{len(tokens)} tokens done — {len(wallet_hits)} unique wallets so far")
        time.sleep(0.2)

    _p(f"👥 {len(wallet_hits)} unique wallets found across all tokens.")

    # Step 3: filter by multi-token presence
    candidates = sorted(
        [(a, info) for a, info in wallet_hits.items() if info["count"] >= MIN_TOKEN_HITS],
        key=lambda x: x[1]["count"],
        reverse=True,
    )
    skipped_quality = len(wallet_hits) - len(candidates)
    _p(f"⭐ {len(candidates)} wallets held tokens across {MIN_TOKEN_HITS}+ winners. Checking activity...")

    if not candidates:
        _p("⚠️ No multi-token wallets found. Market may be thin right now — try again in a few hours.")
        return {"added": 0, "skipped_quality": skipped_quality, "skipped_inactive": 0,
                "skipped_duplicate": len(already), "total_checked": 0, "sources": {}}

    # Step 4: parallel activity check
    added = 0
    skipped_inactive = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_map = {ex.submit(_is_active, addr): (addr, info) for addr, info in candidates}
        for future in as_completed(future_map):
            addr, info = future_map[future]
            try:
                if not future.result():
                    skipped_inactive += 1
                    continue
                sym_str = "-".join(info["tokens"][:2])
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
        "sources":           {"gecko+rpc": added},
    }
