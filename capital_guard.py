"""
Capital Guard — Tier 1 protective rails.

Stops the bleeding the grail loss exposed:
  1. Slippage warning — projects entry & SL-exit slippage from liq pool depth
  2. Position-size guardrail — blocks size >50% capital, warns >25%
  3. SL realism check — flags when SL won't fill cleanly on thin liq
  4. Revenge-trade guard — blocks re-entry within 30min of a realized loss,
     warns within 2h

All severities and thresholds are tunable here. Pure functions — no Redis
side-effects except _last_loss_lookup which reads positions:closed.

USAGE:
    from capital_guard import run_guard, format_panel
    decision = run_guard(size_usd, capital_usd, liq_usd, entry_price, sl_price)
    if decision["block"] and not force:
        # refuse the trade, show reasons
        ...
    panel = format_panel(decision)  # always show

DECISION CONTRACT (returned dict):
    {
      "block":   bool,           # if True and force=False, refuse trade
      "warn":    bool,           # if True, show panel but allow with force
      "reasons_block": [str],
      "reasons_warn":  [str],
      "reasons_info":  [str],    # info-level lines, never block
      "metrics": {
          "size_pct_capital":      float,
          "size_pct_liq":          float,
          "entry_slip_pct":        float,
          "sl_exit_slip_pct":      float,
          "minutes_since_loss":    float | None,
      }
    }
"""

import json
import time
import logging
from redis_client import get_redis

log = logging.getLogger(__name__)
_redis = get_redis()

# ---- THRESHOLDS (tune here, nowhere else) ----

# Position size as % of capital
SIZE_BLOCK_PCT_CAP = 0.50   # >50% of capital = block
SIZE_WARN_PCT_CAP = 0.25    # >25% = warn

# Liquidity pool depth
LIQ_BLOCK_USD = 20_000      # <$20K total pool = graveyard, block
LIQ_WARN_USD = 100_000      # <$100K = warn ("thin")
LIQ_INFO_USD = 250_000      # <$250K = info-level note

# Size as % of pool TVL — your single trade impact
SIZE_PCT_LIQ_BLOCK = 0.05   # >5% of pool = block (you ARE the pool)
SIZE_PCT_LIQ_WARN = 0.02    # >2% = warn (noticeable single-trade impact)

# Slippage projections (constant-product AMM approx; reserve_side ~= liq/2)
# entry slippage ~ size / (liq/2)  ; SL exit assumes pool shrinks ~50% by SL fire
SL_EXIT_POOL_SHRINK = 0.50  # assumed pool depth at SL fire time
SL_SLIP_BLOCK_PCT = 25.0    # projected SL-fire slippage >25% = block
SL_SLIP_WARN_PCT = 10.0     # >10% = warn

# Revenge-trade window
REVENGE_BLOCK_MIN = 30      # last realized loss within 30min = block
REVENGE_WARN_MIN = 120      # within 2h = warn

K_CLOSED = "positions:closed"
K_LAST_BUY_BLOCK = "guard:last_block"   # diagnostic


# ---------- LAST LOSS LOOKUP ----------

def _minutes_since_last_loss() -> float | None:
    """Return minutes since most-recent realized-loss close, or None if no loss
    found in positions:closed (last 100 entries)."""
    try:
        raw = _redis.get(K_CLOSED)
        if not raw:
            return None
        closed = json.loads(raw) or []
        if not isinstance(closed, list):
            return None
        now = time.time()
        for entry in closed:  # already newest-first
            if not isinstance(entry, dict):
                continue
            pnl = entry.get("pnl_usd")
            ts = entry.get("closed_at")
            if pnl is None or ts is None:
                continue
            try:
                if float(pnl) < 0:
                    return max(0.0, (now - float(ts)) / 60.0)
            except (TypeError, ValueError):
                continue
        return None
    except Exception as e:
        log.warning(f"_minutes_since_last_loss failed: {e}")
        return None


# ---------- CORE CHECKS ----------

def _size_check(size_usd: float, capital_usd: float):
    if not capital_usd or capital_usd <= 0:
        return ([], [], [])
    pct = size_usd / capital_usd
    if pct >= SIZE_BLOCK_PCT_CAP:
        return (
            [f"Size ${size_usd:.2f} is {pct*100:.0f}% of capital ${capital_usd:.2f} — "
             f"hard block at {SIZE_BLOCK_PCT_CAP*100:.0f}%. Memecoin strategy is many small bets, "
             f"not one conviction. Use `force` flag to override."],
            [], [])
    if pct >= SIZE_WARN_PCT_CAP:
        return (
            [],
            [f"Size ${size_usd:.2f} is {pct*100:.0f}% of capital ${capital_usd:.2f} — "
             f"above {SIZE_WARN_PCT_CAP*100:.0f}% guardrail. Concentration risk."],
            [])
    return ([], [], [f"Size {pct*100:.0f}% of capital — within guardrail."])


def _liquidity_check(size_usd: float, liq_usd: float):
    if liq_usd is None:
        return ([], [f"Liquidity unknown — slippage projection skipped. Treat as thin."], [])
    if liq_usd < LIQ_BLOCK_USD:
        return (
            [f"Liquidity ${liq_usd:,.0f} is GRAVEYARD (<${LIQ_BLOCK_USD:,.0f}) — "
             f"exit will be impossible at any size."],
            [], [])
    blocks, warns, infos = [], [], []
    if liq_usd < LIQ_WARN_USD:
        warns.append(
            f"Thin liquidity ${liq_usd:,.0f} — in a fast dump, SL may slip 20-50% "
            f"past trigger regardless of bot polling speed. This is the grail-loss class.")
    elif liq_usd < LIQ_INFO_USD:
        infos.append(f"Moderate liquidity ${liq_usd:,.0f} — manageable but watch the exit.")
    return (blocks, warns, infos)


def _size_vs_pool_check(size_usd: float, liq_usd: float):
    if not liq_usd or liq_usd <= 0:
        return ([], [], [])
    pct = size_usd / liq_usd
    if pct >= SIZE_PCT_LIQ_BLOCK:
        return (
            [f"Your ${size_usd:.2f} buy is {pct*100:.1f}% of the entire ${liq_usd:,.0f} pool — "
             f"you ARE the liquidity. Your own exit will rug yourself."],
            [], [])
    if pct >= SIZE_PCT_LIQ_WARN:
        return (
            [],
            [f"Size ${size_usd:.2f} is {pct*100:.1f}% of pool — your single sell "
             f"moves price meaningfully. Project ~{pct*200:.1f}% slippage even calm."],
            [])
    return ([], [], [])


def _slippage_projection(size_usd: float, liq_usd: float):
    """Returns (entry_slip_pct, sl_exit_slip_pct) — both percent values."""
    if not liq_usd or liq_usd <= 0:
        return (None, None)
    # constant-product: impact ~= size / reserve_quote_side; reserve_side ~= liq/2
    reserve_side = liq_usd / 2.0
    entry_slip = (size_usd / reserve_side) * 100  # %
    # At SL fire, assume the pool has shrunk by SL_EXIT_POOL_SHRINK (panic dump
    # drains liq). Position value is also smaller, but only ~30% smaller; pool
    # shrinks much more. Effective exit slippage compounds: smaller pool +
    # multi-seller competition.
    reserve_at_sl = reserve_side * (1 - SL_EXIT_POOL_SHRINK)
    size_at_sl = size_usd * 0.70  # remaining notional at SL trigger (~-30%)
    sl_slip = (size_at_sl / reserve_at_sl) * 100 if reserve_at_sl > 0 else None
    return (entry_slip, sl_slip)


def _sl_realism_check(sl_slip_pct: float | None, liq_usd: float):
    if sl_slip_pct is None:
        return ([], [], [])
    if sl_slip_pct >= SL_SLIP_BLOCK_PCT:
        return (
            [f"Projected SL-fire slippage ~{sl_slip_pct:.0f}% — your -30% SL will fill "
             f"closer to -55%+. Either smaller size or tighter SL needed."],
            [], [])
    if sl_slip_pct >= SL_SLIP_WARN_PCT:
        return (
            [],
            [f"Projected SL-fire slippage ~{sl_slip_pct:.0f}% — expect -30% SL to fill "
             f"around -{30 + sl_slip_pct:.0f}%. Plan for it."],
            [])
    return ([], [], [f"Projected SL-fire slippage ~{sl_slip_pct:.1f}% — SL should fill cleanly."])


def _revenge_check():
    mins = _minutes_since_last_loss()
    if mins is None:
        return ([], [], ["No recent realized loss — revenge guard clear."], None)
    if mins <= REVENGE_BLOCK_MIN:
        return (
            [f"Last realized loss was {mins:.0f}min ago (<{REVENGE_BLOCK_MIN}min revenge window). "
             f"Hard block. Cool down. Use `force` flag if you're sure this isn't tilt."],
            [], [], mins)
    if mins <= REVENGE_WARN_MIN:
        return (
            [],
            [f"Last realized loss was {mins:.0f}min ago (<{REVENGE_WARN_MIN}min). "
             f"Revenge-trade risk. Confirm this is a planned entry, not tilt."],
            [], mins)
    return ([], [], [f"Last realized loss {mins/60:.1f}h ago — outside revenge window."], mins)


# ---------- ORCHESTRATOR ----------

def run_guard(size_usd: float, capital_usd: float, liq_usd: float | None,
              entry_price: float = None, sl_price: float = None) -> dict:
    """Run all checks. Returns decision dict (see module docstring)."""
    reasons_block = []
    reasons_warn = []
    reasons_info = []

    # Defensive coercion
    try:
        size_usd = float(size_usd) if size_usd is not None else 0.0
    except (TypeError, ValueError):
        size_usd = 0.0
    try:
        capital_usd = float(capital_usd) if capital_usd is not None else 0.0
    except (TypeError, ValueError):
        capital_usd = 0.0
    try:
        liq_usd = float(liq_usd) if liq_usd is not None else None
    except (TypeError, ValueError):
        liq_usd = None

    # 1. Size vs capital
    b, w, i = _size_check(size_usd, capital_usd)
    reasons_block += b; reasons_warn += w; reasons_info += i

    # 2. Liquidity floor
    b, w, i = _liquidity_check(size_usd, liq_usd)
    reasons_block += b; reasons_warn += w; reasons_info += i

    # 3. Size vs pool TVL
    b, w, i = _size_vs_pool_check(size_usd, liq_usd)
    reasons_block += b; reasons_warn += w; reasons_info += i

    # 4. Slippage projection + SL realism
    entry_slip, sl_slip = _slippage_projection(size_usd, liq_usd)
    b, w, i = _sl_realism_check(sl_slip, liq_usd)
    reasons_block += b; reasons_warn += w; reasons_info += i

    # 5. Revenge guard
    b, w, i, mins_since = _revenge_check()
    reasons_block += b; reasons_warn += w; reasons_info += i

    metrics = {
        "size_pct_capital":   (size_usd / capital_usd * 100) if capital_usd > 0 else None,
        "size_pct_liq":       (size_usd / liq_usd * 100) if liq_usd else None,
        "entry_slip_pct":     entry_slip,
        "sl_exit_slip_pct":   sl_slip,
        "minutes_since_loss": mins_since,
    }

    return {
        "block": len(reasons_block) > 0,
        "warn":  len(reasons_warn) > 0,
        "reasons_block": reasons_block,
        "reasons_warn":  reasons_warn,
        "reasons_info":  reasons_info,
        "metrics": metrics,
    }


# ---------- FORMATTING ----------

def format_panel(decision: dict, prefix: str = "🛡 *Capital Guard*") -> str:
    """Telegram-formatted panel. Always safe to show. Returns empty string only
    if decision is None."""
    if not decision:
        return ""
    lines = [prefix]
    if decision["block"]:
        lines.append("🚫 *BLOCKED*")
        for r in decision["reasons_block"]:
            lines.append(f"   • {r}")
    elif decision["warn"]:
        lines.append("⚠️  *WARNING*")
    else:
        lines.append("✅ All checks pass.")
    if decision["reasons_warn"]:
        for r in decision["reasons_warn"]:
            lines.append(f"   ⚠ {r}")
    m = decision.get("metrics") or {}
    snapshot = []
    if m.get("size_pct_capital") is not None:
        snapshot.append(f"size={m['size_pct_capital']:.0f}% cap")
    if m.get("size_pct_liq") is not None:
        snapshot.append(f"={m['size_pct_liq']:.2f}% pool")
    if m.get("entry_slip_pct") is not None:
        snapshot.append(f"entry slip~{m['entry_slip_pct']:.1f}%")
    if m.get("sl_exit_slip_pct") is not None:
        snapshot.append(f"SL slip~{m['sl_exit_slip_pct']:.0f}%")
    if snapshot:
        lines.append(f"   _Metrics: {' | '.join(snapshot)}_")
    if decision["block"]:
        lines.append("   _Append `force` to override (e.g. `/buy CA SIZE force`)._")
    return "\n".join(lines)


def format_check_panel(liq_usd: float | None, capital_usd: float,
                       default_size_usd: float) -> str:
    """For /check — projects guard at default 15%-of-capital sizing.
    No revenge check here (info-only on /check)."""
    decision = run_guard(default_size_usd, capital_usd, liq_usd)
    # Strip revenge-related reasons from /check display — only relevant on /buy
    decision["reasons_block"] = [r for r in decision["reasons_block"]
                                 if "revenge" not in r.lower() and "realized loss" not in r.lower()]
    decision["reasons_warn"] = [r for r in decision["reasons_warn"]
                                if "revenge" not in r.lower() and "realized loss" not in r.lower()]
    decision["block"] = len(decision["reasons_block"]) > 0
    decision["warn"] = len(decision["reasons_warn"]) > 0
    return format_panel(decision, prefix="🛡 *Guard preview* (at default 15% sizing)")
