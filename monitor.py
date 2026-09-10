#!/usr/bin/env python3

import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


BASE_URL = "https://www.ricardo.ch"
STATE_FILE = Path("seen.json")

# Broader searches = fewer requests to Ricardo.
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
    "Sony a7": [
        "Sony a7 II",
        "Sony a7 III",
        "Sony a7 IV",
    ],
    "Samyang lenses": [
        "Samyang Sony E",
    ],
    "Sigma lenses": [
        "Sigma Sony E",
    ],
    "DJI Air": [
        "DJI Air",
    ],
    "DJI Mini": [
        "DJI Mini",
    ],
    "DJI RS": [
        "DJI RS",
    ],
    "DJI Osmo Action": [
        "DJI Osmo Action",
    ],
    "DJI Pocket": [
        "DJI Pocket",
    ],
    "Godox": [
        "Godox",
    ],
    "Rode": [
        "Rode",
    ],
}


ARTICLE_RE = re.compile(
    r"/(?:de|fr|it)/a/[^\"'?]+-(\d{8,})/?",
    re.I,
)

BUY_NOW_RE = re.compile(
    r"Sofort\s*kaufen",
    re.I,
)

OFFER_RE = re.compile(
    r"Preis\s*vorschlagen",
    re.I,
)

PRICE_RE = re.compile(
    r"(?:CHF\s*)?(\d{1,3}(?:['’ ]\d{3})*(?:\.\d{2})?)"
)


@dataclass(frozen=True)
class Listing:
    article_id: str
    title: str
    url: str
    price: str
    price_suggestion: bool
    group: str
    query: str


def search_url(query):
    return f"{BASE_URL}/de/s/{quote(query, safe='')}/"


def clean_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def likely_relevant(group, title):
    t = title.lower()

    accessory_words = [
        "cage",
        "smallrig",
        "akku",
        "battery",
        "ladegerät",
        "charger",
        "l-bracket",
        "strap",
        "gurt",
        "cover",
        "case only",
        "adapter only",
    ]

    if group in ("Sony a6xxx", "Sony a7"):
        if any(x in t for x in accessory_words):
            return False

    if group == "DJI RS":
        negatives = [
            "plate",
            "platte",
            "cable",
            "kabel",
            "case only",
            "handle only",
        ]

        if any(x in t for x in negatives):
            return False

    if group == "Rode":
        wanted = [
            "videomic",
            "wireless",
        ]

        return any(x in t for x in wanted)

    if group == "Sigma lenses":
        return (
            "sony" in t
            or "e-mount" in t
            or " e " in t
            or " fe " in t
        )

    if group == "Samyang lenses":
        return (
            "sony" in t
            or "e-mount" in t
            or " e " in t
            or " fe " in t
        )

    return True


def parse_page(html, group, query):
    soup = BeautifulSoup(html, "html.parser")

    found = {}

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")

        match = ARTICLE_RE.search(href)

        if not match:
            continue

        article_id = match.group(1)

        node = anchor
        block_text = ""

        # Walk upwards until we reach the listing card.
        for _ in range(10):
            node = getattr(node, "parent", None)

            if node is None:
                break

            text = clean_text(
                node.get_text(" ", strip=True)
            )

            if BUY_NOW_RE.search(text):
                block_text = text
                break

        # We ONLY want Buy Now listings.
        if not block_text:
            continue

        if not BUY_NOW_RE.search(block_text):
            continue

        title = clean_text(
            anchor.get_text(" ", strip=True)
        )

        if len(title) < 4:
            continue

        if len(title) > 220:
            title = title[:217] + "..."

        if not likely_relevant(group, title):
            continue

        price_suggestion = bool(
            OFFER_RE.search(block_text)
        )

        numbers = PRICE_RE.findall(block_text)

        if numbers:
            price = (
                "CHF "
                + numbers[-1].replace("’", "'")
            )
        else:
            price = "Price not parsed"

        url = urljoin(
            BASE_URL,
            href.split("?")[0],
        )

        found[article_id] = Listing(
            article_id=article_id,
            title=title,
            url=url,
            price=price,
            price_suggestion=price_suggestion,
            group=group,
            query=query,
        )

    return list(found.values())


def load_seen():
    if not STATE_FILE.exists():
        return set()

    try:
        data = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

        return set(
            data.get("seen_ids", [])
        )

    except Exception:
        return set()


def save_seen(ids):
    STATE_FILE.write_text(
        json.dumps(
            {
                "seen_ids": sorted(ids)
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def send_email(items):
    recipient = os.environ["ALERT_EMAIL"]

    host = os.environ["SMTP_HOST"]

    port = int(
        os.getenv("SMTP_PORT", "587")
    )

    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]

    sender = os.getenv(
        "SMTP_FROM",
        username,
    )

    message = EmailMessage()

    message["Subject"] = (
        f"Ricardo: {len(items)} new listing"
        f"{'s' if len(items) != 1 else ''}"
    )

    message["From"] = sender
    message["To"] = recipient

    lines = [
        f"{len(items)} new Ricardo listing(s):",
        "",
    ]

    for item in items:
        offer = (
            "Buy Now + Price Suggestion"
            if item.price_suggestion
            else "Buy Now"
        )

        lines.extend(
            [
                item.title,
                f"{item.price} — {offer}",
                f"Category: {item.group}",
                item.url,
                "",
            ]
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

        smtp.send_message(message)


def main():
    all_items = {}
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            locale="de-CH",
            viewport={
                "width": 1440,
                "height": 1000,
            },
        )

        page = context.new_page()

        for group, queries in SEARCHES.items():
            for query in queries:
                print(f"\nChecking: {query}")

                try:
                    response = page.goto(
                        search_url(query),
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )

                    if response is None:
                        raise RuntimeError(
                            "No HTTP response"
                        )

                    print(
                        f"{query}: HTTP "
                        f"{response.status}"
                    )

                    if response.status == 403:
                        raise RuntimeError(
                            "Ricardo returned HTTP 403"
                        )

                    if response.status >= 400:
                        raise RuntimeError(
                            f"HTTP {response.status}"
                        )

                    page.wait_for_timeout(2500)

                    html = page.content()

                    lower = html.lower()

                    if "captcha" in lower:
                        raise RuntimeError(
                            "Possible CAPTCHA/challenge page"
                        )

                    items = parse_page(
                        html,
                        group,
                        query,
                    )

                    print(
                        f"{query}: "
                        f"{len(items)} matching "
                        f"Buy Now listing(s)"
                    )

                    for item in items:
                        all_items[
                            item.article_id
                        ] = item

                except Exception as exc:
                    error = (
                        f"{query}: {exc}"
                    )

                    errors.append(error)
                    print(error)

                # Be polite to Ricardo.
                time.sleep(2)

        browser.close()

    # Important:
    # Don't overwrite seen.json when Ricardo blocked everything.
    if not all_items:
        print(
            "\nNo matching listings were obtained.",
            file=sys.stderr,
        )

        if errors:
            print(
                "\n".join(errors),
                file=sys.stderr,
            )

        return 2

    seen = load_seen()

    current_ids = set(
        all_items.keys()
    )

    first_run = not STATE_FILE.exists()

    initial_mode = os.getenv(
        "INITIAL_MODE",
        "notify",
    ).lower()

    if first_run and initial_mode == "baseline":
        save_seen(current_ids)

        print(
            f"\nBaseline created with "
            f"{len(current_ids)} listings."
        )

        print("No email sent.")

        return 0

    new_items = [
        item
        for article_id, item
        in all_items.items()
        if article_id not in seen
    ]

    if new_items:
        new_items.sort(
            key=lambda item: int(
                item.article_id
            ),
            reverse=True,
        )

        send_email(new_items)

        print(
            f"\nEMAIL SENT: "
            f"{len(new_items)} new listing(s)."
        )

    else:
        print(
            "\nNo new matching listings."
        )

    save_seen(
        seen | current_ids
    )

    if errors:
        print(
            f"\nThere were "
            f"{len(errors)} search errors.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
