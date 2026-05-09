import telebot
from groq import Groq
import sqlite3
import json
import os
import base64

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TEXT_MODEL = "llama-3.3-70b-versatile"
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
DB_PATH = os.environ.get("DB_PATH", "bot_memory.db")

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
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO conversations (user_id, history)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET history = excluded.history
    ''', (user_id, json.dumps(history)))
    conn.commit()
    conn.close()

# ---------- TEXT HANDLER ----------
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.chat.id
    user_text = message.text
    history = load_history(user_id)
    history.append({"role": "user", "content": user_text})
    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=history,
        max_tokens=1024
    )
    reply = response.choices[0].message.content
    history.append({"role": "assistant", "content": reply})
    save_history(user_id, history)
    bot.reply_to(message, reply)

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