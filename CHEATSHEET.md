# 📚 SSHETTY Bot — Cheat Sheet

Bot: **@SSHETTY_bot** on Telegram

---

## 🛡 RUG CHECK (vet before buying)

| Command | What |
|---|---|
| Paste a CA | Auto rug-check |
| `/check <CA>` | Same, explicit |
| `/scan` | Find 5 candidates the bot likes right now |

---

## 📡 SIGNAL (scored lean + tracked accuracy)

| Command | What |
|---|---|
| `/signal <CA>` | 0-100 score → BULLISH / NEUTRAL / BEARISH, with every point explained |
| `/signal stats` | YOUR real hit rate per lean (the number paid tools hide) |
| 📸 Photo + caption "chart" | Reads the token *identity* off the image, then scores it |
| `/backtest <CA>` | Instant grade — replays one coin's past price, no 6h wait |
| `/backtest sweep` | Same across several trending coins for a bigger sample |

**Backtest = a quick smell test, not proof.** It only replays past *price/volume* (the part free APIs allow), so it tests the **momentum half** of the score — NOT the rug-safety half. Below 55% = no edge. A good number here does NOT mean the full `/signal` works; only the live 6h tracker proves that.

**Read it honestly:** the score is on-chain + flow data, not a crystal ball. Until ~20 signals of a lean resolve, confidence shows **UNKNOWN**. Below 55% hit rate = no edge. It reads the token's *identity* from a chart photo — it never reads the candles to "predict." No order placement (rule #2).

---

## 📥 OPEN & MANAGE TRADES

| Command | What |
|---|---|
| `/buy <CA>` | Track a trade (15% of capital default) |
| `/buy <CA> 5` | Track with $5 size |
| `/buy <CA> 5 0.0001` | Track with $5 size + manual entry price |
| `/sell <CA>` | Close + show P&L |
| `/positions` | Live status of all open trades |
| `/closed` | Last 20 closed trades with P&L |
| 📸 Photo + caption "buy" | Vision parses Bitget screenshot → opens trade |
| 📸 Photo + caption "sell" | Same → closes trade |

---

## 🚨 DEV SELL TRACKER (auto-starts)

| Command | What |
|---|---|
| `/devfeed on` / `off` / `status` | Watch top 50 pump.fun coins every 3min — alerts when token *creator* sells |

**When you get a dev sell alert:** The dev is exiting. Price often pumps briefly on FOMO, then dumps hard. If you're holding → consider exiting before the bounce, not after. Advisory only — some devs sell partially, not a full rug.

---

## 👁 BACKGROUND SCANNERS (24/7)

| Command | What |
|---|---|
| `/watcher on` / `off` / `status` | Pings when 3+ pump.fun tokens form a narrative |
| `/swfeed on` / `off` / `status` | Pings when 2+ smart wallets buy same fresh CA |

---

## 😴 SLEEP & SANITY

| Command | What |
|---|---|
| `/sleep on` | Mute watcher + swfeed (TP/SL still fire) |
| `/sleep off` | Wake-up summary of what you missed |
| `/sleep status` | How long you've been asleep |

---

## 📊 LOOK BACK / LEARN

| Command | What |
|---|---|
| `/stats` | Win rate, total P&L, best/worst |
| `/stats positions` | All closed trades listed |
| `/stats watcher` | Watcher alert breakdown |
| `/stats narratives` | Which narratives made you money |
| `/losses` | Real losses vs wicks (Fib + volume) |
| `/alerts` | Last 20 watcher pings |
| `/alerts goblin` | Search past alerts by keyword |
| `/history` | Last 20 things you rug-checked |
| `/lookup <CA>` | Everything bot remembers about a CA |

---

## 💰 SETTINGS

| Command | What |
|---|---|
| `/capital` | Show current capital |
| `/capital 50` | Update capital |

---

## 🐋 SMART WALLET MANAGEMENT

| Command | What |
|---|---|
| `/listwallets` | Show wallets (paginated 50/page) |
| `/listwallets 2` | Page 2 |
| `/addwallet <addr> <label>` | Manually add one |
| `/removewallet <addr>` | Remove one |
| `/discoverwallet` | Auto-find new ones (3-8 min) |

---

## 🧠 PERSONAL MEMORY (AI brain)

| Command | What |
|---|---|
| `/remember <fact>` | Permanent rule, applied to every chat |
| `/memories` | List your saved rules |
| `/forget <fact>` | Delete one |
| `/reset` | Wipe chat history (memories stay) |

---

## 🩺 DIAGNOSTICS

| Command | What |
|---|---|
| `/memstats` | How much data is stored |
| `/redisping` | Confirm Redis is alive |

---

## 🎯 THE FOUR THINGS THAT MATTER

### 1. The bot doesn't trade for you. You trade. It informs and tracks.
- It tells you what looks safe (rug check)
- It tells you how much to risk (trade card)
- It pings you when smart money moves or narratives form
- You execute on Bitget. Period.

### 2. Three brains running simultaneously:
- **Watcher** — scans pump.fun for hype (narratives)
- **Smart wallet feed** — scans 204 wallets for convergence (smart money)
- **Position tracker** — watches YOUR open trades for TP/SL

### 3. All your data is in Upstash Redis.
- Wallets, positions, alerts, history, memories — everything
- Survives Railway redeploys, restarts, crashes
- If Redis breaks, nothing works → run `/redisping` to check

### 4. The trade card is your discipline:
- Always 15% of capital on GREEN, $5 on YELLOW
- Always TP1 = 2x sell half, TP2 = 3x sell rest, SL = -30%
- Your edge is exit discipline. The bot enforces it. Don't override.

---

## 🚨 RED FLAGS — IF YOU SEE THESE, ACT

| Symptom | What it means | Action |
|---|---|---|
| `/redisping` shows ❌ | Redis broken, data not persisting | Check `REDIS_URL` on Railway |
| `/swfeed status` shows ⛔ Stopped | Background feed died | `/swfeed on` |
| `/watcher status` shows ⛔ Stopped | Narrative scanner off | `/watcher on` |
| No alerts for 4+ hours in active market | Something silently broke | Restart both feeds |
| Bot doesn't reply at all | Railway is down or trial expired | Check Railway dashboard |

---

## 🗓 SHORT-TERM TO-DOS

1. **In ~11 days:** Railway trial dies. Decide: $5/mo Hobby plan, or migrate to Render/Fly.io
2. **After 10-20 trades:** `/stats narratives` will show patterns. Use this to filter alerts.
3. **If you find a narrative type that consistently loses:** `/remember don't act on [pattern]`

---

## 🔑 IMPORTANT STUFF TO KEEP SOMEWHERE SAFE

- **Bot URL:** https://t.me/SSHETTY_bot
- **Repo:** https://github.com/Analyticshetty/Telegram-bot
- **Railway project:** heartfelt-quietude → worker service
- **Upstash dashboard:** https://console.upstash.com
- **REDIS_URL (env var on Railway):** must start with `rediss://` — back it up in a password manager

---

## 🧠 HOW THE BOT WORKS (one paragraph)

The bot runs 24/7 on Railway. Three background threads scan continuously: **watcher** looks for new narratives forming on pump.fun, **smart wallet feed** looks for 2+ tracked wallets buying the same fresh token, **position tracker** watches your open trades. Anything you do (paste a CA, /scan, /buy) saves to Upstash Redis so it persists across restarts. The chat AI (Groq Llama-4) talks to you with full memory of your last 30 messages and any permanent /remember facts. All data is in Redis except the bot code itself (in GitHub). Costs $0/month until Railway trial ends.

---

## 🤝 RULES YOU SHOULD ENFORCE WITH ME (next session)

1. Stop me if I'm being a yes-man — make me push back, explain trade-offs, recommend AGAINST things
2. Make me explain WHY before I build, not just HOW
3. Ask for TLDR if I write too much
4. Push back on my recommendations — they're not gospel
5. Make me update this file at end of each session

---

*Saved to: `TelegramBot/CHEATSHEET.md` in your repo. View anytime locally or on GitHub.*
