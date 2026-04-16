#!/usr/bin/env python3
"""
Congress Trading Bot
Paper-trades by mirroring US Congress member stock disclosures.
Dashboard: http://0.0.0.0:8081 | Telegram alerts | Claude-powered politician scoring
"""

import io
import os
import json
import re
import time
import socket
import logging
import subprocess
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import dns.resolver
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from anthropic import Anthropic
from dotenv import load_dotenv
from flask import Flask, render_template_string

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
#  DNS PATCH — route specific hostnames through Google DNS (8.8.8.8)
#  The DigitalOcean droplet's default resolver fails to resolve these domains.
# ══════════════════════════════════════════════════════════════════════════════
_GOOGLE_DNS_HOSTS = {"housestockwatcher.com", "senatestockwatcher.com", "www.ethics.senate.gov"}
_google_resolver  = dns.resolver.Resolver(configure=False)
_google_resolver.nameservers = ["8.8.8.8", "8.8.4.4"]
_orig_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host in _GOOGLE_DNS_HOSTS:
        try:
            answers = _google_resolver.resolve(host, "A")
            ip = str(answers[0])
            return _orig_getaddrinfo(ip, port, family, type, proto, flags)
        except Exception:
            pass  # fall through to system resolver
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _patched_getaddrinfo

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════
PAPER_TRADE_SIZE        = 100        # Fixed $ per paper trade
PAPER_STARTING_BALANCE  = 1000       # Starting paper balance ($)
DAILY_LOSS_LIMIT_PCT    = 0.10       # Pause if balance drops 10% in a day
MIN_POLITICIAN_SCORE    = 60         # Min Claude score (0–100) to follow
RECESS_THRESHOLD        = 50         # Lower threshold when < 5 fresh PTRs available
SCORE_REFRESH_HOURS     = 24         # Hours between politician re-scores
DISCLOSURE_POLL_MINUTES = 30         # Minutes between disclosure polls
MIN_TRADE_VALUE         = 15_000     # Min disclosed $ value to act on
DASHBOARD_PORT          = 8081
TAKE_PROFIT_PCT         = 0.15       # Close at +15%
STOP_LOSS_PCT           = 0.07       # Close at -7%
MAX_HOLD_DAYS           = 30         # Auto-close after 30 days
POSITION_CHECK_MINUTES  = 5          # How often to check open positions
MAX_OPEN_POSITIONS      = 5          # Never hold more than this many at once
MAX_POS_PER_POLITICIAN  = 2          # Max concurrent positions per politician
DISCLOSURE_LOOKBACK_DAYS = 7         # Ignore disclosures filed more than N days ago

# ══════════════════════════════════════════════════════════════════════════════
#  CREDENTIALS
# ══════════════════════════════════════════════════════════════════════════════
ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("congress_bot")

# ══════════════════════════════════════════════════════════════════════════════
#  CLIENTS
# ══════════════════════════════════════════════════════════════════════════════
_ai  = Anthropic(api_key=ANTHROPIC_KEY)
app  = Flask(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════════════════════
STATE_FILE = "state.json"
_lock      = threading.Lock()


def _blank_state() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "balance":             PAPER_STARTING_BALANCE,
        "daily_start_balance": PAPER_STARTING_BALANCE,
        "daily_reset_date":    now.date().isoformat(),
        "positions":           {},   # ticker → position dict
        "closed_trades":       [],   # historical closed trades
        "politician_scores":   {},   # name → score_dict
        "seen_trade_ids":      [],   # dedup Capitol Trades IDs (capped at 2000)
        "paused":              False,
        "last_score_refresh":  None,
        "active_threshold":    MIN_POLITICIAN_SCORE,
        "stats": {
            "total_trades": 0,
            "wins":         0,
            "losses":       0,
            "total_pnl":    0.0,
        },
    }


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            base = _blank_state()
            base.update(saved)
            # Ensure nested stats dict is complete
            for k, v in _blank_state()["stats"].items():
                base["stats"].setdefault(k, v)
            return base
        except Exception as e:
            log.error(f"State load error: {e} — using defaults")
    return _blank_state()


def save_state():
    """Must be called while holding _lock."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


state = load_state()


def _check_daily_reset():
    """Roll over daily tracking at midnight UTC. Must be called with _lock held."""
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("daily_reset_date") != today:
        state["daily_start_balance"] = state["balance"]
        state["daily_reset_date"]    = today
        save_state()


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════
_TG_BASE = f"https://api.telegram.org/bot{TG_TOKEN}"


def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"{_TG_BASE}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


def tg_poll_commands():
    """Daemon thread: long-poll Telegram for /stop /start /scores."""
    offset = 0
    while True:
        try:
            r = requests.get(
                f"{_TG_BASE}/getUpdates",
                params={"timeout": 30, "offset": offset},
                timeout=40,
            )
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg    = upd.get("message", {})
                text   = msg.get("text", "").strip().lower()
                cid    = str(msg.get("chat", {}).get("id", ""))
                if cid != str(TG_CHAT_ID):
                    continue
                if text == "/stop":
                    with _lock:
                        state["paused"] = True
                        save_state()
                    tg_send("⏸ Bot <b>paused</b>. No new positions will open.")
                    log.info("Bot paused via Telegram")
                elif text == "/start":
                    with _lock:
                        state["paused"] = False
                        save_state()
                    tg_send("▶️ Bot <b>resumed</b>. Watching for signals.")
                    log.info("Bot resumed via Telegram")
                elif text == "/scores":
                    _tg_send_scores()
                elif text == "/status":
                    _tg_send_status()
        except Exception as e:
            log.warning(f"Telegram poll error: {e}")
            time.sleep(5)


def _tg_send_scores():
    with _lock:
        scores = dict(state.get("politician_scores", {}))
    if not scores:
        tg_send("No politician scores yet. Scoring runs every 24h.")
        return
    lines = ["<b>Politician Scores</b>"]
    for name, info in sorted(scores.items(), key=lambda x: -x[1].get("score", 0)):
        score = info.get("score", 0)
        threshold = state.get("active_threshold", MIN_POLITICIAN_SCORE)
        flag  = "✅" if score >= threshold else "❌"
        lines.append(f"{flag} {name}: <b>{score}</b>/100")
    tg_send("\n".join(lines))


def _tg_send_status():
    with _lock:
        bal      = state["balance"]
        paused   = state["paused"]
        open_ct  = len(state["positions"])
        stats    = state["stats"]
        dstart   = state["daily_start_balance"]
    day_pnl = bal - dstart
    wins    = stats["wins"]
    losses  = stats["losses"]
    wr      = wins / (wins + losses) * 100 if (wins + losses) else 0
    status  = "⏸ PAUSED" if paused else "▶️ ACTIVE"
    tg_send(
        f"<b>Bot Status: {status}</b>\n"
        f"Balance: ${bal:.2f} (day {day_pnl:+.2f})\n"
        f"Open positions: {open_ct}\n"
        f"W/L: {wins}/{losses} ({wr:.0f}%)"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ALPACA
# ══════════════════════════════════════════════════════════════════════════════
_ALPACA_HDRS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}
_ALPACA_DATA = "https://data.alpaca.markets"


def _alpaca_get(path: str, base: str | None = None) -> dict:
    b = base or ALPACA_BASE
    r = requests.get(f"{b}{path}", headers=_ALPACA_HDRS, timeout=15)
    r.raise_for_status()
    return r.json()


def _alpaca_post(path: str, body: dict) -> dict:
    r = requests.post(f"{ALPACA_BASE}{path}", headers=_ALPACA_HDRS, json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def _alpaca_delete(path: str):
    r = requests.delete(f"{ALPACA_BASE}{path}", headers=_ALPACA_HDRS, timeout=15)
    # 200 or 204 both mean success
    if r.status_code not in (200, 204):
        r.raise_for_status()


def get_stock_price(ticker: str) -> float | None:
    """Latest mid-price via Alpaca IEX feed. Falls back to last bar close."""
    try:
        data  = _alpaca_get(f"/v2/stocks/{ticker}/quotes/latest?feed=iex", base=_ALPACA_DATA)
        quote = data.get("quote", {})
        ask   = quote.get("ap", 0) or 0
        bid   = quote.get("bp", 0) or 0
        if ask > 0 and bid > 0:
            return (ask + bid) / 2
        if ask > 0:
            return ask
        if bid > 0:
            return bid
    except Exception:
        pass
    # Fallback: latest bar
    try:
        data = _alpaca_get(f"/v2/stocks/{ticker}/bars/latest?feed=iex", base=_ALPACA_DATA)
        bar  = data.get("bar", {})
        return bar.get("c") or None  # close price
    except Exception as e:
        log.warning(f"Price fetch failed for {ticker}: {e}")
    return None


def place_paper_order(ticker: str, notional: float, side: str) -> dict | None:
    """Place a fractional notional market order on Alpaca paper account."""
    try:
        return _alpaca_post("/v2/orders", {
            "symbol":        ticker,
            "notional":      str(round(notional, 2)),
            "side":          side,
            "type":          "market",
            "time_in_force": "day",
        })
    except Exception as e:
        log.error(f"Alpaca order failed {ticker} {side}: {e}")
        return None


def close_alpaca_position(ticker: str):
    """Close a position on Alpaca paper account (best-effort)."""
    try:
        _alpaca_delete(f"/v2/positions/{ticker}")
    except Exception as e:
        log.warning(f"Alpaca close failed {ticker}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  DISCLOSURE SOURCES  (tried in order; first group with data wins)
#
#  Group 1 — free JSON APIs (House + Senate)
#    HSW   https://housestockwatcher.com/api
#    SSW   https://senatestockwatcher.com/api
#
#  Group 2 — official government sources
#    House Clerk  https://disclosures-clerk.house.gov  (PTR XML index + PDFs)
#
#  Group 3 — Capitol Trades (fallbacks)
#    HTML scrape  https://www.capitoltrades.com/trades   (SPA — data via BFF)
#    BFF JSON     https://bff.capitoltrades.com/trades   (503 as of 2026-04-10)
# ══════════════════════════════════════════════════════════════════════════════
_HSW_API    = "https://housestockwatcher.com/api"
_SSW_API    = "https://senatestockwatcher.com/api"

_HOUSE_CLERK_BASE = "https://disclosures-clerk.house.gov"
_HOUSE_CLERK_HDR  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "text/html,application/xhtml+xml,application/xml,*/*",
    "Referer":    "https://disclosures-clerk.house.gov/FinancialDisclosure",
}
_HOUSE_PTR_PDF_MAX = 30   # max PDFs to download per poll

_CT_HTML_URL = "https://www.capitoltrades.com/trades"
_CT_BFF_API  = "https://bff.capitoltrades.com/trades"
_CT_BFF_HDR  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Origin":     "https://www.capitoltrades.com",
    "Referer":    "https://www.capitoltrades.com/trades",
}


def _fetch_from_capitoltrades(pages: int = 2) -> list[dict]:
    """Fetch from the Capitol Trades BFF JSON API. Returns raw items or raises."""
    trades = []
    for page in range(1, pages + 1):
        r = requests.get(
            _CT_BFF_API,
            headers=_CT_BFF_HDR,
            params={"page": page, "pageSize": 100},
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Capitol Trades BFF HTTP {r.status_code} on page {page}")
        data  = r.json()
        batch = data.get("data", [])
        trades.extend(batch)
        log.debug(f"Capitol Trades BFF page {page}: {len(batch)} trades")
        meta       = data.get("meta", {})
        pag        = meta.get("pagination", {})
        page_count = pag.get("pageCount", pag.get("totalPages", 1))
        if page >= page_count:
            break
    return trades


def _fetch_from_hsw() -> list[dict]:
    """Fetch from House Stock Watcher. Returns normalised items or raises.

    Response: flat JSON array of House-only disclosures.
    Fields of interest: representative, ticker, type, amount, transaction_date,
                        disclosure_date, asset_description, asset_type.
    """
    r = requests.get(_HSW_API, timeout=30)
    r.raise_for_status()
    raw_list = r.json()
    if not isinstance(raw_list, list):
        raise RuntimeError(f"HSW unexpected response shape: {type(raw_list)}")
    return [_normalise_hsw(item) for item in raw_list if _hsw_has_ticker(item)]


def _hsw_has_ticker(raw: dict) -> bool:
    ticker = (raw.get("ticker") or "").strip()
    return bool(ticker) and ticker != "--"


def _normalise_hsw(raw: dict) -> dict:
    """Map a House Stock Watcher record to our internal schema."""
    name   = (raw.get("representative") or "").strip()
    ticker = (raw.get("ticker") or "").upper().strip()

    tx_raw  = (raw.get("type") or "").lower()
    tx_type = "buy" if "purchase" in tx_raw else ("sell" if "sale" in tx_raw else tx_raw)

    # amount is a range string like "$1,001 - $15,000"
    tx_value = 0.0
    rng  = raw.get("amount") or ""
    nums = re.findall(r"[\d,]+", rng.replace("$", ""))
    if nums:
        vals     = [int(n.replace(",", "")) for n in nums]
        tx_value = sum(vals) / len(vals)

    tx_date    = raw.get("transaction_date") or ""
    filed_date = raw.get("disclosure_date")  or tx_date
    asset_type = (raw.get("asset_type") or "stock").lower()
    trade_id   = f"hsw_{name}_{ticker}_{tx_date}"

    return {
        "id":         trade_id,
        "politician": name,
        "ticker":     ticker,
        "asset_type": asset_type,
        "tx_type":    tx_type,
        "tx_value":   tx_value,
        "filed_date": filed_date,
        "tx_date":    tx_date,
        "_source":    "hsw",
    }


def _fetch_from_ssw() -> list[dict]:
    """Fetch from Senate Stock Watcher. Returns normalised items or raises.

    Response: flat JSON array of senator objects, each with a nested
    `transactions` list. Fields: first_name, last_name, date_recieved (sic),
    transactions[].ticker, .type, .amount, .transaction_date, .asset_type.
    """
    r = requests.get(_SSW_API, timeout=30)
    r.raise_for_status()
    raw_list = r.json()
    if not isinstance(raw_list, list):
        raise RuntimeError(f"SSW unexpected response shape: {type(raw_list)}")
    trades = []
    for senator in raw_list:
        first      = (senator.get("first_name") or "").strip()
        last       = (senator.get("last_name")  or "").strip()
        name       = f"{first} {last}".strip()
        filed_date = senator.get("date_recieved") or ""   # field has typo in source
        for txn in senator.get("transactions") or []:
            ticker = (txn.get("ticker") or "").upper().strip()
            if not ticker or ticker == "--":
                continue
            trades.append(_normalise_ssw(txn, name, filed_date))
    return trades


def _normalise_ssw(txn: dict, senator_name: str, filed_date: str) -> dict:
    """Map one Senate Stock Watcher transaction to our internal schema."""
    ticker = (txn.get("ticker") or "").upper().strip()

    tx_raw  = (txn.get("type") or "").lower()
    tx_type = "buy" if "purchase" in tx_raw else ("sell" if "sale" in tx_raw else tx_raw)

    # amount is a range string like "$1,001 - $15,000"
    tx_value = 0.0
    rng  = txn.get("amount") or ""
    nums = re.findall(r"[\d,]+", rng.replace("$", ""))
    if nums:
        vals     = [int(n.replace(",", "")) for n in nums]
        tx_value = sum(vals) / len(vals)

    tx_date    = txn.get("transaction_date") or ""
    asset_type = (txn.get("asset_type") or "stock").lower()
    trade_id   = f"ssw_{senator_name}_{ticker}_{tx_date}"

    return {
        "id":         trade_id,
        "politician": senator_name,
        "ticker":     ticker,
        "asset_type": asset_type,
        "tx_type":    tx_type,
        "tx_value":   tx_value,
        "filed_date": filed_date,
        "tx_date":    tx_date,
        "_source":    "ssw",
    }


def _parse_house_ptr_pdf(pdf_bytes: bytes, member_name: str, filed_date: str) -> list[dict]:
    """Extract individual stock transactions from a House PTR PDF.

    House PTR PDFs are text-based (not scanned). pypdf extracts the text; we
    regex-parse the repeating pattern:
        (TICKER)
        [asset_type]
        [SP] P|S  MM/DD/YYYYmm/dd/yyyy  $lo - $hi
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text   = "\n".join(page.extract_text() or "" for page in reader.pages)

    # Two consecutive MM/DD/YYYY dates appear butted against each other because
    # the "Notification Date" column immediately follows "Date" in the PDF layout.
    tx_re = re.compile(
        r"\(([A-Z]{1,5})\)"                         # (TICKER)
        r"[^\n]*\n"                                  # rest of asset name line
        r"\[[A-Z]+\]\s*\n"                           # [ST] asset type line
        r"(?:SP\s+)?"                                # optional "SP " (spouse row)
        r"([PS])\s+"                                 # P=purchase  S=sale
        r"(\d{2}/\d{2}/\d{4})"                      # transaction date
        r"(\d{2}/\d{2}/\d{4})"                      # notification date (butted)
        r"\s*(\$[\d,]+\s*-\s*\$[\d,]+)",            # amount range
    )

    trades = []
    seen   = set()
    for m in tx_re.finditer(text):
        ticker      = m.group(1).upper()
        tx_type     = "buy" if m.group(2) == "P" else "sell"
        tx_date_raw = m.group(3)   # MM/DD/YYYY
        amount_str  = m.group(5)

        try:
            tx_date = datetime.strptime(tx_date_raw, "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            tx_date = tx_date_raw

        nums     = re.findall(r"[\d,]+", amount_str.replace("$", ""))
        tx_value = sum(int(n.replace(",", "")) for n in nums) / max(len(nums), 1) if nums else 0.0

        trade_id = f"house_{member_name}_{ticker}_{tx_date}"
        if trade_id in seen:          # dedup SP (spouse) duplicate rows
            continue
        seen.add(trade_id)

        trades.append({
            "id":         trade_id,
            "politician": member_name,
            "ticker":     ticker,
            "asset_type": "stock",
            "tx_type":    tx_type,
            "tx_value":   tx_value,
            "filed_date": filed_date,
            "tx_date":    tx_date,
            "_source":    "house_clerk",
        })
    return trades


def _fetch_from_house_clerk() -> list[dict]:
    """Fetch House Periodic Transaction Reports from the official House Clerk site.

    Steps:
      1. Download the year's PTR index XML  (financial-pdfs/{year}FD.xml)
      2. Filter to filings within DISCLOSURE_LOOKBACK_DAYS
      3. Download each PTR PDF and parse with _parse_house_ptr_pdf()

    Returns normalised trades or raises on index download failure.
    """
    year      = datetime.now(timezone.utc).year
    index_url = f"{_HOUSE_CLERK_BASE}/public_disc/financial-pdfs/{year}FD.xml"
    r         = requests.get(index_url, headers=_HOUSE_CLERK_HDR, timeout=30)
    r.raise_for_status()

    root   = ET.fromstring(r.content.decode("utf-8-sig"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=DISCLOSURE_LOOKBACK_DAYS)

    # Collect PTR entries filed within the lookback window
    recent_ptrs = []
    for member in root.findall("Member"):
        if member.findtext("FilingType") != "P":
            continue
        date_str = member.findtext("FilingDate") or ""
        try:
            filed_dt = datetime.strptime(date_str, "%m/%d/%Y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if filed_dt < cutoff:
            continue
        first  = (member.findtext("First") or "").strip()
        last   = (member.findtext("Last")  or "").strip()
        name   = f"{first} {last}".strip()
        doc_id = member.findtext("DocID") or ""
        recent_ptrs.append((name, doc_id, filed_dt.strftime("%Y-%m-%d")))

    if not recent_ptrs:
        log.info("House Clerk PTR: no filings within lookback window")
        return []

    # Cap to avoid downloading too many PDFs in one poll
    recent_ptrs = recent_ptrs[:_HOUSE_PTR_PDF_MAX]
    log.info(f"House Clerk PTR: {len(recent_ptrs)} PTR(s) within lookback window — downloading PDFs")

    trades = []
    for name, doc_id, filed_iso in recent_ptrs:
        pdf_url = f"{_HOUSE_CLERK_BASE}/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
        try:
            pdf_r = requests.get(pdf_url, headers=_HOUSE_CLERK_HDR, timeout=20)
            pdf_r.raise_for_status()
            parsed = _parse_house_ptr_pdf(pdf_r.content, name, filed_iso)
            log.debug(f"  {name} (DocID={doc_id}): {len(parsed)} trade(s)")
            trades.extend(parsed)
        except Exception as e:
            log.debug(f"  House PTR {doc_id} ({name}) failed: {e}")

    log.info(f"House Clerk PTR: {len(trades)} trade(s) extracted from {len(recent_ptrs)} PTR(s)")
    return trades


def _fetch_from_capitoltrades_html() -> list[dict]:
    """Attempt to scrape trade data from the Capitol Trades HTML page.

    NOTE: capitoltrades.com is a Next.js SPA — trade data is fetched
    client-side from bff.capitoltrades.com. When the BFF is down, the static
    HTML contains only page shell (no trade rows). This function will raise
    RuntimeError in that case and the caller falls through to the next source.
    """
    hdrs = {**_CT_BFF_HDR, "Accept": "text/html,application/xhtml+xml"}
    r    = requests.get(_CT_HTML_URL, headers=hdrs, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")

    # Next.js app-router pages don't embed __NEXT_DATA__; look for a trades table.
    table = soup.find("table")
    if not table:
        raise RuntimeError(
            "Capitol Trades HTML: no <table> found — page is a client-side SPA "
            "and BFF (bff.capitoltrades.com) appears to be down"
        )

    trades = []
    rows   = table.find_all("tr")[1:]   # skip header row
    for row in rows:
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cells) < 6:
            continue
        # Expected columns when table is present:
        # politician | ticker | asset | tx_type | date | amount
        politician = cells[0]
        ticker     = cells[1].upper().strip()
        tx_type    = cells[3].lower()
        tx_type    = "buy" if "purchase" in tx_type or "buy" in tx_type else ("sell" if "sale" in tx_type else tx_type)
        date_str   = cells[4]
        amount_str = cells[5]

        nums     = re.findall(r"[\d,]+", amount_str.replace("$", ""))
        tx_value = sum(int(n.replace(",", "")) for n in nums) / max(len(nums), 1) if nums else 0.0

        try:
            filed_date = datetime.strptime(date_str, "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            filed_date = date_str

        if not ticker or not politician:
            continue
        trades.append({
            "id":         f"ct_html_{politician}_{ticker}_{filed_date}",
            "politician": politician,
            "ticker":     ticker,
            "asset_type": "stock",
            "tx_type":    tx_type,
            "tx_value":   tx_value,
            "filed_date": filed_date,
            "tx_date":    filed_date,
            "_source":    "ct_html",
        })

    if not trades:
        raise RuntimeError("Capitol Trades HTML: table found but no parseable rows")
    return trades


def fetch_recent_disclosures(pages: int = 2) -> list[dict]:
    """Fetch recent disclosures. Sources tried in order; first group with data wins.

    1. House Stock Watcher + Senate Stock Watcher (primary, free JSON APIs)
    2. House Clerk PTR (official government source — XML index + PDF parsing)
    3. Capitol Trades HTML scrape (SPA; only works if BFF renders data server-side)
    4. Capitol Trades BFF JSON (last resort; 503 since ~2026-04-10)
    """
    # ── 1. House Stock Watcher + Senate Stock Watcher (free JSON APIs) ──────
    merged = []
    for label, fn in (("House Stock Watcher", _fetch_from_hsw),
                      ("Senate Stock Watcher", _fetch_from_ssw)):
        try:
            batch = fn()
            log.info(f"{label}: fetched {len(batch)} disclosures")
            merged.extend(batch)
        except Exception as e:
            log.warning(f"{label} unavailable: {e}")
    if merged:
        return merged

    # ── 2. House Clerk PTR  (official gov source — XML index + PDF parsing) ─
    try:
        trades = _fetch_from_house_clerk()
        if trades:
            return trades
    except Exception as e:
        log.warning(f"House Clerk PTR unavailable: {e}")

    # ── 3. Capitol Trades HTML (SPA — only works if BFF serves SSR data) ────
    try:
        trades = _fetch_from_capitoltrades_html()
        log.info(f"Capitol Trades HTML: {len(trades)} trade(s) scraped")
        return trades
    except Exception as e:
        log.warning(f"Capitol Trades HTML: {e}")

    # ── 4. Capitol Trades BFF JSON (last resort) ──────────────────────────────
    try:
        trades = _fetch_from_capitoltrades(pages)
        log.info(f"Capitol Trades BFF: fetched {len(trades)} raw disclosures")
        return trades
    except Exception as e:
        log.error(f"Capitol Trades BFF also failed ({e}) — no disclosures available")
        return []


def _parse_disclosure(raw: dict) -> dict | None:
    """Normalise a raw disclosure item into our internal format.
    HSW/SSW records are already normalised; pass them through.
    """
    # Already normalised (HSW, SSW, House Clerk PTR, Capitol Trades HTML)
    if raw.get("_source") in ("hsw", "ssw", "house_clerk", "ct_html"):
        return raw if (raw.get("politician") and raw.get("ticker")) else None

    # Capitol Trades BFF shape
    try:
        pol    = raw.get("politician") or {}
        issuer = raw.get("issuer") or {}

        first = pol.get("firstName") or pol.get("first_name") or ""
        last  = pol.get("lastName")  or pol.get("last_name")  or ""
        name  = f"{first} {last}".strip()

        ticker = (
            issuer.get("ticker")
            or issuer.get("tickerSymbol")
            or raw.get("ticker")
            or ""
        ).upper().strip()

        # Deduplicate ID — use _txId, then id, then composite key
        trade_id = (
            raw.get("_txId")
            or raw.get("id")
            or f"{name}_{ticker}_{raw.get('txDate', raw.get('tradeDate', ''))}"
        )

        asset_type = (raw.get("assetType") or raw.get("asset_type") or "").lower()
        tx_type    = (raw.get("txType")    or raw.get("tx_type")    or "").lower()

        # Value: may be a range object or a direct number
        val_obj = raw.get("txValue") or raw.get("value") or {}
        if isinstance(val_obj, dict):
            lo = val_obj.get("lowerBound") or val_obj.get("min") or 0
            hi = val_obj.get("upperBound") or val_obj.get("max") or lo
            tx_value = (lo + hi) / 2 if hi else lo
        elif isinstance(val_obj, (int, float)):
            tx_value = float(val_obj)
        else:
            tx_value = 0.0

        filed_date = raw.get("filedDate") or raw.get("filedAt") or ""
        tx_date    = raw.get("txDate")    or raw.get("tradeDate") or ""

        if not name or not ticker:
            return None

        return {
            "id":         trade_id,
            "politician": name,
            "ticker":     ticker,
            "asset_type": asset_type,
            "tx_type":    tx_type,
            "tx_value":   float(tx_value),
            "filed_date": filed_date,
            "tx_date":    tx_date,
        }
    except Exception as e:
        log.debug(f"Disclosure parse error: {e}")
        return None


def poll_disclosures():
    """Check disclosure sources for new trades and act on qualifying ones."""
    log.info("Polling disclosure sources for new trades...")
    try:
        raw_trades = fetch_recent_disclosures(pages=2)
    except Exception as e:
        log.error(f"Disclosure poll failed: {e}")
        return
    log.info(f"Fetched {len(raw_trades)} raw disclosures")

    with _lock:
        _check_daily_reset()
        bal        = state["balance"]
        dstart     = state["daily_start_balance"]
        paused     = state["paused"]
        seen_set   = set(state["seen_trade_ids"])
        # Include current mark-to-market value of open positions so deployed
        # capital isn't counted as a loss — only realized + unrealized PnL matters.
        open_pos_value = sum(
            pos.get("cost", 0) + pos.get("pnl", 0.0)
            for pos in state["positions"].values()
        )

    # Enforce daily loss limit on effective balance (cash + open position value).
    # This prevents capital deployed into open positions from falsely triggering.
    effective_balance = bal + open_pos_value
    if not paused and effective_balance > 0 and (effective_balance / dstart - 1) <= -DAILY_LOSS_LIMIT_PCT:
        log.warning(f"Daily loss limit hit (effective ${effective_balance:.2f} vs start ${dstart:.2f}). Pausing.")
        tg_send(
            f"⚠️ <b>Daily loss limit hit</b>\n"
            f"Effective balance ${effective_balance:.2f} vs start ${dstart:.2f}\n"
            f"Bot paused. Send /start to resume."
        )
        with _lock:
            state["paused"] = True
            save_state()
        paused = True

    new_ids: list[str] = []
    for raw in raw_trades:
        trade = _parse_disclosure(raw)
        if not trade or trade["id"] in seen_set:
            continue
        seen_set.add(trade["id"])
        new_ids.append(trade["id"])
        _evaluate_trade(trade, paused)

    if new_ids:
        with _lock:
            state["seen_trade_ids"].extend(new_ids)
            state["seen_trade_ids"] = state["seen_trade_ids"][-2000:]
            save_state()
        log.info(f"Processed {len(new_ids)} new disclosures")
    else:
        log.info("No new disclosures found")


def _evaluate_trade(trade: dict, paused: bool):
    """Apply all filters and open a position if the trade qualifies."""
    ticker     = trade["ticker"]
    politician = trade["politician"]
    asset_type = trade["asset_type"]
    tx_type    = trade["tx_type"]
    tx_value   = trade["tx_value"]

    # Only stocks and ETFs
    if asset_type and asset_type not in ("stock", "etf", "equity", ""):
        log.debug(f"Skip {ticker}: asset_type='{asset_type}'")
        return

    # Only buys
    if tx_type and tx_type not in ("buy", "purchase"):
        log.debug(f"Skip {ticker}: tx_type='{tx_type}'")
        return

    # Minimum trade value
    if tx_value > 0 and tx_value < MIN_TRADE_VALUE:
        log.debug(f"Skip {ticker}: disclosed ${tx_value:,.0f} < ${MIN_TRADE_VALUE:,}")
        return

    # Age filter — only act on disclosures filed within the lookback window
    filed = trade.get("filed_date") or trade.get("tx_date") or ""
    if filed:
        try:
            filed_dt = datetime.fromisoformat(filed.replace("Z", "+00:00"))
            if filed_dt.tzinfo is None:
                filed_dt = filed_dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - filed_dt).days
            if age_days > DISCLOSURE_LOOKBACK_DAYS:
                log.debug(f"Skip {ticker}: disclosure filed {age_days}d ago > {DISCLOSURE_LOOKBACK_DAYS}d cutoff")
                return
        except Exception:
            pass  # unparseable date — allow through rather than silently drop

    # Politician score gate
    with _lock:
        score_info       = state["politician_scores"].get(politician, {})
        score            = score_info.get("score", 0)
        active_threshold = state.get("active_threshold", MIN_POLITICIAN_SCORE)

    if score < active_threshold:
        log.info(f"Skip {politician}/{ticker}: score {score} < {active_threshold}")
        return

    if paused:
        log.info(f"Skip {politician}/{ticker}: bot paused")
        return

    # Position-limit checks
    with _lock:
        if ticker in state["positions"]:
            log.info(f"Skip {ticker}: position already open")
            return
        if len(state["positions"]) >= MAX_OPEN_POSITIONS:
            log.info(f"Skip {ticker}: max open positions ({MAX_OPEN_POSITIONS}) reached")
            return
        pol_count = sum(1 for p in state["positions"].values() if p["politician"] == politician)
        if pol_count >= MAX_POS_PER_POLITICIAN:
            log.info(f"Skip {politician}/{ticker}: per-politician limit ({MAX_POS_PER_POLITICIAN}) reached")
            return
        # Insufficient balance
        if state["balance"] < PAPER_TRADE_SIZE:
            log.warning(f"Skip {ticker}: balance ${state['balance']:.2f} < trade size ${PAPER_TRADE_SIZE}")
            return

    log.info(f"Signal ▶ {politician} bought ${tx_value:,.0f} of {ticker} (score {score})")
    _open_position(trade, score)


# ══════════════════════════════════════════════════════════════════════════════
#  TRADE EXECUTION
# ══════════════════════════════════════════════════════════════════════════════
def _open_position(trade: dict, score: int):
    ticker     = trade["ticker"]
    politician = trade["politician"]

    price = get_stock_price(ticker)
    if not price or price <= 0:
        log.warning(f"No price for {ticker} — skipping")
        return

    order = place_paper_order(ticker, PAPER_TRADE_SIZE, "buy")
    if not order:
        return

    shares = PAPER_TRADE_SIZE / price

    with _lock:
        state["balance"] -= PAPER_TRADE_SIZE
        state["positions"][ticker] = {
            "politician":     politician,
            "score":          score,
            "disclosed_value": trade["tx_value"],
            "entry_price":    price,
            "current_price":  price,
            "shares":         shares,
            "cost":           PAPER_TRADE_SIZE,
            "pnl":            0.0,
            "pnl_pct":        0.0,
            "alpaca_order_id": order.get("id", ""),
            "opened_at":      datetime.now(timezone.utc).isoformat(),
            "filed_date":     trade.get("filed_date", ""),
        }
        state["stats"]["total_trades"] += 1
        save_state()

    tg_send(
        f"🟢 <b>New Position Opened</b>\n"
        f"Ticker: <b>{ticker}</b>\n"
        f"Politician: {politician} (score {score}/100)\n"
        f"Disclosed: ${trade['tx_value']:,.0f}\n"
        f"Entry: ${price:.2f} | Size: ${PAPER_TRADE_SIZE}\n"
        f"Shares: {shares:.4f}\n"
        f"Balance: ${state['balance']:.2f}"
    )
    log.info(f"Opened {ticker} @ ${price:.2f} ({shares:.4f} shares)")


def check_open_positions():
    """Check every open position for exit conditions (TP / SL / max hold)."""
    with _lock:
        tickers = list(state["positions"].keys())

    for ticker in tickers:
        price = get_stock_price(ticker)
        if not price:
            continue

        with _lock:
            pos = state["positions"].get(ticker)
            if not pos:
                continue
            # Update current price in state for dashboard
            pos["current_price"] = price
            entry   = pos["entry_price"]
            shares  = pos["shares"]
            pnl     = (price - entry) * shares
            pnl_pct = (price / entry - 1) * 100
            pos["pnl"]     = pnl
            pos["pnl_pct"] = pnl_pct

        try:
            opened_at = datetime.fromisoformat(pos["opened_at"])
        except Exception:
            opened_at = datetime.now(timezone.utc)
        age_days = (datetime.now(timezone.utc) - opened_at).days

        reason = None
        if pnl_pct >= TAKE_PROFIT_PCT * 100:
            reason = f"take profit +{pnl_pct:.1f}%"
        elif pnl_pct <= -STOP_LOSS_PCT * 100:
            reason = f"stop loss {pnl_pct:.1f}%"
        elif age_days >= MAX_HOLD_DAYS:
            reason = f"max hold ({age_days}d)"

        if reason:
            _close_position(ticker, pos, price, reason)
        else:
            with _lock:
                save_state()


def _close_position(ticker: str, pos: dict, exit_price: float, reason: str):
    shares   = pos["shares"]
    cost     = pos["cost"]
    proceeds = shares * exit_price
    pnl      = proceeds - cost
    pnl_pct  = (proceeds / cost - 1) * 100

    close_alpaca_position(ticker)

    closed = {
        **pos,
        "ticker":       ticker,
        "exit_price":   exit_price,
        "proceeds":     proceeds,
        "pnl":          pnl,
        "pnl_pct":      pnl_pct,
        "close_reason": reason,
        "closed_at":    datetime.now(timezone.utc).isoformat(),
    }

    with _lock:
        state["balance"] += proceeds
        state["closed_trades"].append(closed)
        if ticker in state["positions"]:
            del state["positions"][ticker]
        state["stats"]["total_pnl"] += pnl
        if pnl > 0:
            state["stats"]["wins"] += 1
        else:
            state["stats"]["losses"] += 1
        save_state()

    emoji = "✅" if pnl > 0 else "🔴"
    tg_send(
        f"{emoji} <b>Position Closed</b>\n"
        f"Ticker: <b>{ticker}</b>\n"
        f"Politician: {pos['politician']}\n"
        f"Entry: ${pos['entry_price']:.2f} → Exit: ${exit_price:.2f}\n"
        f"PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%)\n"
        f"Reason: {reason}\n"
        f"Balance: ${state['balance']:.2f}"
    )
    log.info(f"Closed {ticker}: {reason} | PnL ${pnl:+.2f} ({pnl_pct:+.1f}%)")


# ══════════════════════════════════════════════════════════════════════════════
#  CLAUDE POLITICIAN SCORING

def _quiver_tickers_for(politician: str, seen_ids: list[str]) -> list[str]:
    """Return up to 10 tickers for one politician from quiver_ seed IDs.

    Processes only quiver_-prefixed entries one at a time — no full map
    is built in memory. Stops collecting once 10 unique tickers are found.
    """
    tickers: set[str] = set()
    prefix = f"quiver_{politician}_"
    for tid in seen_ids:
        if not tid.startswith("quiver_"):
            continue
        if tid.startswith(prefix):
            # ID format: quiver_{name}_{TICKER}_{date}
            rest = tid[len(prefix):]
            m = re.match(r"([A-Z]{1,5})_", rest)
            if m:
                tickers.add(m.group(1))
                if len(tickers) >= 10:
                    break
    return sorted(tickers)
# ══════════════════════════════════════════════════════════════════════════════
def _run_scorer():
    """Run score.py in a subprocess to isolate the Claude API memory spike.

    The child process reads state.json, calls Claude, writes updated scores back,
    then exits — freeing all that memory before control returns here. After it
    completes we reload the relevant fields from disk into the live state dict.
    """
    log.info("Launching score.py subprocess for politician scoring...")
    scorer_path = os.path.join(os.path.dirname(__file__), "score.py")
    venv_python = os.path.join(os.path.dirname(__file__), "venv", "bin", "python")
    try:
        result = subprocess.run(
            [venv_python, scorer_path],
            timeout=120,
            cwd=os.path.dirname(__file__),
        )
        if result.returncode != 0:
            log.warning(f"score.py exited with code {result.returncode}")
    except subprocess.TimeoutExpired:
        log.error("score.py timed out after 120s")
        return
    except Exception as e:
        log.error(f"score.py subprocess failed: {e}")
        return

    # Reload scoring results from disk into the live state dict
    try:
        with open(STATE_FILE) as f:
            fresh = json.load(f)
        with _lock:
            state["politician_scores"]  = fresh.get("politician_scores", state["politician_scores"])
            state["last_score_refresh"] = fresh.get("last_score_refresh", state["last_score_refresh"])
            state["active_threshold"]   = fresh.get("active_threshold", state["active_threshold"])
        log.info("Reloaded politician scores from disk after subprocess scoring")
    except Exception as e:
        log.error(f"Failed to reload scores from disk: {e}")


def score_politicians():  # kept for reference; called via _run_scorer() subprocess
    """Use Claude to score active politicians based on their recent trade history.

    Scoring runs on its own 24h schedule independent of disclosure availability.
    If no fresh disclosures are returned (e.g., Congress recess), scoring falls
    back to the last-known trade counts stored in state rather than skipping.

    NOTE: This function is no longer called directly. All scoring is dispatched
    through _run_scorer() which executes score.py as a subprocess so the memory
    spike from the Claude API response is isolated and freed on child exit.
    """
    log.info("Refreshing politician scores via Claude...")

    # ── Load existing scores; seen_trade_ids queried per-politician later ────
    with _lock:
        existing_scores = dict(state.get("politician_scores", {}))
        seen_ids_snap   = list(state.get("seen_trade_ids", []))

    # ── Build fresh-data summaries from any available disclosures ────────────
    fresh_summaries: dict[str, dict] = {}   # keyed by politician name
    try:
        raw_trades = fetch_recent_disclosures(pages=5)
    except Exception as e:
        log.warning(f"Disclosure fetch for scoring failed: {e} — scoring from existing data only")
        raw_trades = []

    if raw_trades:
        cutoff: datetime = datetime.now(timezone.utc) - timedelta(days=90)
        pol_map: dict[str, list[dict]] = {}
        for raw in raw_trades:
            trade = _parse_disclosure(raw)
            if not trade:
                continue
            if trade["filed_date"]:
                try:
                    filed = datetime.fromisoformat(trade["filed_date"].replace("Z", "+00:00"))
                    if filed.tzinfo is None:
                        filed = filed.replace(tzinfo=timezone.utc)
                    if filed < cutoff:
                        continue
                except Exception:
                    pass
            pol_map.setdefault(trade["politician"], []).append(trade)

        for pol, trades in pol_map.items():
            buys    = [t for t in trades if t["tx_type"] in ("buy", "purchase")]
            sells   = [t for t in trades if t["tx_type"] == "sell"]
            tickers = sorted({t["ticker"] for t in trades})
            avg_val = sum(t["tx_value"] for t in trades if t["tx_value"] > 0) / max(len(trades), 1)
            fresh_summaries[pol] = {
                "name":         pol,
                "total_trades": len(trades),
                "buys":         len(buys),
                "sells":        len(sells),
                "tickers":      tickers[:10],
                "avg_value":    round(avg_val),
            }

    # ── Merge: fresh data takes precedence; fill gaps from existing scores ────
    # Always score the full known politician pool so Claude produces a complete
    # ranked list regardless of how many new PTRs were filed.
    summaries_map: dict[str, dict] = {}

    for pol, info in existing_scores.items():
        if pol in fresh_summaries:
            summaries_map[pol] = fresh_summaries[pol]
        else:
            trade_count = info.get("trade_count_90d", 0)
            if trade_count > 0:
                summaries_map[pol] = {
                    "name":         pol,
                    "total_trades": trade_count,
                    "buys":         trade_count,
                    "sells":        0,
                    "tickers":      _quiver_tickers_for(pol, seen_ids_snap),
                    "avg_value":    0,
                    "data_note":    "no recent filings — scored from historical trade count",
                }

    # Also include any politicians who appeared in fresh PTRs but aren't yet in state
    for pol, summary in fresh_summaries.items():
        if pol not in summaries_map:
            summaries_map[pol] = summary

    summaries = list(summaries_map.values())

    if not summaries:
        log.warning("No politician data available for scoring — skipping")
        return

    fresh_ct = len(fresh_summaries)
    hist_ct  = len(summaries) - fresh_ct
    log.info(f"Scoring pool: {len(summaries)} politicians ({fresh_ct} with fresh PTRs, {hist_ct} from historical data)")

    # Sort by activity; cap at 20 politicians per call to limit memory + tokens
    summaries.sort(key=lambda x: -x["total_trades"])
    summaries = summaries[:20]

    # Embed prior score AND an explicit floor in each entry so Claude has a
    # concrete lower bound to reason against, not just a soft anchor.
    for s in summaries:
        prior = existing_scores.get(s["name"], {}).get("score", None)
        s["prior_score"] = prior
        if prior is not None:
            s["score_floor"] = max(0, prior - 15)

    # Use a lower threshold during recess when fresh PTR data is sparse.
    active_threshold = RECESS_THRESHOLD if fresh_ct < 5 else MIN_POLITICIAN_SCORE
    if active_threshold != MIN_POLITICIAN_SCORE:
        log.info(f"Recess mode: using threshold {active_threshold} (only {fresh_ct} fresh PTRs)")

    prompt = f"""You are evaluating US Congress member stock disclosures to score their predictive value for copy-trading.

Score each politician 0-100 where:
• 80-100: Strong signal — high-value trades, sector expertise from committee work, consistent recent activity
• 60-79: Above threshold, worth following
• 40-59: Borderline — low frequency or small values
• 0-39: Weak signal — dormant, tiny trades, or random tickers

Key scoring factors:
1. Trade frequency and recency (more recent = higher weight)
2. Average disclosed value (larger = more conviction)
3. Sector concentration vs. committee assignments (e.g., Defense committee member trading defense stocks = premium)
4. Known high-signal politicians: Nancy Pelosi, Brian Mast, Michael McCaul, Dan Crenshaw, Tommy Tuberville
5. Ratio of buys to sells (active buyers > pure sellers)

CRITICAL SCORING RULE — prior score anchoring:
Each entry has a prior_score (last known score) and a score_floor (prior_score minus 15).
- You MUST NOT score any politician below their score_floor unless you have specific new negative evidence in the data above.
- Absence of new filings is NOT negative evidence — Congress is frequently in recess.
- If a politician had prior_score=80 and score_floor=65, their new score must be ≥ 65 unless something in their data clearly justifies a larger drop.
- The tickers list shows their known trading history — use it to assess sector expertise even when no new filings are present.

Politicians to score (last 90 days; prior_score = last known score, score_floor = minimum allowed score):
{json.dumps(summaries, indent=2)}

IMPORTANT: Return ONLY a raw JSON object. No markdown fences, no ```json, no explanation, no text before or after. Start your response with {{ and end with }}.

{{
  "scores": {{
    "First Last": {{
      "score": 72,
      "reasoning": "one sentence max",
      "committee_relevance": "high|medium|low|unknown"
    }}
  }}
}}"""

    try:
        response = _ai.messages.create(
            model="claude-opus-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text.strip()

        # Strip markdown fences if present (```json ... ``` or ``` ... ```)
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)
        raw_text = raw_text.strip()

        # Extract the outermost JSON object
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if not match:
            log.error(f"Claude scoring: no JSON found in response. First 500 chars: {raw_text[:500]!r}")
            return

        try:
            result = json.loads(match.group())
        except json.JSONDecodeError as e:
            log.error(f"Claude scoring JSON parse error: {e}. First 500 chars: {raw_text[:500]!r}")
            return

        scores_out = result.get("scores", {})

        now_iso = datetime.now(timezone.utc).isoformat()
        with _lock:
            for pol, info in scores_out.items():
                trade_count = next(
                    (s["total_trades"] for s in summaries if s["name"] == pol), 0
                )
                state["politician_scores"][pol] = {
                    "score":               int(info.get("score", 0)),
                    "reasoning":           info.get("reasoning", ""),
                    "committee_relevance": info.get("committee_relevance", "unknown"),
                    "last_scored":         now_iso,
                    "trade_count_90d":     trade_count,
                }
            state["last_score_refresh"] = now_iso
            state["active_threshold"]   = active_threshold
            save_state()

        above = sum(1 for v in scores_out.values() if int(v.get("score", 0)) >= active_threshold)
        log.info(f"Scored {len(scores_out)} politicians — {above} above threshold ({active_threshold})")
        if above == 0:
            log.warning(f"0 politicians above threshold — Claude raw response (first 500): {raw_text[:500]!r}")
        tg_send(
            f"📊 <b>Politician scores updated</b>\n"
            f"{above}/{len(scores_out)} politicians above threshold ({active_threshold})\n"
            f"Send /scores for full list"
        )

    except Exception as e:
        status = getattr(e, "status_code", None)
        if status in (400, 429):
            # Usage/quota limit — don't retry until the next scheduled 24h refresh.
            # Update last_score_refresh so restarts don't immediately retry either.
            # Existing politician_scores in state remain active for trade decisions.
            with _lock:
                state["last_score_refresh"] = datetime.now(timezone.utc).isoformat()
                save_state()
            log.warning(
                f"Claude API usage limit (HTTP {status}) — scoring skipped; "
                f"existing scores remain active until next 24h refresh"
            )
        else:
            log.error(f"Claude scoring failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  DAILY SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
def send_daily_summary():
    log.info("Sending daily summary")
    with _lock:
        bal    = state["balance"]
        dstart = state["daily_start_balance"]
        stats  = state["stats"]
        n_open = len(state["positions"])
        scores = state["politician_scores"]

    day_pnl   = bal - dstart
    total_pnl = stats["total_pnl"]
    wins      = stats["wins"]
    losses    = stats["losses"]
    wr        = wins / (wins + losses) * 100 if (wins + losses) else 0

    top_lines = "\n".join(
        f"  {name}: {info['score']}"
        for name, info in sorted(scores.items(), key=lambda x: -x[1].get("score", 0))[:5]
    ) or "  (none yet)"

    tg_send(
        f"📈 <b>Daily Summary</b>\n"
        f"Balance: <b>${bal:.2f}</b>  (day {day_pnl:+.2f})\n"
        f"Total PnL: ${total_pnl:+.2f}\n"
        f"Trades: {stats['total_trades']} | W/L: {wins}/{losses} ({wr:.0f}%)\n"
        f"Open positions: {n_open}\n\n"
        f"<b>Top politicians:</b>\n{top_lines}"
    )

    with _lock:
        state["daily_start_balance"] = bal
        state["daily_reset_date"]    = datetime.now(timezone.utc).date().isoformat()
        save_state()


# ══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
_DASH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Congress Trading Bot</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:14px;font-size:14px;line-height:1.5}
h1{color:#58a6ff;font-size:1.3em;margin-bottom:12px;display:flex;align-items:center;gap:8px}
h2{color:#8b949e;font-size:.75em;margin:18px 0 8px;text-transform:uppercase;letter-spacing:1.5px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.dot-green{background:#3fb950;box-shadow:0 0 6px #3fb95088}
.dot-orange{background:#f0883e;box-shadow:0 0 6px #f0883e88}
.pill{display:inline-flex;align-items:center;gap:6px;background:#161b22;border:1px solid #30363d;border-radius:20px;padding:4px 12px;font-size:.8em;color:#8b949e;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:6px}
@media(min-width:480px){.grid{grid-template-columns:repeat(6,1fr)}}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px}
.card .lbl{color:#8b949e;font-size:.7em;margin-bottom:3px}
.card .val{font-size:1.2em;font-weight:700}
.green{color:#3fb950}.red{color:#f85149}.blue{color:#58a6ff}.grey{color:#8b949e}
table{width:100%;border-collapse:collapse;font-size:.82em}
th{background:#161b22;padding:7px 8px;text-align:left;color:#8b949e;font-size:.75em;border-bottom:1px solid #30363d;white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid #21262d;vertical-align:top}
tr:hover td{background:#161b22}
.badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:.72em;font-weight:600}
.badge-g{background:#1a4a1f;color:#3fb950}
.badge-r{background:#4a1a1a;color:#f85149}
.badge-y{background:#4a3a1a;color:#e3b341}
.mono{font-family:monospace}
.reason{color:#8b949e;font-size:.78em}
</style>
</head>
<body>
<h1>
  <span>Congress Trading Bot</span>
</h1>

<div class="pill">
  <span class="dot {{ 'dot-orange' if d.paused else 'dot-green' }}"></span>
  {{ 'PAUSED' if d.paused else 'LIVE' }}
  &nbsp;·&nbsp; {{ d.now }}
</div>

<div class="grid">
  <div class="card">
    <div class="lbl">Balance</div>
    <div class="val blue">${{ "%.2f"|format(d.balance) }}</div>
  </div>
  <div class="card">
    <div class="lbl">Total PnL</div>
    <div class="val {{ 'green' if d.total_pnl >= 0 else 'red' }}">${{ "%+.2f"|format(d.total_pnl) }}</div>
  </div>
  <div class="card">
    <div class="lbl">Win Rate</div>
    <div class="val {{ 'green' if d.win_rate >= 50 else 'red' }}">{{ "%.0f"|format(d.win_rate) }}%</div>
  </div>
  <div class="card">
    <div class="lbl">Open</div>
    <div class="val">{{ d.open_count }}</div>
  </div>
  <div class="card">
    <div class="lbl">Total Trades</div>
    <div class="val">{{ d.total_trades }}</div>
  </div>
  <div class="card">
    <div class="lbl">Following</div>
    <div class="val green">{{ d.tracked_pols }}</div>
  </div>
</div>

{% if d.positions %}
<h2>Open Positions</h2>
<div style="overflow-x:auto">
<table>
  <thead><tr>
    <th>Ticker</th><th>Politician</th><th>Entry</th><th>Current</th><th>PnL</th><th>Opened</th>
  </tr></thead>
  <tbody>
  {% for p in d.positions %}
  <tr>
    <td><strong>{{ p.ticker }}</strong></td>
    <td>{{ p.politician }}<br><span class="reason">score {{ p.score }}</span></td>
    <td class="mono">${{ "%.2f"|format(p.entry_price) }}</td>
    <td class="mono">${{ "%.2f"|format(p.current_price) }}</td>
    <td class="{{ 'green' if p.pnl >= 0 else 'red' }} mono">
      ${{ "%+.2f"|format(p.pnl) }}<br><span style="font-size:.85em">{{ "%+.1f"|format(p.pnl_pct) }}%</span>
    </td>
    <td>{{ p.opened_at[:10] }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
</div>
{% endif %}

{% if d.closed_trades %}
<h2>Recent Closed Trades</h2>
<div style="overflow-x:auto">
<table>
  <thead><tr>
    <th>Ticker</th><th>Politician</th><th>Entry → Exit</th><th>PnL</th><th>Reason</th><th>Date</th>
  </tr></thead>
  <tbody>
  {% for t in d.closed_trades %}
  <tr>
    <td><strong>{{ t.ticker }}</strong></td>
    <td>{{ t.politician }}</td>
    <td class="mono">${{ "%.2f"|format(t.entry_price) }} → ${{ "%.2f"|format(t.exit_price) }}</td>
    <td class="{{ 'green' if t.pnl >= 0 else 'red' }} mono">
      ${{ "%+.2f"|format(t.pnl) }}<br><span style="font-size:.85em">{{ "%+.1f"|format(t.pnl_pct) }}%</span>
    </td>
    <td class="reason">{{ t.close_reason }}</td>
    <td>{{ t.closed_at[:10] }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
</div>
{% endif %}

{% if d.scores %}
<h2>Politician Scores</h2>
<div style="overflow-x:auto">
<table>
  <thead><tr>
    <th>Politician</th><th>Score</th><th>Status</th><th>Committee</th><th>Notes</th>
  </tr></thead>
  <tbody>
  {% for p in d.scores %}
  <tr>
    <td>{{ p.name }}</td>
    <td><strong>{{ p.score }}</strong><span class="grey">/100</span></td>
    <td>
      {% if p.score >= 80 %}<span class="badge badge-g">Strong</span>
      {% elif p.score >= 60 %}<span class="badge badge-y">Active</span>
      {% else %}<span class="badge badge-r">Below</span>{% endif %}
    </td>
    <td>{{ p.committee }}</td>
    <td class="reason">{{ p.reasoning[:80] }}{% if p.reasoning|length > 80 %}…{% endif %}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
</div>
{% endif %}

</body>
</html>"""


@app.route("/")
def dashboard():
    with _lock:
        s = json.loads(json.dumps(state, default=str))

    stats    = s.get("stats", {})
    wins     = stats.get("wins", 0)
    losses   = stats.get("losses", 0)
    win_rate = wins / (wins + losses) * 100 if (wins + losses) else 0

    positions_data = [
        {
            **pos,
            "ticker": ticker,
        }
        for ticker, pos in s.get("positions", {}).items()
    ]

    closed = sorted(
        s.get("closed_trades", []),
        key=lambda x: x.get("closed_at", ""),
        reverse=True,
    )[:25]

    scores = sorted(
        [
            {
                "name":      name,
                "score":     info.get("score", 0),
                "reasoning": info.get("reasoning", ""),
                "committee": info.get("committee_relevance", "unknown"),
            }
            for name, info in s.get("politician_scores", {}).items()
        ],
        key=lambda x: -x["score"],
    )

    tracked = sum(
        1 for v in s.get("politician_scores", {}).values()
        if v.get("score", 0) >= s.get("active_threshold", MIN_POLITICIAN_SCORE)
    )

    d = type("D", (), {
        "balance":      s["balance"],
        "total_pnl":    stats.get("total_pnl", 0.0),
        "win_rate":     win_rate,
        "open_count":   len(s.get("positions", {})),
        "total_trades": stats.get("total_trades", 0),
        "tracked_pols": tracked,
        "paused":       s.get("paused", False),
        "positions":    positions_data,
        "closed_trades": closed,
        "scores":       scores,
        "now":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })()

    return render_template_string(_DASH_HTML, d=d)


@app.route("/health")
def health():
    with _lock:
        return {
            "status":    "ok",
            "balance":   state["balance"],
            "paused":    state["paused"],
            "positions": len(state["positions"]),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════
def _start_dashboard():
    # Kill any stale process already bound to the dashboard port so Flask
    # doesn't fail silently on restart.
    try:
        import subprocess
        subprocess.run(
            ["fuser", "-k", f"{DASHBOARD_PORT}/tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)  # brief pause for the OS to release the port
    except Exception:
        pass
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)


def main():
    log.info("=" * 60)
    log.info("Congress Trading Bot starting up")
    log.info(f"Balance: ${state['balance']:.2f} | Positions: {len(state['positions'])}")
    log.info(f"Dashboard: http://0.0.0.0:{DASHBOARD_PORT}")
    log.info("=" * 60)

    tg_send(
        f"🤖 <b>Congress Trading Bot started</b>\n"
        f"Balance: ${state['balance']:.2f}\n"
        f"Open positions: {len(state['positions'])}\n"
        f"Dashboard: http://167.71.60.207:{DASHBOARD_PORT}"
    )

    # Telegram command listener
    if TG_TOKEN:
        threading.Thread(target=tg_poll_commands, daemon=True, name="tg-poll").start()
        log.info("Telegram command listener started")
    else:
        log.warning("TG_TOKEN not set — Telegram disabled")

    # Flask dashboard
    threading.Thread(target=_start_dashboard, daemon=True, name="dashboard").start()

    # Initial scoring (skip if fresh within 24h)
    with _lock:
        last_refresh = state.get("last_score_refresh")
    stale = True
    if last_refresh:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_refresh)).total_seconds()
            stale = age >= SCORE_REFRESH_HOURS * 3600
        except Exception:
            pass
    if stale:
        _run_scorer()

    # ── Startup seed: mark all current disclosures as seen (no trades placed) ──
    log.info("Seeding seen_trade_ids from current disclosures to prevent backfill trading...")
    _probe_trades = fetch_recent_disclosures(pages=2)
    if _probe_trades:
        _src = _probe_trades[0].get("_source", "capitoltrades")
        _src_label = {
            "hsw":         "House Stock Watcher (housestockwatcher.com)",
            "ssw":         "Senate Stock Watcher (senatestockwatcher.com)",
            "house_clerk": "House Clerk PTR (disclosures-clerk.house.gov)",
            "ct_html":     "Capitol Trades HTML (capitoltrades.com)",
        }.get(_src, "Capitol Trades BFF (bff.capitoltrades.com)")
        _src_counts = {}
        for t in _probe_trades:
            s = t.get("_source", "capitoltrades")
            _src_counts[s] = _src_counts.get(s, 0) + 1
        if len(_src_counts) > 1:
            _src_short = {"hsw": "House", "ssw": "Senate", "house_clerk": "HouseClerk", "ct_html": "CT-HTML"}
            _detail = ", ".join(f"{_src_short.get(s, s)}: {n}" for s, n in _src_counts.items())
            log.info(f"Data source: {len(_probe_trades)} records ({_detail})")
        else:
            log.info(f"Data source: {_src_label} returned {len(_probe_trades)} records")

        with _lock:
            existing_seen = set(state["seen_trade_ids"])
            seeded_ids = []
            for raw in _probe_trades:
                trade = _parse_disclosure(raw)
                if trade and trade["id"] not in existing_seen:
                    existing_seen.add(trade["id"])
                    seeded_ids.append(trade["id"])
            if seeded_ids:
                state["seen_trade_ids"].extend(seeded_ids)
                state["seen_trade_ids"] = state["seen_trade_ids"][-2000:]
                save_state()
            log.info(f"Startup seed complete — {len(seeded_ids)} new IDs marked seen, {len(existing_seen)} total (no trades placed)")
    else:
        log.warning("Startup seed returned 0 records — all sources may be unavailable")

    # Scheduling timestamps (last_disclosure pre-set so probe counts as first poll)
    last_disclosure  = time.time()
    last_pos_check   = 0.0
    # Anchor last_scoring to the actual last refresh time (persisted in state) so
    # the 24h interval survives restarts and doesn't double-trigger at startup.
    with _lock:
        _last_refresh_str = state.get("last_score_refresh")
    if _last_refresh_str:
        try:
            last_scoring = datetime.fromisoformat(_last_refresh_str).timestamp()
        except Exception:
            last_scoring = 0.0
    else:
        last_scoring = 0.0
    last_daily       = None

    log.info("Main loop running")

    while True:
        now_ts  = time.time()
        now_utc = datetime.now(timezone.utc)

        # Poll Capitol Trades every 30 min
        if now_ts - last_disclosure >= DISCLOSURE_POLL_MINUTES * 60:
            poll_disclosures()
            last_disclosure = now_ts

        # Check positions every 5 min
        if now_ts - last_pos_check >= POSITION_CHECK_MINUTES * 60:
            check_open_positions()
            last_pos_check = now_ts

        # Re-score politicians every 24h
        if now_ts - last_scoring >= SCORE_REFRESH_HOURS * 3600:
            _run_scorer()
            last_scoring = now_ts

        # Daily summary at midnight UTC (within first 2 min of hour 0)
        today = now_utc.date()
        if last_daily != today and now_utc.hour == 0 and now_utc.minute < 2:
            send_daily_summary()
            last_daily = today

        time.sleep(30)


if __name__ == "__main__":
    main()
