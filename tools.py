"""
External tools the bot can call via Groq function calling.

External-data tools (web_search, get_token_data, fetch_url) hit live APIs.
State tools (get_watcher_alerts, get_recent_checks, get_smart_wallet_signals,
get_positions, get_watcher_status) read what the bot itself has done so the
chat LLM stops hallucinating lists of alerts/positions/wallets.
"""

import os
import json
import time
import requests

TAVILY_API_KEY    = os.environ.get("TAVILY_API_KEY")
OWNER_TELEGRAM_ID = os.environ.get("OWNER_TELEGRAM_ID")
TAVILY_URL        = "https://api.tavily.com/search"
DEXSCREENER_URL   = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
TIMEOUT           = 12


# ---------- Tool implementations ----------

def web_search(query: str, max_results: int = 5) -> str:
    """Search the web via Tavily. Returns a compact text summary."""
    if not TAVILY_API_KEY:
        return "ERROR: TAVILY_API_KEY not configured in Railway Variables."
    try:
        r = requests.post(
            TAVILY_URL,
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "max_results": max_results,
                "include_answer": True,
                "search_depth": "basic",
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return f"Search failed: HTTP {r.status_code} — {r.text[:200]}"
        data = r.json()
        out = []
        if data.get("answer"):
            out.append(f"SUMMARY: {data['answer']}")
        for i, res in enumerate(data.get("results", [])[:max_results], 1):
            title   = res.get("title", "")
            url     = res.get("url", "")
            content = (res.get("content") or "")[:400]
            out.append(f"\n[{i}] {title}\n{url}\n{content}")
        return "\n".join(out) if out else "No results."
    except Exception as e:
        return f"Search error: {e.__class__.__name__}: {e}"


def fetch_url(url: str) -> str:
    """Fetch a URL and return its main text content via Tavily Extract."""
    if not TAVILY_API_KEY:
        return "ERROR: TAVILY_API_KEY not configured."
    try:
        r = requests.post(
            "https://api.tavily.com/extract",
            json={"api_key": TAVILY_API_KEY, "urls": [url]},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return f"Fetch failed: HTTP {r.status_code}"
        data = r.json()
        results = data.get("results") or []
        if not results:
            return "No content extracted."
        content = (results[0].get("raw_content") or "")[:4000]
        return f"URL: {url}\n\n{content}"
    except Exception as e:
        return f"Fetch error: {e.__class__.__name__}: {e}"


def get_token_data(mint: str) -> str:
    """Live DEXScreener data for a Solana token: price, MC, FDV, liquidity, volume."""
    try:
        r = requests.get(DEXSCREENER_URL.format(mint=mint), timeout=TIMEOUT)
        if r.status_code != 200:
            return f"DEXScreener returned HTTP {r.status_code}"
        data = r.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return f"No DEXScreener pair found for {mint}. Token may be too new or untracked."
        # Use pair with highest liquidity
        pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
        p = pairs[0]
        base = p.get("baseToken") or {}
        out = {
            "symbol":       base.get("symbol"),
            "name":         base.get("name"),
            "price_usd":    p.get("priceUsd"),
            "market_cap":   p.get("marketCap"),
            "fdv":          p.get("fdv"),  # Bitget usually displays FDV as MC
            "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
            "volume_1h":    (p.get("volume") or {}).get("h1"),
            "volume_24h":   (p.get("volume") or {}).get("h24"),
            "price_change_1h":  (p.get("priceChange") or {}).get("h1"),
            "price_change_24h": (p.get("priceChange") or {}).get("h24"),
            "txns_1h":      (p.get("txns") or {}).get("h1"),
            "dex":          p.get("dexId"),
            "pair_url":     p.get("url"),
            "pair_created_at": p.get("pairCreatedAt"),
        }
        return json.dumps(out, indent=2)
    except Exception as e:
        return f"get_token_data error: {e.__class__.__name__}: {e}"


# ---------- STATE TOOLS (read the bot's own memory — kills hallucinated lists) ----------

def _ago_str(ts) -> str:
    """Human-readable age of a unix ts, e.g. '4m', '2h', '3d'."""
    try:
        delta = time.time() - float(ts)
    except (TypeError, ValueError):
        return "?"
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta/60)}m"
    if delta < 86400:
        return f"{delta/3600:.1f}h"
    return f"{delta/86400:.1f}d"


def get_watcher_alerts(limit: int = 20, keyword: str = None) -> str:
    """Return recent watcher / smart-wallet / narrative alerts from memory_store."""
    try:
        import memory_store
        limit = max(1, min(int(limit or 20), 100))
        if keyword:
            entries = memory_store.search_alerts(keyword, limit=200)[:limit]
        else:
            entries = memory_store.get_recent_alerts(limit=limit)
        if not entries:
            return json.dumps({"count": 0, "alerts": [],
                               "note": "No watcher alerts stored. Either watcher is off or nothing has fired."})
        out = []
        for a in entries:
            out.append({
                "ago":       _ago_str(a.get("ts")),
                "narrative": a.get("narrative"),
                "symbol":    a.get("symbol"),
                "mint":      a.get("mint"),
                "verdict":   a.get("verdict"),
                "mc":        a.get("mc"),
                "liq":       a.get("liq"),
                "smart_wallets": a.get("smart_wallets"),
                "cluster_size":  a.get("cluster_size"),
            })
        return json.dumps({"count": len(out), "alerts": out}, default=str)
    except Exception as e:
        return f"get_watcher_alerts error: {e.__class__.__name__}: {e}"


def get_recent_checks(limit: int = 20) -> str:
    """Return recent /check rug-check results for the owner."""
    try:
        import memory_store
        limit = max(1, min(int(limit or 20), 100))
        user_id = OWNER_TELEGRAM_ID or ""
        entries = memory_store.get_recent_checks(user_id, limit=limit)
        if not entries:
            return json.dumps({"count": 0, "checks": [],
                               "note": "No /check history stored yet."})
        out = []
        for c in entries:
            out.append({
                "ago":     _ago_str(c.get("ts")),
                "symbol":  c.get("symbol"),
                "mint":    c.get("mint"),
                "verdict": c.get("verdict"),
                "mc":      c.get("mc"),
                "liq":     c.get("liq"),
                "red_flags":    c.get("reasons_red") or [],
                "yellow_flags": c.get("reasons_yellow") or [],
            })
        return json.dumps({"count": len(out), "checks": out}, default=str)
    except Exception as e:
        return f"get_recent_checks error: {e.__class__.__name__}: {e}"


def get_smart_wallet_signals(limit: int = 20) -> str:
    """Return recent smart-wallet convergence/accumulation signals from sw_feed:signals."""
    try:
        import smart_wallet_feed
        limit = max(1, min(int(limit or 20), 100))
        entries = smart_wallet_feed.get_recent_signals(limit=limit)
        if not entries:
            return json.dumps({"count": 0, "signals": [],
                               "note": "No smart-wallet signals stored yet. Either swfeed hasn't fired or it's off."})
        out = []
        for s in entries:
            out.append({
                "ago":          _ago_str(s.get("ts")),
                "symbol":       s.get("symbol"),
                "mint":         s.get("mint"),
                "wallet_count": s.get("wallet_count"),
                "wallets":      s.get("wallet_labels") or [],
                "new_buyer":    s.get("new_buyer"),
                "verdict":      s.get("verdict"),
                "mc":           s.get("mc"),
                "liq":          s.get("liq"),
                "age_minutes":  s.get("age_minutes"),
            })
        return json.dumps({"count": len(out), "signals": out}, default=str)
    except Exception as e:
        return f"get_smart_wallet_signals error: {e.__class__.__name__}: {e}"


def get_positions(status: str = "open") -> str:
    """Return positions tracked by the bot. status: 'open' or 'closed'."""
    try:
        import position_tracker
        status = (status or "open").lower()
        if status == "closed":
            raw = position_tracker.list_closed(limit=20)
        else:
            raw = position_tracker.list_open()
        if not raw:
            return json.dumps({"status": status, "count": 0, "positions": [],
                               "note": f"No {status} positions."})
        out = []
        for p in raw:
            row = {
                "symbol":      p.get("symbol"),
                "mint":        p.get("mint"),
                "size_usd":    p.get("size_usd"),
                "entry_price": p.get("entry_price"),
                "tp1_price":   p.get("tp1_price"),
                "tp2_price":   p.get("tp2_price"),
                "sl_price":    p.get("sl_price"),
                "tp1_hit":     p.get("tp1_hit"),
                "opened_ago":  _ago_str(p.get("opened_at")),
            }
            if status == "closed":
                row["closed_ago"]   = _ago_str(p.get("closed_at"))
                row["close_reason"] = p.get("close_reason")
                row["exit_price"]   = p.get("exit_price")
                row["pnl_usd"]      = p.get("pnl_usd")
                row["pnl_pct"]      = p.get("pnl_pct")
            else:
                try:
                    live = position_tracker.get_live_price(p.get("mint"))
                    if live and p.get("entry_price"):
                        row["live_price"] = live
                        row["pnl_pct"]    = round((live / p["entry_price"] - 1) * 100, 1)
                except Exception:
                    pass
            out.append(row)
        return json.dumps({"status": status, "count": len(out), "positions": out}, default=str)
    except Exception as e:
        return f"get_positions error: {e.__class__.__name__}: {e}"


def get_watcher_status() -> str:
    """Return current run state of watcher, swfeed, devfeed, sleep mode + last cycle info."""
    out = {}
    try:
        import watcher
        out["watcher"] = watcher.get_status()
    except Exception as e:
        out["watcher"] = {"error": f"{e.__class__.__name__}: {e}"}
    try:
        import smart_wallet_feed
        out["swfeed"] = smart_wallet_feed.get_status()
    except Exception as e:
        out["swfeed"] = {"error": f"{e.__class__.__name__}: {e}"}
    try:
        import dev_tracker
        if hasattr(dev_tracker, "get_status"):
            out["devfeed"] = dev_tracker.get_status()
        elif hasattr(dev_tracker, "is_running"):
            out["devfeed"] = {"running": dev_tracker.is_running()}
    except Exception as e:
        out["devfeed"] = {"error": f"{e.__class__.__name__}: {e}"}
    try:
        import sleep_mode
        out["sleep"] = sleep_mode.status()
    except Exception as e:
        out["sleep"] = {"error": f"{e.__class__.__name__}: {e}"}
    try:
        import position_tracker
        out["position_tracker_running"] = position_tracker.is_running()
        out["open_position_count"]      = len(position_tracker.list_open())
    except Exception as e:
        out["position_tracker"] = {"error": f"{e.__class__.__name__}: {e}"}
    return json.dumps(out, default=str)


# ---------- Tool schema for Groq function calling ----------

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current/real-time information. Use whenever the user "
                "asks about prices, news, current events, recent token activity, exchange "
                "status, social mentions, or anything that needs up-to-date info. "
                "Do NOT use for things you already know from the conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query. Be specific and include token names, dates, or context.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_token_data",
            "description": (
                "Get LIVE on-chain data for a specific Solana token: current price, "
                "market cap, FDV, liquidity, 1h/24h volume, price change. ALWAYS use "
                "this (NOT web_search) when the user asks about a specific token's "
                "price, MC, FDV, volume, or liquidity. Note: Bitget app usually "
                "displays FDV labeled as 'MC' — show both to the user when relevant."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mint": {
                        "type": "string",
                        "description": "Solana mint address (base58, 32-44 chars).",
                    },
                },
                "required": ["mint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_watcher_alerts",
            "description": (
                "Read the bot's OWN log of past watcher / smart-wallet / narrative alerts. "
                "Use this whenever the user asks 'what alerts have you sent', 'show me recent "
                "alerts', 'have we alerted on X', 'what narratives are firing', etc. Do NOT "
                "fabricate alert lists — if this returns count=0, say so honestly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit":   {"type": "integer", "description": "How many alerts to return (max 100). Default 20."},
                    "keyword": {"type": "string",  "description": "Optional case-insensitive filter on narrative/symbol/mint."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_checks",
            "description": (
                "Read the bot's history of /check rug-check results that the owner has run. "
                "Use when the user asks 'what tokens have I checked', 'what was the verdict on X', "
                "'show me my recent rug checks'. Per-user, scoped to the owner."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many checks to return (max 100). Default 20."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_smart_wallet_signals",
            "description": (
                "Read recent smart-wallet convergence/accumulation signals — the 2+-wallets-in-same-CA "
                "events from the smart wallet feed (swfeed). Use when the user asks 'which tokens "
                "have multiple smart wallets bought', 'what swfeed signals fired today', 'any "
                "convergence on X'. DIFFERENT from get_watcher_alerts (those are narrative clusters)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many signals to return (max 100). Default 20."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_positions",
            "description": (
                "Read the owner's positions tracked by the bot. status='open' returns currently "
                "open positions with live P&L; status='closed' returns the last 20 closed positions "
                "with realized P&L. Use whenever the user asks about 'my positions', 'what am I "
                "holding', 'last trades', 'P&L', 'closed trades'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["open", "closed"], "description": "Default 'open'."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_watcher_status",
            "description": (
                "Get current run state of all background modules: watcher (narrative scanner), "
                "swfeed (smart-wallet feed), devfeed (dev-sell tracker), sleep mode, position "
                "tracker. Use when the user asks 'is the watcher running', 'what's the bot doing', "
                "'is swfeed on', 'am I in sleep mode'."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Fetch the main readable content of a specific URL. Use when the user "
                "gives you a link or when web_search returned a promising URL you need "
                "to read fully."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL including https://"},
                },
                "required": ["url"],
            },
        },
    },
]


# ---------- Dispatcher ----------

def execute_tool(name: str, args: dict) -> str:
    if name == "web_search":
        return web_search(args.get("query", ""))
    if name == "get_token_data":
        return get_token_data(args.get("mint", ""))
    if name == "fetch_url":
        return fetch_url(args.get("url", ""))
    if name == "get_watcher_alerts":
        return get_watcher_alerts(args.get("limit", 20), args.get("keyword"))
    if name == "get_recent_checks":
        return get_recent_checks(args.get("limit", 20))
    if name == "get_smart_wallet_signals":
        return get_smart_wallet_signals(args.get("limit", 20))
    if name == "get_positions":
        return get_positions(args.get("status", "open"))
    if name == "get_watcher_status":
        return get_watcher_status()
    return f"Unknown tool: {name}"
