import telebot
from groq import Groq
import redis
import json
import os
import base64
import threading
import logging

log = logging.getLogger(__name__)
from rug_check import check_token, format_report, is_valid_solana_mint
from tools import TOOLS_SCHEMA, execute_tool
from scanner import scan as run_scan, format_scan_results
from smart_wallets import add_wallet, remove_wallet, load_wallets, _all_wallets
from wallet_discovery import discover_wallets
import watcher as watcher_module
import memory_store
import position_tracker
import sleep_mode
import loss_tracker
from datetime import datetime, timezone, timedelta

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY")
OWNER_TELEGRAM_ID = os.environ.get("OWNER_TELEGRAM_ID")
TEXT_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
TEXT_MODEL_FALLBACK = "llama-3.3-70b-versatile"
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# Redis — persistent memory across redeploys
_redis = redis.from_url(
    os.environ.get("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True,
    ssl_cert_reqs=None,
)
MAX_HISTORY = 30  # cap to avoid runaway context

SYSTEM_PROMPT = (
    "You are SSHETTY bot — a brutally honest Solana memecoin and crypto trading "
    "assistant for Shashi. You help with rug detection, on-chain analysis, trade "
    "discipline, and meme coin strategy. You have a built-in /check command that "
    "runs mechanical on-chain rug checks (mint authority, freeze authority, "
    "liquidity, age, Rugcheck composite). When the user pastes a Solana CA, that "
    "check auto-runs and the result is in this conversation. Use it as context.\n\n"
    "You have three live tools available via function calling:\n"
    "  - get_token_data(mint): LIVE Solana token data (price, MC, FDV, liq, volume). "
    "ALWAYS use this for any token-specific data question. NEVER web_search for token "
    "price/MC. NOTE: Bitget UI shows FDV labeled as 'MC' — when answering, mention "
    "BOTH market_cap and fdv if they differ significantly.\n"
    "  - web_search(query): general web search for non-token info (BTC/ETH price, news, "
    "exchange status, events).\n"
    "  - fetch_url(url): read a specific URL's content.\n"
    "Never guess at prices, MC, or volumes. Always call a tool first.\n\n"
    "Rules you enforce:\n"
    "- Never recommend buying a token marked RED.\n"
    "- For YELLOW, only allow buys with strict $5 position + 2x take-profit + 50% cost-basis-out plan.\n"
    "- For GREEN, remind user that mechanical pass != price will pump. Most clean tokens still die quietly.\n"
    "- Always remind: post-grad survivor zone is MC $80K–$250K, age 1h–12h.\n"
    "- Brutal honesty. No sugarcoating. Short answers preferred."
)

MAX_TOOL_ITERATIONS = 4

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Groq(api_key=GROQ_API_KEY)

# ---------- REDIS STORAGE ----------

def load_history(user_id):
    try:
        data = _redis.get(f"history:{user_id}")
        return json.loads(data) if data else []
    except Exception:
        return []

def save_history(user_id, history):
    sys_msgs = [m for m in history if m["role"] == "system"]
    other    = [m for m in history if m["role"] != "system"][-MAX_HISTORY:]
    history  = sys_msgs + other
    try:
        _redis.set(f"history:{user_id}", json.dumps(history))
    except Exception as e:
        log.warning(f"Redis save_history failed: {e}")

def _get_state(key: str, default: str = "") -> str:
    try:
        val = _redis.get(f"state:{key}")
        return val if val is not None else default
    except Exception:
        return default

def _set_state(key: str, value: str):
    try:
        _redis.set(f"state:{key}", value)
    except Exception as e:
        log.warning(f"Redis set_state failed: {e}")

# ---------- LONG-TERM MEMORY ----------

MEMORY_KEY = "shashi:memories"

def load_memories() -> list:
    try:
        data = _redis.get(MEMORY_KEY)
        return json.loads(data) if data else []
    except Exception:
        return []

def save_memories(memories: list):
    try:
        _redis.set(MEMORY_KEY, json.dumps(memories))
    except Exception as e:
        log.warning(f"Redis save_memories failed: {e}")

def memories_as_context() -> str:
    memories = load_memories()
    if not memories:
        return ""
    lines = "\n".join(f"- {m}" for m in memories)
    return f"\n\nShashi's permanent rules & facts (always apply these):\n{lines}"

def ensure_system_prompt(history):
    full_prompt = SYSTEM_PROMPT + memories_as_context()
    if not history or history[0].get("role") != "system":
        history = [{"role": "system", "content": full_prompt}] + history
    else:
        history[0] = {"role": "system", "content": full_prompt}
    return history


def run_rug_check_and_remember(message, mint: str):
    """Run rug check, send report to user, and write everything into chat history
    so follow-up questions like 'is it safe to buy?' have full context."""
    user_id = message.chat.id
    bot.reply_to(message, f"🔍 Checking `{mint[:8]}...{mint[-6:]}`...", parse_mode="Markdown")
    try:
        result = check_token(mint)
        report = format_report(result)
        bot.send_message(
            user_id,
            report,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        # Persist to memory store — survives redeploys, available to /history /lookup
        try:
            d = result.get("details") or {}
            memory_store.save_check(
                user_id=user_id,
                mint=mint,
                symbol=d.get("symbol"),
                verdict=result.get("verdict"),
                mc=d.get("market_cap"),
                liq=d.get("liquidity_usd"),
                reasons_red=result.get("reasons_red"),
                reasons_yellow=result.get("reasons_yellow"),
            )
        except Exception as e:
            log.warning(f"save_check failed: {e}")
        # Inject into history so Groq remembers it
        history = ensure_system_prompt(load_history(user_id))
        history.append({"role": "user", "content": f"[Pasted Solana CA] {mint}"})
        compact = (
            f"Rug-check result for {mint}:\n"
            f"VERDICT: {result['verdict']}\n"
            f"Red flags: {result.get('reasons_red') or 'none'}\n"
            f"Warnings: {result.get('reasons_yellow') or 'none'}\n"
            f"Passed: {result.get('reasons_green') or 'none'}\n"
            f"Details: {result.get('details')}"
        )
        history.append({"role": "assistant", "content": compact})
        save_history(user_id, history)
    except Exception as e:
        bot.reply_to(message, f"⚠️ Check failed: {e.__class__.__name__}: {e}")

# ---------- RUG CHECK COMMAND ----------
@bot.message_handler(commands=['check', 'rug'])
def handle_check(message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(
            message,
            "Usage: `/check <SOLANA_MINT_ADDRESS>`\n\nExample:\n`/check So11111111111111111111111111111111111111112`",
            parse_mode="Markdown",
        )
        return
    mint = parts[1].strip()
    if not is_valid_solana_mint(mint):
        bot.reply_to(message, "❌ That doesn't look like a Solana mint address (base58, 32–44 chars).")
        return
    run_rug_check_and_remember(message, mint)


@bot.message_handler(commands=['help', 'start'])
def handle_help(message):
    bot.reply_to(
        message,
        "*SSHETTY bot*\n\n"
        "💬 Just message me — Groq-powered crypto assistant with memory.\n"
        "📸 Send a photo — I'll describe it.\n"
        "🛡 Paste any Solana CA — auto rug-check.\n"
        "🛡 `/check <mint>` — explicit rug-check.\n"
        "🔍 `/scan` — Bitget-Latest-equivalent token scanner.\n"
        "🧹 `/reset` — wipe conversation memory.\n"
        "🧠 `/remember <fact>` — save a permanent rule or fact.\n"
        "📋 `/memories` — show all permanent memories.\n"
        "🗑 `/forget <fact>` — delete a memory.\n\n"
        "*Smart Wallet Tracker (owner only):*\n"
        "🐋 `/addwallet <addr> <label>` — track a wallet.\n"
        "📋 `/listwallets` — show all tracked wallets.\n"
        "❌ `/removewallet <addr>` — stop tracking a wallet.\n"
        "🔍 `/discoverwallet` — auto-find smart money wallets.\n"
        "👁 `/watcher on/off/status` — narrative alert scanner.\n"
        "💰 `/capital <amount>` — update your capital (used for trade sizing).\n\n"
        "*Position Tracker (24/7 TP/SL watchdog):*\n"
        "📥 `/buy <CA> [size] [entry]` — open position (auto-pings TP1/TP2/SL)\n"
        "📤 `/sell <CA>` — manually close\n"
        "📂 `/positions` — list open positions\n"
        "📁 `/closed` — last 20 closed\n\n"
        "*Sleep + Loss tracking:*\n"
        "😴 `/sleep on/off/status` — silence watcher alerts (TP/SL still fire)\n"
        "📊 `/losses` — log of all losing trades + Fib/volume analysis\n\n"
        "*Memory (persists across redeploys):*\n"
        "🚨 `/alerts [keyword]` — last 20 watcher alerts (or search by word)\n"
        "📋 `/history` — your last 20 /check results\n"
        "🔎 `/lookup <CA>` — everything bot remembers about a CA\n"
        "🧠 `/memstats` — memory store size\n\n"
        "_Every GREEN/YELLOW report includes a 💼 Trade Card: entry $, TP1 (2x, sell 50%), TP2 (3x), SL (-30%)._\n\n"
        "_Defensive checks only. Not financial advice._",
        parse_mode="Markdown",
    )


# ---------- OWNER GUARD ----------
def is_owner(message) -> bool:
    """Returns True only if sender is the bot owner (OWNER_TELEGRAM_ID env var)."""
    if not OWNER_TELEGRAM_ID:
        return False
    return str(message.chat.id) == str(OWNER_TELEGRAM_ID)

def owner_only(message) -> bool:
    """Sends rejection and returns False if not owner."""
    if is_owner(message):
        return True
    bot.reply_to(message, "⛔ Owner-only command.")
    return False


# ---------- SMART WALLET COMMANDS ----------
@bot.message_handler(commands=['addwallet'])
def handle_addwallet(message):
    if not owner_only(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message, "Usage: `/addwallet <solana_address> <label>`\nExample: `/addwallet AbC...xyz ansem`", parse_mode="Markdown")
        return
    addr, label = parts[1].strip(), parts[2].strip()
    if not is_valid_solana_mint(addr):
        bot.reply_to(message, "❌ That doesn't look like a valid Solana address.")
        return
    if add_wallet(addr, label):
        active = load_wallets()
        bot.reply_to(message, f"✅ Added `{label}` (`{addr[:8]}...`). Total tracked: {len(active)}", parse_mode="Markdown")
    else:
        bot.reply_to(message, f"⚠️ Address already tracked.")


@bot.message_handler(commands=['removewallet'])
def handle_removewallet(message):
    if not owner_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: `/removewallet <solana_address>`", parse_mode="Markdown")
        return
    addr = parts[1].strip()
    if remove_wallet(addr):
        bot.reply_to(message, f"✅ Removed `{addr[:8]}...`", parse_mode="Markdown")
    else:
        bot.reply_to(message, f"⚠️ Address not found in tracked list.")


@bot.message_handler(commands=['listwallets'])
def handle_listwallets(message):
    if not owner_only(message):
        return
    all_w = _all_wallets()
    active = [w for w in all_w if not str(w.get("address", "")).startswith("TODO")]
    todo   = [w for w in all_w if str(w.get("address", "")).startswith("TODO")]

    lines = [f"*Smart Wallet List* ({len(active)} active, {len(todo)} pending)\n"]
    if active:
        for w in active:
            addr = w.get("address", "")
            lines.append(f"• `{addr[:8]}...{addr[-4:]}` — {w.get('label','?')} _(src: {w.get('source','?')})_")
    else:
        lines.append("_(No active wallets yet — add via /addwallet)_")

    if todo:
        lines.append(f"\n_{len(todo)} TODO placeholder(s) — replace in smart\\_wallets.json_")

    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


# ---------- WALLET DISCOVERY ----------
@bot.message_handler(commands=['discoverwallet', 'discoverwallets'])
def handle_discover(message):
    if not owner_only(message):
        return

    chat_id = message.chat.id

    bot.reply_to(
        message,
        "🔍 *Wallet discovery started in background.*\n"
        "Bot stays fully responsive while this runs.\n"
        "I'll message you with progress + final results.\n"
        "Takes 3–8 minutes.",
        parse_mode="Markdown",
    )

    def progress(msg):
        try:
            bot.send_message(chat_id, msg)
        except Exception:
            pass

    def _run():
        try:
            result = discover_wallets(progress_callback=progress)

            added   = result["added"]
            total   = len(load_wallets())
            src_str = ", ".join(f"{k}: {v}" for k, v in result["sources"].items()) if result.get("sources") else "gecko+rpc"

            summary = (
                f"✅ *Discovery complete!*\n\n"
                f"🐋 *{added} new wallets added* (total now: {total})\n"
                f"❌ {result.get('skipped_quality', 0)} didn't appear in 2+ tokens\n"
                f"😴 {result.get('skipped_inactive', 0)} inactive (no tx in 7 days)\n"
                f"♻️ {result.get('skipped_duplicate', 0)} already tracked\n\n"
                f"Source: {src_str}\n"
                f"Run `/listwallets` to see the full list."
            )

            if added == 0:
                summary += (
                    "\n\n⚠️ *0 added.* Possible reasons:\n"
                    "• Market slow — few tokens graduated recently\n"
                    "• Solana RPC rate-limited — try again in 30 min\n"
                    "• Add manually: `/addwallet <addr> <label>`"
                )

            bot.send_message(chat_id, summary, parse_mode="Markdown")

        except Exception as e:
            bot.send_message(
                chat_id,
                f"⚠️ Discovery failed: `{e.__class__.__name__}: {str(e)[:300]}`",
                parse_mode="Markdown",
            )

    # Run in background thread — never blocks the polling loop
    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------- WATCHER COMMANDS ----------

def _watcher_alert(text: str):
    """Send alert to owner — or queue it if sleep mode is on."""
    if not OWNER_TELEGRAM_ID:
        return
    # Sleep mode: queue watcher alerts only (position TP/SL still fire)
    if sleep_mode.queue_alert(text):
        log.info("Watcher alert queued (sleep mode on)")
        return
    try:
        bot.send_message(
            OWNER_TELEGRAM_ID,
            text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning(f"Watcher alert send failed: {e}")


@bot.message_handler(commands=['watcher'])
def handle_watcher(message):
    if not owner_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    action = parts[1].strip().lower() if len(parts) > 1 else "status"

    if action == "on":
        if watcher_module.is_running():
            bot.reply_to(message, "👁 Watcher already running.")
        else:
            watcher_module.start(_watcher_alert)
            bot.reply_to(
                message,
                "👁 *Watcher ON* — scanning every 5 min.\n"
                "Alerts when a narrative forms on pump.fun + Twitter confirms.\n"
                "Stop with `/watcher off`",
                parse_mode="Markdown",
            )
    elif action == "off":
        watcher_module.stop()
        bot.reply_to(message, "🔕 Watcher stopped.")
    else:
        s = watcher_module.get_status()
        status = "✅ Running" if s["running"] else "⛔ Stopped"
        if s["scan_count"] == 0:
            scan_line = "⏳ First scan not done yet"
        else:
            scan_line = (
                f"🔄 Scans done: {s['scan_count']}\n"
                f"⏱ Last scan: {s['mins_ago']} min ago\n"
                f"🚨 Last scan alerts: {s['last_found']}"
            )
        bot.reply_to(
            message,
            f"👁 *Watcher status:* {status}\n\n"
            f"{scan_line}\n\n"
            f"`/watcher on` — start\n"
            f"`/watcher off` — stop",
            parse_mode="Markdown",
        )


# ---------- MEMORY COMMANDS ----------

@bot.message_handler(commands=['remember'])
def handle_remember(message):
    if not owner_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Usage: `/remember <fact>`\nExample: `/remember never buy RED tokens`", parse_mode="Markdown")
        return
    fact = parts[1].strip()
    memories = load_memories()
    if fact.lower() in [m.lower() for m in memories]:
        bot.reply_to(message, "✅ Already remembered.")
        return
    memories.append(fact)
    save_memories(memories)
    bot.reply_to(message, f"🧠 Remembered: _{fact}_\nTotal memories: {len(memories)}", parse_mode="Markdown")


@bot.message_handler(commands=['memories'])
def handle_memories(message):
    if not owner_only(message):
        return
    memories = load_memories()
    if not memories:
        bot.reply_to(message, "🧠 No memories saved yet.\nUse `/remember <fact>` to add one.", parse_mode="Markdown")
        return
    lines = [f"🧠 *Permanent memories ({len(memories)}):*\n"]
    for i, m in enumerate(memories, 1):
        lines.append(f"{i}. {m}")
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=['forget'])
def handle_forget(message):
    if not owner_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Usage: `/forget <fact>`\nOr `/forget all` to wipe everything.", parse_mode="Markdown")
        return
    arg = parts[1].strip()
    if arg.lower() == "all":
        save_memories([])
        bot.reply_to(message, "🧠 All memories wiped.")
        return
    memories = load_memories()
    new = [m for m in memories if m.lower() != arg.lower()]
    if len(new) == len(memories):
        bot.reply_to(message, "⚠️ Memory not found. Use `/memories` to see exact text.", parse_mode="Markdown")
        return
    save_memories(new)
    bot.reply_to(message, f"✅ Forgotten. Memories remaining: {len(new)}", parse_mode="Markdown")


# ---------- CAPITAL COMMAND ----------
@bot.message_handler(commands=['capital'])
def handle_capital(message):
    if not owner_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        cap = _get_state("capital_usd", "25")
        bot.reply_to(message, f"💰 Current capital: *${cap}*\nTo update: `/capital 50`", parse_mode="Markdown")
        return
    try:
        amount = float(parts[1].strip().replace("$", ""))
        _set_state("capital_usd", str(amount))
        entry = round(amount * 0.15, 2)
        bot.reply_to(message, f"✅ Capital updated to *${amount:.2f}*\n15% entry size = *${entry:.2f}*", parse_mode="Markdown")
    except ValueError:
        bot.reply_to(message, "❌ Invalid amount. Example: `/capital 50`", parse_mode="Markdown")


# ---------- POSITION TRACKER COMMANDS ----------

def _position_alert(text: str):
    """Send TP/SL alert to owner."""
    if not OWNER_TELEGRAM_ID:
        return
    try:
        bot.send_message(OWNER_TELEGRAM_ID, text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        log.warning(f"Position alert send failed: {e}")


def _default_size_usd() -> float:
    """15% of capital, default sizing."""
    try:
        cap = float(_get_state("capital_usd", "25"))
        return round(cap * 0.15, 2)
    except Exception:
        return 3.75


@bot.message_handler(commands=['buy'])
def handle_buy(message):
    if not owner_only(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message,
            "Usage: `/buy <CA> [size_usd] [entry_price]`\n\n"
            "Examples:\n"
            "  `/buy 7xKj...pump` — uses 15% of capital, live price\n"
            "  `/buy 7xKj...pump 5` — $5 size, live price\n"
            "  `/buy 7xKj...pump 5 0.00012` — $5, manual entry price",
            parse_mode="Markdown")
        return
    mint = parts[1].strip()
    if not is_valid_solana_mint(mint):
        bot.reply_to(message, "❌ Not a valid Solana CA.")
        return
    size = _default_size_usd()
    entry = None
    try:
        if len(parts) >= 3:
            size = float(parts[2].replace("$", ""))
        if len(parts) >= 4:
            entry = float(parts[3].replace("$", ""))
    except ValueError:
        bot.reply_to(message, "❌ Size/entry must be numbers.")
        return

    bot.reply_to(message, "📥 Opening position…")
    result = position_tracker.open_position(mint, size, entry)
    if not result.get("ok"):
        bot.reply_to(message, f"❌ {result.get('error', 'unknown error')}")
        return
    p = result["position"]
    bot.send_message(
        message.chat.id,
        f"✅ *Position opened — {p['symbol']}*\n\n"
        + position_tracker.format_position(p, live_price=p["entry_price"])
        + "\n\n_Tracker pinging you every 60s. TP1/TP2/SL fire automatically._",
        parse_mode="Markdown", disable_web_page_preview=True,
    )


@bot.message_handler(commands=['sell'])
def handle_sell(message):
    if not owner_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: `/sell <CA>`", parse_mode="Markdown")
        return
    mint = parts[1].strip()
    result = position_tracker.close_position(mint, reason="manual")
    if not result.get("ok"):
        bot.reply_to(message, f"❌ {result.get('error')}")
        return
    p = result["position"]
    pnl = p.get("pnl_usd", 0)
    pnl_pct = p.get("pnl_pct", 0)
    icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
    bot.reply_to(
        message,
        f"{icon} *Closed {p['symbol']}*\n"
        f"Exit: ${p['exit_price']:.8f}\n"
        f"P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=['positions'])
def handle_positions(message):
    if not owner_only(message):
        return
    positions = position_tracker.list_open()
    bot.reply_to(message, position_tracker.format_open_list(positions),
                 parse_mode="Markdown", disable_web_page_preview=True)


@bot.message_handler(commands=['closed'])
def handle_closed(message):
    if not owner_only(message):
        return
    positions = position_tracker.list_closed(limit=20)
    bot.reply_to(message, position_tracker.format_closed_list(positions),
                 parse_mode="Markdown", disable_web_page_preview=True)


# ---------- SLEEP MODE COMMANDS ----------

@bot.message_handler(commands=['sleep'])
def handle_sleep(message):
    if not owner_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    action = parts[1].strip().lower() if len(parts) > 1 else "status"

    if action == "on":
        if sleep_mode.is_sleeping():
            bot.reply_to(message, "😴 Already sleeping.")
            return
        result = sleep_mode.turn_on()
        if result.get("ok"):
            bot.reply_to(
                message,
                "😴 *Sleep mode ON*\n\n"
                "Watcher narrative alerts will be queued silently.\n"
                "Position TP/SL/chat still fire normally.\n\n"
                "Wake with `/sleep off` for the summary.",
                parse_mode="Markdown",
            )
        else:
            bot.reply_to(message, f"⚠️ Failed: {result.get('error')}")

    elif action == "off":
        if not sleep_mode.is_sleeping():
            bot.reply_to(message, "☀️ Wasn't sleeping.")
            return
        result = sleep_mode.turn_off()
        if result.get("ok"):
            summary = sleep_mode.format_wake_summary(
                result.get("queue", []),
                result.get("duration_mins"),
            )
            bot.reply_to(message, summary, parse_mode="Markdown", disable_web_page_preview=True)
        else:
            bot.reply_to(message, f"⚠️ Failed: {result.get('error')}")

    else:
        s = sleep_mode.status()
        if s["sleeping"]:
            mins = s.get("mins_asleep", 0)
            bot.reply_to(
                message,
                f"😴 *Sleep mode: ON*\n\n"
                f"⏱ Asleep for: {mins // 60}h {mins % 60}m\n"
                f"📨 Alerts queued: {s['queue_size']}\n\n"
                f"`/sleep off` — wake + summary",
                parse_mode="Markdown",
            )
        else:
            bot.reply_to(
                message,
                "☀️ *Sleep mode: OFF*\n\n"
                "`/sleep on`  — silence watcher alerts\n"
                "`/sleep off` — wake + summary",
                parse_mode="Markdown",
            )


# ---------- LOSS TRACKER COMMANDS (data-only) ----------

@bot.message_handler(commands=['losses'])
def handle_losses(message):
    if not owner_only(message):
        return
    losses = loss_tracker.get_recent_losses(limit=20)
    s = loss_tracker.stats()
    header = (
        f"📊 *Loss log*  ({s['total']} total | "
        f"{s['real']} real / {s['unconfirmed']} unconfirmed | "
        f"net ${s['total_pnl']:+.2f})\n"
    )
    bot.reply_to(
        message,
        header + "\n" + loss_tracker.format_losses_list(losses),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ---------- MEMORY COMMANDS (alert / check / scan history) ----------

def _ago(ts: int) -> str:
    """Human-readable 'X min ago' / 'X h ago' / 'X d ago'."""
    now = datetime.now(timezone.utc).timestamp()
    secs = max(0, int(now - (ts or 0)))
    if secs < 60: return f"{secs}s ago"
    if secs < 3600: return f"{secs // 60}m ago"
    if secs < 86400: return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


@bot.message_handler(commands=['alerts'])
def handle_alerts(message):
    if not owner_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    # Optional needle: /alerts goblin
    needle = parts[1].strip() if len(parts) > 1 else None
    if needle:
        items = memory_store.search_alerts(needle, limit=200)
        header = f"🔎 *Alerts matching '{needle}'* ({len(items)} found)"
    else:
        items = memory_store.get_recent_alerts(limit=20)
        header = f"🚨 *Last {len(items)} watcher alerts*"

    if not items:
        bot.reply_to(message, "🚨 No alerts yet. Turn watcher on with `/watcher on`.", parse_mode="Markdown")
        return

    lines = [header, ""]
    for a in items[:20]:
        v_icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(a.get("verdict"), "⚪")
        sym = a.get("symbol") or "?"
        mint = a.get("mint") or ""
        mc = a.get("mc") or 0
        liq = a.get("liq") or 0
        narr = a.get("narrative") or "?"
        sw = a.get("smart_wallets") or 0
        lines.append(
            f"{v_icon} *{sym}* — \"{narr}\"  _{_ago(a.get('ts'))}_\n"
            f"   `{mint}`\n"
            f"   MC ${mc:,.0f} | Liq ${liq:,.0f} | 🐋 {sw}"
        )
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


@bot.message_handler(commands=['history'])
def handle_history(message):
    if not owner_only(message):
        return
    user_id = message.chat.id
    items = memory_store.get_recent_checks(user_id, limit=20)
    if not items:
        bot.reply_to(message, "📋 No /check history yet. Paste a CA to start.", parse_mode="Markdown")
        return
    lines = [f"📋 *Last {len(items)} rug checks*", ""]
    for c in items:
        v_icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "INVALID": "❌"}.get(c.get("verdict"), "⚪")
        sym = c.get("symbol") or "?"
        mint = c.get("mint") or ""
        mc = c.get("mc") or 0
        liq = c.get("liq") or 0
        lines.append(
            f"{v_icon} *{sym}*  _{_ago(c.get('ts'))}_\n"
            f"   `{mint}`\n"
            f"   MC ${mc:,.0f} | Liq ${liq:,.0f}"
        )
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


@bot.message_handler(commands=['lookup'])
def handle_lookup(message):
    """Look up everything the bot remembers about a CA."""
    if not owner_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: `/lookup <CA>`", parse_mode="Markdown")
        return
    ca = parts[1].strip()
    alert = memory_store.get_alert_by_ca(ca)
    check = memory_store.get_check_by_ca(ca)
    if not alert and not check:
        bot.reply_to(message, f"🔎 Nothing in memory for `{ca[:8]}...`\nRun `/check {ca}` to scan.", parse_mode="Markdown")
        return
    lines = [f"🔎 *Memory for `{ca[:8]}...{ca[-4:]}`*", ""]
    if alert:
        v_icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(alert.get("verdict"), "⚪")
        lines.append(
            f"🚨 *Watcher alert* _{_ago(alert.get('ts'))}_\n"
            f"   {v_icon} {alert.get('symbol')} | \"{alert.get('narrative')}\"\n"
            f"   MC ${(alert.get('mc') or 0):,.0f} | Liq ${(alert.get('liq') or 0):,.0f} | 🐋 {alert.get('smart_wallets') or 0}"
        )
    if check:
        v_icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(check.get("verdict"), "⚪")
        red = len(check.get("reasons_red") or [])
        yel = len(check.get("reasons_yellow") or [])
        lines.append(
            f"\n📋 *Rug check* _{_ago(check.get('ts'))}_\n"
            f"   {v_icon} {check.get('symbol')} | {red} red, {yel} yellow flags\n"
            f"   MC ${(check.get('mc') or 0):,.0f} | Liq ${(check.get('liq') or 0):,.0f}"
        )
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


@bot.message_handler(commands=['memstats'])
def handle_memstats(message):
    if not owner_only(message):
        return
    s = memory_store.stats()
    bot.reply_to(
        message,
        f"🧠 *Memory store stats*\n\n"
        f"🚨 Alerts saved: {s['alerts']}\n"
        f"🔍 Scans saved: {s['scans']}\n"
        f"📌 Seen narratives (24h TTL): {s['seen_narratives']}\n"
        f"📌 Seen tokens (24h TTL): {s['seen_tokens']}\n\n"
        f"Commands: `/alerts [keyword]`, `/history`, `/lookup <CA>`",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=['scan'])
def handle_scan(message):
    bot.reply_to(message, "🔍 Scanning Solana new + trending pools… give me 15–30 seconds.")
    try:
        results = run_scan(limit_results=5)
        bot.send_message(
            message.chat.id,
            format_scan_results(results),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        try:
            memory_store.save_scan(results_count=len(results), top_results=results)
        except Exception as e:
            log.warning(f"save_scan failed: {e}")
    except Exception as e:
        bot.reply_to(message, f"⚠️ Scan failed: {e.__class__.__name__}: {e}")


@bot.message_handler(commands=['reset'])
def handle_reset(message):
    save_history(message.chat.id, [{"role": "system", "content": SYSTEM_PROMPT}])
    bot.reply_to(message, "🧹 Memory wiped. Fresh start.")


# ---------- TEXT HANDLER ----------
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.chat.id
    user_text = (message.text or "").strip()

    # Auto-route: if message is a bare Solana mint address, run rug check
    if is_valid_solana_mint(user_text):
        run_rug_check_and_remember(message, user_text)
        return

    history = ensure_system_prompt(load_history(user_id))
    history.append({"role": "user", "content": user_text})
    try:
        reply = chat_with_tools(history)
    except Exception as e:
        reply = f"⚠️ Error: {e.__class__.__name__}: {str(e)[:300]}"
    history.append({"role": "assistant", "content": reply})
    save_history(user_id, history)
    bot.reply_to(message, reply)


def _groq_call_with_tools(messages, model):
    return client.chat.completions.create(
        model=model,
        messages=messages,
        tools=TOOLS_SCHEMA,
        tool_choice="auto",
        max_tokens=1024,
    )


def chat_with_tools(messages):
    """Groq chat loop with function-calling. Runs tools when Groq requests them."""
    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            response = _groq_call_with_tools(messages, TEXT_MODEL)
        except Exception as e:
            # If primary model botches tool format, retry with fallback model
            if "tool_use_failed" in str(e) or "Failed to call a function" in str(e):
                response = _groq_call_with_tools(messages, TEXT_MODEL_FALLBACK)
            else:
                raise
        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return msg.content or "(empty response)"
        # Record the assistant's tool-call turn
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        })
        # Execute each tool call and append result
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            result = execute_tool(tc.function.name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result[:6000],
            })
    return "I tried searching but ran out of steps. Try rephrasing the question."

# ---------- IMAGE HANDLER ----------
@bot.message_handler(content_types=['photo'])
def handle_image(message):
    user_id = message.chat.id
    caption = message.caption or "What is in this image? Describe it in detail."

    # Download image from Telegram
    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded = bot.download_file(file_info.file_path)
    image_b64 = base64.b64encode(downloaded).decode("utf-8")

    # Send to vision model
    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}"
                        }
                    },
                    {
                        "type": "text",
                        "text": caption
                    }
                ]
            }
        ],
        max_tokens=1024
    )
    reply = response.choices[0].message.content
    bot.reply_to(message, reply)

# ---------- START ----------
print("Bot is running with persistent memory (Redis) and image support...")
# Auto-start position tracker — always watching open positions, no-op if none
try:
    position_tracker.start(_position_alert)
    print(f"Position tracker started. Open positions: {len(position_tracker.list_open())}")
except Exception as e:
    log.warning(f"Position tracker failed to start: {e}")
bot.polling(none_stop=True, interval=0)
