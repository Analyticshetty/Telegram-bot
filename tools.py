"""
External tools the bot can call via Groq function calling.
"""

import os
import json
import requests

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
TAVILY_URL     = "https://api.tavily.com/search"
TIMEOUT        = 12


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
    if name == "fetch_url":
        return fetch_url(args.get("url", ""))
    return f"Unknown tool: {name}"
