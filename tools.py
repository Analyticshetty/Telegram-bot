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


def get_smart_wallets(page: int = 1, page_size: int = 25) -> str:
    """List tracked smart wallets. Returns total count + a sample of labels so
    the LLM doesn't get overwhelmed and falsely report 'none'."""
    try:
        import smart_wallets
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 25), 50))
        all_w = smart_wallets.load_wallets()
        total = len(all_w)
        start = (page - 1) * page_size
        chunk = all_w[start:start + page_size]
        labels_sample = [w.get("label") for w in chunk if w.get("label")]
        out = [{"address": (w.get("address") or "")[:10] + "...",
                "label":   w.get("label"),
                "source":  w.get("source")} for w in chunk]
        summary = (
            f"{total} smart wallets tracked total. "
            f"Page {page} (size {page_size}) returns {len(out)} entries."
            if total > 0
            else "Wallet list is EMPTY. Either Redis was wiped or smart_wallets:data is missing. "
                 "Tell Shashi to run /listwallets to confirm, and /discoverwallet to rebuild if so."
        )
        return json.dumps({
            "summary":       summary,
            "total":         total,
            "page":          page,
            "page_size":     page_size,
            "returned":      len(out),
            "labels_sample": labels_sample,
            "wallets":       out,
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
    """Pull everything the bot remembers about a CA. Pre-formats timestamps and
    trims raw fields so the LLM can't fabricate metrics from raw dumps."""
    try:
        import memory_store
        import smart_wallet_feed
        import position_tracker
        import loss_tracker
        mint = (mint or "").strip()
        if not mint:
            return json.dumps({"error": "no mint provided"})
        out = {"mint": mint}

        # Most recent rug check — keep only safe-to-summarize fields
        try:
            chk = memory_store.get_check_by_ca(mint)
            if chk:
                out["last_check"] = {
                    "ago":          _ago_str(chk.get("ts")),
                    "symbol":       chk.get("symbol"),
                    "verdict":      chk.get("verdict"),
                    "market_cap":   chk.get("mc"),
                    "liquidity":    chk.get("liq"),
                    "red_flags":    chk.get("reasons_red") or [],
                    "yellow_flags": chk.get("reasons_yellow") or [],
                }
            else:
                out["last_check"] = None
        except Exception:
            out["last_check"] = None

        # Most recent watcher alert
        try:
            al = memory_store.get_alert_by_ca(mint)
            if al:
                out["last_alert"] = {
                    "ago":           _ago_str(al.get("ts")),
                    "narrative":     al.get("narrative"),
                    "symbol":        al.get("symbol"),
                    "verdict":       al.get("verdict"),
                    "market_cap":    al.get("mc"),
                    "liquidity":     al.get("liq"),
                    "smart_wallets": al.get("smart_wallets"),
                    "cluster_size":  al.get("cluster_size"),
                }
            else:
                out["last_alert"] = None
        except Exception:
            out["last_alert"] = None

        # Smart-wallet signal events for this CA
        try:
            all_sigs = smart_wallet_feed.get_recent_signals(limit=200)
            matching = [s for s in all_sigs if s.get("mint") == mint]
            out["sw_signals_count"] = len(matching)
            out["sw_signals"] = [{
                "ago":          _ago_str(s.get("ts")),
                "wallet_count": s.get("wallet_count"),
                "wallets":      s.get("wallet_labels") or [],
                "verdict":      s.get("verdict"),
            } for s in matching[:5]]
        except Exception:
            out["sw_signals"] = []
            out["sw_signals_count"] = 0

        # Open position with live P&L
        try:
            op = position_tracker.get_position(mint)
            if op:
                live = position_tracker.get_live_price(mint) or op.get("entry_price")
                pnl_pct = ((live / op["entry_price"]) - 1) * 100 if op.get("entry_price") else 0
                out["open_position"] = {
                    "ago":         _ago_str(op.get("opened_at")),
                    "symbol":      op.get("symbol"),
                    "size_usd":    op.get("size_usd"),
                    "entry_price": op.get("entry_price"),
                    "live_price":  live,
                    "live_pnl_pct": round(pnl_pct, 1),
                }
            else:
                out["open_position"] = None
        except Exception:
            out["open_position"] = None

        # Closed positions on this CA
        try:
            closed = position_tracker.list_closed(limit=50)
            matching_c = [p for p in closed if p.get("mint") == mint]
            out["closed_positions"] = [{
                "ago":          _ago_str(p.get("closed_at")),
                "symbol":       p.get("symbol"),
                "size_usd":     p.get("size_usd"),
                "pnl_usd":      p.get("pnl_usd"),
                "pnl_pct":      p.get("pnl_pct"),
                "close_reason": p.get("close_reason"),
            } for p in matching_c]
        except Exception:
            out["closed_positions"] = []

        # Loss log entries for this CA
        try:
            losses = loss_tracker.get_recent_losses(limit=100)
            matching_l = [l for l in losses if l.get("mint") == mint]
            out["losses"] = [{
                "ago":            _ago_str(l.get("ts")),
                "classification": l.get("classification"),
                "pnl_usd":        l.get("pnl_usd"),
                "pnl_pct":        l.get("pnl_pct"),
            } for l in matching_l]
        except Exception:
            out["losses"] = []

        out["summary_hint"] = (
            "Use ONLY the fields present in this JSON. Do NOT invent risk-percentage, "
            "score, or rating fields — they're not in this data. Format timestamps "
            "using the 'ago' field, never raw numbers."
        )

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

def _tool(name, desc, params=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": params or {},
                "required": required or [],
            },
        },
    }

_INT  = {"type": "integer"}
_NUM  = {"type": "number"}
_STR  = {"type": "string"}
_BOOL = {"type": "boolean"}

TOOLS_SCHEMA = [
    # External data
    _tool("web_search", "Search the web for news/prices of non-Solana things.",
          {"query": _STR}, ["query"]),
    _tool("get_token_data", "Live DEXScreener data for a Solana mint.",
          {"mint": _STR}, ["mint"]),
    _tool("fetch_url", "Read a specific URL.", {"url": _STR}, ["url"]),

    # State readers
    _tool("get_watcher_alerts", "Past narrative-cluster alerts from /watcher.",
          {"limit": _INT, "keyword": _STR}),
    _tool("get_recent_checks", "Owner's recent /check rug-check history.",
          {"limit": _INT}),
    _tool("get_smart_wallet_signals", "Past 2+-wallet convergence signals from /swfeed.",
          {"limit": _INT}),
    _tool("get_positions", "Tracked positions. status='open' (live P&L) or 'closed'.",
          {"status": {"type": "string", "enum": ["open", "closed"]}}),
    _tool("get_watcher_status", "Run state of watcher, swfeed, devfeed, sleep, position tracker."),
    _tool("get_capital", "Current trading capital in USD."),
    _tool("get_losses", "Realized losses with REAL/UNCONFIRMED classification.",
          {"limit": _INT}),
    _tool("get_stats", "Aggregate trading stats: win rate, P&L, narrative ROI."),
    _tool("get_memories", "Permanent /remember facts saved by the owner."),
    _tool("get_smart_wallets", "Paginated list of the 204 tracked smart wallets.",
          {"page": _INT, "page_size": _INT}),
    _tool("get_recent_scans", "Past /scan results.", {"limit": _INT}),
    _tool("get_signal_log", "Past /signal predictions with 6h outcomes.", {"limit": _INT}),
    _tool("get_signal_accuracy", "Self-tracked hit rate of /signal predictions."),
    _tool("get_lookup", "EVERYTHING the bot knows about a CA in one shot (use for any 'what about <CA>' question).",
          {"mint": _STR}, ["mint"]),

    # On-demand reruns
    _tool("run_rug_check", "Trigger the 9-engine rug check on a CA (same as /check).",
          {"mint": _STR}, ["mint"]),
    _tool("run_scan", "Trigger /scan — top Solana candidates.", {"limit": _INT}),

    # Actions (owner-only; non-owner gets REFUSED back)
    _tool("open_position", "Open a tracked position (same as /buy). Owner-only.",
          {"mint": _STR, "size_usd": _NUM, "entry_price": _NUM}, ["mint"]),
    _tool("close_position", "Close a tracked position (same as /sell). Owner-only.",
          {"mint": _STR}, ["mint"]),
    _tool("set_capital", "Update tracked capital (same as /capital). Owner-only.",
          {"amount_usd": _NUM}, ["amount_usd"]),
    _tool("add_memory", "Save a permanent fact (same as /remember). Owner-only.",
          {"text": _STR}, ["text"]),
    _tool("forget_memory", "Delete a memory by exact text (same as /forget). Owner-only.",
          {"text": _STR}, ["text"]),
    _tool("toggle_sleep", "Turn sleep mode on/off. Owner-only.",
          {"on": _BOOL}, ["on"]),
    _tool("toggle_watcher", "Stop watcher (start blocked from chat). Owner-only.",
          {"on": _BOOL}, ["on"]),
    _tool("toggle_swfeed", "Stop swfeed (start blocked from chat). Owner-only.",
          {"on": _BOOL}, ["on"]),
    _tool("toggle_devfeed", "Stop devfeed (start blocked from chat). Owner-only.",
          {"on": _BOOL}, ["on"]),
    _tool("add_wallet", "Add a wallet to tracking (same as /addwallet). Owner-only.",
          {"address": _STR, "label": _STR}, ["address", "label"]),
    _tool("remove_wallet", "Remove a wallet from tracking (same as /removewallet). Owner-only.",
          {"address": _STR}, ["address"]),
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
