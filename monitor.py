#!/usr/bin/env python3
"""
Ricardo.ch hourly monitor for selected photo/video gear.

- Searches only the configured terms.
- Keeps only listings that offer "Sofort kaufen" (Buy Now).
- "Sofort kaufen oder Preis vorschlagen" also matches.
- Deduplicates by Ricardo article ID.
- Sends one email containing all newly discovered matching listings.
- State is stored in seen.json.

Environment variables:
  ALERT_EMAIL       Recipient address
  SMTP_HOST         e.g. smtp.gmail.com or smtp.mail.yahoo.com
  SMTP_PORT         usually 587
  SMTP_USERNAME     SMTP account username/email
  SMTP_PASSWORD     SMTP app password
  SMTP_FROM         optional; defaults to SMTP_USERNAME
  INITIAL_MODE      "notify" (default) or "baseline"

IMPORTANT:
Ricardo may change its HTML or anti-bot rules. This script uses public search pages
and does not log in or buy/bid on anything.
"""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, asdict
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.ricardo.ch"
STATE_FILE = Path(os.getenv("STATE_FILE", "seen.json"))

# Exact watch list requested.
SEARCHES = {
    "Sony a6xxx": [
        "Sony a6000", "Sony a6100", "Sony a6300", "Sony a6400",
        "Sony a6500", "Sony a6600", "Sony a6700",
    ],
    "Sony a7 II / III / IV": [
        "Sony a7 II", "Sony a7III", "Sony a7 III", "Sony a7 IV",
        "Sony Alpha 7 II", "Sony Alpha 7 III", "Sony Alpha 7 IV",
    ],
    "Samyang lenses": [
        "Samyang Sony E", "Samyang E-Mount", "Samyang FE",
    ],
    "Sigma Sony E lenses": [
        "Sigma Sony E", "Sigma E-Mount", "Sigma Sony FE",
    ],
    "DJI Air": [
        "DJI Air 2", "DJI Air 2S", "DJI Air 3", "DJI Air 3S",
    ],
    "DJI Mini": [
        "DJI Mini 3 Pro", "DJI Mini 4 Pro", "DJI Mini 5 Pro",
        "DJI Mini 3", "DJI Mini 4", "DJI Mini 5",
    ],
    "DJI RS gimbals": [
        "DJI RS", "DJI RS 2", "DJI RS 3", "DJI RS 4",
        "DJI RSC 2",
    ],
    "DJI Osmo Action": [
        "DJI Osmo Action 3", "DJI Osmo Action 4",
        "DJI Osmo Action 5", "DJI Osmo Action 6",
    ],
    "DJI Pocket": [
        "DJI Pocket", "DJI Osmo Pocket", "DJI Pocket 2", "DJI Pocket 3",
    ],
    "Godox": [
        "Godox Blitz", "Godox Flash", "Godox Licht", "Godox Light",
        "Godox Softbox", "Godox AD200", "Godox AD300", "Godox AD400",
        "Godox AD600", "Godox V1", "Godox V860",
    ],
    "Rode microphones": [
        "Rode VideoMic", "RØDE VideoMic", "Rode Wireless",
        "RØDE Wireless", "Rode Wireless GO", "Rode Wireless PRO",
    ],
}

# Avoid obvious accessories/false positives for camera body searches.
CAMERA_ACCESSORY_NEGATIVE = re.compile(
    r"\b(cage|smallrig|akku|battery|ladegerät|charger|griff|grip|"
    r"l.?bracket|winkel|strap|gurt|display|schutz|case|tasche|cover|"
    r"adapter|dummy battery|netzteil)\b",
    re.I,
)

# Lens searches should be actual lenses, not adapters/caps/etc.
LENS_NEGATIVE = re.compile(
    r"\b(adapter|deckel|cap|hood only|gegenlichtblende|filter only|"
    r"tasche|case|mount adapter)\b",
    re.I,
)

ARTICLE_RE = re.compile(r"/(?:de|fr|it)/a/[^\"'?]+-(\d{8,})/?")
PRICE_RE = re.compile(r"(?:CHF\s*)?(\d{1,3}(?:['’ ]\d{3})*(?:\.\d{2})?)")
BUY_NOW_RE = re.compile(r"Sofort\s*kaufen", re.I)
OFFER_RE = re.compile(r"Preis\s*vorschlagen", re.I)


@dataclass(frozen=True)
class Listing:
    article_id: str
    title: str
    url: str
    price: str
    buy_now: bool
    price_suggestion: bool
    group: str
    query: str


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/152 Safari/537.36"
        ),
        "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml",
    })
    return s


def search_url(query: str) -> str:
    # Ricardo's public /de/s/<query>/ search endpoint.
    return f"{BASE_URL}/de/s/{quote(query, safe='')}/"


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def likely_relevant(group: str, title: str) -> bool:
    t = title.lower()

    if group.startswith("Sony a6") or group.startswith("Sony a7"):
        return not CAMERA_ACCESSORY_NEGATIVE.search(title)

    if "lenses" in group.lower():
        if LENS_NEGATIVE.search(title):
            return False
        # Sigma must explicitly look Sony/E/FE related, otherwise Sigma Canon/L/etc.
        if group.startswith("Sigma"):
            return bool(re.search(r"\b(sony|e[- ]?mount|fe)\b", t, re.I))
        return True

    if group == "DJI RS gimbals":
        return not re.search(r"\b(plate|platte|griff|handle|case|tasche|motor only|cable|kabel)\b", t, re.I)

    if group == "Rode microphones":
        return not re.search(r"\b(cable|kabel|windshield only|deadcat only|adapter only|case only)\b", t, re.I)

    return True


def extract_listing_blocks(page_html: str, group: str, query: str) -> list[Listing]:
    soup = BeautifulSoup(page_html, "html.parser")
    found: dict[str, Listing] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = ARTICLE_RE.search(href)
        if not m:
            continue

        article_id = m.group(1)
        url = urljoin(BASE_URL, href.split("?")[0])

        # Ricardo cards vary. Walk up a few ancestors and choose the first block
        # containing offer-type text. This makes the parser less brittle.
        node = a
        block_text = ""
        for _ in range(7):
            node = getattr(node, "parent", None)
            if node is None:
                break
            txt = clean_text(node.get_text(" ", strip=True))
            if BUY_NOW_RE.search(txt) or OFFER_RE.search(txt):
                block_text = txt
                break

        if not block_text:
            continue

        # Requirement: Buy Now OR Buy Now + Price Suggestion.
        buy_now = bool(BUY_NOW_RE.search(block_text))
        if not buy_now:
            continue

        price_suggestion = bool(OFFER_RE.search(block_text))

        # Prefer anchor text as title; fallback to a concise piece of block text.
        title = clean_text(a.get_text(" ", strip=True))
        if len(title) < 4:
            title = clean_text(block_text.split("Sofort")[0])[:180]
        if len(title) > 220:
            title = title[:217] + "..."

        if not likely_relevant(group, title):
            continue

        # Find the most plausible CHF/number occurrence near the offer label.
        price = "Price not parsed"
        nums = PRICE_RE.findall(block_text)
        if nums:
            # Usually the card's final monetary amount near "Sofort kaufen" is the buy-now price.
            price = "CHF " + nums[-1].replace("’", "'")

        item = Listing(
            article_id=article_id,
            title=title or f"Ricardo article {article_id}",
            url=url,
            price=price,
            buy_now=True,
            price_suggestion=price_suggestion,
            group=group,
            query=query,
        )
        found[article_id] = item

    return list(found.values())


def fetch_query(s: requests.Session, group: str, query: str) -> list[Listing]:
    url = search_url(query)
    r = s.get(url, timeout=30)
    r.raise_for_status()

    # A basic anti-bot/challenge sanity check.
    lower = r.text.lower()
    if len(r.text) < 5000 or "captcha" in lower:
        raise RuntimeError(f"Ricardo returned a possible challenge page for: {query}")

    return extract_listing_blocks(r.text, group, query)


def load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen_ids", []))
    except Exception:
        return set()


def save_seen(ids: Iterable[str]) -> None:
    STATE_FILE.write_text(
        json.dumps({"seen_ids": sorted(set(ids))}, indent=2),
        encoding="utf-8",
    )


def send_email(items: list[Listing]) -> None:
    recipient = os.environ["ALERT_EMAIL"]
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.getenv("SMTP_FROM", username)

    msg = EmailMessage()
    msg["Subject"] = f"Ricardo: {len(items)} new photo/video listing{'s' if len(items) != 1 else ''}"
    msg["From"] = sender
    msg["To"] = recipient

    lines = [
        f"{len(items)} new matching Ricardo listing(s):",
        "",
    ]

    for x in items:
        offer = "Buy Now + Price Suggestion" if x.price_suggestion else "Buy Now"
        lines += [
            f"{x.title}",
            f"{x.price} — {offer}",
            f"Category: {x.group}",
            x.url,
            "",
        ]

    lines.append("Only listings not previously seen by the monitor are included.")
    msg.set_content("\n".join(lines))

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(username, password)
        smtp.send_message(msg)


def main() -> int:
    s = session()
    all_items: dict[str, Listing] = {}
    errors: list[str] = []

    for group, queries in SEARCHES.items():
        for query in queries:
            try:
                items = fetch_query(s, group, query)
                for item in items:
                    all_items[item.article_id] = item
            except Exception as exc:
                errors.append(f"{query}: {exc}")
            time.sleep(0.8)  # Be polite to Ricardo; ~50 queries is still lightweight.

    if not all_items and errors:
        print("No listings parsed; errors:")
        print("\n".join(errors))
        return 2

    seen = load_seen()
    current_ids = set(all_items)
    new_items = [x for aid, x in all_items.items() if aid not in seen]

    # First-run behavior:
    # INITIAL_MODE=baseline -> record everything, send nothing.
    # INITIAL_MODE=notify   -> email all currently matching items.
    initial_mode = os.getenv("INITIAL_MODE", "notify").lower()
    first_run = not STATE_FILE.exists()

    if first_run and initial_mode == "baseline":
        save_seen(current_ids)
        print(f"Baseline created with {len(current_ids)} listings. No email sent.")
        return 0

    if new_items:
        new_items.sort(key=lambda x: int(x.article_id), reverse=True)
        send_email(new_items)
        print(f"Sent alert for {len(new_items)} new listing(s).")
    else:
        print("No new matching listings.")

    # Keep every ID ever observed, so expired/relisted pages do not trigger accidentally.
    save_seen(seen | current_ids)

    if errors:
        print(f"\nNon-fatal search errors ({len(errors)}):", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
