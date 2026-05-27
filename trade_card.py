"""
Module 3 — Trade Card.

Advisory only. Reads capital from Redis (set:capital_usd), computes:
  - Entry size in USD (15% of capital for GREEN, fixed $5 for YELLOW)
  - TP1 at 2x (sell 50% — recovers cost basis, Shashi's proven edge)
  - TP2 at 3x (sell remainder, ride free)
  - SL at -30% (memecoin standard)

Does NOT execute trades. Rule #2 in handover: never wire execution
until the 12 risk rules are confirmed Y/N by user.
"""

import os
import redis
import logging

log = logging.getLogger(__name__)

_redis = redis.from_url(
    os.environ.get("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True,
    ssl_cert_reqs=None,
)

DEFAULT_CAPITAL_USD = 25.0
GREEN_POSITION_PCT  = 0.15   # 15% of capital
YELLOW_POSITION_USD = 5.0    # fixed $5 (per SYSTEM_PROMPT rule)
TP1_MULT = 2.0               # sell 50% here
TP1_SELL_PCT = 0.50
TP2_MULT = 3.0               # sell remainder here
SL_PCT   = -0.30             # -30% stop loss


def get_capital_usd() -> float:
    try:
        val = _redis.get("state:capital_usd")
        return float(val) if val else DEFAULT_CAPITAL_USD
    except Exception:
        return DEFAULT_CAPITAL_USD


def compute_trade_card(verdict: str, price_usd, capital_usd: float = None) -> dict | None:
    """Returns trade card dict, or None if verdict is RED/INVALID or price missing."""
    if verdict not in ("GREEN", "YELLOW"):
        return None
    try:
        price = float(price_usd) if price_usd is not None else None
    except (TypeError, ValueError):
        price = None
    if not price or price <= 0:
        return None

    if capital_usd is None:
        capital_usd = get_capital_usd()

    if verdict == "GREEN":
        entry_usd = round(capital_usd * GREEN_POSITION_PCT, 2)
    else:
        entry_usd = min(YELLOW_POSITION_USD, round(capital_usd * GREEN_POSITION_PCT, 2))

    tokens     = entry_usd / price
    tp1_price  = price * TP1_MULT
    tp2_price  = price * TP2_MULT
    sl_price   = price * (1 + SL_PCT)
    tp1_value  = entry_usd * TP1_MULT * TP1_SELL_PCT       # USD returned at TP1
    tp2_value  = entry_usd * TP2_MULT * (1 - TP1_SELL_PCT)  # USD returned at TP2
    total_out  = tp1_value + tp2_value
    sl_loss    = entry_usd * SL_PCT                          # negative

    return {
        "verdict":    verdict,
        "capital":    capital_usd,
        "entry_usd":  entry_usd,
        "entry_price": price,
        "tokens":     tokens,
        "tp1_price":  tp1_price,
        "tp1_sell_pct": TP1_SELL_PCT,
        "tp1_value":  tp1_value,
        "tp2_price":  tp2_price,
        "tp2_value":  tp2_value,
        "total_out_if_both": total_out,
        "sl_price":   sl_price,
        "sl_loss":    sl_loss,
    }


def _fmt_price(p: float) -> str:
    """Format price with enough precision for memecoins."""
    if p is None:
        return "?"
    if p >= 1:
        return f"${p:,.4f}"
    if p >= 0.01:
        return f"${p:.4f}"
    if p >= 0.0001:
        return f"${p:.6f}"
    return f"${p:.9f}"


def format_trade_card(card: dict) -> str:
    """Telegram-formatted (Markdown). Empty string if card is None."""
    if not card:
        return ""
    v = card["verdict"]
    badge = "🟢" if v == "GREEN" else "🟡"
    sizing_note = (
        "15% of capital"
        if v == "GREEN"
        else f"YELLOW rules: fixed ${YELLOW_POSITION_USD:.0f} cap"
    )

    lines = [
        f"\n💼 *Trade Card* {badge} (capital ${card['capital']:.2f})",
        f"   Entry: *${card['entry_usd']:.2f}* @ {_fmt_price(card['entry_price'])}  _({sizing_note})_",
        f"   🎯 TP1 @ {_fmt_price(card['tp1_price'])} (2x) → sell 50% = +${card['tp1_value']:.2f}",
        f"   🎯 TP2 @ {_fmt_price(card['tp2_price'])} (3x) → sell rest = +${card['tp2_value']:.2f}",
        f"   🛑 SL  @ {_fmt_price(card['sl_price'])} (-30%) → -${abs(card['sl_loss']):.2f}",
        f"   _If both TPs hit: +${card['total_out_if_both'] - card['entry_usd']:.2f} profit. Pre-set TP — your edge._",
        f"   _Advisory only. Manual execution on Bitget._",
    ]
    return "\n".join(lines)


def trade_card_for_check(check_result: dict) -> str:
    """Convenience: build + format from a check_token() result dict."""
    verdict = check_result.get("verdict")
    price   = (check_result.get("details") or {}).get("price_usd")
    card    = compute_trade_card(verdict, price)
    return format_trade_card(card)
