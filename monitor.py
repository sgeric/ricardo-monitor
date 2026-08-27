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
Ricardo may change its HTML or anti-bot rules.
This script uses public search pages and does not log in or buy/bid on anything.
"""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.ricardo.ch"
STATE_FILE = Path(os.getenv("STATE_FILE", "seen.json"))


SEARCHES = {
    "Sony a6xxx": [
        "Sony a6000",
        "Sony a6100",
        "Sony a6300",
        "Sony a6400",
        "Sony a6500",
        "Sony a6600",
        "Sony a6700",
    ],

    "Sony a7 II / III / IV": [
        "Sony a7 II",
        "Sony a7III",
        "Sony a7 III",
        "Sony a7 IV",
        "Sony Alpha 7 II",
        "Sony Alpha 7 III",
        "Sony Alpha 7 IV",
    ],

    "Samyang lenses": [
        "Samyang Sony E",
        "Samyang E-Mount",
        "Samyang FE",
    ],

    "Sigma Sony E lenses": [
        "Sigma Sony E",
        "Sigma E-Mount",
        "Sigma Sony FE",
    ],

    "DJI Air": [
        "DJI Air 2",
        "DJI Air 2S",
        "DJI Air 3",
        "DJI Air 3S",
    ],

    "DJI Mini": [
        "DJI Mini 3",
        "DJI Mini 3 Pro",
        "DJI Mini 4",
        "DJI Mini 4 Pro",
        "DJI Mini 5",
        "DJI Mini 5 Pro",
    ],

    "DJI RS gimbals": [
        "DJI RS",
        "DJI RS 2",
        "DJI RS 3",
        "DJI RS 4",
        "DJI RSC 2",
    ],

    "DJI Osmo Action": [
        "DJI Osmo Action 3",
        "DJI Osmo Action 4",
        "DJI Osmo Action 5",
        "DJI Osmo Action 6",
    ],

    "DJI Pocket": [
        "DJI Pocket",
        "DJI Pocket 2",
        "DJI Pocket 3",
        "DJI Osmo Pocket",
    ],

    "Godox": [
        "Godox Blitz",
        "Godox Flash",
        "Godox Licht",
        "Godox Light",
        "Godox Softbox",
        "Godox AD200",
        "Godox AD300",
        "Godox AD400",
        "Godox AD600",
        "Godox V1",
        "Godox V860",
    ],

    "Rode microphones": [
        "Rode VideoMic",
        "RØDE VideoMic",
        "Rode Wireless",
        "RØDE Wireless",
        "Rode Wireless GO",
        "Rode Wireless PRO",
    ],
}


CAMERA_ACCESSORY_NEGATIVE = re.compile(
    r"\b("
    r"cage|smallrig|akku|battery|ladegerät|charger|griff|grip|"
    r"l.?bracket|winkel|strap|gurt|display|schutz|case|tasche|cover|"
    r"adapter|dummy battery|netzteil"
    r")\b",
    re.I,
)


LENS_NEGATIVE = re.compile(
    r"\b("
    r"adapter|deckel|cap|hood only|gegenlichtblende|filter only|"
    r"tasche|case|mount adapter"
    r")\b",
    re.I,
)


ARTICLE_RE = re.compile(
    r"/(?:de|fr|it)/a/[^\"'?]+-(\d{8,})/?"
)

PRICE_RE = re.compile(
    r"(?:CHF\s*)?(\d{1,3}(?:['’ ]\d{3})*(?:\.\d{2})?)"
)

BUY_NOW_RE = re.compile(
    r"Sofort\s*kaufen",
    re.I,
)

OFFER_RE = re.compile(
    r"Preis\s*vorschlagen",
    re.I,
)


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


def create_session() -> requests.Session:
    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0 Safari/537.36"
            ),
            "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml",
        }
    )

    return session


def search_url(query: str) -> str:
    return f"{BASE_URL}/de/s/{quote(query, safe='')}/"


def clean_text(text: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        html.unescape(text or ""),
    ).strip()


def likely_relevant(group: str, title: str) -> bool:
    title_lower = title.lower()

    if group.startswith("Sony a6") or group.startswith("Sony a7"):
        return not CAMERA_ACCESSORY_NEGATIVE.search(title)

    if "lenses" in group.lower():
        if LENS_NEGATIVE.search(title):
            return False

        if group.startswith("Sigma"):
            return bool(
                re.search(
                    r"\b(sony|e[- ]?mount|fe)\b",
                    title_lower,
                    re.I,
                )
            )

        return True

    if group == "DJI RS gimbals":
        return not re.search(
            r"\b("
            r"plate|platte|griff|handle|case|tasche|"
            r"motor only|cable|kabel"
            r")\b",
            title,
            re.I,
        )

    if group == "Rode microphones":
        return not re.search(
            r"\b("
            r"cable|kabel|windshield only|deadcat only|"
            r"adapter only|case only"
            r")\b",
            title,
            re.I,
        )

    return True


def extract_listing_blocks(
    page_html: str,
    group: str,
    query: str,
) -> list[Listing]:

    soup = BeautifulSoup(page_html, "html.parser")

    found: dict[str, Listing] = {}

    for anchor in soup.find_all("a", href=True):

        href = anchor["href"]

        article_match = ARTICLE_RE.search(href)

        if not article_match:
            continue

        article_id = article_match.group(1)

        url = urljoin(
            BASE_URL,
            href.split("?")[0],
        )

        node = anchor
        block_text = ""

        for _ in range(8):

            node = getattr(node, "parent", None)

            if node is None:
                break

            text = clean_text(
                node.get_text(
                    " ",
                    strip=True,
                )
            )

            if (
                BUY_NOW_RE.search(text)
                or OFFER_RE.search(text)
            ):
                block_text = text
                break

        if not block_text:
            continue

        buy_now = bool(
            BUY_NOW_RE.search(block_text)
        )

        if not buy_now:
            continue

        price_suggestion = bool(
            OFFER_RE.search(block_text)
        )

        title = clean_text(
            anchor.get_text(
                " ",
                strip=True,
            )
        )

        if len(title) < 4:
            title = clean_text(
                block_text.split("Sofort")[0]
            )[:180]

        if len(title) > 220:
            title = title[:217] + "..."

        if not likely_relevant(
            group,
            title,
        ):
            continue

        price = "Price not parsed"

        numbers = PRICE_RE.findall(
            block_text
        )

        if numbers:
            price = (
                "CHF "
                + numbers[-1].replace(
                    "’",
                    "'",
                )
            )

        listing = Listing(
            article_id=article_id,
            title=title or f"Ricardo article {article_id}",
            url=url,
            price=price,
            buy_now=True,
            price_suggestion=price_suggestion,
            group=group,
            query=query,
        )

        found[article_id] = listing

    return list(found.values())


def fetch_query(
    session: requests.Session,
    group: str,
    query: str,
) -> list[Listing]:

    url = search_url(query)

    response = session.get(
        url,
        timeout=30,
    )

    response.raise_for_status()

    lower = response.text.lower()

    if (
        len(response.text) < 5000
        or "captcha" in lower
    ):
        raise RuntimeError(
            f"Ricardo returned a possible challenge page for: {query}"
        )

    return extract_listing_blocks(
        response.text,
        group,
        query,
    )


def load_seen() -> set[str]:

    if not STATE_FILE.exists():
        return set()

    try:
        data = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

        return set(
            data.get(
                "seen_ids",
                [],
            )
        )

    except Exception:
        return set()


def save_seen(
    ids: Iterable[str],
) -> None:

    STATE_FILE.write_text(
        json.dumps(
            {
                "seen_ids": sorted(
                    set(ids)
                )
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def send_email(
    items: list[Listing],
) -> None:

    recipient = os.environ[
        "ALERT_EMAIL"
    ]

    host = os.environ[
        "SMTP_HOST"
    ]

    port = int(
        os.getenv(
            "SMTP_PORT",
            "587",
        )
    )

    username = os.environ[
        "SMTP_USERNAME"
    ]

    password = os.environ[
        "SMTP_PASSWORD"
    ]

    sender = os.getenv(
        "SMTP_FROM",
        username,
    )

    message = EmailMessage()

    message["Subject"] = (
        f"Ricardo: {len(items)} new photo/video "
        f"listing{'s' if len(items) != 1 else ''}"
    )

    message["From"] = sender
    message["To"] = recipient

    lines = [
        f"{len(items)} new matching Ricardo listing(s):",
        "",
    ]

    for item in items:

        offer_type = (
            "Buy Now + Price Suggestion"
            if item.price_suggestion
            else "Buy Now"
        )

        lines.extend(
            [
                item.title,
                f"{item.price} — {offer_type}",
                f"Category: {item.group}",
                f"Search: {item.query}",
                item.url,
                "",
            ]
        )

    lines.append(
        "Only listings not previously seen by the monitor are included."
    )

    message.set_content(
        "\n".join(lines)
    )

    with smtplib.SMTP(
        host,
        port,
        timeout=30,
    ) as smtp:

        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()

        smtp.login(
            username,
            password,
        )

        smtp.send_message(
            message
        )


def main() -> int:

    session = create_session()

    all_items: dict[str, Listing] = {}

    errors: list[str] = []

    for group, queries in SEARCHES.items():

        for query in queries:

            try:

                items = fetch_query(
                    session,
                    group,
                    query,
                )

                print(
                    f"{query}: "
                    f"{len(items)} matching listing(s)"
                )

                for item in items:
                    all_items[
                        item.article_id
                    ] = item

            except Exception as exc:

                errors.append(
                    f"{query}: {exc}"
                )

            time.sleep(0.8)

    if not all_items and errors:

        print(
            "No listings parsed."
        )

        print(
            "\n".join(errors)
        )

        return 2

    seen = load_seen()

    current_ids = set(
        all_items.keys()
    )

    new_items = [
        item
        for article_id, item
        in all_items.items()
        if article_id not in seen
    ]

    initial_mode = os.getenv(
        "INITIAL_MODE",
        "notify",
    ).lower()

    first_run = (
        not STATE_FILE.exists()
    )

    if (
        first_run
        and initial_mode == "baseline"
    ):

        save_seen(
            current_ids
        )

        print(
            f"Baseline created with "
            f"{len(current_ids)} listings."
        )

        print(
            "No email sent."
        )

        return 0

    if new_items:

        new_items.sort(
            key=lambda item: int(
                item.article_id
            ),
            reverse=True,
        )

        send_email(
            new_items
        )

        print(
            f"Sent alert for "
            f"{len(new_items)} new listing(s)."
        )

    else:

        print(
            "No new matching listings."
        )

    save_seen(
        seen | current_ids
    )

    if errors:

        print(
            f"\nNon-fatal search errors "
            f"({len(errors)}):",
            file=sys.stderr,
        )

        print(
            "\n".join(errors),
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
