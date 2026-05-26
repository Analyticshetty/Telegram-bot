"""
Wallet Discovery — finds active profitable wallets using Solscan + GeckoTerminal.

Strategy:
  1. Pull Solana tokens from GeckoTerminal (trending + new)
  2. For each token, get top holders via Solscan free API
     Solscan returns OWNER wallets directly — no ATA resolution, no Solana RPC
  3. Wallets appearing as top holder in 2+ tokens = smart money candidate
  4. Verify activity via Solana RPC getSignaturesForAddress (1 call per candidate)
  5. Add to smart_wallets.json
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
SOLSCAN_HOLDERS = "https://public-api.solscan.io/token/holders"
TIMEOUT        = 10
GECKO_TIMEOUT  = 6
ACTIVITY_DAYS  = 7
MIN_TOKEN_HITS = 2
MAX_WORKERS    = 5
SOLANA_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

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


# ---------- Solana RPC (only used for activity check) ----------

def _rpc(method: str, params: list):
    try:
        r = requests.post(
            SOLANA_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=TIMEOUT,
        )
        body = r.json()
        if "error" in body:
            return None
        return body.get("result")
    except Exception:
        return None


# ---------- TOKEN DISCOVERY ----------

def _get_tokens(limit: int = 60) -> list:
    tokens = []
    seen   = set()

    for url in (GECKO_TRENDING, GECKO_NEW):
        for page in range(1, 3):
            try:
                r = requests.get(
                    url,
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

                    if liq < 5_000 or vol < 500 or pc <= 0:
                        continue

                    tokens.append({
                        "mint":   mint,
                        "symbol": (attr.get("name") or mint[:6]).split("/")[0].strip(),
                    })

                    if len(tokens) >= limit:
                        break

                time.sleep(0.4)

            except Exception as e:
                log.warning(f"GeckoTerminal error: {e}")
                break

            if len(tokens) >= limit:
                break

        if len(tokens) >= limit:
            break

    return tokens[:limit]


# ---------- HOLDER LOOKUP via Solscan ----------

def _get_holder_wallets(mint: str, debug_callback=None) -> list:
    """
    Returns up to 10 owner wallet addresses using Solscan free holder API.
    Solscan returns actual wallet owners — no ATA resolution, no Solana RPC calls.
    """
    try:
        r = requests.get(
            SOLSCAN_HOLDERS,
            params={"tokenAddress": mint, "limit": 10, "offset": 0},
            headers={"accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            if debug_callback:
                debug_callback(f"🔬 {mint[:8]}: Solscan HTTP {r.status_code}")
            return []

        data = r.json().get("data") or []
        owners = []
        for item in data:
            # Solscan returns 'owner' = actual wallet, 'address' = token account (ATA)
            addr = item.get("owner") or ""
            if not addr:
                addr = item.get("address") or ""
            if addr and SOLANA_MINT_RE.match(addr) and addr not in SKIP_ADDRESSES:
                owners.append(addr)

        if debug_callback:
            debug_callback(f"🔬 {mint[:8]}: Solscan → {len(owners)} holders")

        return owners

    except Exception as e:
        if debug_callback:
            debug_callback(f"🔬 {mint[:8]}: Solscan error: {e}")
        return []


# ---------- ACTIVITY CHECK ----------

def _is_active(address: str) -> bool:
    cutoff = int(time.time()) - (ACTIVITY_DAYS * 86400)
    result = _rpc("getSignaturesForAddress", [address, {"limit": 1, "commitment": "confirmed"}])
    if not isinstance(result, list) or not result:
        return False
    return (result[0].get("blockTime") or 0) >= cutoff


# ---------- MAIN ----------

def discover_wallets(progress_callback=None) -> dict:
    def _p(msg):
        if progress_callback:
            progress_callback(msg)

    already = {w["address"].lower() for w in load_wallets()}

    # Step 1: get tokens
    _p("🔍 Fetching tokens from GeckoTerminal (trending + new pools)...")
    tokens = _get_tokens(limit=60)

    if not tokens:
        _p("⚠️ No tokens returned by GeckoTerminal. Check Railway internet connectivity.")
        return {"added": 0, "skipped_quality": 0, "skipped_inactive": 0,
                "skipped_duplicate": len(already), "total_checked": 0, "sources": {}}

    _p(f"📋 {len(tokens)} tokens found. Fetching top holders via Solscan...")

    # Step 2: for each token, collect top holder wallets
    wallet_hits: dict = {}
    solscan_errors = 0

    for i, tok in enumerate(tokens):
        dbg = _p if i < 5 else None
        owners = _get_holder_wallets(tok["mint"], debug_callback=dbg)
        sym    = tok.get("symbol") or tok["mint"][:6]

        if not owners and i < 5:
            solscan_errors += 1

        for addr in owners:
            if addr.lower() in already:
                continue
            if addr not in wallet_hits:
                wallet_hits[addr] = {"count": 0, "tokens": []}
            wallet_hits[addr]["count"]  += 1
            wallet_hits[addr]["tokens"].append(sym)

        if (i + 1) % 10 == 0:
            _p(f"⏳ {i+1}/{len(tokens)} tokens done — {len(wallet_hits)} unique wallets so far")

        time.sleep(0.5)

    total_unique = len(wallet_hits)

    if total_unique == 0:
        if solscan_errors >= 3:
            _p("❌ Solscan API blocked or down. Try: /addwallet <addr> <label> manually.")
        else:
            _p(f"⚠️ {len(tokens)} tokens checked, 0 new unique wallets found (all may be already tracked or filtered).")
        return {
            "added": 0, "skipped_quality": 0, "skipped_inactive": 0,
            "skipped_duplicate": len(already), "total_checked": 0, "sources": {"solscan": 0},
        }

    _p(f"👥 {total_unique} unique wallets across all tokens.")

    # Step 3: keep wallets appearing in 2+ tokens
    candidates = sorted(
        [(a, info) for a, info in wallet_hits.items() if info["count"] >= MIN_TOKEN_HITS],
        key=lambda x: x[1]["count"],
        reverse=True,
    )
    skipped_quality = total_unique - len(candidates)

    if not candidates:
        _p(f"⚠️ {total_unique} wallets found but none in 2+ tokens — lowering to 1 for this run...")
        candidates = list(wallet_hits.items())[:30]
        skipped_quality = 0

    _p(f"⭐ {len(candidates)} candidates. Checking on-chain activity (last 7 days)...")

    # Step 4: activity check
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
        "sources":           {"solscan": added},
    }
