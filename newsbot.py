#!/usr/bin/env python3
"""
newsbot: texts you a 9 AM Eastern news digest (US + Brazil) and
breaking-news alerts. Summaries and headlines only, never links.

Usage:
    python newsbot.py              # run forever (scheduler)
    python newsbot.py --test-sms   # send a test text
    python newsbot.py --digest-now # build and send the digest right now
    python newsbot.py --check-now  # run one breaking-news check right now
"""
import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

# ---------------------------------------------------------------- config
TZ = ZoneInfo(os.getenv("NEWSBOT_TZ", "America/New_York"))  # handles EST/EDT
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "9"))
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "10"))
MAX_ALERTS_PER_DAY = int(os.getenv("MAX_ALERTS_PER_DAY", "6"))
QUIET_START = int(os.getenv("QUIET_START", "23"))  # no alerts from 11 PM...
QUIET_END = int(os.getenv("QUIET_END", "7"))       # ...until 7 AM
ITEMS_PER_SECTION = int(os.getenv("ITEMS_PER_SECTION", "5"))
MODEL = os.getenv("NEWSBOT_MODEL", "claude-haiku-4-5-20251001")
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "console").lower()  # twilio | ntfy | console
STATE_FILE = HERE / "state.json"

FEEDS = {
    "US": [
        ("Google News US", "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"),
        ("NPR", "https://feeds.npr.org/1001/rss.xml"),
        ("BBC US", "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"),
        ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ],
    "BR": [
        ("Google News BR", "https://news.google.com/rss?hl=pt-BR&gl=BR&ceid=BR:pt-419"),
        ("G1", "https://g1.globo.com/rss/g1/"),
        ("Folha", "https://feeds.folha.uol.com.br/emcimadahora/rss091.xml"),
        ("Agencia Brasil", "https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml"),
        ("BBC Brasil", "https://feeds.bbci.co.uk/portuguese/rss.xml"),
    ],
}
REGION_NAME = {"US": "United States", "BR": "Brazil"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "newsbot.log")],
)
log = logging.getLogger("newsbot")


# ---------------------------------------------------------------- state
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log.warning("state.json unreadable, starting fresh")
    return {"seen": {}, "alerts": [], "alert_counts": {}, "seeded": False}


def save_state(st):
    cutoff = (datetime.now(TZ) - timedelta(days=3)).timestamp()
    st["seen"] = {k: v for k, v in st["seen"].items() if v > cutoff}
    st["alerts"] = st["alerts"][-30:]
    STATE_FILE.write_text(json.dumps(st))


# ---------------------------------------------------------------- feeds
def clean(text, n=280):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n]


def fetch_region(region, per_feed=15):
    items = []
    for source, url in FEEDS[region]:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "newsbot/1.0"})
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            for e in feed.entries[:per_feed]:
                title = clean(e.get("title", ""), 200)
                if not title:
                    continue
                items.append({
                    "id": e.get("id") or e.get("link") or title,
                    "region": region,
                    "source": source,
                    "title": title,
                    "snippet": clean(e.get("summary", "")),
                })
        except Exception as ex:
            log.warning("feed failed (%s): %s", source, ex)
    return items


# ---------------------------------------------------------------- LLM
def ask(prompt, max_tokens=1500):
    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    r = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text.strip()


def ascii_safe(text):
    """Keep texts in plain characters so they stay cheap, single-encoding SMS."""
    swaps = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
             "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " "}
    for k, v in swaps.items():
        text = text.replace(k, v)
    return text


def numbered(items):
    return "\n".join(
        f"{i+1}. [{it['source']}] {it['title']} - {it['snippet']}"
        for i, it in enumerate(items)
    )


# ---------------------------------------------------------------- SMS
def split_message(text, limit=1500):
    parts, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > limit and cur:
            parts.append(cur.rstrip())
            cur = ""
        cur += para + "\n"
    if cur.strip():
        parts.append(cur.rstrip())
    return parts


def send_sms(text):
    text = ascii_safe(text)
    for part in split_message(text):
        if SMS_PROVIDER == "twilio":
            sid, tok = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
            r = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                auth=(sid, tok),
                data={"From": os.environ["TWILIO_FROM"], "To": os.environ["MY_PHONE"], "Body": part},
                timeout=20,
            )
            r.raise_for_status()
        elif SMS_PROVIDER == "ntfy":
            r = requests.post(
                f"https://ntfy.sh/{os.environ['NTFY_TOPIC']}",
                data=part.encode("utf-8"), timeout=20,
            )
            r.raise_for_status()
        else:
            print("\n----- (console, not sent) -----\n" + part + "\n-------------------------------")
    log.info("message sent via %s (%d chars)", SMS_PROVIDER, len(text))


# ---------------------------------------------------------------- digest
def build_digest():
    now = datetime.now(TZ)
    header = f"News - {now.strftime('%a %b %-d')}" if os.name != "nt" else f"News - {now.strftime('%a %b %d')}"
    data = {r: fetch_region(r) for r in FEEDS}

    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("no ANTHROPIC_API_KEY: sending headline-only digest")
        lines = [header]
        for r, items in data.items():
            lines.append(f"\n{REGION_NAME[r].upper()}")
            seen = set()
            for it in items:
                if it["title"] in seen:
                    continue
                seen.add(it["title"])
                lines.append(f"- {it['title']}")
                if len(seen) >= ITEMS_PER_SECTION:
                    break
        return "\n".join(lines)

    prompt = f"""You write a morning news text message. Below are fresh headlines (with short snippets) from several outlets, grouped by country.

For each country, pick the {ITEMS_PER_SECTION} most important stories of the day. Rank by real significance and by how many outlets are covering the same story. Skip sports, celebrity gossip, lifestyle, and opinion unless truly major.

Write each pick as ONE short headline-style summary (max ~170 characters) that says what happened and why it matters. Use only facts present in the items below. Do not invent details. Translate Brazilian items into English.

Output rules: plain text only, no markdown, no emojis, no links, no quotes around items, plain ASCII punctuation. Use exactly this layout:

{header}

UNITED STATES
1) ...
2) ...

BRAZIL
1) ...
2) ...

UNITED STATES ITEMS:
{numbered(data['US'][:40])}

BRAZIL ITEMS:
{numbered(data['BR'][:40])}"""
    return ask(prompt)


def send_digest():
    try:
        send_sms(build_digest())
    except Exception:
        log.exception("digest failed")


# ---------------------------------------------------------------- breaking
def in_quiet_hours(now):
    h = now.hour
    return (h >= QUIET_START or h < QUIET_END) if QUIET_START > QUIET_END else (QUIET_START <= h < QUIET_END)


def parse_json_block(text):
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {"alerts": []}


def check_breaking():
    try:
        st = load_state()
        now = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")

        new = []
        for r in FEEDS:
            for it in fetch_region(r, per_feed=20):
                key = it["id"]
                if key not in st["seen"]:
                    st["seen"][key] = now.timestamp()
                    new.append(it)

        # First run: remember what's already out there, don't alert on it.
        if not st["seeded"]:
            st["seeded"] = True
            save_state(st)
            log.info("seeded %d existing items; alerts start next check", len(new))
            return

        if not new:
            save_state(st)
            return
        if in_quiet_hours(now):
            log.info("quiet hours: %d new items folded into the morning digest", len(new))
            save_state(st)
            return
        used = st["alert_counts"].get(today, 0)
        if used >= MAX_ALERTS_PER_DAY:
            save_state(st)
            return
        if not os.getenv("ANTHROPIC_API_KEY"):
            log.warning("breaking alerts need ANTHROPIC_API_KEY; skipping")
            save_state(st)
            return

        recent = "\n".join("- " + a["text"] for a in st["alerts"][-10:]) or "(none)"
        new = new[:60]
        prompt = f"""You are the editor of a breaking-news text alert service for one reader who follows the United States and Brazil.

Below are NEW headlines that just appeared. Decide whether any represent genuinely MAJOR breaking news. Qualifies: mass-casualty events, attacks, major disasters, war or major military escalation involving the US or Brazil, sudden death/resignation/removal/indictment of a head of state or top official, major Supreme Court or election outcomes, national emergencies, market-crashing events. Does NOT qualify: routine politics, ongoing stories with no new major development, sports, celebrity, business as usual, opinion, features.

Be strict. Most checks should return no alerts. Merge items about the same event into one alert. Do NOT alert on events already covered by these recent alerts:
{recent}

For each alert write ONE text-message summary, max ~280 characters, starting with "BREAKING (US):" or "BREAKING (Brazil):". Say what happened and why it matters, using only facts in the items. English, plain ASCII, no links, no emojis. Return ONLY JSON:
{{"alerts": [{{"text": "..."}}]}}
Return {{"alerts": []}} if nothing qualifies.

NEW ITEMS:
{numbered(new)}"""
        result = parse_json_block(ask(prompt, max_tokens=800))
        for a in result.get("alerts", [])[: MAX_ALERTS_PER_DAY - used]:
            send_sms(a["text"])
            st["alerts"].append({"ts": now.isoformat(), "text": a["text"]})
            st["alert_counts"][today] = st["alert_counts"].get(today, 0) + 1
        save_state(st)
    except Exception:
        log.exception("breaking check failed")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-sms", action="store_true")
    ap.add_argument("--digest-now", action="store_true")
    ap.add_argument("--check-now", action="store_true")
    args = ap.parse_args()

    if args.test_sms:
        send_sms("newsbot test: if you can read this, texting works.")
        return
    if args.digest_now:
        send_digest()
        return
    if args.check_now:
        check_breaking()
        return

    sched = BlockingScheduler(timezone=TZ)
    # misfire_grace_time: if the computer was asleep at 9:00, still send when it wakes (within 3 hours)
    sched.add_job(send_digest, CronTrigger(hour=DIGEST_HOUR, minute=0, timezone=TZ),
                  misfire_grace_time=3 * 3600, coalesce=True, id="digest")
    sched.add_job(check_breaking, IntervalTrigger(minutes=POLL_MINUTES),
                  next_run_time=datetime.now(TZ), misfire_grace_time=300, coalesce=True, id="breaking")
    log.info("newsbot running: digest at %02d:00 %s, breaking check every %d min via %s",
             DIGEST_HOUR, TZ.key, POLL_MINUTES, SMS_PROVIDER)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("stopped")


if __name__ == "__main__":
    main()
