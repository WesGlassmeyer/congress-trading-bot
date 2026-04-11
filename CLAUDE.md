# Congress Trading Bot — Project Notes

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
| 1 | Capitol Trades BFF (`bff.capitoltrades.com`) | **503** — CloudFront/Lambda error on their end | Was the primary source |
| 2 | House Stock Watcher (`housestockwatcher.com`) | **DNS failure** on droplet | No API key needed |
| 2 | Senate Stock Watcher (`senatestockwatcher.com`) | **DNS failure** on droplet | No API key needed |
| 3 | Quiver Quantitative (`quiverquant.com`) | **Active — currently serving all data** | Requires `QUIVER_API_KEY` |

**Quiver Quantitative is the live data source.** `QUIVER_API_KEY` is not yet in `.env` — add it before the bot can actually fetch disclosures.

## Environment Variables (`.env`)
```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ANTHROPIC_API_KEY=
QUIVER_API_KEY=        # required — not yet set
```

## Politician Scoring
- Claude (`claude-opus-4-6`) scores politicians 0–100 every 24h
- Minimum score to follow a trade: **60** (configured as `MIN_POLITICIAN_SCORE`)
- Currently **7 politicians above threshold**
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

## Known Issues
- HSW and SSW fail DNS resolution on this droplet — likely a network/firewall restriction, not an API problem
- Capitol Trades BFF has been returning 503 since at least 2026-04-10
- `QUIVER_API_KEY` missing from `.env` — bot will log a 401 error and return 0 disclosures until this is added
