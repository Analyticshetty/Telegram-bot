"""
Wallet Discovery — finds active profitable wallets using public Solana RPC + GeckoTerminal.

Strategy:
  1. Pull Solana tokens from GeckoTerminal (trending + new, loose filters)
  2. For each token, get top holders via Solana RPC getTokenLargestAccounts
     then resolve each token account (ATA) to its owner wallet via getAccountInfo
  3. Wallets appearing as top holder in 2+ tokens = smart money candidate
  4. Verify each wallet has on-chain activity in last 7 days
  5. Add to smart_wallets.json

Free, no auth, no Cloudflare. All public Solana RPC.
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
TIMEOUT        = 8
GECKO_TIMEOUT  = 6   # GeckoTerminal specifically — shorter, fail fast
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


# ---------- RPC helper ----------

def _rpc(method: str, params: list):
    """POST to Solana RPC. Returns result or None. Logs errors."""
    try:
        r = requests.post(
            SOLANA_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=TIMEOUT,
        )
        body = r.json()
        if "error" in body:
            log.warning(f"RPC {method} error: {body['error']}")
            return None
        return body.get("result")
    except Exception as e:
        log.warning(f"RPC {method} exception: {e}")
        return None


# ---------- TOKEN DISCOVERY ----------

def _get_tokens(limit: int = 60) -> list:
    tokens = []
    seen   = set()

    for url in (GECKO_TRENDING, GECKO_NEW):
        for page in range(1, 3):   # max 2 pages per source
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


# ---------- HOLDER RESOLUTION ----------

def _resolve_ata_owner(ata_address: str) -> str:
    """Returns the owner wallet of a token account (ATA). Empty string on failure."""
    result = _rpc("getAccountInfo", [ata_address, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    if not result:
        return ""
    value = result.get("value") or {}
    try:
        owner = value["data"]["parsed"]["info"]["owner"]
        if owner and SOLANA_MINT_RE.match(owner) and owner not in SKIP_ADDRESSES:
            return owner
    except (KeyError, TypeError):
        pass
    return ""


def _get_holder_wallets(mint: str) -> list:
    """Returns up to 8 owner wallet addresses for the top holders of a token."""
    # Step 1: get top token accounts
    result = _rpc("getTokenLargestAccounts", [mint])
    if not result:
        return []

    atas = []
    for acct in (result.get("value") or [])[:8]:
        addr = acct.get("address") or ""
        amt  = float(acct.get("uiAmount") or 0)
        if addr and amt > 0:
            atas.append(addr)

    if not atas:
        return []

    # Step 2: resolve each ATA to its owner wallet (parallel)
    owners = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_resolve_ata_owner, ata): ata for ata in atas}
        for future in as_completed(futures):
            owner = future.result()
            if owner:
                owners.append(owner)

    return owners


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

    _p(f"📋 {len(tokens)} tokens found. Resolving top holders via Solana RPC...")

    # Step 2: for each token, collect top holder wallets
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
            _p(f"⏳ {i+1}/{len(tokens)} tokens done — {len(wallet_hits)} unique wallets found so far")

        time.sleep(0.3)

    total_unique = len(wallet_hits)
    _p(f"👥 {total_unique} unique wallets found across all tokens.")

    # Step 3: keep wallets that appeared in 2+ different tokens
    candidates = sorted(
        [(a, info) for a, info in wallet_hits.items() if info["count"] >= MIN_TOKEN_HITS],
        key=lambda x: x[1]["count"],
        reverse=True,
    )
    skipped_quality = total_unique - len(candidates)

    if not candidates:
        _p(
            f"⚠️ {total_unique} wallets found but none appeared in {MIN_TOKEN_HITS}+ tokens. "
            "Lowering threshold to 1 for this run to seed the list..."
        )
        # Fall back: take any wallet that appeared at least once, top 30 by nothing
        candidates = list(wallet_hits.items())[:30]
        skipped_quality = 0

    _p(f"⭐ {len(candidates)} candidate wallets. Verifying on-chain activity (last 7 days)...")

    # Step 4: activity check in parallel
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
        "sources":           {"gecko+rpc": added},
    }
