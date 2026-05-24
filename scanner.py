"""
Bitget-Latest-equivalent Solana token scanner.

Pulls fresh Solana pools from GeckoTerminal (free, no auth) → runs each
through the full rug-check stack (GoPlus + Rugcheck + Solana RPC + DEXScreener)
→ returns the top tokens that pass Bitget-style filters.

This is the universe Bitget's "Latest" panel shows, sourced from the same
underlying on-chain data via GeckoTerminal.
"""

import requests
from datetime import datetime, timezone
from rug_check import check_token

GECKOTERMINAL_NEW    = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
GECKOTERMINAL_TREND  = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
TIMEOUT              = 10

# Bitget-Latest-equivalent filter thresholds
FILTER_DEFAULTS = {
    "min_liquidity_usd":   5000,
    "max_liquidity_usd":   2_000_000,
    "min_market_cap":      30_000,
    "max_market_cap":      5_000_000,
    "min_age_minutes":     5,
    "max_age_minutes":     1440,    # 24h
    "min_volume_1h_usd":   1000,
    "max_top10_pct":       35.0,
    "min_buy_ratio_1h":    0.45,    # at least 45% buys
}


def fetch_geckoterminal_pools(endpoint: str, page: int = 1):
    try:
        r = requests.get(f"{endpoint}?page={page}", timeout=TIMEOUT, headers={"Accept": "application/json;version=20230302"})
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("data") or []
    except Exception:
        return []


def extract_pool_info(pool: dict) -> dict:
    """Pull the bits we need from a GeckoTerminal pool object."""
    attrs = pool.get("attributes") or {}
    rels  = (pool.get("relationships") or {})
    base  = ((rels.get("base_token") or {}).get("data") or {}).get("id") or ""
    # base_token id looks like 'solana_<mint>' — strip prefix
    mint = base.split("_", 1)[1] if "_" in base else base

    created_at = attrs.get("pool_created_at")
    age_min = None
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
        except Exception:
            pass

    def f(x):
        try: return float(x)
        except: return None

    vol_1h = ((attrs.get("volume_usd") or {}).get("h1"))
    txns_1h = (attrs.get("transactions") or {}).get("h1") or {}
    buys_1h, sells_1h = txns_1h.get("buys") or 0, txns_1h.get("sells") or 0
    buy_ratio = buys_1h / max(buys_1h + sells_1h, 1)

    return {
        "mint":              mint,
        "name":              attrs.get("name"),
        "symbol":            (attrs.get("name") or "").split(" / ")[0] if attrs.get("name") else "",
        "dex":               attrs.get("dex_id"),
        "liquidity_usd":     f(attrs.get("reserve_in_usd")),
        "market_cap_usd":    f(attrs.get("market_cap_usd")) or f(attrs.get("fdv_usd")),
        "price_usd":         f(attrs.get("base_token_price_usd")),
        "price_change_1h":   f((attrs.get("price_change_percentage") or {}).get("h1")),
        "price_change_24h":  f((attrs.get("price_change_percentage") or {}).get("h24")),
        "volume_1h":         f(vol_1h),
        "buys_1h":           buys_1h,
        "sells_1h":          sells_1h,
        "buy_ratio_1h":      buy_ratio,
        "age_minutes":       age_min,
        "pool_address":      attrs.get("address"),
    }


def passes_pre_filter(info: dict, f=FILTER_DEFAULTS) -> tuple[bool, list]:
    """Fast pre-filter using only GeckoTerminal data (cheap)."""
    fails = []
    if not info.get("mint"):
        fails.append("no mint address")
    liq = info.get("liquidity_usd")
    if liq is None or liq < f["min_liquidity_usd"]:
        fails.append(f"liquidity < ${f['min_liquidity_usd']:,}")
    elif liq > f["max_liquidity_usd"]:
        fails.append(f"liquidity > ${f['max_liquidity_usd']:,}")
    mc = info.get("market_cap_usd")
    if mc is not None:
        if mc < f["min_market_cap"]:
            fails.append(f"MC < ${f['min_market_cap']:,}")
        elif mc > f["max_market_cap"]:
            fails.append(f"MC > ${f['max_market_cap']:,}")
    age = info.get("age_minutes")
    if age is None or age < f["min_age_minutes"]:
        fails.append(f"age < {f['min_age_minutes']}min")
    elif age > f["max_age_minutes"]:
        fails.append(f"age > {f['max_age_minutes']}min")
    vol = info.get("volume_1h")
    if vol is None or vol < f["min_volume_1h_usd"]:
        fails.append(f"1h vol < ${f['min_volume_1h_usd']:,}")
    if info.get("buy_ratio_1h", 0) < f["min_buy_ratio_1h"]:
        fails.append(f"buy ratio < {f['min_buy_ratio_1h']:.0%}")
    return (len(fails) == 0, fails)


def scan(limit_results: int = 5, include_trending: bool = True) -> list:
    """Returns a ranked list of candidates that pass ALL filters."""
    pools = []
    pools += fetch_geckoterminal_pools(GECKOTERMINAL_NEW, page=1)
    pools += fetch_geckoterminal_pools(GECKOTERMINAL_NEW, page=2)
    if include_trending:
        pools += fetch_geckoterminal_pools(GECKOTERMINAL_TREND, page=1)

    # Dedupe by mint
    seen, candidates = set(), []
    for p in pools:
        info = extract_pool_info(p)
        if not info["mint"] or info["mint"] in seen:
            continue
        seen.add(info["mint"])
        ok, fails = passes_pre_filter(info)
        if ok:
            candidates.append(info)

    # Now run full rug-check on each pre-filtered candidate
    results = []
    for info in candidates[:20]:  # cap deep-check budget
        try:
            verdict = check_token(info["mint"])
            if verdict["verdict"] == "GREEN":
                info["verdict"] = "GREEN"
                info["verdict_details"] = verdict
                results.append(info)
            elif verdict["verdict"] == "YELLOW" and len(verdict.get("reasons_red") or []) == 0:
                info["verdict"] = "YELLOW"
                info["verdict_details"] = verdict
                results.append(info)
        except Exception:
            continue
        if len(results) >= limit_results:
            break

    # Rank: GREEN first, then by volume_1h desc
    results.sort(key=lambda x: (0 if x["verdict"] == "GREEN" else 1, -(x.get("volume_1h") or 0)))
    return results[:limit_results]


def format_scan_results(results: list) -> str:
    if not results:
        return ("🔍 *Scan complete* — no tokens passed all filters right now.\n\n"
                "Try again in 5–10 minutes. Filters: liq $5K–$2M, MC $30K–$5M, "
                "age 5min–24h, vol 1h >$1K, top10 <35%, GoPlus clean.")

    lines = [f"🔍 *Bitget-Latest scan* — {len(results)} candidates passed"]
    for i, r in enumerate(results, 1):
        v_icon = "🟢" if r["verdict"] == "GREEN" else "🟡"
        sym = r.get("symbol") or "?"
        mint = r["mint"]
        liq = r.get("liquidity_usd") or 0
        mc  = r.get("market_cap_usd") or 0
        age = r.get("age_minutes") or 0
        vol = r.get("volume_1h") or 0
        pc1 = r.get("price_change_1h") or 0
        age_str = f"{age:.0f}m" if age < 60 else f"{age/60:.1f}h"
        lines.append(
            f"\n{v_icon} *{i}. {sym}*\n"
            f"`{mint}`\n"
            f"💧 Liq ${liq:,.0f}  |  📈 MC ${mc:,.0f}\n"
            f"⏱ {age_str}  |  📊 1h vol ${vol:,.0f}  |  {pc1:+.1f}%\n"
        )
    lines.append("\n_Paste any CA back to me for a full /check report._")
    lines.append("_Then verify Bitget app shows 'no contract risks detected' before buying._")
    return "\n".join(lines)
