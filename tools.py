"""
External tools the bot can call via Groq function calling.
"""

import os
import json
import requests

TAVILY_API_KEY  = os.environ.get("TAVILY_API_KEY")
TAVILY_URL      = "https://api.tavily.com/search"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
TIMEOUT         = 12


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
    return f"Unknown tool: {name}"
