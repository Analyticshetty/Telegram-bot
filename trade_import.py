"""
Trade Import (Module #5) — parse Bitget/exchange screenshots via vision LLM,
extract trade details, resolve to Solana CA, open/close position with one tap.

Flow:
  1. User sends photo with caption "buy" / "sell" / "trade" (or /import command)
  2. Vision LLM extracts: action, symbol, price, size_usd
  3. Bot resolves symbol → CA (recent check history first, then DEXScreener search)
  4. Bot replies with inline buttons: ✅ Confirm | ❌ Cancel | 🔄 Pick different CA
  5. On confirm: opens or closes position via position_tracker

Storage:
  pending_import:{user_id}:{message_id}   JSON of parsed trade + candidate CAs
                                          (TTL 10 min — expires if not confirmed)
"""

import json
import logging
import requests
from redis_client import get_redis

log = logging.getLogger(__name__)
_redis = get_redis()

TIMEOUT = 10
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/"
PENDING_TTL_SECS = 600  # 10 minutes to confirm

EXTRACT_PROMPT = """You are looking at a screenshot from a crypto trading app (likely Bitget, Binance, or similar).

Return ONLY a JSON object — no other text. First decide which kind of screen this is.

TYPE A — ORDER/FILL screen (shows a single trade with a fill price and a USD/USDT amount):
  {
    "screen_type": "order",
    "action": "buy" or "sell",
    "symbol": token ticker without /USDT (e.g. "GOBLIN", "PEPE"),
    "price": fill price as a number (USDT per token),
    "size_usd": total USD/USDT amount of the trade (filled amount, not order amount)
  }

TYPE B — HOLDINGS / ASSET-DETAIL screen (shows how much of a token you hold, often with
"Market cap (buy price)" and "Latest market cap"). Common on Bitget memecoin spot holdings.
Numbers here may be in INR (₹) — DO NOT convert them, just read the market caps in USD:
  {
    "screen_type": "holding",
    "action": "buy",
    "symbol": token ticker (e.g. "grail"),
    "tokens_held": the "Available" token amount as a number (e.g. 151131.492461),
    "entry_mc_usd": the "Market cap (buy price)" in USD (e.g. "$238.85K" -> 238850),
    "latest_mc_usd": the "Latest market cap" in USD (e.g. "$199.64K" -> 199640)
  }
  Expand K/M/B suffixes to full numbers ($238.85K -> 238850, $1.2M -> 1200000).

If this is NOT a crypto trade or holdings screen, return: {"error": "not a trade"}

Return ONLY the JSON object. No markdown, no explanation."""


def extract_trade_from_image(groq_client, image_b64: str, vision_model: str) -> dict:
    """Run vision LLM on screenshot. Returns dict with parsed fields or {'error': ...}."""
    try:
        response = groq_client.chat.completions.create(
            model=vision_model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    {"type": "text", "text": EXTRACT_PROMPT},
                ],
            }],
            max_tokens=300,
        )
        raw = (response.choices[0].message.content or "").strip()
        # Strip markdown fences if model added them
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        return parsed
    except json.JSONDecodeError as e:
        log.warning(f"Trade import JSON parse failed: {e}, raw was: {raw[:200] if 'raw' in dir() else 'n/a'}")
        return {"error": f"Could not parse vision output: {e}"}
    except Exception as e:
        log.warning(f"Trade import vision call failed: {e}")
        return {"error": f"Vision call failed: {e}"}


def find_candidate_cas(symbol: str, memory_store_module=None) -> list:
    """Find Solana CAs matching this symbol. Returns list of dicts ranked by relevance.
    Each dict: {mint, symbol, liquidity_usd, mc_usd, age_minutes, source}"""
    if not symbol:
        return []
    candidates = []
    seen = set()

    # Source 1: recent /check history for exact symbol match (highest priority — user already vetted these)
    if memory_store_module:
        try:
            # Pull from the alerts and checks indexes — both have a 30-day CA TTL
            # We need to search recent checks across the user, but we don't have user_id here.
            # Best-effort: scan check_by_ca keys via SCAN. Cheap on free tier.
            for key in _redis.scan_iter(match="mem:check_by_ca:*", count=200):
                try:
                    raw = _redis.get(key)
                    if not raw: continue
                    c = json.loads(raw)
                    if (c.get("symbol") or "").upper() == symbol.upper():
                        mint = c.get("mint")
                        if mint and mint not in seen:
                            seen.add(mint)
                            candidates.append({
                                "mint":      mint,
                                "symbol":    c.get("symbol"),
                                "source":    "recent_check",
                                "verdict":   c.get("verdict"),
                                "liquidity_usd": c.get("liq"),
                                "mc_usd":    c.get("mc"),
                            })
                except Exception:
                    continue
        except Exception as e:
            log.warning(f"trade_import memory scan failed: {e}")

    # Source 2: DEXScreener search by symbol (fallback if no recent match or to add live options)
    try:
        r = requests.get(DEXSCREENER_SEARCH, params={"q": symbol}, timeout=TIMEOUT)
        if r.status_code == 200:
            pairs = (r.json() or {}).get("pairs") or []
            # Solana only, exact symbol match, group by mint, keep top-liquidity pair per mint
            by_mint = {}
            sym_l = symbol.lower()
            for p in pairs:
                if p.get("chainId") != "solana":
                    continue
                base = p.get("baseToken") or {}
                if (base.get("symbol") or "").lower() != sym_l:
                    continue
                m = base.get("address")
                if not m: continue
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if m not in by_mint or liq > by_mint[m]["liquidity_usd"]:
                    by_mint[m] = {
                        "mint":     m,
                        "symbol":   base.get("symbol"),
                        "source":   "dexscreener",
                        "liquidity_usd": liq,
                        "mc_usd":   p.get("fdv") or p.get("marketCap"),
                        "volume_24h": (p.get("volume") or {}).get("h24") or 0,
                    }
            # Sort by liquidity desc and append (skipping already-seen)
            ranked = sorted(by_mint.values(), key=lambda x: x.get("liquidity_usd") or 0, reverse=True)
            for c in ranked:
                if c["mint"] not in seen:
                    seen.add(c["mint"])
                    candidates.append(c)
    except Exception as e:
        log.warning(f"trade_import dexscreener search failed: {e}")

    return candidates[:5]  # cap at top 5


def reconstruct_holding(parsed: dict, live_price: float) -> dict:
    """For a 'holding' screen, derive entry price + USD size from the market-cap
    ratio and live price. No FX needed: entry/current price scales with market cap
    (same supply), so entry_price = live_price * (entry_mc / latest_mc).
    Returns {'price', 'size_usd'} or {'error': ...}."""
    try:
        tokens = float(parsed.get("tokens_held") or 0)
        entry_mc = float(parsed.get("entry_mc_usd") or 0)
        latest_mc = float(parsed.get("latest_mc_usd") or 0)
    except (TypeError, ValueError):
        return {"error": "Could not read numbers off the holding screen"}
    if tokens <= 0:
        return {"error": "No token amount found on screen"}
    if entry_mc <= 0 or latest_mc <= 0:
        return {"error": "Missing market-cap values — can't derive entry"}
    if not live_price or live_price <= 0:
        return {"error": "Could not fetch live price for this CA"}
    entry_price = live_price * (entry_mc / latest_mc)
    size_usd = tokens * entry_price
    return {"price": entry_price, "size_usd": size_usd}


def save_pending(user_id, message_id, payload: dict):
    """Stash the parsed trade + candidates for confirmation step."""
    try:
        key = f"pending_import:{user_id}:{message_id}"
        _redis.set(key, json.dumps(payload), ex=PENDING_TTL_SECS)
    except Exception as e:
        log.warning(f"trade_import save_pending failed: {e}")


def load_pending(user_id, message_id) -> dict | None:
    try:
        key = f"pending_import:{user_id}:{message_id}"
        raw = _redis.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def delete_pending(user_id, message_id):
    try:
        _redis.delete(f"pending_import:{user_id}:{message_id}")
    except Exception:
        pass


def format_confirmation(parsed: dict, candidates: list) -> str:
    """Build the confirmation message text."""
    action = (parsed.get("action") or "?").upper()
    sym = parsed.get("symbol") or "?"

    if parsed.get("screen_type") == "holding":
        tokens = parsed.get("tokens_held")
        entry_mc = parsed.get("entry_mc_usd")
        latest_mc = parsed.get("latest_mc_usd")
        roi = None
        try:
            if entry_mc and latest_mc:
                roi = (float(latest_mc) / float(entry_mc) - 1) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            roi = None
        lines = [
            f"🔎 *Detected {action} (holding screen)*",
            "",
            f"   Token: *{sym}*",
            f"   Held:  {tokens:,.2f}" if isinstance(tokens, (int, float)) else "   Held: ?",
            f"   Entry MC:  ${float(entry_mc):,.0f}" if entry_mc else "   Entry MC: ?",
            f"   Latest MC: ${float(latest_mc):,.0f}" if latest_mc else "   Latest MC: ?",
        ]
        if roi is not None:
            lines.append(f"   Implied ROI: {roi:+.1f}%")
        lines.append("")
        lines.append("_Exact entry price + USD size computed from live price on confirm._")
        lines.append("")
        if not candidates:
            lines.append("⚠️ *No CA found.* Send the CA manually or try `/check <CA>` first.")
            return "\n".join(lines)
        lines.append(f"*Found {len(candidates)} candidate CA(s):*")
        for i, c in enumerate(candidates, 1):
            src_icon = "🧠" if c.get("source") == "recent_check" else "🔍"
            liq = c.get("liquidity_usd") or 0
            mc = c.get("mc_usd") or 0
            verdict_str = f" [{c['verdict']}]" if c.get("verdict") else ""
            lines.append(
                f"   {i}. {src_icon} `{c['mint'][:8]}...{c['mint'][-4:]}`{verdict_str}\n"
                f"      Liq ${liq:,.0f} | MC ${mc:,.0f}"
            )
        lines.append("")
        lines.append("_Tap a number below to confirm, or ❌ to cancel._")
        return "\n".join(lines)

    price = parsed.get("price")
    size = parsed.get("size_usd")
    lines = [
        f"🔎 *Detected {action} trade*",
        "",
        f"   Token: *{sym}*",
        f"   Price: ${price:.10f}".rstrip("0").rstrip(".") if price else "   Price: ?",
        f"   Size:  ${size:.2f}" if size else "   Size: ?",
        "",
    ]
    if not candidates:
        lines.append("⚠️ *No CA found.* Send the CA manually or try `/check <CA>` first.")
        return "\n".join(lines)

    lines.append(f"*Found {len(candidates)} candidate CA(s):*")
    for i, c in enumerate(candidates, 1):
        src_icon = "🧠" if c.get("source") == "recent_check" else "🔍"
        liq = c.get("liquidity_usd") or 0
        mc  = c.get("mc_usd") or 0
        verdict_str = f" [{c['verdict']}]" if c.get("verdict") else ""
        lines.append(
            f"   {i}. {src_icon} `{c['mint'][:8]}...{c['mint'][-4:]}`{verdict_str}\n"
            f"      Liq ${liq:,.0f} | MC ${mc:,.0f}"
        )
    lines.append("")
    lines.append("_Tap a number below to confirm, or ❌ to cancel._")
    return "\n".join(lines)
