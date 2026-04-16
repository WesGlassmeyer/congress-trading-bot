# Congress Trading Bot — Project Notes

> Last updated: 2026-04-16 (session 2)

## What this is
Python bot that monitors US Congress stock disclosures and mirrors qualifying trades on an Alpaca paper-trading account. Sends alerts via Telegram. Scores politicians using Claude before following their trades.

## Infrastructure
- Runs on the same DigitalOcean droplet as other bots
- Dashboard on port **8081** (`http://<droplet-ip>:8081`)
- Entry point: `bot.py`
- State persisted to `state.json` (gitignored — live state only)
- Scoring runs in a **separate subprocess** via `score.py` to isolate Claude API memory spike

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
- Scoring runs in `score.py` as a **subprocess** of `bot.py` — child exits after scoring, freeing memory
- `bot.py` calls `_run_scorer()` which launches the subprocess then reloads scores from `state.json`
- Minimum score to follow a trade: **60** (configured as `MIN_POLITICIAN_SCORE`)
- Recess mode threshold: **50** when fewer than 5 fresh PTRs are available
- Currently **8 politicians above threshold** (out of 28 scored)
- Capped at 20 politicians per scoring call
- Prior scores logged before each API call to verify correct values reach the prompt
- **Python-side floor clamp**: after Claude responds, `final_score = max(returned_score, prior - 15)` — enforced unconditionally; logs a warning when it fires
- If scoring returns malformed JSON, first 500 chars of Claude's raw response are logged

### Score anchoring history
- 2026-04-16: Bad scoring run drove all scores to 20–35 (Claude ignored prompt anchoring)
- Fix: stronger prompt mandate + Python clamp added; April 11 scores manually restored:
  Mullin 80, Hickenlooper 78, Hern 76, Gottheimer 74, Tuberville 68,
  Jackson 66, Cisneros 65, King 60, Moore 58, Boozman 56

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