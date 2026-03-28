import os
import re
import json
import logging
from datetime import date, datetime, timedelta
import requests
from bs4 import BeautifulSoup
import spotipy
from spotipy.oauth2 import SpotifyOAuth

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

PLAYLIST_NAME = "Brooklyn Venues Weekly"
SCOPE = "playlist-read-private playlist-modify-public playlist-modify-private"
TRACKS_PER_ARTIST = 3


def get_spotify_client() -> spotipy.Spotify:
    sp_oauth = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        scope=SCOPE,
    )
    token_info = sp_oauth.refresh_access_token(os.environ["SPOTIFY_REFRESH_TOKEN"])
    return spotipy.Spotify(auth=token_info["access_token"])


VENUE_NAMES = {"barbès", "barbes", "lunatico", "bar lunatico"}


def clean_artist_name(title: str) -> str:
    """Extract the primary artist name from a raw event title."""
    title = title.strip()

    # Filter out venue names
    if title.lower() in VENUE_NAMES:
        return ""

    # "X presents: Y" or "X Presents: Y" → the actual artist is Y
    m = re.search(r"\bpresents?:\s*(.+)", title, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
    else:
        # "X presents Y" (no colon) → artist is Y
        m = re.search(r"\bpresents\s+(.+)", title, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
        else:
            # "X presents" with nothing after → not an artist entry
            if re.search(r"\bpresents\s*$", title, re.IGNORECASE):
                return ""

    # Remove parenthetical guest lists
    title = re.sub(r"\s*\([^)]*\)", "", title)
    # Split on " + " (multiple billed artists — take the first)
    title = re.split(r"\s+\+\s+", title)[0]
    # Split on "w/" (guest)
    title = re.split(r"\s+[Ww]/\s*", title)[0]
    # Split on " with " (guest)
    title = re.split(r"\s+[Ww]ith\s+", title)[0]
    # Strip subtitle after ": "
    parts = re.split(r":\s+", title)
    if len(parts) > 1 and len(parts[0].strip()) > 2:
        title = parts[0]
    # Strip " - remix/release/subtitle"
    title = re.split(r"\s+-\s+", title)[0]
    # Strip "plays the music of ..."
    title = re.split(r"\s+plays\s+the\s+music\s+of\b", title, flags=re.IGNORECASE)[0]
    # Strip trailing descriptive phrases: "SINGLE RELEASE", "In residence ...", ". sentence"
    title = re.sub(r"\s+(?:SINGLE\s+RELEASE|[Ii]n\s+[Rr]esidence)\b.*$", "", title)
    title = re.sub(r"\.\s+\w+(?:\s+\w+)+$", "", title)  # ". Multi word phrase"

    title = title.strip().rstrip(".,;:")  # strip trailing punctuation
    if len(title) < 3:
        return ""
    # Skip suspiciously generic single words that tend to produce wrong Spotify matches
    if re.match(r'^[A-Z][A-Za-z]+s?$', title) and len(title.split()) == 1 and len(title) < 12:
        return ""
    # Title-case names that are fully uppercase (Barbes events come in all caps)
    if title == title.upper() and any(c.isalpha() for c in title):
        title = title.title()
        # Fix apostrophe title-case issue: "It'S" → "It's"
        title = re.sub(r"'([A-Z])", lambda m: "'" + m.group(1).lower(), title)
    return title


def scrape_lunatico() -> list[dict]:
    """Scrape upcoming events from barlunatico.com/music (Squarespace).
    Returns list of {"artist": str, "date": str} dicts."""
    log.info("Scraping Lunatico...")
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get("https://www.barlunatico.com/music", headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    events = []

    # Try to get events as containers with both title and date
    for container_sel, title_sel, date_sel in [
        (".eventlist-event", ".eventlist-title", ".eventlist-meta-date"),
        (".summary-item", ".summary-title", ".summary-metadata-item--date"),
        (".eventlist-event", ".eventlist-title-link", ".eventlist-meta-date"),
    ]:
        containers = soup.select(container_sel)
        if containers:
            for c in containers:
                title_el = c.select_one(title_sel)
                date_el = c.select_one(date_sel)
                if title_el:
                    artist = clean_artist_name(title_el.get_text(strip=True))
                    if artist:
                        events.append({
                            "artist": artist,
                            "date": date_el.get_text(strip=True) if date_el else "",
                        })
            if events:
                log.info(f"Lunatico: {len(events)} events via '{container_sel}'")
                return events

    # Fallback: title-only selectors (no date)
    for selector in [".eventlist-title", ".eventlist-title-link", ".summary-title"]:
        elements = soup.select(selector)
        if elements:
            events = [{"artist": a, "date": ""} for el in elements
                      if (a := clean_artist_name(el.get_text(strip=True)))]
            log.info(f"Lunatico: {len(events)} events via '{selector}' (no dates)")
            return events

    log.warning("Lunatico: no events found — page structure may have changed")
    return []


def scrape_barbes() -> list[dict]:
    """Scrape upcoming events from barbesbrooklyn.com/events (Wix, JS-rendered).
    Returns list of {"artist": str, "date": str} dicts."""
    from playwright.sync_api import sync_playwright

    log.info("Scraping Barbes website (headless)...")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # The Barbes website embeds Viewcy — load the embed directly
        page.goto("https://viewcyembed.com/barbes/000000/FFFCFC/850505",
                  wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(4000)

        # Structure: each event card has H1 (title) and a SPAN matching "Day, Mon DD YYYY"
        dom_events = page.evaluate("""() => {
            const DATE_RE = /^[A-Z][a-z]{2},\\s[A-Z][a-z]{2}\\s\\d/;
            const events = [];
            for (const h1 of document.querySelectorAll('h1')) {
                const title = h1.innerText?.trim();
                if (!title) continue;
                // Search for a date span within the same parent container
                const container = h1.parentElement;
                let dateText = '';
                for (const span of container.querySelectorAll('span')) {
                    const t = span.innerText?.trim();
                    if (t && DATE_RE.test(t)) { dateText = t; break; }
                }
                // If not found in parent, try grandparent
                if (!dateText) {
                    for (const span of (container.parentElement || container).querySelectorAll('span')) {
                        const t = span.innerText?.trim();
                        if (t && DATE_RE.test(t)) { dateText = t; break; }
                    }
                }
                events.push({ title, date: dateText });
            }
            return events;
        }""")

        browser.close()

    if dom_events:
        events = []
        today = date.today()
        cutoff = today + timedelta(days=7)
        for e in dom_events:
            a = clean_artist_name(e["title"])
            if not a:
                continue
            # "Sat, Mar 28 2026" → "Sat Mar 28"
            parts = e["date"].replace(',', '').split()
            date_str = ' '.join(parts[:3]) if len(parts) >= 3 else e["date"]
            # Filter to events within the next 14 days
            if date_str:
                try:
                    parsed = datetime.strptime(f"{date_str} {today.year}", "%a %b %d %Y").date()
                    if parsed < today:
                        parsed = parsed.replace(year=today.year + 1)
                    if parsed > cutoff:
                        continue
                except ValueError:
                    pass  # Unparseable date — include it
            events.append({"artist": a, "date": date_str})
        log.info(f"Barbes: {len(events)} events in next 7 days (from embed)")
        return events

    log.warning("Barbes: no events found in embed")
    return []


def _extract_event_titles(data: object) -> list[str]:
    """Recursively search a JSON structure for event title strings."""
    titles: list[str] = []

    def walk(obj: object):
        if isinstance(obj, dict):
            # Viewcy event objects have a "name" or "title" field alongside date fields
            name = obj.get("name") or obj.get("title") or obj.get("eventName")
            if name and isinstance(name, str) and len(name) > 2:
                # Only include if it looks like an event (has a date-like sibling key)
                has_date = any(k in obj for k in ("startDate", "startTime", "date", "scheduledAt"))
                if has_date:
                    titles.append(name)
                    return
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return titles


def find_or_create_playlist(sp: spotipy.Spotify, name: str) -> str:
    user_id = sp.me()["id"]
    playlists = sp.current_user_playlists(limit=50)
    while playlists:
        for pl in playlists["items"]:
            if pl["name"] == name:
                return pl["id"]
        playlists = sp.next(playlists) if playlists["next"] else None
    pl = sp._post(
        "me/playlists",
        payload={"name": name, "public": False,
                 "description": "Weekly: upcoming artists at Barbes & Lunatico, Brooklyn"}
    )
    return pl["id"]


def get_top_tracks(sp: spotipy.Spotify, artist_name: str) -> list[str]:
    results = sp.search(q=f'artist:"{artist_name}"', type="artist", limit=1)
    items = results["artists"]["items"]
    if not items:
        log.warning(f"  NOT FOUND on Spotify: {artist_name!r}")
        return []

    artist = items[0]
    a, b = artist["name"].lower(), artist_name.lower()
    # Reject if names don't overlap, or if the result is more than 30% longer
    # (catches "Super Yamba Band" matching "Kaleta & Super Yamba Band")
    if a not in b and b not in a:
        log.warning(f"  MISMATCH: searched {artist_name!r}, got {artist['name']!r} — skipping")
        return []
    if b in a and len(a) > len(b) * 1.3:
        log.warning(f"  MISMATCH (superset): searched {artist_name!r}, got {artist['name']!r} — skipping")
        return []

    track_results = sp.search(q=f'artist:"{artist["name"]}"', type="track", limit=TRACKS_PER_ARTIST)
    tracks = track_results["tracks"]["items"]
    if not tracks:
        log.warning(f"  No tracks found for {artist['name']!r}")
        return []
    log.info(f"  {artist['name']!r}: {[t['name'] for t in tracks]}")
    return [t["uri"] for t in tracks]


def dedup_events(events: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out = []
    for e in events:
        key = (e["artist"].lower(), e["date"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def write_readme(lunatico: list[dict], barbes: list[dict]) -> None:
    lunatico = dedup_events(lunatico)
    barbes = dedup_events(barbes)
    today = date.today().strftime("%B %d, %Y")
    lines = [
        "# Brooklyn Venues Weekly",
        f"*Updated: {today}*\n",
        "---\n",
        "## Lunatico",
        "*[barlunatico.com/music](https://www.barlunatico.com/music)*\n",
    ]

    if lunatico:
        has_dates = any(e["date"] for e in lunatico)
        if has_dates:
            lines += ["| Date | Artist |", "|------|--------|"]
            for e in lunatico:
                lines.append(f"| {e['date']} | {e['artist']} |")
        else:
            for e in lunatico:
                lines.append(f"- {e['artist']}")
    else:
        lines.append("*No events found*")

    lines += [
        "\n---\n",
        "## Barbes",
        "*[viewcy.com/barbes](https://www.viewcy.com/barbes)*\n",
    ]

    if barbes:
        has_dates = any(e["date"] for e in barbes)
        if has_dates:
            lines += ["| Date | Artist |", "|------|--------|"]
            for e in barbes:
                lines.append(f"| {e['date']} | {e['artist']} |")
        else:
            for e in barbes:
                lines.append(f"- {e['artist']}")
    else:
        lines.append("*No events found*")

    lines += ["\n---\n", f"*Spotify playlist: **{PLAYLIST_NAME}***"]

    with open("README.md", "w") as f:
        f.write("\n".join(lines) + "\n")

    log.info("README.md written")


def update_playlist():
    sp = get_spotify_client()

    lunatico = scrape_lunatico()
    barbes = scrape_barbes()

    if not lunatico and not barbes:
        log.error("Both scrapers returned nothing — aborting to preserve existing playlist")
        return

    write_readme(lunatico, barbes)

    # Deduplicate artists across both venues
    seen: set[str] = set()
    all_artists: list[str] = []
    for event in lunatico + barbes:
        key = event["artist"].lower()
        if key not in seen:
            seen.add(key)
            all_artists.append(event["artist"])

    log.info(f"\n=== {len(all_artists)} unique artists ===")
    for a in all_artists:
        log.info(f"  {a}")

    track_uris: list[str] = []
    log.info("\n=== Spotify search ===")
    for artist in all_artists:
        track_uris.extend(get_top_tracks(sp, artist))

    if not track_uris:
        log.error("No tracks found on Spotify — aborting playlist update")
        return

    playlist_id = find_or_create_playlist(sp, PLAYLIST_NAME)
    sp.playlist_replace_items(playlist_id, [])
    for i in range(0, len(track_uris), 100):
        sp.playlist_add_items(playlist_id, track_uris[i:i + 100])

    log.info(f"\nDone: {len(track_uris)} tracks added to '{PLAYLIST_NAME}'")


if __name__ == "__main__":
    update_playlist()
