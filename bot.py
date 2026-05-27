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
    """Send alert to owner."""
    if not OWNER_TELEGRAM_ID:
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
bot.polling(none_stop=True, interval=0)
