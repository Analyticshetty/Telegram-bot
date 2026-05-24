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

RUGCHECK_URL    = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
SOLANA_RPC      = "https://api.mainnet-beta.solana.com"
GOPLUS_URL      = "https://api.gopluslabs.io/api/v1/solana/token_security"
TIMEOUT         = 8

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
        details["pair_url"]      = pair.get("url")
        details["dex"]           = pair.get("dexId")
        details["pair_address"]  = pair.get("pairAddress")

        if liq_usd < 1000:
            reasons_red.append(f"Liquidity only ${liq_usd:,.0f} — exit will be impossible")
        elif liq_usd < 10000:
            reasons_yellow.append(f"Thin liquidity ${liq_usd:,.0f} — high slippage risk")
        else:
            reasons_green.append(f"Liquidity ${liq_usd:,.0f}")

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
    else:
        reasons_yellow.append("No DEXScreener pair found (may not be tradeable yet)")

    # --- 3. GoPlus Security (Bitget-equivalent risk engine) ---
    gp = get_goplus(mint)
    if gp:
        details["goplus_trusted"] = gp.get("trusted_token")
        # Mintable / freezable (cross-check with RPC)
        mintable = (gp.get("mintable") or {})
        if str(mintable.get("status")) == "1":
            reasons_red.append("GoPlus: token is mintable")
        closable = (gp.get("closable") or {})
        if str(closable.get("status")) == "1":
            reasons_red.append("GoPlus: account is closable (can be frozen)")
        freezable = (gp.get("freezable") or {})
        if str(freezable.get("status")) == "1":
            reasons_red.append("GoPlus: token is freezable")
        # Transfer fee — Bitget flags any non-zero
        tf = (gp.get("transfer_fee") or {})
        if tf:
            fee_pct = tf.get("transfer_fee_percent") or tf.get("current_fee_rate")
            if fee_pct and float(str(fee_pct).rstrip("%") or 0) > 0:
                reasons_yellow.append(f"GoPlus: transfer fee {fee_pct}")
        # Transfer hook (honeypot vector)
        th = gp.get("transfer_hook") or {}
        if th and th.get("status") == "1":
            reasons_red.append("GoPlus: transfer hook present (honeypot risk)")
        # Non-transferable
        nt = (gp.get("non_transferable") or {})
        if str(nt.get("status")) == "1":
            reasons_red.append("GoPlus: non-transferable token")
        # Default account state frozen
        das = (gp.get("default_account_state") or {})
        if das.get("default_account_state") == "frozen":
            reasons_red.append("GoPlus: default account state is frozen")
        # Top holders concentration (GoPlus returns 'holders')
        holders = gp.get("holders") or []
        if holders:
            top10_pct = 0.0
            for h in holders[:10]:
                try:
                    top10_pct += float(h.get("percent", 0)) * 100 if float(h.get("percent", 0)) < 1 else float(h.get("percent", 0))
                except Exception:
                    pass
            details["top10_holders_pct"] = round(top10_pct, 1)
            if top10_pct > 50:
                reasons_red.append(f"Top 10 holders own {top10_pct:.0f}% — dump risk")
            elif top10_pct > 30:
                reasons_yellow.append(f"Top 10 holders own {top10_pct:.0f}% — concentrated")
            else:
                reasons_green.append(f"Top 10 holders {top10_pct:.0f}% — distributed")
        # Trusted ecosystem token
        if gp.get("trusted_token") == 1:
            reasons_green.append("GoPlus: trusted Solana ecosystem token")
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
    if d.get("liquidity_usd") is not None:
        lines.append(f"💧 Liquidity: ${d['liquidity_usd']:,.0f}")
    if d.get("age_minutes") is not None:
        age = d["age_minutes"]
        age_str = f"{age:.0f} min" if age < 60 else f"{age/60:.1f} h" if age < 1440 else f"{age/1440:.1f} d"
        lines.append(f"⏱ Age: {age_str}")
    if d.get("rugcheck_score") is not None:
        lines.append(f"📊 Rugcheck score: {d['rugcheck_score']}")
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

    lines.append("\n_Mechanical on-chain checks only. Does not predict price, dead launches, slow rugs, or your discipline._")
    return "\n".join(lines)
