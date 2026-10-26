#!/usr/bin/env python3
"""Watch arrampicata-arco.com/novita.html and email when Heinz Grill adds a route.

Usage:
    python check_routes.py          # normal run (used by GitHub Actions)
    python check_routes.py --test   # print a sample email, send nothing, save nothing
"""
import json
import os
import random
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

URL = "https://www.arrampicata-arco.com/novita.html"
STATE_FILE = Path(__file__).with_name("seen_routes.json")

# Links on the page that are navigation, not routes
IGNORE = {"index.html", "novita.html", "links.html", "sitemap.html", "contact.html"}

# False = skip entries that only describe improvements to existing routes
# (e.g. "added rings to the belays", "an old 1913 route was improved").
# True  = email about those too.
INCLUDE_UPDATES = False
UPDATE_WORDS = ("migliorat", "anelli", "chiod", "vecchia via", "ripulit", "sistemat", "rinnovat")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", SMTP_USER)
FORCE_SEND = os.environ.get("FORCE_SEND", "").lower() == "true"

HEADLINES = [
    "The Grill is on fire!",
    "Barbecue season is on!",
    "Heinz Grill sharpened his drill!",
]

CLOSERS = [
    "Namaste at the anchor.",
    "Viva la speranza... Gaaaaacho!",
    "Your Master is calling you.",
]


# ---------- scraping ----------

def clean(s):
    return " ".join(s.split())


def route_title(link_text):
    t = clean(link_text)
    m = re.search(r'["“«]([^"”»]+)["”»]', t)
    if m:
        name = m.group(1).strip()
        if re.match(r"vi[ae]\b", name, re.I):  # name already starts with "via"
            return name[0].upper() + name[1:]
        return f'Via "{name}"'
    return t[:1].upper() + t[1:]


def area_of(li):
    parts = []
    for node in li.children:
        if getattr(node, "name", None) in ("br", "a"):
            break
        parts.append(str(node))
    area = clean("".join(parts)).split(":")[0].strip(" :-")
    return area if 0 < len(area) <= 80 else None


def date_of(li):
    box = li.find_parent("div", class_="ottagono")
    if box:
        for s in box.find_all(string=True, recursive=False):
            if s.strip():
                return clean(s)
    return None


def is_update(li):
    text = li.get_text(" ").lower()
    return any(w in text for w in UPDATE_WORDS) and "nuov" not in text


def fetch_routes():
    r = requests.get(URL, timeout=30, headers={"User-Agent": "route-watcher/1.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")  # the page's own UTF-8 declaration wins

    routes = {}
    for li in soup.select("#content li"):
        area, date, update = area_of(li), date_of(li), is_update(li)
        for a in li.find_all("a", href=True):
            full = urljoin(URL, a["href"]).split("#")[0]
            if not full.endswith(".html") or full.rsplit("/", 1)[-1] in IGNORE:
                continue
            routes.setdefault(full, {
                "title": route_title(a.get_text()),
                "area": area,
                "date": date,
                "update": update,
            })
    return routes


# ---------- email ----------

def build_email(new):
    headline = random.choice(HEADLINES)
    closer = random.choice(CLOSERS)
    if len(new) == 1:
        intro = "Something freshly bolted appeared on arrampicata-arco.com"
    else:
        intro = "Several freshly bolted things appeared on arrampicata-arco.com"

    def block(url, info):
        line = info["title"] + (f" at {info['area']}" if info["area"] else "")
        return f"{line}\n{url}"

    routes = "\n\n".join(block(u, i) for u, i in new.items())
    return headline, f"{headline}\n\n{intro}\n\n{routes}\n\n{closer}\n"


def send_mail(new, dry_run=False):
    subject, body = build_email(new)
    if dry_run:
        print(f"Subject: {subject}\n\n{body}")
        return
    if not (SMTP_USER and SMTP_PASS and MAIL_TO):
        raise SystemExit("SMTP_USER, SMTP_PASS and MAIL_TO must be set.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = MAIL_TO
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context()) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


# ---------- main ----------

def wanted(info):
    return INCLUDE_UPDATES or not info["update"]


def main():
    current = fetch_routes()
    if not current:
        raise SystemExit("No routes found - the page layout may have changed.")

    if "--test" in sys.argv:
        reportable = {u: i for u, i in current.items() if wanted(i)}
        skipped = [i["title"] for i in current.values() if not wanted(i)]
        print(f"Found {len(current)} links; {len(reportable)} count as routes.")
        print(f"Skipped as updates: {skipped or 'none'}\n")
        send_mail(dict(list(reportable.items())[:2]), dry_run=True)
        return

    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps(sorted(current), indent=2))
        print(f"Baseline saved with {len(current)} links.")
        if FORCE_SEND:
            to_report = {u: i for u, i in current.items() if wanted(i)}
            if to_report:
                send_mail(to_report)
                print(f"Forced email sent for {len(to_report)} route(s).")
            else:
                print("No reportable routes in forced-send mode.")
        else:
            print("No email sent on first run.")
        return

    seen = set(json.loads(STATE_FILE.read_text()))
    unseen = {u: i for u, i in current.items() if u not in seen}
    if not unseen:
        print("Nothing new.")
        return

    to_report = {u: i for u, i in unseen.items() if wanted(i)}
    if to_report:
        send_mail(to_report)  # if this raises, state is not saved and we retry next week
        print(f"Emailed {len(to_report)} new route(s).")
    else:
        print(f"{len(unseen)} new link(s), all updates - no email.")

    STATE_FILE.write_text(json.dumps(sorted(seen | set(current)), indent=2))


if __name__ == "__main__":
    main()
