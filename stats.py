"""
Stats (Module #4) — aggregate outcomes across positions, alerts, checks.

Pulls from:
  - position_tracker  (closed positions = actual P&L)
  - memory_store      (watcher alerts + /check history)
  - loss_tracker      (classified losses)

Surfaces:
  /stats              overall summary
  /stats positions    detailed position breakdown
  /stats watcher      watcher alert performance (hypothetical hit rate)
  /stats narratives   best/worst narrative categories
"""

import time
import logging
from collections import Counter, defaultdict

import position_tracker
import memory_store
import loss_tracker

log = logging.getLogger(__name__)


def _hours_since(ts):
    if not ts: return None
    return (time.time() - ts) / 3600


def _classify_outcome(closed_position: dict) -> str:
    """Returns 'WIN' / 'LOSS' / 'SCRATCH'."""
    pnl = closed_position.get("pnl_usd", 0) or 0
    if pnl > 0.50:  return "WIN"
    if pnl < -0.50: return "LOSS"
    return "SCRATCH"


def position_stats() -> dict:
    closed = position_tracker.list_closed(limit=500)
    open_pos = position_tracker.list_open()

    n_closed = len(closed)
    if n_closed == 0:
        return {
            "n_closed": 0, "n_open": len(open_pos), "wins": 0, "losses": 0, "scratches": 0,
            "win_rate": 0, "total_pnl": 0, "avg_pnl": 0, "best": None, "worst": None,
            "by_reason": {},
        }

    wins   = [c for c in closed if _classify_outcome(c) == "WIN"]
    losses = [c for c in closed if _classify_outcome(c) == "LOSS"]
    scratches = [c for c in closed if _classify_outcome(c) == "SCRATCH"]

    total_pnl = sum(c.get("pnl_usd", 0) or 0 for c in closed)
    avg_pnl = total_pnl / n_closed if n_closed else 0
    win_rate = len(wins) / n_closed * 100 if n_closed else 0

    best = max(closed, key=lambda c: c.get("pnl_usd", 0) or 0)
    worst = min(closed, key=lambda c: c.get("pnl_usd", 0) or 0)

    by_reason = Counter(c.get("close_reason", "?") for c in closed)

    return {
        "n_closed": n_closed,
        "n_open":   len(open_pos),
        "wins":     len(wins),
        "losses":   len(losses),
        "scratches": len(scratches),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_pnl":  avg_pnl,
        "best":     best,
        "worst":    worst,
        "by_reason": dict(by_reason),
    }


def watcher_stats() -> dict:
    """Stats on the watcher alert stream."""
    alerts = memory_store.get_recent_alerts(limit=500)
    if not alerts:
        return {"n": 0, "verdicts": {}, "narratives": {}, "with_smart_wallets": 0, "twitter_confirmed": 0}

    verdicts = Counter(a.get("verdict") for a in alerts)
    narratives = Counter(a.get("narrative") for a in alerts)
    with_sw = sum(1 for a in alerts if (a.get("smart_wallets") or 0) > 0)
    twitter_ok = sum(1 for a in alerts if a.get("twitter_ok"))

    # Most recent
    last_ts = alerts[0].get("ts") if alerts else None

    return {
        "n":                 len(alerts),
        "verdicts":          dict(verdicts),
        "narratives":        narratives.most_common(10),
        "with_smart_wallets": with_sw,
        "twitter_confirmed": twitter_ok,
        "last_alert_hours_ago": _hours_since(last_ts),
    }


def check_stats(user_id) -> dict:
    checks = memory_store.get_recent_checks(user_id, limit=200)
    if not checks:
        return {"n": 0, "verdicts": {}}
    verdicts = Counter(c.get("verdict") for c in checks)
    return {
        "n":        len(checks),
        "verdicts": dict(verdicts),
    }


def narrative_performance() -> list:
    """Cross-reference alerts with closed positions to see which narratives paid off."""
    alerts = memory_store.get_recent_alerts(limit=500)
    closed = position_tracker.list_closed(limit=500)

    # Build mint → narrative map from alerts
    mint_to_narrative = {}
    for a in alerts:
        if a.get("mint") and a.get("narrative"):
            mint_to_narrative[a["mint"]] = a["narrative"]

    # For each closed position, look up its narrative if we alerted on it
    by_narrative = defaultdict(lambda: {"n": 0, "wins": 0, "total_pnl": 0})
    for c in closed:
        mint = c.get("mint")
        narrative = mint_to_narrative.get(mint)
        if not narrative:
            continue
        by_narrative[narrative]["n"] += 1
        if (c.get("pnl_usd") or 0) > 0:
            by_narrative[narrative]["wins"] += 1
        by_narrative[narrative]["total_pnl"] += c.get("pnl_usd") or 0

    # Convert to list ranked by total_pnl
    out = []
    for narrative, d in by_narrative.items():
        wr = d["wins"] / d["n"] * 100 if d["n"] else 0
        out.append({
            "narrative": narrative,
            "n":         d["n"],
            "wins":      d["wins"],
            "win_rate":  wr,
            "total_pnl": d["total_pnl"],
        })
    out.sort(key=lambda x: x["total_pnl"], reverse=True)
    return out


# ---------- FORMATTERS ----------

def format_overall(user_id) -> str:
    p = position_stats()
    w = watcher_stats()
    c = check_stats(user_id)
    l = loss_tracker.stats()

    lines = ["📊 *Overall Stats*", ""]

    # Trades
    lines.append("*🎯 Trades (your /buy positions):*")
    if p["n_closed"] == 0:
        lines.append("   No closed trades yet. Start with `/buy <CA>`.")
    else:
        pnl_icon = "🟢" if p["total_pnl"] > 0 else "🔴"
        lines.append(f"   Closed: {p['n_closed']} | Open: {p['n_open']}")
        lines.append(f"   {pnl_icon} P&L: ${p['total_pnl']:+.2f} (avg ${p['avg_pnl']:+.2f}/trade)")
        lines.append(f"   Win rate: {p['win_rate']:.0f}% ({p['wins']}W / {p['losses']}L / {p['scratches']} scratch)")
        if p["best"]:
            lines.append(f"   🏆 Best: {p['best'].get('symbol')} ${p['best'].get('pnl_usd', 0):+.2f}")
        if p["worst"]:
            lines.append(f"   💀 Worst: {p['worst'].get('symbol')} ${p['worst'].get('pnl_usd', 0):+.2f}")
        if p["by_reason"]:
            reasons = " | ".join(f"{k}: {v}" for k, v in p["by_reason"].items())
            lines.append(f"   Exits: {reasons}")
    lines.append("")

    # Watcher
    lines.append("*👁 Watcher alerts:*")
    if w["n"] == 0:
        lines.append("   No alerts yet. Turn on with `/watcher on`.")
    else:
        v = w["verdicts"]
        lines.append(f"   Total: {w['n']} | 🟢 {v.get('GREEN', 0)} / 🟡 {v.get('YELLOW', 0)}")
        lines.append(f"   With smart wallets: {w['with_smart_wallets']}")
        lines.append(f"   Twitter confirmed: {w['twitter_confirmed']}")
        if w.get("last_alert_hours_ago") is not None:
            lines.append(f"   Last alert: {w['last_alert_hours_ago']:.1f}h ago")
    lines.append("")

    # Checks
    lines.append("*🛡 Rug checks:*")
    if c["n"] == 0:
        lines.append("   No checks yet.")
    else:
        v = c["verdicts"]
        lines.append(f"   Total: {c['n']} | 🟢 {v.get('GREEN', 0)} / 🟡 {v.get('YELLOW', 0)} / 🔴 {v.get('RED', 0)}")
    lines.append("")

    # Losses
    lines.append("*📊 Loss quality:*")
    if l["total"] == 0:
        lines.append("   No losses logged yet.")
    else:
        lines.append(f"   Logged: {l['total']} | 🔴 Real: {l['real']} / 🟡 Unconfirmed: {l['unconfirmed']}")
        lines.append(f"   Net P&L from losses: ${l['total_pnl']:+.2f}")
    lines.append("")

    lines.append("_Sub-commands: `/stats positions` `/stats watcher` `/stats narratives`_")
    return "\n".join(lines)


def format_positions_detail() -> str:
    p = position_stats()
    if p["n_closed"] == 0:
        return "📊 No closed trades yet."
    closed = position_tracker.list_closed(limit=50)
    lines = [f"📊 *Closed trades ({len(closed)} of {p['n_closed']})*", ""]
    for c in closed:
        sym = c.get("symbol") or "?"
        pnl = c.get("pnl_usd", 0)
        pct = c.get("pnl_pct", 0)
        reason = c.get("close_reason", "?")
        icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        lines.append(f"{icon} *{sym}* — {reason} | ${pnl:+.2f} ({pct:+.1f}%)")
    return "\n".join(lines)


def format_watcher_detail() -> str:
    w = watcher_stats()
    if w["n"] == 0:
        return "👁 No watcher alerts yet."
    lines = [f"👁 *Watcher Stats ({w['n']} alerts)*", ""]
    v = w["verdicts"]
    lines.append(f"Verdicts: 🟢 {v.get('GREEN', 0)} / 🟡 {v.get('YELLOW', 0)}")
    lines.append(f"With smart wallets: {w['with_smart_wallets']}")
    lines.append(f"Twitter confirmed: {w['twitter_confirmed']}")
    lines.append("")
    lines.append("*Top narratives by frequency:*")
    for narr, count in w["narratives"][:10]:
        lines.append(f"   {count}× — \"{narr}\"")
    return "\n".join(lines)


def format_narratives() -> str:
    perf = narrative_performance()
    if not perf:
        return ("🎯 No narrative performance data yet.\n\n"
                "Needs: watcher alert → you `/buy` it → it closes.\n"
                "Then this report cross-references them.")
    lines = ["🎯 *Narrative Performance*", "_Only narratives where you actually traded._", ""]
    for n in perf[:15]:
        icon = "🟢" if n["total_pnl"] > 0 else "🔴" if n["total_pnl"] < 0 else "⚪"
        lines.append(
            f"{icon} *\"{n['narrative']}\"*\n"
            f"   {n['n']} trades | {n['win_rate']:.0f}% WR | ${n['total_pnl']:+.2f}"
        )
    return "\n".join(lines)
