#!/usr/bin/env python3
"""
Standalone politician scorer — runs as a subprocess of bot.py.

Reads state.json, calls Claude API to score politicians, writes results
back to state.json, then exits. Runs in its own process so the memory
spike from the API response is cleaned up on exit instead of bloating
the long-running bot process.
"""

import io
import os
import json
import re
import socket
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import dns.resolver
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
#  DNS PATCH — route specific hostnames through Google DNS (8.8.8.8)
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
            pass
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _patched_getaddrinfo

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════
MIN_POLITICIAN_SCORE  = 60
RECESS_THRESHOLD      = 50
DISCLOSURE_LOOKBACK_DAYS = 7

TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("congress_scorer")

# ══════════════════════════════════════════════════════════════════════════════
#  DISCLOSURE SOURCE CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════
_HSW_API    = "https://housestockwatcher.com/api"
_SSW_API    = "https://senatestockwatcher.com/api"

_HOUSE_CLERK_BASE = "https://disclosures-clerk.house.gov"
_HOUSE_CLERK_HDR  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "text/html,application/xhtml+xml,application/xml,*/*",
    "Referer":    "https://disclosures-clerk.house.gov/FinancialDisclosure",
}
_HOUSE_PTR_PDF_MAX = 30

_CT_HTML_URL = "https://www.capitoltrades.com/trades"
_CT_BFF_API  = "https://bff.capitoltrades.com/trades"
_CT_BFF_HDR  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Origin":     "https://www.capitoltrades.com",
    "Referer":    "https://www.capitoltrades.com/trades",
}

# ══════════════════════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  DISCLOSURE FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_from_capitoltrades(pages: int = 2) -> list[dict]:
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
        meta       = data.get("meta", {})
        pag        = meta.get("pagination", {})
        page_count = pag.get("pageCount", pag.get("totalPages", 1))
        if page >= page_count:
            break
    return trades


def _hsw_has_ticker(raw: dict) -> bool:
    ticker = (raw.get("ticker") or "").strip()
    return bool(ticker) and ticker != "--"


def _normalise_hsw(raw: dict) -> dict:
    name   = (raw.get("representative") or "").strip()
    ticker = (raw.get("ticker") or "").upper().strip()
    tx_raw  = (raw.get("type") or "").lower()
    tx_type = "buy" if "purchase" in tx_raw else ("sell" if "sale" in tx_raw else tx_raw)
    tx_value = 0.0
    rng  = raw.get("amount") or ""
    nums = re.findall(r"[\d,]+", rng.replace("$", ""))
    if nums:
        vals     = [int(n.replace(",", "")) for n in nums]
        tx_value = sum(vals) / len(vals)
    tx_date    = raw.get("transaction_date") or ""
    filed_date = raw.get("disclosure_date")  or tx_date
    asset_type = (raw.get("asset_type") or "stock").lower()
    return {
        "id":         f"hsw_{name}_{ticker}_{tx_date}",
        "politician": name,
        "ticker":     ticker,
        "asset_type": asset_type,
        "tx_type":    tx_type,
        "tx_value":   tx_value,
        "filed_date": filed_date,
        "tx_date":    tx_date,
        "_source":    "hsw",
    }


def _fetch_from_hsw() -> list[dict]:
    r = requests.get(_HSW_API, timeout=30)
    r.raise_for_status()
    raw_list = r.json()
    if not isinstance(raw_list, list):
        raise RuntimeError(f"HSW unexpected response shape: {type(raw_list)}")
    return [_normalise_hsw(item) for item in raw_list if _hsw_has_ticker(item)]


def _normalise_ssw(txn: dict, senator_name: str, filed_date: str) -> dict:
    ticker = (txn.get("ticker") or "").upper().strip()
    tx_raw  = (txn.get("type") or "").lower()
    tx_type = "buy" if "purchase" in tx_raw else ("sell" if "sale" in tx_raw else tx_raw)
    tx_value = 0.0
    rng  = txn.get("amount") or ""
    nums = re.findall(r"[\d,]+", rng.replace("$", ""))
    if nums:
        vals     = [int(n.replace(",", "")) for n in nums]
        tx_value = sum(vals) / len(vals)
    tx_date    = txn.get("transaction_date") or ""
    asset_type = (txn.get("asset_type") or "stock").lower()
    return {
        "id":         f"ssw_{senator_name}_{ticker}_{tx_date}",
        "politician": senator_name,
        "ticker":     ticker,
        "asset_type": asset_type,
        "tx_type":    tx_type,
        "tx_value":   tx_value,
        "filed_date": filed_date,
        "tx_date":    tx_date,
        "_source":    "ssw",
    }


def _fetch_from_ssw() -> list[dict]:
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
        filed_date = senator.get("date_recieved") or ""
        for txn in senator.get("transactions") or []:
            ticker = (txn.get("ticker") or "").upper().strip()
            if not ticker or ticker == "--":
                continue
            trades.append(_normalise_ssw(txn, name, filed_date))
    return trades


def _parse_house_ptr_pdf(pdf_bytes: bytes, member_name: str, filed_date: str) -> list[dict]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text   = "\n".join(page.extract_text() or "" for page in reader.pages)
    tx_re  = re.compile(
        r"\(([A-Z]{1,5})\)"
        r"[^\n]*\n"
        r"\[[A-Z]+\]\s*\n"
        r"(?:SP\s+)?"
        r"([PS])\s+"
        r"(\d{2}/\d{2}/\d{4})"
        r"(\d{2}/\d{2}/\d{4})"
        r"\s*(\$[\d,]+\s*-\s*\$[\d,]+)",
    )
    trades = []
    seen   = set()
    for m in tx_re.finditer(text):
        ticker      = m.group(1).upper()
        tx_type     = "buy" if m.group(2) == "P" else "sell"
        tx_date_raw = m.group(3)
        amount_str  = m.group(5)
        try:
            tx_date = datetime.strptime(tx_date_raw, "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            tx_date = tx_date_raw
        nums     = re.findall(r"[\d,]+", amount_str.replace("$", ""))
        tx_value = sum(int(n.replace(",", "")) for n in nums) / max(len(nums), 1) if nums else 0.0
        trade_id = f"house_{member_name}_{ticker}_{tx_date}"
        if trade_id in seen:
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
    year      = datetime.now(timezone.utc).year
    index_url = f"{_HOUSE_CLERK_BASE}/public_disc/financial-pdfs/{year}FD.xml"
    r         = requests.get(index_url, headers=_HOUSE_CLERK_HDR, timeout=30)
    r.raise_for_status()
    root   = ET.fromstring(r.content.decode("utf-8-sig"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=DISCLOSURE_LOOKBACK_DAYS)
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
    recent_ptrs = recent_ptrs[:_HOUSE_PTR_PDF_MAX]
    log.info(f"House Clerk PTR: {len(recent_ptrs)} PTR(s) within lookback window — downloading PDFs")
    trades = []
    for name, doc_id, filed_iso in recent_ptrs:
        pdf_url = f"{_HOUSE_CLERK_BASE}/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
        try:
            pdf_r = requests.get(pdf_url, headers=_HOUSE_CLERK_HDR, timeout=20)
            pdf_r.raise_for_status()
            parsed = _parse_house_ptr_pdf(pdf_r.content, name, filed_iso)
            trades.extend(parsed)
        except Exception as e:
            log.debug(f"  House PTR {doc_id} ({name}) failed: {e}")
    log.info(f"House Clerk PTR: {len(trades)} trade(s) extracted from {len(recent_ptrs)} PTR(s)")
    return trades


def _fetch_from_capitoltrades_html() -> list[dict]:
    hdrs = {**_CT_BFF_HDR, "Accept": "text/html,application/xhtml+xml"}
    r    = requests.get(_CT_HTML_URL, headers=hdrs, timeout=30)
    r.raise_for_status()
    soup  = BeautifulSoup(r.text, "lxml")
    table = soup.find("table")
    if not table:
        raise RuntimeError("Capitol Trades HTML: no <table> found — SPA with BFF down")
    trades = []
    rows   = table.find_all("tr")[1:]
    for row in rows:
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cells) < 6:
            continue
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
    try:
        trades = _fetch_from_house_clerk()
        if trades:
            return trades
    except Exception as e:
        log.warning(f"House Clerk PTR unavailable: {e}")
    try:
        trades = _fetch_from_capitoltrades_html()
        log.info(f"Capitol Trades HTML: {len(trades)} trade(s) scraped")
        return trades
    except Exception as e:
        log.warning(f"Capitol Trades HTML: {e}")
    try:
        trades = _fetch_from_capitoltrades(pages)
        log.info(f"Capitol Trades BFF: fetched {len(trades)} raw disclosures")
        return trades
    except Exception as e:
        log.error(f"Capitol Trades BFF also failed ({e}) — no disclosures available")
        return []


def _parse_disclosure(raw: dict) -> dict | None:
    if raw.get("_source") in ("hsw", "ssw", "house_clerk", "ct_html"):
        return raw if (raw.get("politician") and raw.get("ticker")) else None
    try:
        pol    = raw.get("politician") or {}
        issuer = raw.get("issuer") or {}
        first  = pol.get("firstName") or pol.get("first_name") or ""
        last   = pol.get("lastName")  or pol.get("last_name")  or ""
        name   = f"{first} {last}".strip()
        ticker = (
            issuer.get("ticker") or issuer.get("tickerSymbol") or raw.get("ticker") or ""
        ).upper().strip()
        trade_id = (
            raw.get("_txId") or raw.get("id")
            or f"{name}_{ticker}_{raw.get('txDate', raw.get('tradeDate', ''))}"
        )
        asset_type = (raw.get("assetType") or raw.get("asset_type") or "").lower()
        tx_type    = (raw.get("txType")    or raw.get("tx_type")    or "").lower()
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


def _quiver_tickers_for(politician: str, seen_ids: list[str]) -> list[str]:
    tickers: set[str] = set()
    prefix = f"quiver_{politician}_"
    for tid in seen_ids:
        if not tid.startswith("quiver_"):
            continue
        if tid.startswith(prefix):
            rest = tid[len(prefix):]
            m = re.match(r"([A-Z]{1,5})_", rest)
            if m:
                tickers.add(m.group(1))
                if len(tickers) >= 10:
                    break
    return sorted(tickers)


# ══════════════════════════════════════════════════════════════════════════════
#  SCORING
# ══════════════════════════════════════════════════════════════════════════════

def score_politicians():
    log.info("Refreshing politician scores via Claude...")

    state = load_state()
    existing_scores = dict(state.get("politician_scores", {}))
    seen_ids_snap   = list(state.get("seen_trade_ids", []))

    # Build fresh summaries from recent disclosures
    fresh_summaries: dict[str, dict] = {}
    try:
        raw_trades = fetch_recent_disclosures(pages=5)
    except Exception as e:
        log.warning(f"Disclosure fetch for scoring failed: {e} — scoring from existing data only")
        raw_trades = []

    if raw_trades:
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
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

    # Merge: fresh data takes precedence; fill gaps from existing scores
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

    summaries.sort(key=lambda x: -x["total_trades"])
    summaries = summaries[:20]

    for s in summaries:
        prior = existing_scores.get(s["name"], {}).get("score", None)
        s["prior_score"] = prior
        if prior is not None:
            s["score_floor"] = max(0, prior - 15)

    active_threshold = RECESS_THRESHOLD if fresh_ct < 5 else MIN_POLITICIAN_SCORE
    if active_threshold != MIN_POLITICIAN_SCORE:
        log.info(f"Recess mode: using threshold {active_threshold} (only {fresh_ct} fresh PTRs)")

    # Log prior scores so we can verify correct values are reaching the prompt
    prior_log = ", ".join(
        f"{s['name']}: prior={s.get('prior_score')}/floor={s.get('score_floor', 'n/a')}"
        for s in summaries
    )
    log.info(f"Prior scores being sent to Claude: {prior_log}")

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

MANDATORY SCORING RULE — you must follow this exactly:
- You MUST start from each politician's prior_score. That is the baseline.
- The score_floor is an absolute minimum — you cannot go below it under any circumstances.
- If you have no new negative evidence, return exactly the prior_score. Do not adjust it downward.
- Only adjust the score if you have specific new data in the entry (e.g. new trades, new tickers, a change in activity).
- Absence of new filings is NOT negative evidence — Congress is frequently in recess.
- prior_score=null means this is a new politician; score them from scratch using the trading data provided.

Politicians to score (last 90 days; prior_score = last known score, score_floor = absolute minimum allowed):
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
        _ai = Anthropic(api_key=ANTHROPIC_KEY)
        response = _ai.messages.create(
            model="claude-opus-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text.strip()

        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)
        raw_text = raw_text.strip()

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
        # Reload state fresh before writing to pick up any changes made while we ran
        state = load_state()
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
        save_state(state)

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
            state = load_state()
            state["last_score_refresh"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            log.warning(
                f"Claude API usage limit (HTTP {status}) — scoring skipped; "
                f"existing scores remain active until next 24h refresh"
            )
        else:
            log.error(f"Claude scoring failed: {e}")


if __name__ == "__main__":
    score_politicians()
