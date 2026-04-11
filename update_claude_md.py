#!/usr/bin/env python3
"""
Auto-update CLAUDE.md with current bot status.
Called by the PostToolUse git-push hook.
"""
import json
import os
import re
from datetime import datetime, timezone

REPO     = os.path.dirname(os.path.abspath(__file__))
STATE_F  = os.path.join(REPO, "state.json")
CLAUDE_F = os.path.join(REPO, "CLAUDE.md")
MIN_SCORE = 60


# ── helpers ───────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_F):
        try:
            with open(STATE_F) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def politician_count(state: dict) -> tuple[int, int]:
    """Return (above_threshold, total_scored)."""
    scores = state.get("politician_scores", {})
    above  = sum(1 for v in scores.values()
                 if isinstance(v, dict) and v.get("score", 0) >= MIN_SCORE)
    return above, len(scores)


def active_source_line(state: dict) -> str:
    """One-line summary of which source last supplied data."""
    positions = state.get("positions", {})
    if positions:
        sources = {p.get("source", "") for p in positions.values() if isinstance(p, dict)}
        if sources:
            return f"Most recent positions opened via: {', '.join(sorted(sources))}."
    return "House Clerk PTR is the active data source (`disclosures-clerk.house.gov`)."


# ── section content ──────────────────────────────────────────────────────────

def data_sources_block() -> str:
    return """\
## Data Sources (current status)
Priority order in `fetch_recent_disclosures()`:

| # | Source | Status | Notes |
|---|--------|--------|-------|
| 1 | House Stock Watcher (`housestockwatcher.com`) | **Globally down** — no DNS A record | No API key needed |
| 1 | Senate Stock Watcher (`senatestockwatcher.com`) | **Globally down** — no DNS A record | No API key needed |
| 2 | House Clerk PTR (`disclosures-clerk.house.gov`) | **Active — primary data source** | XML index + PDF parsing; free |
| 3 | Capitol Trades HTML (`capitoltrades.com`) | **SPA fallback** — no embedded data while BFF is down | BeautifulSoup; fails gracefully |
| 4 | Capitol Trades BFF (`bff.capitoltrades.com`) | **503** — CloudFront/Lambda error | Down since 2026-04-10 |

**House Clerk PTR is the live data source.** No API key required.\
"""


def env_block() -> str:
    return """\
## Environment Variables (`.env`)
```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ANTHROPIC_API_KEY=
```"""


def known_issues_block() -> str:
    return """\
## Known Issues
- HSW and SSW globally down — no A records in DNS anywhere (not just a droplet issue)
- Capitol Trades BFF returning 503 on all endpoints since 2026-04-10
- `efts.senate.gov` domain does not exist (NXDOMAIN); Senate trades unavailable until HSW/SSW recover
- Senate stock data gap: only House PTRs available via House Clerk source\
"""


# ── main ─────────────────────────────────────────────────────────────────────

def update_claude_md():
    state = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    above, total = politician_count(state)
    pol_line = (f"Currently **{above} politician{'s' if above != 1 else ''} above threshold**"
                f" (out of {total} scored)")

    with open(CLAUDE_F) as f:
        text = f.read()

    # 1. Add / update "Last updated" line in the header block
    if "Last updated:" in text:
        text = re.sub(r"Last updated: \d{4}-\d{2}-\d{2}", f"Last updated: {today}", text)
    else:
        # Insert after the first heading line
        text = re.sub(
            r"(# Congress Trading Bot.*?\n)",
            f"\\1\n> Last updated: {today}\n",
            text, count=1,
        )

    # 2. Replace Data Sources section (up to but not including the next ## heading)
    text = re.sub(
        r"## Data Sources \(current status\).*?(?=\n## )",
        data_sources_block() + "\n",
        text,
        flags=re.DOTALL,
    )

    # 3. Replace Environment Variables section (full section up to next ##)
    text = re.sub(
        r"## Environment Variables.*?(?=\n## )",
        env_block() + "\n",
        text,
        flags=re.DOTALL,
    )

    # 4. Update politician threshold count
    text = re.sub(
        r"Currently \*\*\d+ politicians? above threshold[^*]*\*\*[^\n]*",
        pol_line,
        text,
    )

    # 5. Replace Known Issues section (to end of file or next ##)
    if "## Known Issues" in text:
        text = re.sub(
            r"## Known Issues.*$",
            known_issues_block(),
            text,
            flags=re.DOTALL,
        )
    else:
        text += "\n\n" + known_issues_block() + "\n"

    with open(CLAUDE_F, "w") as f:
        f.write(text)

    print(f"CLAUDE.md updated: {today} | {above}/{total} politicians above threshold")


if __name__ == "__main__":
    update_claude_md()
