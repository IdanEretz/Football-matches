"""
Scrape football match schedules for Teddy Stadium (Jerusalem) and generate an ICS calendar.

Data sources:
  - Beitar Jerusalem: beitarfc.co.il (official site)
  - Hapoel Jerusalem: hjfc.co.il (fan club site)

Only future home matches at Teddy Stadium are included.
"""

import hashlib
import re
import sys
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from icalendar import Calendar, Event
from pathlib import Path

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
MATCH_DURATION = timedelta(hours=2, minutes=30)
DEFAULT_HOUR, DEFAULT_MINUTE = 20, 30  # default kickoff when time is TBD
OUTPUT_DIR = Path(__file__).parent / "docs"

# Derby keywords — a match between these two is always at Teddy
BEITAR_KW = "בית"
HAPOEL_JLM_KW = "הפועל ירושלים"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}


def fetch_beitar_matches() -> list[dict]:
    """Scrape upcoming home matches from Beitar Jerusalem's official site."""
    from bs4 import BeautifulSoup

    url = "https://www.beitarfc.co.il/%D7%9E%D7%A9%D7%97%D7%A7%D7%99%D7%9D/"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    matches = []
    now = datetime.now(tz=ISRAEL_TZ)

    # Debug: count total game items found
    total_items = len(soup.find_all(class_="game_list_item"))
    print(f"    DEBUG: Found {total_items} game_list_item elements on Beitar site")

    for item in soup.find_all(class_="game_list_item"):
        teams_div = item.find(class_="teams_names")
        if not teams_div:
            continue

        home_div = teams_div.find(class_="home")
        away_div = teams_div.find(class_="away")
        home_name = home_div.get_text(strip=True) if home_div else ""
        away_name = away_div.get_text(strip=True) if away_div else ""

        # Include if Beitar is home, OR if it's a Jerusalem derby (always at Teddy)
        is_beitar_home = BEITAR_KW in home_name
        is_derby = (BEITAR_KW in home_name and HAPOEL_JLM_KW in away_name) or \
                   (HAPOEL_JLM_KW in home_name and BEITAR_KW in away_name)
        if not is_beitar_home and not is_derby:
            continue

        # Parse date from game_info text: "RR DD/MM/YY -> HH:MM"
        info = item.find(class_="game_info")
        info_text = info.get_text(strip=True) if info else ""
        date_match = re.search(r"(\d{2}/\d{2}/\d{2})\s*->\s*(\d{2}:\d{2})", info_text)
        if not date_match:
            continue

        date_part, time_part = date_match.groups()
        day, month, year = date_part.split("/")
        hour, minute = map(int, time_part.split(":"))
        # Treat obviously wrong times (e.g. 01:59) as TBD → default to 20:30
        if hour < 10:
            hour, minute = DEFAULT_HOUR, DEFAULT_MINUTE
        match_dt = datetime(
            2000 + int(year), int(month), int(day), hour, minute,
            tzinfo=ISRAEL_TZ,
        )

        if match_dt < now:
            continue

        matches.append({
            "home_team": home_name,
            "away_team": away_name,
            "datetime": match_dt,
            "venue": "Teddy Stadium",
            "source": "beitar",
        })

    return matches


def fetch_hapoel_matches() -> list[dict]:
    """Scrape upcoming home matches from Hapoel Jerusalem's site.

    Page structure (upcoming section):
      time, venue, guest (אורחת), home (מארחת), date
    Hapoel home games at Teddy have venue = "טדי".
    """
    from bs4 import BeautifulSoup

    url = "https://www.hjfc.co.il/schedule"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    text = soup.get_text(separator="\n", strip=True)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    matches = []
    now = datetime.now(tz=ISRAEL_TZ)

    # Find the "משחקים קרובים" (upcoming matches) section
    try:
        start_idx = lines.index("משחקים קרובים")
    except ValueError:
        print("  Warning: could not find upcoming matches section on Hapoel site")
        return []

    # Skip past the header row: שעה, מגרש, אורחת, מארחת, תאריך
    i = start_idx + 1
    while i < len(lines) and lines[i] in ("שעה", "מגרש", "אורחת", "מארחת", "תאריך"):
        i += 1

    # Parse match rows: each match is a group of 4-5 values
    # With venue: time, venue, guest, home, date
    # Without venue: guest, home, date (time/venue may be missing for TBD matches)
    while i < len(lines):
        line = lines[i]

        # Stop at the next section header
        if line in ("משחקים שהסתיימו", "שעה", "תוצאה"):
            break
        # Stop at footer-like repeated headers
        if line in ("אורחת", "מארחת", "תאריך"):
            break

        # Try to detect a date line (DD/MM/YYYY)
        date_match = re.match(r"(\d{2}/\d{2}/\d{4})$", line)
        if date_match:
            # Walk backwards to collect the match info before this date
            date_str = date_match.group(1)
            # The values before the date are: [time?, venue?, guest, home]
            # We need at least guest and home (2 lines before date)
            preceding = []
            j = i - 1
            while j >= start_idx and len(preceding) < 4:
                prev = lines[j]
                if re.match(r"\d{2}/\d{2}/\d{4}$", prev):
                    break
                if prev in ("שעה", "מגרש", "אורחת", "מארחת", "תאריך", "משחקים קרובים"):
                    break
                preceding.insert(0, prev)
                j -= 1

            home_team = preceding[-1] if len(preceding) >= 1 else ""
            guest_team = preceding[-2] if len(preceding) >= 2 else ""
            venue = preceding[-3] if len(preceding) >= 3 else ""
            time_str = preceding[-4] if len(preceding) >= 4 else ""

            # Include home games at Teddy, OR Jerusalem derbies (always at Teddy)
            is_hapoel_home = HAPOEL_JLM_KW in home_team
            is_teddy = "טדי" in venue
            is_derby = (BEITAR_KW in home_team and HAPOEL_JLM_KW in guest_team) or \
                       (HAPOEL_JLM_KW in home_team and BEITAR_KW in guest_team)
            if not ((is_hapoel_home and is_teddy) or is_derby):
                i += 1
                continue

            day, month, year = date_str.split("/")
            if time_str and re.match(r"\d{2}:\d{2}", time_str):
                hour, minute = map(int, time_str.split(":"))
            else:
                hour, minute = DEFAULT_HOUR, DEFAULT_MINUTE
            # Treat obviously wrong times as TBD
            if hour < 10:
                hour, minute = DEFAULT_HOUR, DEFAULT_MINUTE

            match_dt = datetime(
                int(year), int(month), int(day), hour, minute,
                tzinfo=ISRAEL_TZ,
            )

            if match_dt < now:
                i += 1
                continue

            matches.append({
                "home_team": home_team,
                "away_team": guest_team,
                "datetime": match_dt,
                "venue": "Teddy Stadium",
                "source": "hapoel",
            })

        i += 1

    return matches


def deduplicate_matches(all_matches: list[dict]) -> list[dict]:
    """Remove duplicate matches (e.g. a derby appears for both teams).

    Two matches with the same teams within 3 days are treated as duplicates.
    When both sources list a derby, prefer the home team's own site.
    """
    sorted_matches = sorted(all_matches, key=lambda m: m["datetime"])
    unique = []
    for m in sorted_matches:
        teams = tuple(sorted([m["home_team"], m["away_team"]]))
        duplicate = False
        for j, existing in enumerate(unique):
            existing_teams = tuple(sorted([existing["home_team"], existing["away_team"]]))
            if teams == existing_teams and abs((m["datetime"] - existing["datetime"]).days) <= 3:
                # Same matchup within 3 days — keep the entry from the home team's site
                home_is_hapoel = HAPOEL_JLM_KW in existing["home_team"]
                if home_is_hapoel and m["source"] == "hapoel":
                    unique[j] = m  # replace with Hapoel's data
                elif not home_is_hapoel and m["source"] == "beitar":
                    unique[j] = m  # replace with Beitar's data
                duplicate = True
                break
        if not duplicate:
            unique.append(m)
    return sorted(unique, key=lambda m: m["datetime"])


def create_ics(matches: list[dict]) -> Calendar:
    """Create an ICS calendar from match data."""
    cal = Calendar()
    cal.add("prodid", "-//Teddy Stadium Football//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "Teddy Stadium Matches")
    cal.add("x-wr-timezone", "Asia/Jerusalem")

    now_utc = datetime.now(tz=timezone.utc)

    for match in matches:
        event = Event()
        summary = f"⚽ {match['home_team']} vs {match['away_team']}"
        event.add("summary", summary)
        # Use UTC times for maximum compatibility
        dt_utc = match["datetime"].astimezone(timezone.utc)
        event.add("dtstart", dt_utc)
        event.add("dtend", dt_utc + MATCH_DURATION)
        event.add("location", "Teddy Stadium, Jerusalem")
        # Stable UID based on date — doesn't change between runs
        uid_hash = hashlib.md5(
            f"{match['datetime'].strftime('%Y%m%d')}-{match['home_team']}-{match['away_team']}".encode()
        ).hexdigest()[:8]
        event.add("uid", f"teddy-{match['datetime'].strftime('%Y%m%d')}-{uid_hash}@football-matches")
        event.add("dtstamp", now_utc)
        cal.add_component(event)

    return cal


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    print("Fetching schedules from team websites...")

    print("  Beitar Jerusalem...")
    beitar = fetch_beitar_matches()
    print(f"    Found {len(beitar)} upcoming home matches")

    print("  Hapoel Jerusalem...")
    hapoel = fetch_hapoel_matches()
    print(f"    Found {len(hapoel)} upcoming home matches")

    matches = deduplicate_matches(beitar + hapoel)
    print(f"\nTotal unique matches at Teddy: {len(matches)}")

    for m in matches:
        dt = m["datetime"].strftime("%a %d/%m/%Y %H:%M")
        print(f"  {dt}  {m['home_team']} vs {m['away_team']}")

    cal = create_ics(matches)

    OUTPUT_DIR.mkdir(exist_ok=True)
    ics_path = OUTPUT_DIR / "teddy_matches.ics"
    ics_path.write_bytes(cal.to_ical())
    print(f"\nCalendar saved to {ics_path}")


if __name__ == "__main__":
    main()
