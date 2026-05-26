"""
Wallet Discovery — finds active profitable wallets without any paid APIs or Cloudflare.

Strategy (same logic as Dragon, using only APIs already in the bot):
  1. Pull 30 recently successful Solana tokens from GeckoTerminal
     (age 1-48h, high volume, positive price, liquidity $10K-$2M)
  2. For each token, fetch top 10 holders via GoPlus
  3. Wallets appearing as top holder in 2+ DIFFERENT winning tokens = smart money
  4. Filter out known program/pool addresses
  5. Verify each candidate has on-chain activity in last 7 days (Solana RPC)
  6. Add qualifying wallets to smart_wallets.json

No auth. No Cloudflare. All free. Self-improving — runs anytime with /discoverwallet.
"""

import requests
import time
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from smart_wallets import add_wallet, load_wallets

log = logging.getLogger(__name__)

SOLANA_RPC      = "https://api.mainnet-beta.solana.com"
GOPLUS_URL      = "https://api.gopluslabs.io/api/v1/solana/token_security"
GECKO_TRENDING  = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
GECKO_NEW       = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
TIMEOUT         = 10
ACTIVITY_DAYS   = 7
MIN_TOKEN_HITS  = 2      # wallet must appear in top-holders of this many winning tokens
MAX_WORKERS     = 8
SOLANA_MINT_RE  = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Known non-human addresses to skip
SKIP_ADDRESSES = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bNX",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
}


# ---------- TOKEN DISCOVERY ----------

def _get_successful_tokens(limit: int = 30) -> list:
    """Returns list of winning Solana tokens from GeckoTerminal trending + new pools."""
    tokens = []
    seen   = set()

    for url in (GECKO_TRENDING, GECKO_NEW):
        try:
            r = requests.get(
                url,
                params={"page": 1},
                headers={"Accept": "application/json"},
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                continue
            pools = r.json().get("data") or []
            for pool in pools:
                attr = pool.get("attributes") or {}
                rel  = pool.get("relationships") or {}

                # Extract mint from relationships
                bt_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
                mint  = bt_id.replace("solana_", "")

                if not mint or not SOLANA_MINT_RE.match(mint) or mint in seen:
                    continue
                seen.add(mint)

                liq = float(attr.get("reserve_in_usd") or 0)
                vol = float((attr.get("volume_usd") or {}).get("h24") or 0)
                pc  = float((attr.get("price_change_percentage") or {}).get("h24") or 0)

                # Parse age
                age_minutes = None
                created_at  = attr.get("pool_created_at") or ""
                if created_at:
                    try:
                        from datetime import datetime, timezone
                        created     = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        age_minutes = (datetime.now(timezone.utc) - created).total_seconds() / 60
                    except Exception:
                        pass

                # Quality filter
                if liq < 10_000 or liq > 2_000_000:
                    continue
                if vol < 5_000:
                    continue
                if pc < 5:
                    continue
                if age_minutes and (age_minutes < 60 or age_minutes > 2880):
                    continue

                tokens.append({
                    "mint":    mint,
                    "symbol":  attr.get("name") or mint[:6],
                    "liq":     liq,
                    "vol":     vol,
                    "pc":      pc,
                })

                if len(tokens) >= limit:
                    break
        except Exception as e:
            log.warning(f"GeckoTerminal fetch failed: {e}")

        if len(tokens) >= limit:
            break

    return tokens[:limit]


# ---------- HOLDER EXTRACTION ----------

def _get_top_holders(mint: str) -> list:
    """Returns top holder wallet addresses from GoPlus."""
    try:
        r = requests.get(GOPLUS_URL, params={"contract_addresses": mint}, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        data   = r.json()
        result = (data.get("result") or {}).get(mint) or (data.get("result") or {}).get(mint.lower())
        if not result or not isinstance(result, dict):
            return []
        holders = result.get("holders") or []
        addrs   = []
        for h in holders[:10]:
            if not isinstance(h, dict):
                continue
            addr = h.get("address") or ""
            if (addr and SOLANA_MINT_RE.match(addr) and addr not in SKIP_ADDRESSES):
                addrs.append(addr)
        return addrs
    except Exception:
        return []


# ---------- ACTIVITY VERIFIER ----------

def _is_active(address: str) -> bool:
    """True if wallet has at least 1 tx in last ACTIVITY_DAYS days."""
    cutoff = int(time.time()) - (ACTIVITY_DAYS * 86400)
    try:
        r = requests.post(
            SOLANA_RPC,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "getSignaturesForAddress",
                "params":  [address, {"limit": 1, "commitment": "confirmed"}],
            },
            timeout=TIMEOUT,
        )
        sigs = (r.json().get("result") or []) if r.status_code == 200 else []
        return bool(sigs) and (sigs[0].get("blockTime") or 0) >= cutoff
    except Exception:
        return False


# ---------- MAIN ----------

def discover_wallets(progress_callback=None) -> dict:
    """Full discovery run. Returns stats dict."""
    def _p(msg):
        if progress_callback:
            progress_callback(msg)
        log.info(msg)

    already = {w["address"].lower() for w in load_wallets()}

    # Step 1: winning tokens
    _p("🔍 Finding recently successful Solana tokens from GeckoTerminal...")
    tokens = _get_successful_tokens(limit=30)
    if not tokens:
        _p("⚠️ No tokens found. Retrying in 5s...")
        time.sleep(5)
        tokens = _get_successful_tokens(limit=30)

    if not tokens:
        return {"added": 0, "skipped_quality": 0, "skipped_inactive": 0,
                "skipped_duplicate": 0, "total_checked": 0, "sources": {},
                "error": "GeckoTerminal returned no qualifying tokens"}

    _p(f"📋 {len(tokens)} winning tokens found. Fetching top holders for each...")

    # Step 2: collect holder hits per wallet
    wallet_hits: dict = {}

    for i, tok in enumerate(tokens):
        holders = _get_top_holders(tok["mint"])
        sym     = tok.get("symbol") or tok["mint"][:6]
        for addr in holders:
            if addr.lower() in already:
                continue
            if addr not in wallet_hits:
                wallet_hits[addr] = {"count": 0, "tokens": []}
            wallet_hits[addr]["count"]  += 1
            wallet_hits[addr]["tokens"].append(sym)
        if (i + 1) % 5 == 0:
            _p(f"⏳ {i+1}/{len(tokens)} tokens processed — {len(wallet_hits)} unique wallets seen so far")
        time.sleep(0.4)   # respect GoPlus ~20 req/s free limit

    _p(f"👥 {len(wallet_hits)} unique wallets found. Filtering by multi-token presence...")

    # Step 3: keep only wallets that appeared in 2+ tokens
    candidates = sorted(
        [(a, info) for a, info in wallet_hits.items() if info["count"] >= MIN_TOKEN_HITS],
        key=lambda x: x[1]["count"],
        reverse=True,
    )
    skipped_quality = len(wallet_hits) - len(candidates)

    if not candidates:
        _p(
            "⚠️ No wallets appeared in 2+ winning tokens. "
            "Market may be slow — try again in a few hours when more tokens have graduated."
        )
        return {"added": 0, "skipped_quality": skipped_quality, "skipped_inactive": 0,
                "skipped_duplicate": len(already), "total_checked": 0, "sources": {}}

    _p(f"⭐ {len(candidates)} wallets in top-holders of 2+ winning tokens. Verifying activity on-chain...")

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
                tokens_str = "-".join(info["tokens"][:2])
                label = f"disc-{info['count']}x-{tokens_str}"[:40]
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
        "sources":           {"gecko+goplus": added},
    }
