import telebot
from groq import Groq
import sqlite3
import json
import os
import base64
from rug_check import check_token, format_report, is_valid_solana_mint
from tools import TOOLS_SCHEMA, execute_tool

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TEXT_MODEL = "llama-3.3-70b-versatile"
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
DB_PATH = os.environ.get("DB_PATH", "bot_memory.db")
MAX_HISTORY = 30  # cap to avoid runaway context

SYSTEM_PROMPT = (
    "You are SSHETTY bot — a brutally honest Solana memecoin and crypto trading "
    "assistant for Shashi. You help with rug detection, on-chain analysis, trade "
    "discipline, and meme coin strategy. You have a built-in /check command that "
    "runs mechanical on-chain rug checks (mint authority, freeze authority, "
    "liquidity, age, Rugcheck composite). When the user pastes a Solana CA, that "
    "check auto-runs and the result is in this conversation. Use it as context.\n\n"
    "You have two web tools available via function calling:\n"
    "  - web_search(query): live web search for current info, prices, news, social mentions.\n"
    "  - fetch_url(url): read the full content of a specific URL.\n"
    "Use them whenever the user asks about anything time-sensitive or current. Do not "
    "guess at prices, news, or current events — search first.\n\n"
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

# ---------- DATABASE SETUP ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            user_id INTEGER PRIMARY KEY,
            history TEXT
        )
    ''')
    conn.commit()
    conn.close()

def load_history(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT history FROM conversations WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return []

def save_history(user_id, history):
    # Trim non-system messages to last MAX_HISTORY
    sys_msgs = [m for m in history if m["role"] == "system"]
    other    = [m for m in history if m["role"] != "system"][-MAX_HISTORY:]
    history  = sys_msgs + other
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO conversations (user_id, history)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET history = excluded.history
    ''', (user_id, json.dumps(history)))
    conn.commit()
    conn.close()

def ensure_system_prompt(history):
    if not history or history[0].get("role") != "system":
        history = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    else:
        # Refresh in case prompt evolved
        history[0] = {"role": "system", "content": SYSTEM_PROMPT}
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
        "🛡 `/check <mint>` — explicit form.\n"
        "🧹 `/reset` — wipe conversation memory.\n\n"
        "_Defensive checks only. Not financial advice._",
        parse_mode="Markdown",
    )


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
    reply = chat_with_tools(history)
    history.append({"role": "assistant", "content": reply})
    save_history(user_id, history)
    bot.reply_to(message, reply)


def chat_with_tools(messages):
    """Groq chat loop with function-calling. Runs tools when Groq requests them."""
    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            max_tokens=1024,
        )
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
init_db()
print("Bot is running with persistent memory and image support...")
bot.polling(none_stop=True, interval=0)
