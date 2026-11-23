#!/usr/bin/env python3
"""Alert when a non-3D session on DATE has enough free SUPER LUX seats.

Run once per check (GitHub Actions calls it every 15 minutes).
  watch.py                 normal check for DATE
  watch.py --date 17122026 check another date (for testing)
  watch.py --dry-run       print the result, no Telegram message, no state change

Alerts go to Telegram: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

MOVIE_URL = "https://multiplex.ua/movie/356405"
CINEMA_ID = "0000000024"  # Любава, Черкаси
DATE = "19122026"  # ddmmyyyy, as the site writes it
SEAT_TYPE = "SUPER LUX"
SEATS_NEEDED = 4

HERE = pathlib.Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

SESSION_RE = re.compile(
    r'<div class="ns(?P<locked> locked)?\s*"(?P<attrs>[^>]*)>\s*'
    r'<p class="time"><span>(?P<time>[^<]+)</span>'
)
SEAT_RE = re.compile(r'<div[^>]*class="seat-[^>]*>')


class SiteChanged(Exception):
    """The page loaded but no longer looks the way this script expects."""


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            # Without this the site picks the city from the IP address.
            "Cookie": f"cinemaN={CINEMA_ID}",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def attr(tag, name):
    match = re.search(rf"{name}=['\"]?([^'\"\s>]*)", tag)
    return match.group(1) if match else None


def sessions_on(html, date):
    """Open non-3D sessions of our cinema on `date`: [(session_id, time)]."""
    all_sessions = list(SESSION_RE.finditer(html))
    if not all_sessions:
        raise SiteChanged("no sessions at all on the movie page")
    found = []
    for match in all_sessions:
        attrs = match["attrs"]
        cinema, _, session = (attr(attrs, "data-id") or "").partition("-")
        if (
            cinema == CINEMA_ID
            and attr(attrs, "data-anchor") == date
            and not match["locked"]  # sold out
            and attr(attrs, "data-3d") != "true"
        ):
            found.append((session, match["time"].strip()))
    return found


def free_seats(seatplan_html):
    """Free seats of SEAT_TYPE as labels like '1 Ряд, 3 Місце (500 грн)'."""
    seats = SEAT_RE.findall(seatplan_html)
    if not seats:
        raise SiteChanged("no seats on the seat-map page")
    free = []
    for seat in seats:
        ticket_type = re.search(r'data-ticket-type="([^"]*)"', seat)
        if (
            ticket_type
            and ticket_type.group(1) == SEAT_TYPE
            and attr(seat, "data-status") == "0"
        ):
            label = re.search(r'data-tippy="([^"\n]*)', seat)
            free.append(
                f"{label.group(1).strip() if label else '?'} "
                f"({attr(seat, 'data-price')} грн)"
            )
    return free


def check(date):
    """Sessions that satisfy the request: [{id, time, url, seats}]."""
    hits = []
    for session, time in sessions_on(fetch(MOVIE_URL), date):
        url = f"https://new.multiplex.ua/order/cinema/{CINEMA_ID}/session/{session}"
        seats = free_seats(fetch(url))
        log(f"  {time} session {session}: {len(seats)} free {SEAT_TYPE}")
        if len(seats) >= SEATS_NEEDED:
            hits.append({"id": session, "time": time, "url": url, "seats": seats})
    return hits


def notify(text):
    """Send a Telegram message; raises if it cannot be delivered."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(
        {
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode()
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30
        ):
            pass
    except urllib.error.HTTPError as error:
        # Re-raise without the URL: it contains the bot token.
        raise RuntimeError(
            f"Telegram refused the message: {error.code} {error.read().decode()}"
        ) from None


def log(message):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=DATE, help="ddmmyyyy")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    show_day = datetime.datetime.strptime(args.date, "%d%m%Y").date()
    if datetime.date.today() > show_day:
        log(f"{args.date} has passed, nothing to watch")
        return 0

    state = load_state()
    pretty_date = f"{args.date[:2]}.{args.date[2:4]}"
    try:
        hits = check(args.date)
    except SiteChanged as error:
        # Never fail silently: a broken watcher looks the same as "no seats yet".
        log(f"SITE CHANGED: {error}")
        if not args.dry_run and state.get("status") != "broken":
            notify(f"Multiplex watcher is broken: {error}")
            STATE_FILE.write_text(json.dumps({"status": "broken"}))
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        log(f"network error, will retry next run: {error}")
        return 1

    status = "hit:" + ",".join(hit["id"] for hit in hits) if hits else "none"
    log(f"{pretty_date}: {len(hits)} matching session(s)")
    for hit in hits:
        log(f"  MATCH {hit['time']} {hit['url']} -> {'; '.join(hit['seats'])}")
    if args.dry_run:
        return 0

    # Alert when the set of matching sessions changes, not on every run.
    if hits and state.get("status") != status:
        lines = [f"Multiplex {pretty_date}: {SEAT_TYPE} is available (not 3D)"]
        for hit in hits:
            lines.append(f"\n{hit['time']} - {len(hit['seats'])} free:")
            lines.extend(hit["seats"])
            lines.append(hit["url"])
        notify("\n".join(lines))
    STATE_FILE.write_text(json.dumps({"status": status}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
