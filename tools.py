"""
External tools the bot can call via Groq function calling.

Three buckets of tools:
  1. EXTERNAL — web_search, get_token_data, fetch_url. Hit live APIs.
  2. STATE READERS — get_*. Read what the bot itself has done (state stores in
     Redis). Goal: chat brain can answer ANY question about the bot's data
     without hallucinating.
  3. ACTIONS — run_*, set_*, toggle_*, add_*, remove_*. Let the chat brain
     actually DO things the user asks in natural language. All action tools
     are gated by caller_user_id == OWNER_TELEGRAM_ID — non-owner callers get
     a polite refusal string back to the LLM.
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


# ---------- MORE STATE READERS ----------

def get_capital() -> str:
    """Return current capital from Redis state:capital_usd."""
    try:
        import trade_card
        cap = trade_card.get_capital_usd()
        return json.dumps({"capital_usd": cap, "default_if_unset": trade_card.DEFAULT_CAPITAL_USD})
    except Exception as e:
        return f"get_capital error: {e.__class__.__name__}: {e}"


def get_losses(limit: int = 20) -> str:
    """Recent realized losses from losses:log with Fib/volume classification."""
    try:
        import loss_tracker
        limit = max(1, min(int(limit or 20), 100))
        losses = loss_tracker.get_recent_losses(limit=limit)
        if not losses:
            return json.dumps({"count": 0, "losses": [],
                               "note": "No losses logged yet."})
        out = []
        for l in losses:
            out.append({
                "ago":         _ago_str(l.get("ts")),
                "symbol":      l.get("symbol"),
                "mint":        l.get("mint"),
                "classification": l.get("classification"),
                "pnl_usd":     l.get("pnl_usd"),
                "pnl_pct":     l.get("pnl_pct"),
                "entry_price": l.get("entry_price"),
                "exit_price":  l.get("exit_price"),
                "fib_broken":  l.get("fib_broken"),
                "vol_spike":   l.get("vol_spike"),
            })
        try:
            agg = loss_tracker.stats()
        except Exception:
            agg = {}
        return json.dumps({"count": len(out), "losses": out, "summary": agg}, default=str)
    except Exception as e:
        return f"get_losses error: {e.__class__.__name__}: {e}"


def get_stats() -> str:
    """Aggregate trading stats: position win rate, P&L, narrative ROI, check breakdown."""
    try:
        import stats as stats_module
        user_id = OWNER_TELEGRAM_ID or ""
        out = {
            "positions":  stats_module.position_stats(),
            "watcher":    stats_module.watcher_stats(),
            "checks":     stats_module.check_stats(user_id),
            "narratives": stats_module.narrative_performance(),
        }
        return json.dumps(out, default=str)
    except Exception as e:
        return f"get_stats error: {e.__class__.__name__}: {e}"


def get_memories() -> str:
    """Permanent /remember facts the owner has set."""
    try:
        import redis_client
        raw = redis_client.get_redis().get("shashi:memories")
        memories = json.loads(raw) if raw else []
        return json.dumps({"count": len(memories), "memories": memories})
    except Exception as e:
        return f"get_memories error: {e.__class__.__name__}: {e}"


def get_smart_wallets(page: int = 1, page_size: int = 50) -> str:
    """List tracked smart wallets (204 total). Paginated to avoid blowing context."""
    try:
        import smart_wallets
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 50), 100))
        all_w = smart_wallets.load_wallets()
        total = len(all_w)
        start = (page - 1) * page_size
        chunk = all_w[start:start + page_size]
        out = [{"address": w.get("address"), "label": w.get("label"),
                "source": w.get("source")} for w in chunk]
        return json.dumps({
            "total": total, "page": page, "page_size": page_size,
            "returned": len(out), "wallets": out
        })
    except Exception as e:
        return f"get_smart_wallets error: {e.__class__.__name__}: {e}"


def get_recent_scans(limit: int = 10) -> str:
    """Recent /scan results from mem:scans."""
    try:
        import memory_store
        limit = max(1, min(int(limit or 10), 50))
        scans = memory_store.get_recent_scans(limit=limit)
        if not scans:
            return json.dumps({"count": 0, "scans": [], "note": "No scans run yet."})
        out = []
        for s in scans:
            out.append({
                "ago":           _ago_str(s.get("ts")),
                "results_count": s.get("results_count"),
                "top_results":   s.get("top_results") or [],
            })
        return json.dumps({"count": len(out), "scans": out}, default=str)
    except Exception as e:
        return f"get_recent_scans error: {e.__class__.__name__}: {e}"


def get_signal_log(limit: int = 20) -> str:
    """Recent /signal calls from signals:store, with 6h-horizon outcome if resolved."""
    try:
        import signal_engine
        from redis_client import get_redis
        r = get_redis()
        limit = max(1, min(int(limit or 20), 100))
        ids = r.zrevrange(signal_engine.K_BY_TS, 0, limit - 1) or []
        out = []
        for sid in ids:
            raw = r.hget(signal_engine.K_STORE, sid)
            if not raw:
                continue
            try:
                sig = json.loads(raw)
            except Exception:
                continue
            out.append({
                "ago":      _ago_str(sig.get("created_ts") or sig.get("ts")),
                "id":       sid,
                "mint":     sig.get("mint"),
                "symbol":   sig.get("symbol"),
                "score":    sig.get("score"),
                "lean":     sig.get("lean"),
                "verdict":  sig.get("verdict"),
                "resolved": sig.get("resolved"),
                "outcome":  sig.get("outcome"),
                "price_change_6h": sig.get("price_change_6h"),
            })
        return json.dumps({"count": len(out), "signals": out}, default=str)
    except Exception as e:
        return f"get_signal_log error: {e.__class__.__name__}: {e}"


def get_signal_accuracy() -> str:
    """Self-tracked accuracy of /signal predictions at 6h horizon."""
    try:
        import signal_engine
        return json.dumps(signal_engine.overall_stats(), default=str)
    except Exception as e:
        return f"get_signal_accuracy error: {e.__class__.__name__}: {e}"


def get_lookup(mint: str) -> str:
    """Pull everything the bot remembers about a CA: rug check, watcher alerts,
    smart-wallet signals, positions, losses on this token. The full picture in one tool."""
    try:
        import memory_store
        import smart_wallet_feed
        import position_tracker
        import loss_tracker
        mint = (mint or "").strip()
        if not mint:
            return json.dumps({"error": "no mint provided"})
        out = {"mint": mint}

        # Most recent rug check
        try:
            out["last_check"] = memory_store.get_check_by_ca(mint)
        except Exception:
            out["last_check"] = None

        # Most recent watcher alert
        try:
            out["last_alert"] = memory_store.get_alert_by_ca(mint)
        except Exception:
            out["last_alert"] = None

        # All smart-wallet signal events for this CA
        try:
            all_sigs = smart_wallet_feed.get_recent_signals(limit=200)
            out["sw_signals"] = [s for s in all_sigs if s.get("mint") == mint]
        except Exception:
            out["sw_signals"] = []

        # Open position?
        try:
            out["open_position"] = position_tracker.get_position(mint)
        except Exception:
            out["open_position"] = None

        # Closed positions on this CA?
        try:
            closed = position_tracker.list_closed(limit=50)
            out["closed_positions"] = [p for p in closed if p.get("mint") == mint]
        except Exception:
            out["closed_positions"] = []

        # Loss log entries for this CA?
        try:
            losses = loss_tracker.get_recent_losses(limit=100)
            out["losses"] = [l for l in losses if l.get("mint") == mint]
        except Exception:
            out["losses"] = []

        return json.dumps(out, default=str)
    except Exception as e:
        return f"get_lookup error: {e.__class__.__name__}: {e}"


def run_rug_check(mint: str) -> str:
    """Run the full 9-engine rug check on a Solana CA — same as /check."""
    try:
        from rug_check import check_token, is_valid_solana_mint
        mint = (mint or "").strip()
        if not is_valid_solana_mint(mint):
            return json.dumps({"error": "invalid Solana mint"})
        result = check_token(mint)
        return json.dumps({
            "verdict":        result.get("verdict"),
            "reasons_red":    result.get("reasons_red"),
            "reasons_yellow": result.get("reasons_yellow"),
            "reasons_green":  result.get("reasons_green"),
            "details":        result.get("details"),
        }, default=str)
    except Exception as e:
        return f"run_rug_check error: {e.__class__.__name__}: {e}"


def run_scan(limit: int = 5) -> str:
    """Run /scan logic — top Solana candidates from GeckoTerminal new+trending."""
    try:
        from scanner import scan as _scan
        limit = max(1, min(int(limit or 5), 10))
        results = _scan(limit_results=limit)
        return json.dumps({"count": len(results), "results": results}, default=str)
    except Exception as e:
        return f"run_scan error: {e.__class__.__name__}: {e}"


# ---------- ACTION TOOLS (owner-gated) ----------

def _refuse_non_owner(caller_user_id) -> str | None:
    """Returns refusal string if caller is not the owner, else None."""
    if not OWNER_TELEGRAM_ID:
        return "ERROR: OWNER_TELEGRAM_ID not configured. Action tools refuse to run."
    if str(caller_user_id) != str(OWNER_TELEGRAM_ID):
        return "REFUSED: action tools are owner-only. Caller is not the owner."
    return None


def open_position_action(mint: str, size_usd: float = None,
                         entry_price: float = None, caller_user_id=None) -> str:
    """Open a tracked position — same as /buy. Owner-only."""
    refusal = _refuse_non_owner(caller_user_id)
    if refusal:
        return refusal
    try:
        import position_tracker
        import trade_card
        from rug_check import is_valid_solana_mint
        mint = (mint or "").strip()
        if not is_valid_solana_mint(mint):
            return json.dumps({"ok": False, "error": "invalid Solana mint"})
        if size_usd is None:
            size_usd = round(trade_card.get_capital_usd() * trade_card.GREEN_POSITION_PCT, 2)
        result = position_tracker.open_position(mint, float(size_usd),
                                                float(entry_price) if entry_price else None)
        return json.dumps(result, default=str)
    except Exception as e:
        return f"open_position error: {e.__class__.__name__}: {e}"


def close_position_action(mint: str, caller_user_id=None) -> str:
    """Close a tracked position by CA — same as /sell. Owner-only."""
    refusal = _refuse_non_owner(caller_user_id)
    if refusal:
        return refusal
    try:
        import position_tracker
        result = position_tracker.close_position((mint or "").strip(), reason="chat_action")
        return json.dumps(result, default=str)
    except Exception as e:
        return f"close_position error: {e.__class__.__name__}: {e}"


def set_capital_action(amount_usd: float, caller_user_id=None) -> str:
    """Set capital state — same as /capital <amount>. Owner-only."""
    refusal = _refuse_non_owner(caller_user_id)
    if refusal:
        return refusal
    try:
        from redis_client import get_redis
        amt = float(amount_usd)
        if amt <= 0 or amt > 1_000_000:
            return json.dumps({"ok": False, "error": "amount out of range (0, 1e6)"})
        get_redis().set("state:capital_usd", str(amt))
        return json.dumps({"ok": True, "capital_usd": amt})
    except Exception as e:
        return f"set_capital error: {e.__class__.__name__}: {e}"


def add_memory_action(text: str, caller_user_id=None) -> str:
    """Append a permanent fact to shashi:memories — same as /remember. Owner-only."""
    refusal = _refuse_non_owner(caller_user_id)
    if refusal:
        return refusal
    try:
        from redis_client import get_redis
        text = (text or "").strip()
        if not text or len(text) > 500:
            return json.dumps({"ok": False, "error": "memory text empty or too long (>500)"})
        r = get_redis()
        raw = r.get("shashi:memories")
        mems = json.loads(raw) if raw else []
        if text in mems:
            return json.dumps({"ok": False, "error": "already remembered"})
        mems.append(text)
        r.set("shashi:memories", json.dumps(mems))
        return json.dumps({"ok": True, "count": len(mems)})
    except Exception as e:
        return f"add_memory error: {e.__class__.__name__}: {e}"


def forget_memory_action(text: str, caller_user_id=None) -> str:
    """Remove a memory by exact match — same as /forget. Owner-only."""
    refusal = _refuse_non_owner(caller_user_id)
    if refusal:
        return refusal
    try:
        from redis_client import get_redis
        text = (text or "").strip()
        r = get_redis()
        raw = r.get("shashi:memories")
        mems = json.loads(raw) if raw else []
        before = len(mems)
        mems = [m for m in mems if m != text]
        r.set("shashi:memories", json.dumps(mems))
        return json.dumps({"ok": True, "removed": before - len(mems), "remaining": len(mems)})
    except Exception as e:
        return f"forget_memory error: {e.__class__.__name__}: {e}"


def toggle_sleep_action(on: bool, caller_user_id=None) -> str:
    """Turn sleep mode on/off — same as /sleep on|off. Owner-only."""
    refusal = _refuse_non_owner(caller_user_id)
    if refusal:
        return refusal
    try:
        import sleep_mode
        result = sleep_mode.turn_on() if on else sleep_mode.turn_off()
        return json.dumps(result, default=str)
    except Exception as e:
        return f"toggle_sleep error: {e.__class__.__name__}: {e}"


def toggle_watcher_action(on: bool, caller_user_id=None) -> str:
    """Turn watcher on/off — same as /watcher on|off. Owner-only.
    Note: on=True starts the watcher with a no-op alert handler; the main
    bot.py path already wires the real handler — chat brain can only stop it
    or check status reliably."""
    refusal = _refuse_non_owner(caller_user_id)
    if refusal:
        return refusal
    try:
        import watcher
        if not on:
            watcher.stop()
            try:
                from redis_client import get_redis
                get_redis().set("state:watcher_on", "0")
            except Exception:
                pass
            return json.dumps({"ok": True, "running": False})
        return json.dumps({
            "ok": False,
            "error": "Use /watcher on from Telegram to start watcher — chat-brain start would lack the proper alert handler.",
        })
    except Exception as e:
        return f"toggle_watcher error: {e.__class__.__name__}: {e}"


def toggle_swfeed_action(on: bool, caller_user_id=None) -> str:
    """Stop the smart-wallet feed. (Start requires /swfeed on from Telegram for handler wiring.)"""
    refusal = _refuse_non_owner(caller_user_id)
    if refusal:
        return refusal
    try:
        import smart_wallet_feed
        if not on:
            smart_wallet_feed.stop()
            try:
                from redis_client import get_redis
                get_redis().set("state:swfeed_on", "0")
            except Exception:
                pass
            return json.dumps({"ok": True, "running": False})
        return json.dumps({"ok": False, "error": "Use /swfeed on from Telegram to start."})
    except Exception as e:
        return f"toggle_swfeed error: {e.__class__.__name__}: {e}"


def toggle_devfeed_action(on: bool, caller_user_id=None) -> str:
    """Stop the dev-sell tracker. (Start requires /devfeed on from Telegram for handler wiring.)"""
    refusal = _refuse_non_owner(caller_user_id)
    if refusal:
        return refusal
    try:
        import dev_tracker
        if not on:
            if hasattr(dev_tracker, "stop"):
                dev_tracker.stop()
            try:
                from redis_client import get_redis
                get_redis().set("state:devfeed_on", "0")
            except Exception:
                pass
            return json.dumps({"ok": True, "running": False})
        return json.dumps({"ok": False, "error": "Use /devfeed on from Telegram to start."})
    except Exception as e:
        return f"toggle_devfeed error: {e.__class__.__name__}: {e}"


def add_wallet_action(address: str, label: str, caller_user_id=None) -> str:
    """Add a smart wallet — same as /addwallet. Owner-only."""
    refusal = _refuse_non_owner(caller_user_id)
    if refusal:
        return refusal
    try:
        import smart_wallets
        addr = (address or "").strip()
        lbl  = (label or "").strip() or addr[:6]
        if not addr or len(addr) < 32:
            return json.dumps({"ok": False, "error": "invalid wallet address"})
        ok = smart_wallets.add_wallet(addr, lbl, source="chat")
        return json.dumps({"ok": bool(ok), "address": addr, "label": lbl})
    except Exception as e:
        return f"add_wallet error: {e.__class__.__name__}: {e}"


def remove_wallet_action(address: str, caller_user_id=None) -> str:
    """Remove a smart wallet — same as /removewallet. Owner-only."""
    refusal = _refuse_non_owner(caller_user_id)
    if refusal:
        return refusal
    try:
        import smart_wallets
        addr = (address or "").strip()
        ok = smart_wallets.remove_wallet(addr)
        return json.dumps({"ok": bool(ok), "address": addr})
    except Exception as e:
        return f"remove_wallet error: {e.__class__.__name__}: {e}"


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
            "name": "get_capital",
            "description": "Current trading capital in USD from Redis state. Use whenever the user asks 'what's my capital', 'how much do I have', 'what am I working with'.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_losses",
            "description": "Recent realized losses with Fib/volume REAL_LOSS vs UNCONFIRMED classification. Use for 'what losses have I taken', 'show me my recent losses', 'how bad was X loss'.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "How many losses (max 100). Default 20."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stats",
            "description": "Aggregate trading stats: position win rate, total P&L, watcher accuracy, check breakdown, narrative ROI. Use for 'what's my win rate', 'how am I doing', 'overall P&L', 'best narratives'.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_memories",
            "description": "Read permanent /remember facts the owner has set. Use for 'what rules do I have', 'what memories are saved', 'what did I tell you to remember'.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_smart_wallets",
            "description": "List tracked smart wallets (paginated, 204 total). Use for 'show me the wallets I track', 'how many wallets are on the list', 'is wallet X tracked'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page":      {"type": "integer", "description": "1-indexed page. Default 1."},
                    "page_size": {"type": "integer", "description": "Wallets per page (max 100). Default 50."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_scans",
            "description": "Recent /scan results from memory. Use for 'what did the last scan find', 'show me recent scans'.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "How many scans (max 50). Default 10."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_signal_log",
            "description": "Recent /signal score+lean predictions with their 6h-horizon outcome (if resolved). Use for 'what signals have we run', 'how did the signal on X turn out', 'recent predictions'.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "How many signals (max 100). Default 20."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_signal_accuracy",
            "description": "Overall self-tracked hit rate of /signal predictions at the 6h horizon, broken down by lean (BULLISH/NEUTRAL/BEARISH). Use for 'how accurate are the signals', 'should I trust /signal'.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lookup",
            "description": "Pull EVERYTHING the bot remembers about a Solana CA in one shot: last rug check, last watcher alert, all smart-wallet signals on it, open/closed positions, losses. Use whenever the user names a CA and wants the full picture.",
            "parameters": {
                "type": "object",
                "properties": {"mint": {"type": "string", "description": "Solana mint address."}},
                "required": ["mint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_rug_check",
            "description": "Trigger the 9-engine rug check on a CA — same as /check. Returns verdict + reasons + details. Use when user pastes a CA and asks 'check this' or 'is this safe'.",
            "parameters": {
                "type": "object",
                "properties": {"mint": {"type": "string"}},
                "required": ["mint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_scan",
            "description": "Trigger /scan — top Solana candidates from GeckoTerminal new+trending. Use for 'find me something to buy', 'what's hot', 'scan for opportunities'.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "How many candidates (max 10). Default 5."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_position",
            "description": "Open a tracked position on a CA — same as /buy. Owner-only. Use when user says 'buy X', 'I just bought X', 'track a $5 position on X'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mint":        {"type": "string"},
                    "size_usd":    {"type": "number", "description": "USD size. Defaults to 15% of capital."},
                    "entry_price": {"type": "number", "description": "Manual entry price. Defaults to current live price."},
                },
                "required": ["mint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_position",
            "description": "Close a tracked position by CA — same as /sell. Owner-only. Use when user says 'close X', 'sell X', 'I sold X off-bot, close it in the tracker'.",
            "parameters": {
                "type": "object",
                "properties": {"mint": {"type": "string"}},
                "required": ["mint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_capital",
            "description": "Update the tracked capital in USD — same as /capital <amount>. Owner-only. Use when user says 'I topped up to $X', 'set capital to X'.",
            "parameters": {
                "type": "object",
                "properties": {"amount_usd": {"type": "number"}},
                "required": ["amount_usd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_memory",
            "description": "Save a permanent rule/fact for the bot to remember — same as /remember. Owner-only.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": "Delete a memory by exact text match — same as /forget. Owner-only.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_sleep",
            "description": "Turn sleep mode on or off — same as /sleep. Owner-only. Use for 'go to sleep', 'wake up', 'silence alerts'.",
            "parameters": {
                "type": "object",
                "properties": {"on": {"type": "boolean"}},
                "required": ["on"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_watcher",
            "description": "Stop watcher (on=False). Starting watcher should be done via /watcher on from Telegram (chat-brain start lacks alert handler).",
            "parameters": {
                "type": "object",
                "properties": {"on": {"type": "boolean"}},
                "required": ["on"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_swfeed",
            "description": "Stop smart-wallet feed (on=False). Start via /swfeed on from Telegram.",
            "parameters": {
                "type": "object",
                "properties": {"on": {"type": "boolean"}},
                "required": ["on"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_devfeed",
            "description": "Stop dev-sell tracker (on=False). Start via /devfeed on from Telegram.",
            "parameters": {
                "type": "object",
                "properties": {"on": {"type": "boolean"}},
                "required": ["on"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_wallet",
            "description": "Add a wallet to the smart-wallet tracking list — same as /addwallet. Owner-only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {"type": "string"},
                    "label":   {"type": "string"},
                },
                "required": ["address", "label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_wallet",
            "description": "Remove a wallet from the tracking list — same as /removewallet. Owner-only.",
            "parameters": {
                "type": "object",
                "properties": {"address": {"type": "string"}},
                "required": ["address"],
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

def execute_tool(name: str, args: dict, caller_user_id=None) -> str:
    # External
    if name == "web_search":      return web_search(args.get("query", ""))
    if name == "get_token_data":  return get_token_data(args.get("mint", ""))
    if name == "fetch_url":       return fetch_url(args.get("url", ""))

    # State readers (no gate — read-only is fine for any chatter)
    if name == "get_watcher_alerts":       return get_watcher_alerts(args.get("limit", 20), args.get("keyword"))
    if name == "get_recent_checks":        return get_recent_checks(args.get("limit", 20))
    if name == "get_smart_wallet_signals": return get_smart_wallet_signals(args.get("limit", 20))
    if name == "get_positions":            return get_positions(args.get("status", "open"))
    if name == "get_watcher_status":       return get_watcher_status()
    if name == "get_capital":              return get_capital()
    if name == "get_losses":               return get_losses(args.get("limit", 20))
    if name == "get_stats":                return get_stats()
    if name == "get_memories":             return get_memories()
    if name == "get_smart_wallets":        return get_smart_wallets(args.get("page", 1), args.get("page_size", 50))
    if name == "get_recent_scans":         return get_recent_scans(args.get("limit", 10))
    if name == "get_signal_log":           return get_signal_log(args.get("limit", 20))
    if name == "get_signal_accuracy":      return get_signal_accuracy()
    if name == "get_lookup":               return get_lookup(args.get("mint", ""))

    # On-demand reruns
    if name == "run_rug_check":  return run_rug_check(args.get("mint", ""))
    if name == "run_scan":       return run_scan(args.get("limit", 5))

    # Actions (owner-gated)
    if name == "open_position":   return open_position_action(args.get("mint"), args.get("size_usd"), args.get("entry_price"), caller_user_id)
    if name == "close_position":  return close_position_action(args.get("mint"), caller_user_id)
    if name == "set_capital":     return set_capital_action(args.get("amount_usd"), caller_user_id)
    if name == "add_memory":      return add_memory_action(args.get("text", ""), caller_user_id)
    if name == "forget_memory":   return forget_memory_action(args.get("text", ""), caller_user_id)
    if name == "toggle_sleep":    return toggle_sleep_action(bool(args.get("on")), caller_user_id)
    if name == "toggle_watcher":  return toggle_watcher_action(bool(args.get("on")), caller_user_id)
    if name == "toggle_swfeed":   return toggle_swfeed_action(bool(args.get("on")), caller_user_id)
    if name == "toggle_devfeed":  return toggle_devfeed_action(bool(args.get("on")), caller_user_id)
    if name == "add_wallet":      return add_wallet_action(args.get("address"), args.get("label"), caller_user_id)
    if name == "remove_wallet":   return remove_wallet_action(args.get("address"), caller_user_id)

    return f"Unknown tool: {name}"
