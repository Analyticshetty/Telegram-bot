"""
Solana token rug-check module.
Defensive only. Returns GREEN / YELLOW / RED verdict + reasons.

Free APIs used:
  - Rugcheck.xyz       (composite risk report)
  - DEXScreener        (liquidity / holders / age)
  - Solana mainnet RPC (mint & freeze authority — ground truth)
  - GoPlus Security    (Bitget-equivalent risk engine — Solana endpoint)
"""

import requests
import re
from datetime import datetime, timezone
from smart_wallets import format_smart_wallet_section
from trade_card import trade_card_for_check

RUGCHECK_URL       = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
DEXSCREENER_URL    = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/"
SOLANA_RPC         = "https://api.mainnet-beta.solana.com"
GOPLUS_URL         = "https://api.gopluslabs.io/api/v1/solana/token_security"
GECKO_TOKEN_URL    = "https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}"
TIMEOUT            = 8

SOLANA_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def is_valid_solana_mint(s: str) -> bool:
    return bool(s) and bool(SOLANA_MINT_RE.match(s.strip()))


def _rpc(method: str, params: list):
    r = requests.post(
        SOLANA_RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("result")


def get_mint_authorities(mint: str):
    """Returns (mint_authority, freeze_authority) — None means revoked (good)."""
    res = _rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    if not res or not res.get("value"):
        return ("UNKNOWN", "UNKNOWN")
    parsed = res["value"]["data"]["parsed"]["info"]
    return (parsed.get("mintAuthority"), parsed.get("freezeAuthority"))


def get_token_supply(mint: str):
    """Returns circulating supply as float (decimals applied), or None on failure.
    Ground truth — comes straight from Solana RPC, never stale."""
    try:
        res = _rpc("getTokenSupply", [mint])
        if not res or not res.get("value"):
            return None
        v = res["value"]
        ui = v.get("uiAmount")
        if ui is not None:
            return float(ui)
        amount = v.get("amount")
        decimals = v.get("decimals") or 0
        if amount is not None:
            return float(amount) / (10 ** decimals)
    except Exception:
        pass
    return None


def get_geckoterminal_price(mint: str):
    """Fresh price from GeckoTerminal as second source. Returns float or None."""
    try:
        r = requests.get(
            GECKO_TOKEN_URL.format(mint=mint),
            timeout=TIMEOUT,
            headers={"Accept": "application/json;version=20230302"},
        )
        if r.status_code != 200:
            return None
        attrs = ((r.json() or {}).get("data") or {}).get("attributes") or {}
        p = attrs.get("price_usd")
        return float(p) if p is not None else None
    except Exception:
        return None


def get_rugcheck(mint: str):
    try:
        r = requests.get(RUGCHECK_URL.format(mint=mint), timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def get_goplus(mint: str):
    """GoPlus Solana token security — Bitget-equivalent risk engine."""
    try:
        r = requests.get(GOPLUS_URL, params={"contract_addresses": mint}, timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            result = (data.get("result") or {}).get(mint) or (data.get("result") or {}).get(mint.lower())
            return result
    except Exception:
        pass
    return None


def search_by_symbol(symbol: str):
    """Search DEXScreener for all Solana pairs matching this symbol. Returns list."""
    if not symbol or len(symbol) < 2:
        return []
    try:
        r = requests.get(DEXSCREENER_SEARCH, params={"q": symbol}, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        pairs = (r.json() or {}).get("pairs") or []
        # Solana only, exact symbol match (case-insensitive)
        sym_l = symbol.lower()
        out = []
        for p in pairs:
            if p.get("chainId") != "solana":
                continue
            base = p.get("baseToken") or {}
            if (base.get("symbol") or "").lower() == sym_l:
                out.append(p)
        return out
    except Exception:
        return []


def detect_clone(current_mint: str, current_symbol: str, current_pair: dict):
    """Check if current token is the original or a clone.

    A peer only counts as a serious alternative if it's ALIVE. Dead-but-not-fully-drained
    rugger pairs were previously promoted as 'the real one' just because their stale liquidity
    was higher than the current young token's. Now we require recent activity:
      - volume_24h >= $500, OR
      - buys+sells in last 24h >= 20

    Otherwise the peer is marked dead and excluded from ranking.

    Returns dict with: is_original (bool), real_ca, reason, peers, dead_peer_count."""
    if not current_symbol:
        return {"is_original": None, "real_ca": None, "reason": "no symbol", "peers": []}
    peers = search_by_symbol(current_symbol)

    # Group by mint, keep the pair with highest liquidity per mint, but also retain its activity
    by_mint = {}
    for p in peers:
        base = p.get("baseToken") or {}
        m = base.get("address")
        if not m:
            continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        txns_h24 = (p.get("txns") or {}).get("h24") or {}
        buys  = txns_h24.get("buys")  or 0
        sells = txns_h24.get("sells") or 0
        if m not in by_mint or liq > by_mint[m]["liquidity"]:
            by_mint[m] = {
                "mint":       m,
                "liquidity":  liq,
                "created_at": p.get("pairCreatedAt") or 0,
                "volume_24h": (p.get("volume") or {}).get("h24") or 0,
                "txns_24h":   buys + sells,
                "fdv":        p.get("fdv") or 0,
                "pair_url":   p.get("url"),
            }
    candidates = list(by_mint.values())
    if len(candidates) <= 1:
        return {"is_original": True, "real_ca": current_mint, "reason": "only token with this symbol", "peers": [], "dead_peer_count": 0}

    # Liveness filter — dead peers can't be "the real one"
    def is_alive(c):
        # Always keep the current mint in the running so we can report on it
        if c["mint"].lower() == current_mint.lower():
            return True
        return (c["volume_24h"] or 0) >= 500 or (c["txns_24h"] or 0) >= 20

    alive = [c for c in candidates if is_alive(c)]
    dead_peer_count = len(candidates) - len(alive)

    # If after killing dead peers only the current mint remains, it's the only live one
    if len(alive) <= 1:
        return {
            "is_original":      True,
            "real_ca":          current_mint,
            "peer_count":       len(candidates),
            "alive_count":      len(alive),
            "dead_peer_count":  dead_peer_count,
            "reason":           f"only live '{current_symbol}' token ({dead_peer_count} dead peers ignored)",
            "peers":            [],
        }

    # Score live candidates only: liquidity dominates, recent volume tie-breaks, age small bonus
    def score(c):
        return (c["liquidity"] or 0) + (c["volume_24h"] or 0) * 0.1 + (1e15 - (c["created_at"] or 1e15)) * 1e-9

    alive.sort(key=score, reverse=True)
    top = alive[0]
    is_original = (top["mint"].lower() == current_mint.lower())
    return {
        "is_original":      is_original,
        "real_ca":          top["mint"],
        "real_liq":         top["liquidity"],
        "real_vol_24h":     top["volume_24h"],
        "real_url":         top["pair_url"],
        "peer_count":       len(candidates),
        "alive_count":      len(alive),
        "dead_peer_count":  dead_peer_count,
        "reason":           (
            f"this CA ranks #1 of {len(alive)} LIVE '{current_symbol}' tokens "
            f"({dead_peer_count} dead peers ignored)"
            if is_original
            else f"another '{current_symbol}' token is live with higher liquidity "
                 f"(${top['liquidity']:,.0f}, ${top['volume_24h']:,.0f} 24h vol)"
        ),
        "peers":            alive[:5],
    }


def detect_lifecycle_stage(dex: str, age_minutes):
    """Returns ('stage', 'description')."""
    if age_minutes is None:
        return ("unknown", "age unknown")
    dex_l = (dex or "").lower()
    if dex_l in ("pumpfun", "pump-fun", "pump"):
        return ("pre_grad", "Pre-graduation (still on pump.fun bonding curve)")
    if age_minutes < 60:
        return ("just_grad", f"Just-graduated ({age_minutes:.0f}min ago — most volatile)")
    if age_minutes < 720:
        return ("post_grad", f"Post-grad survivor ({age_minutes/60:.1f}h — the sweet spot)")
    if age_minutes < 1440:
        return ("maturing", f"Maturing ({age_minutes/60:.1f}h — past initial pump)")
    return ("established", f"Established ({age_minutes/1440:.1f}d old)")


def detect_wash_trading(pair: dict):
    """Heuristic wash-trading detection. Returns (is_wash, reason or None)."""
    if not pair:
        return (False, None)
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    vol_24h = (pair.get("volume") or {}).get("h24") or 0
    pc_24h = (pair.get("priceChange") or {}).get("h24") or 0
    txns_24h = (pair.get("txns") or {}).get("h24") or {}
    buys, sells = txns_24h.get("buys") or 0, txns_24h.get("sells") or 0
    if liq < 1000:
        return (False, None)
    vl_ratio = vol_24h / liq
    # Suspicious if huge volume but barely any price movement
    if vl_ratio > 20 and abs(pc_24h) < 15:
        return (True, f"vol/liq ratio {vl_ratio:.1f}x with only {pc_24h:+.1f}% price move — looks washed")
    # Suspicious if exact 50/50 buys/sells (bot pattern) at high volume
    if buys + sells > 200 and abs(buys - sells) / (buys + sells) < 0.02:
        return (True, f"buys/sells exactly balanced ({buys}/{sells}) — bot pattern")
    return (False, None)


def multi_window_flow(pair: dict):
    """Returns flow direction per window. Each entry: ('5m', buys, sells, ratio_pct, icon)."""
    if not pair:
        return []
    txns = pair.get("txns") or {}
    out = []
    for win in ("m5", "h1", "h6", "h24"):
        t = txns.get(win) or {}
        b, s = t.get("buys") or 0, t.get("sells") or 0
        total = b + s
        if total == 0:
            out.append((win, 0, 0, None, "—"))
            continue
        ratio = b / total * 100
        icon = "🟢" if ratio >= 55 else "🟡" if ratio >= 45 else "🔴"
        out.append((win, b, s, ratio, icon))
    return out


def detect_sniper_concentration(top_pct, age_minutes):
    """Heuristic: high top-holder % on a young token = snipers loaded up."""
    if top_pct is None or age_minutes is None:
        return (None, None)
    if age_minutes < 120 and top_pct > 35:
        return ("HIGH", f"Top holders own {top_pct:.0f}% on a {age_minutes:.0f}min-old token — sniper bags loaded")
    if age_minutes < 360 and top_pct > 50:
        return ("HIGH", f"Top holders own {top_pct:.0f}% on a {age_minutes/60:.1f}h-old token — heavy sniper concentration")
    if age_minutes < 720 and top_pct > 40:
        return ("MEDIUM", f"Top holders own {top_pct:.0f}% in post-grad window — watch for dumps")
    return ("LOW", None)


def get_dexscreener(mint: str):
    try:
        r = requests.get(DEXSCREENER_URL.format(mint=mint), timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            pairs = data.get("pairs") or []
            if pairs:
                pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
                return pairs[0]
    except Exception:
        pass
    return None


def check_token(mint: str) -> dict:
    """Returns a dict with verdict (GREEN/YELLOW/RED), reasons (list), and details."""
    mint = mint.strip()
    if not is_valid_solana_mint(mint):
        return {"verdict": "INVALID", "reasons": ["Not a valid Solana mint address"], "details": {}}

    reasons_red    = []
    reasons_yellow = []
    reasons_green  = []
    details = {"mint": mint}

    # Pre-fetch GoPlus once so we know trusted status before clone detection
    gp_data = get_goplus(mint)
    is_trusted_ecosystem = bool(gp_data and isinstance(gp_data, dict) and gp_data.get("trusted_token") in (1, "1"))

    # --- 1. Solana RPC: mint + freeze authority (ground truth) ---
    try:
        mint_auth, freeze_auth = get_mint_authorities(mint)
        details["mint_authority"]   = mint_auth
        details["freeze_authority"] = freeze_auth
        if mint_auth is None:
            reasons_green.append("Mint authority revoked")
        elif mint_auth == "UNKNOWN":
            reasons_yellow.append("Could not verify mint authority")
        else:
            reasons_red.append(f"Mint authority ACTIVE ({mint_auth[:8]}...) — can mint infinite supply")
        if freeze_auth is None:
            reasons_green.append("Freeze authority revoked")
        elif freeze_auth == "UNKNOWN":
            reasons_yellow.append("Could not verify freeze authority")
        else:
            reasons_red.append(f"Freeze authority ACTIVE ({freeze_auth[:8]}...) — can freeze your wallet")
    except Exception as e:
        reasons_yellow.append(f"RPC check failed: {e.__class__.__name__}")

    # --- 2. DEXScreener: liquidity, age, holders proxy ---
    pair = get_dexscreener(mint)
    if pair:
        liq_usd = (pair.get("liquidity") or {}).get("usd") or 0
        details["liquidity_usd"] = liq_usd
        details["price_usd"]     = pair.get("priceUsd")
        details["market_cap"]    = pair.get("marketCap")
        details["fdv"]           = pair.get("fdv")
        details["volume_24h"]    = (pair.get("volume") or {}).get("h24")
        details["volume_1h"]     = (pair.get("volume") or {}).get("h1")
        details["price_change_1h"]  = (pair.get("priceChange") or {}).get("h1")
        details["price_change_24h"] = (pair.get("priceChange") or {}).get("h24")
        details["pair_url"]      = pair.get("url")
        details["dex"]           = pair.get("dexId")
        details["pair_address"]  = pair.get("pairAddress")
        details["symbol"]        = (pair.get("baseToken") or {}).get("symbol")

        # --- FRESH MC COMPUTATION (multi-source) ---
        # DEXScreener's reported MC can lag 5–15 min on fast launches. Compute it ourselves
        # from on-chain supply (ground truth) × freshest price across DEXScreener + GeckoTerminal.
        try:
            supply = get_token_supply(mint)
            dex_price = None
            try:
                dex_price = float(pair.get("priceUsd")) if pair.get("priceUsd") else None
            except (TypeError, ValueError):
                pass
            gecko_price = get_geckoterminal_price(mint)

            # Pick the highest of the live prices (fast-moving launches tend to have the
            # stale source LAGGING below the real price). If they're within 5% just average.
            prices = [p for p in (dex_price, gecko_price) if p and p > 0]
            fresh_price = None
            if prices:
                if len(prices) == 1:
                    fresh_price = prices[0]
                else:
                    spread = abs(prices[0] - prices[1]) / max(prices)
                    fresh_price = sum(prices) / 2 if spread < 0.05 else max(prices)

            details["dex_price"]   = dex_price
            details["gecko_price"] = gecko_price
            details["fresh_price"] = fresh_price
            details["supply"]      = supply

            if supply and fresh_price:
                fresh_mc = supply * fresh_price
                details["fresh_mc"] = fresh_mc
                # If our computed MC differs meaningfully from DEXScreener's reported MC,
                # flag it and override the displayed MC with the fresh one.
                reported_mc = details.get("market_cap") or details.get("fdv")
                if reported_mc and reported_mc > 0:
                    gap = abs(fresh_mc - reported_mc) / reported_mc
                    details["mc_gap_pct"] = round(gap * 100, 1)
                    if gap > 0.20:
                        reasons_yellow.append(
                            f"MC source disagreement: DEXScreener ${reported_mc:,.0f} vs "
                            f"computed ${fresh_mc:,.0f} (Δ{gap*100:.0f}%) — using fresh"
                        )
                # Override so downstream (liq:MC ratio, trade card, display) uses fresh MC
                details["market_cap_reported"] = details.get("market_cap")
                details["market_cap"] = fresh_mc
                if details.get("fdv"):
                    details["fdv_reported"] = details["fdv"]
                    details["fdv"] = fresh_mc
        except Exception as e:
            log_msg = f"Fresh MC compute failed: {e.__class__.__name__}"
            reasons_yellow.append(log_msg) if False else None  # silent on failure

        if liq_usd < 1000:
            reasons_red.append(f"Liquidity only ${liq_usd:,.0f} — exit will be impossible")
        elif liq_usd < 10000:
            reasons_yellow.append(f"Thin liquidity ${liq_usd:,.0f} — high slippage risk")
        else:
            reasons_green.append(f"Liquidity ${liq_usd:,.0f}")

        # --- 2a. LIQUIDITY-TO-MARKET-CAP RATIO ---
        # Big MC + tiny liq = paper wealth that can't be sold. Classic memecoin trap.
        # Skip for trusted ecosystem tokens (SOL/USDC/etc — different math) and very small
        # MC tokens (<$20k — absolute check above is what matters there).
        mc_for_ratio = details.get("market_cap") or details.get("fdv")
        if (
            mc_for_ratio
            and mc_for_ratio >= 20_000
            and liq_usd > 0
            and not is_trusted_ecosystem
        ):
            ratio = liq_usd / mc_for_ratio
            ratio_pct = ratio * 100
            details["liq_to_mc_pct"] = round(ratio_pct, 2)
            if ratio < 0.01:
                reasons_red.append(
                    f"Liq only {ratio_pct:.1f}% of MC (${liq_usd:,.0f} vs ${mc_for_ratio:,.0f}) — exit will crash price"
                )
            elif ratio < 0.03:
                reasons_yellow.append(
                    f"Liq thin vs MC ({ratio_pct:.1f}%) — heavy slippage on exit"
                )
            elif ratio >= 0.05:
                reasons_green.append(f"Healthy liq:MC ratio ({ratio_pct:.1f}%)")

        created_ms = pair.get("pairCreatedAt")
        if created_ms:
            age_min = (datetime.now(timezone.utc).timestamp() * 1000 - created_ms) / 60000
            details["age_minutes"] = round(age_min, 1)
            if age_min < 10:
                reasons_red.append(f"Token only {age_min:.0f} min old — fresh launch, no track record")
            elif age_min < 60:
                reasons_yellow.append(f"Token {age_min:.0f} min old — very young")
            elif age_min < 1440:
                reasons_yellow.append(f"Token {age_min/60:.1f} h old")
            else:
                reasons_green.append(f"Token {age_min/1440:.1f} days old")

        txns = pair.get("txns") or {}
        h24 = txns.get("h24") or {}
        buys, sells = h24.get("buys") or 0, h24.get("sells") or 0
        details["txns_24h"] = {"buys": buys, "sells": sells}
        if buys + sells > 0:
            sell_pct = sells / (buys + sells) * 100
            if sell_pct > 70:
                reasons_red.append(f"Sell pressure {sell_pct:.0f}% in 24h — heavy dumping")
            elif sell_pct > 55:
                reasons_yellow.append(f"Sell-side {sell_pct:.0f}% — bearish flow")

        # --- 2a. CLONE DETECTION ---
        # Skip clone check entirely for ecosystem-trusted tokens (SOL, USDC, etc.)
        if is_trusted_ecosystem:
            reasons_green.append("Verified ecosystem token — clone check skipped")
            details["clone_check"] = {"is_original": True, "reason": "GoPlus trusted_token"}
        else:
            try:
                clone = detect_clone(mint, details.get("symbol"), pair)
                details["clone_check"] = clone
                sym = details.get("symbol")
                dead = clone.get("dead_peer_count", 0)
                if clone["is_original"] is False:
                    real_liq    = clone.get("real_liq", 0)
                    real_vol    = clone.get("real_vol_24h", 0)
                    this_liq    = details.get("liquidity_usd") or 1
                    gap         = real_liq / this_liq if this_liq > 0 else 1
                    # Only escalate to RED if the "real" peer is actually thriving
                    # (real volume is the proof it's alive — stale liq isn't)
                    if gap >= 10 and real_vol >= 5000:
                        reasons_red.append(
                            f"CLONE: live '{sym}' with {gap:.0f}x more liq (${real_liq:,.0f}, "
                            f"${real_vol:,.0f} 24h vol). Real CA: {clone['real_ca']}"
                        )
                    else:
                        reasons_yellow.append(
                            f"AMBIGUITY: another '{sym}' shows higher liq (${real_liq:,.0f}, "
                            f"${real_vol:,.0f} 24h vol). Verify which you want. Other CA: {clone['real_ca']}"
                        )
                elif clone["is_original"] is True:
                    if dead > 0:
                        reasons_green.append(
                            f"Original — only live '{sym}' token ({dead} dead peers ignored)"
                        )
                    elif clone.get("peer_count"):
                        reasons_green.append(
                            f"Original — #1 of {clone.get('alive_count', clone['peer_count'])} live '{sym}' tokens"
                        )
            except Exception as e:
                reasons_yellow.append(f"Clone check failed: {e.__class__.__name__}")

        # --- 2b. LIFECYCLE STAGE ---
        stage, stage_desc = detect_lifecycle_stage(pair.get("dexId"), details.get("age_minutes"))
        details["lifecycle_stage"] = stage
        details["lifecycle_desc"]  = stage_desc
        if stage == "pre_grad":
            reasons_yellow.append("Pre-graduation pump.fun token — extreme volatility expected")
        elif stage == "just_grad":
            reasons_yellow.append("Just-graduated — most volatile window, snipers active")

        # --- 2c. MULTI-WINDOW FLOW ---
        flow = multi_window_flow(pair)
        details["flow_windows"] = flow
        # Score flow agreement
        ratios = [r[3] for r in flow if r[3] is not None]
        if ratios:
            avg_ratio = sum(ratios) / len(ratios)
            if avg_ratio >= 55 and all(r >= 50 for r in ratios):
                reasons_green.append(f"Strong buy flow across all windows (avg {avg_ratio:.0f}%)")
            elif avg_ratio < 45:
                reasons_red.append(f"Sell-dominant flow (avg {avg_ratio:.0f}% buys)")

        # --- 2d. WASH-TRADING DETECTION ---
        is_wash, wash_reason = detect_wash_trading(pair)
        if is_wash:
            reasons_red.append(f"Wash-trading suspected: {wash_reason}")
            details["wash_flag"] = wash_reason
    else:
        reasons_yellow.append("No DEXScreener pair found (may not be tradeable yet)")

    # --- 3. GoPlus Security (Bitget-equivalent risk engine) — already pre-fetched ---
    gp = gp_data
    if gp and isinstance(gp, dict):
        details["goplus_trusted"] = gp.get("trusted_token")

        def _status_is_1(field_name):
            """Safely extract status from a GoPlus field that may be dict/str/None."""
            v = gp.get(field_name)
            if isinstance(v, dict):
                return str(v.get("status")) == "1"
            if isinstance(v, str):
                return v == "1"
            return False

        try:
            if _status_is_1("mintable"):
                reasons_red.append("GoPlus: token is mintable")
            if _status_is_1("closable"):
                reasons_red.append("GoPlus: account is closable (can be frozen)")
            if _status_is_1("freezable"):
                reasons_red.append("GoPlus: token is freezable")
            if _status_is_1("non_transferable"):
                reasons_red.append("GoPlus: non-transferable token")
            if _status_is_1("transfer_hook"):
                reasons_red.append("GoPlus: transfer hook present (honeypot risk)")

            # Transfer fee
            tf = gp.get("transfer_fee")
            if isinstance(tf, dict):
                fee_pct = tf.get("transfer_fee_percent") or tf.get("current_fee_rate")
                if fee_pct:
                    try:
                        if float(str(fee_pct).rstrip("%")) > 0:
                            reasons_yellow.append(f"GoPlus: transfer fee {fee_pct}")
                    except Exception:
                        pass

            # Default account state frozen
            das = gp.get("default_account_state")
            if isinstance(das, dict) and das.get("default_account_state") == "frozen":
                reasons_red.append("GoPlus: default account state is frozen")
            elif isinstance(das, str) and das == "frozen":
                reasons_red.append("GoPlus: default account state is frozen")

            # Top holders concentration
            holders = gp.get("holders") or []
            if isinstance(holders, list) and holders:
                top10_pct = 0.0
                for h in holders[:10]:
                    if not isinstance(h, dict):
                        continue
                    try:
                        pct = float(h.get("percent", 0) or 0)
                        # GoPlus returns either 0-1 fractions or 0-100; normalise
                        top10_pct += pct * 100 if pct < 1 else pct
                    except Exception:
                        pass
                details["top10_holders_pct"] = round(top10_pct, 1)
                if top10_pct > 70:
                    reasons_red.append(f"Top 10 holders own {top10_pct:.0f}% — dump risk")
                elif top10_pct > 50:
                    reasons_yellow.append(f"Top 10 holders own {top10_pct:.0f}% — concentrated")
                else:
                    reasons_green.append(f"Top 10 holders {top10_pct:.0f}% — distributed")

                # --- 3a. SNIPER CONCENTRATION HEURISTIC ---
                top5_pct = 0.0
                for h in holders[:5]:
                    if not isinstance(h, dict):
                        continue
                    try:
                        pct = float(h.get("percent", 0) or 0)
                        top5_pct += pct * 100 if pct < 1 else pct
                    except Exception:
                        pass
                details["top5_holders_pct"] = round(top5_pct, 1)
                sniper_level, sniper_reason = detect_sniper_concentration(top5_pct, details.get("age_minutes"))
                details["sniper_level"] = sniper_level
                if sniper_level == "HIGH":
                    reasons_red.append(f"Sniper risk HIGH: {sniper_reason}")
                elif sniper_level == "MEDIUM":
                    reasons_yellow.append(f"Sniper risk: {sniper_reason}")

            # Trusted ecosystem token
            if gp.get("trusted_token") == 1 or gp.get("trusted_token") == "1":
                reasons_green.append("GoPlus: trusted Solana ecosystem token")
        except Exception as e:
            reasons_yellow.append(f"GoPlus parse warning: {e.__class__.__name__}")
    else:
        reasons_yellow.append("GoPlus data unavailable")

    # --- 4. Rugcheck.xyz composite ---
    rc = get_rugcheck(mint)
    if rc:
        score = rc.get("score") or rc.get("score_normalised")
        details["rugcheck_score"] = score
        risks = rc.get("risks") or []
        details["rugcheck_risks"] = [r.get("name") for r in risks if isinstance(r, dict)]
        for risk in risks:
            if not isinstance(risk, dict):
                continue
            level = (risk.get("level") or "").lower()
            name  = risk.get("name") or "Unknown risk"
            if level == "danger":
                reasons_red.append(f"Rugcheck: {name}")
            elif level == "warn":
                reasons_yellow.append(f"Rugcheck: {name}")
    else:
        reasons_yellow.append("Rugcheck data unavailable")

    # --- Verdict ---
    if reasons_red:
        verdict = "RED"
    elif len(reasons_yellow) >= 3:
        verdict = "RED"
    elif reasons_yellow:
        verdict = "YELLOW"
    else:
        verdict = "GREEN"

    return {
        "verdict": verdict,
        "reasons_red":    reasons_red,
        "reasons_yellow": reasons_yellow,
        "reasons_green":  reasons_green,
        "details": details,
    }


def format_report(result: dict) -> str:
    """Telegram-formatted verdict string (Markdown)."""
    v = result["verdict"]
    if v == "INVALID":
        return "❌ *Invalid address.* Send a valid Solana token mint (32–44 chars, base58)."

    icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}[v]
    label = {
        "GREEN":  "GREEN — no obvious on-chain trap detected",
        "YELLOW": "YELLOW — proceed only with strict rules",
        "RED":    "RED — do NOT buy",
    }[v]

    lines = [f"{icon} *{label}*", ""]

    d = result["details"]

    # Token header
    if d.get("symbol"):
        lines.append(f"*{d['symbol']}*")
    if d.get("market_cap") or d.get("fdv"):
        mc = d.get("market_cap") or 0
        fdv = d.get("fdv") or 0
        if mc and fdv and abs(mc - fdv) > mc * 0.1:
            lines.append(f"📈 MC ${mc:,.0f}  |  FDV ${fdv:,.0f} *(Bitget shows FDV)*")
        elif fdv:
            lines.append(f"📈 MC ${fdv:,.0f}")
    if d.get("liquidity_usd") is not None:
        lines.append(f"💧 Liquidity: ${d['liquidity_usd']:,.0f}")
    if d.get("age_minutes") is not None:
        age = d["age_minutes"]
        age_str = f"{age:.0f} min" if age < 60 else f"{age/60:.1f} h" if age < 1440 else f"{age/1440:.1f} d"
        lines.append(f"⏱ Age: {age_str}")
    if d.get("volume_24h") is not None:
        lines.append(f"📊 24h vol: ${d['volume_24h']:,.0f}")

    # Identity / clone check
    clone = d.get("clone_check") or {}
    dead = clone.get("dead_peer_count", 0)
    alive = clone.get("alive_count", clone.get("peer_count", 0))
    if clone.get("is_original") is True and clone.get("peer_count"):
        if dead > 0 and alive <= 1:
            lines.append(f"\n🆔 *Identity:* ✅ Only live '{d.get('symbol')}' ({dead} dead peers ignored)")
        else:
            lines.append(f"\n🆔 *Identity:* ✅ Original — #1 of {alive} live same-symbol tokens ({dead} dead ignored)")
    elif clone.get("is_original") is False:
        lines.append(f"\n🆔 *Identity:* ⚠️ *CLONE RISK*")
        lines.append(f"   Real CA: `{clone.get('real_ca')}`")
        lines.append(f"   Real liq: ${clone.get('real_liq', 0):,.0f}  |  24h vol: ${clone.get('real_vol_24h', 0):,.0f}")

    # Lifecycle
    if d.get("lifecycle_desc"):
        lines.append(f"\n🔄 *Lifecycle:* {d['lifecycle_desc']}")

    # Flow direction
    flow = d.get("flow_windows") or []
    if flow:
        lines.append("\n🌊 *Flow direction:*")
        for win, b, s, ratio, icon_ in flow:
            win_lbl = {"m5": "5m", "h1": "1h", "h6": "6h", "h24": "24h"}.get(win, win)
            if ratio is None:
                lines.append(f"   {win_lbl}: {icon_} no trades")
            else:
                lines.append(f"   {win_lbl}: {icon_} {ratio:.0f}% buys ({b}/{b+s})")

    if d.get("rugcheck_score") is not None:
        lines.append(f"\n📊 Rugcheck score: {d['rugcheck_score']}")
    if d.get("pair_url"):
        lines.append(f"🔗 [DEXScreener]({d['pair_url']})")

    if result["reasons_red"]:
        lines.append("\n*🚨 Red flags:*")
        for r in result["reasons_red"]:
            lines.append(f"• {r}")
    if result["reasons_yellow"]:
        lines.append("\n*⚠️ Warnings:*")
        for r in result["reasons_yellow"]:
            lines.append(f"• {r}")
    if result["reasons_green"]:
        lines.append("\n*✅ Passed:*")
        for r in result["reasons_green"]:
            lines.append(f"• {r}")

    # Smart wallet section — network call, cached 5 min
    mint = d.get("mint")
    if mint:
        lines.append(format_smart_wallet_section(mint, symbol=d.get("symbol")))

    # Trade card — only for GREEN/YELLOW. RED never gets sizing.
    tc = trade_card_for_check(result)
    if tc:
        lines.append(tc)

    # Capital Guard preview — projects size/liq/slippage at default 15% sizing.
    # Skip for RED (no sizing anyway) and INVALID.
    try:
        if result.get("verdict") in ("GREEN", "YELLOW"):
            import capital_guard
            from trade_card import get_capital_usd, GREEN_POSITION_PCT, YELLOW_POSITION_USD
            cap = get_capital_usd()
            default_size = (round(cap * GREEN_POSITION_PCT, 2) if result["verdict"] == "GREEN"
                            else min(YELLOW_POSITION_USD, round(cap * GREEN_POSITION_PCT, 2)))
            liq = d.get("liquidity_usd")
            panel = capital_guard.format_check_panel(liq, cap, default_size)
            if panel:
                lines.append("\n" + panel)
    except Exception as e:
        # Never let guard break the report
        lines.append(f"\n_(Capital Guard preview failed: {e.__class__.__name__})_")

    lines.append("\n_Mechanical on-chain checks only. Does not predict price, dead launches, slow rugs, or your discipline._")
    return "\n".join(lines)
