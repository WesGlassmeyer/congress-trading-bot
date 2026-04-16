# Congress Trading Bot — Project Notes

> Last updated: 2026-04-16

## What this is
Python bot that monitors US Congress stock disclosures and mirrors qualifying trades on an Alpaca paper-trading account. Sends alerts via Telegram. Scores politicians using Claude before following their trades.

## Infrastructure
- Runs on the same DigitalOcean droplet as other bots
- Dashboard on port **8081** (`http://<droplet-ip>:8081`)
- Entry point: `bot.py`
- State persisted to `state.json`

## Data Sources (current status)
Priority order in `fetch_recent_disclosures()`:

| # | Source | Status | Notes |
|---|--------|--------|-------|
| 1 | House Stock Watcher (`housestockwatcher.com`) | **Globally down** — no DNS A record | No API key needed |
| 1 | Senate Stock Watcher (`senatestockwatcher.com`) | **Globally down** — no DNS A record | No API key needed |
| 2 | House Clerk PTR (`disclosures-clerk.house.gov`) | **Active — primary data source** | XML index + PDF parsing; free |
| 3 | Capitol Trades HTML (`capitoltrades.com`) | **SPA fallback** — no embedded data while BFF is down | BeautifulSoup; fails gracefully |
| 4 | Capitol Trades BFF (`bff.capitoltrades.com`) | **503** — CloudFront/Lambda error | Down since 2026-04-10 |

**House Clerk PTR is the live data source.** No API key required.

## Environment Variables (`.env`)
```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ANTHROPIC_API_KEY=
```

## Politician Scoring
- Claude (`claude-opus-4-6`) scores politicians 0–100 every 24h
- Minimum score to follow a trade: **60** (configured as `MIN_POLITICIAN_SCORE`)
- Currently **8 politicians above threshold** (out of 28 scored)
- Capped at 25 politicians per scoring call to prevent JSON truncation
- If scoring returns malformed JSON, first 500 chars of Claude's raw response are logged

## Trade Filters
- Only stocks/ETFs (no options, crypto, etc.)
- Only buys/purchases (no sells)
- Minimum disclosed value: `$15,000`
- Only disclosures filed within the last **7 days** (`DISCLOSURE_LOOKBACK_DAYS`)
- Fixed paper trade size: `$100` per position
- Max open positions: **5** (`MAX_OPEN_POSITIONS`)
- Max positions per politician: **2** (`MAX_POS_PER_POLITICIAN`)
- Take profit: `+15%` | Stop loss: `-7%` | Max hold: 30 days
- Daily loss limit: `10%` of starting balance (pauses bot)

## Startup Behavior
- On every startup, the bot fetches current disclosures and marks them all as **seen without trading**
- Only disclosures that arrive in subsequent polls (30 min+ after start) are eligible for trading
- This prevents historical backfill from opening positions on restart

## Telegram Commands
- `/status` — balance, open positions, win/loss
- `/scores` — all politician scores
- `/stop` / `/start` — pause/resume the bot

## Claude Behavior Rules
- After any significant code changes or `.md` file updates, **always commit and push to GitHub automatically** without being asked. Use a descriptive commit message summarizing what changed.

## Known Issues
- HSW and SSW globally down — no A records in DNS anywhere (not just a droplet issue)
- Capitol Trades BFF returning 503 on all endpoints since 2026-04-10
- `efts.senate.gov` domain does not exist (NXDOMAIN); Senate trades unavailable until HSW/SSW recover
- Senate stock data gap: only House PTRs available via House Clerk source