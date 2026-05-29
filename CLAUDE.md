# SSHETTY Bot — Project Briefing

**Read this first every session. Full detail in `../Telegram.md`.**

## Who & What
- User: **Shashi** (Mumbai, IST) — Solana memecoin trader, Bitget app
- Bot: `@SSHETTY_bot` on Telegram, deployed on Railway (`heartfelt-quietude` → `worker`)
- Repo: `github.com/Analyticshetty/Telegram-bot`
- Redis: Upstash (`REDIS_URL` must start `rediss://`)
- Running cost: **$0/month**

## Capital
Ask Shashi to confirm at session start — stored in Redis `state:capital_usd`. Last known: **$12** (topping to $100–200 in next couple of days per user).
Capital history: $30 → $150 → $10 → $25 → $36 → $12. Pattern: sleep-deprived dopamine trading + concentration into thin-liquidity tokens (grail loss May 29–30 was a $44K-pool liquidity slippage event, NOT a flinch). Hold the line.

## What's Built & Live
| Module | Command | Status |
|---|---|---|
| 9-engine rug check | `/check <CA>` or paste CA | ✅ |
| Position tracker (TP1/TP2/SL) | `/buy` `/sell` `/positions` | ✅ |
| Watcher (narrative alerts) | `/watcher on/off` | ✅ **auto-restarts on crash** |
| Smart wallet feed (204 wallets) | `/swfeed status` | ✅ auto-starts |
| Smart wallet signal (2+ wallets, any time/age) | automatic | ✅ |
| Dev sell tracker | `/devfeed status` | ✅ auto-starts |
| Signal scoring + accuracy tracker | `/signal <CA>`, `/signal stats` | ✅ |
| Momentum backtest | `/backtest <CA>`, `/backtest sweep` | ✅ |
| Sleep mode | `/sleep on/off` | ✅ |
| Stats, losses, history | `/stats` `/losses` `/history` | ✅ |
| Trade import (Bitget screenshot) | Photo + caption "buy/sell" (slash-prefixed OK) | ✅ — both **order screens** and **holdings/asset-detail screens** (supply-based cost basis, no FX, time-independent) |
| Chart photo → signal | Photo + caption "chart" | ✅ |
| Dead-man's switch | healthchecks.io | ✅ Telegram + email alerts |
| Daily 9am IST P&L summary | automatic | ✅ |

## Non-Negotiable Rules
1. **Brutal honesty. No sugarcoating.**
2. **NEVER wire trade execution** — 12 risk rules in `Moondev/Opus_trading text.md` still unconfirmed by Shashi.
3. **NEVER recommend RED tokens.**
4. **NEVER recommend leverage >3x.**
5. **$0/month** — do not add paid services without explicit approval.
6. **No GMGN API** — Cloudflare blocks Railway.
7. **Don't suggest rebuilding** anything working.
8. **Don't suggest Ollama/local LLMs** — 8GB RAM laptop, will fail.
9. **Don't be a yes-man** — push back, educate, disagree when warranted.
10. Shashi's real edge: **exit discipline** (TP at 2x). Don't undersell it.

## Session Checklist
- [ ] Confirm capital (`/capital` or ask)
- [ ] `/swfeed status` and `/devfeed status` — both should be running
- [ ] Watcher auto-restarts now — confirm with `/watcher status`
- [ ] Check healthchecks.io is green (bot alive)
- [ ] Ask what Shashi wants to work on

## Key APIs (all free)
Groq · Tavily · Helius · DEXScreener · GoPlus · Rugcheck · GeckoTerminal · Upstash Redis · Firecrawl (backup scrape)

## DO NOT USE
- GMGN (Cloudflare blocked)
- Any paid API without approval
- Ollama / local models
