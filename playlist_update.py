import os
import re
import json
import logging
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


def clean_artist_name(title: str) -> str:
    """Extract the primary artist name from a raw event title."""
    title = re.sub(r"\s*\([^)]*\)", "", title)         # remove (parentheticals)
    title = re.split(r"\s+[Ww]/\s*", title)[0]         # split on w/
    title = re.split(r"\s+[Ww]ith\s+", title)[0]       # split on "with"
    parts = re.split(r":\s+", title)
    if len(parts) > 1 and len(parts[0].strip()) > 2:   # strip subtitle after colon
        title = parts[0]
    return title.strip()


def scrape_lunatico() -> list[str]:
    """Scrape upcoming artists from barlunatico.com/music (Squarespace)."""
    log.info("Scraping Lunatico...")
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get("https://www.barlunatico.com/music", headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    for selector in [
        ".eventlist-title",
        ".eventlist-title-link",
        ".summary-title",
        '[data-automation="event-title"]',
        ".eventitem-column-meta h1",
    ]:
        elements = soup.select(selector)
        if elements:
            artists = [clean_artist_name(el.get_text(strip=True)) for el in elements]
            artists = [a for a in artists if a]
            log.info(f"Lunatico: {len(artists)} artists via '{selector}'")
            return artists

    log.warning("Lunatico: no events found — page structure may have changed")
    return []


def scrape_barbes() -> list[str]:
    """Scrape upcoming artists from viewcy.com/barbes using Playwright to intercept API calls."""
    from playwright.sync_api import sync_playwright

    log.info("Scraping Barbes via Viewcy (headless)...")
    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        def handle_response(response):
            if "backend.viewcy.com" in response.url and response.status == 200:
                try:
                    body = response.json()
                    captured.append({"url": response.url, "body": body})
                    log.info(f"Barbes: captured {response.url}: {str(body)[:200]}")
                except Exception:
                    pass

        page.on("response", handle_response)
        page.goto("https://www.viewcy.com/barbes", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)

        # Try clicking an Events/Upcoming tab to trigger event data loading
        for label in ["Events", "Upcoming", "Schedule", "Shows"]:
            tab = page.locator(f"text={label}").first
            if tab.is_visible():
                log.info(f"Barbes: clicking '{label}' tab")
                tab.click()
                page.wait_for_timeout(3000)
                break

        page.wait_for_timeout(2000)
        browser.close()

    for item in captured:
        titles = _extract_event_titles(item["body"])
        if titles:
            artists = [clean_artist_name(t) for t in titles]
            artists = [a for a in artists if a]
            log.info(f"Barbes: {len(artists)} artists from {item['url']}")
            return artists

    log.warning(f"Barbes: no event titles found across {len(captured)} captured responses")
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
    pl = sp.user_playlist_create(
        user_id, name, public=False,
        description="Weekly: upcoming artists at Barbes & Lunatico, Brooklyn"
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
    if a not in b and b not in a:
        log.warning(f"  MISMATCH: searched {artist_name!r}, got {artist['name']!r} — skipping")
        return []

    track_results = sp.search(q=f'artist:"{artist["name"]}"', type="track", limit=TRACKS_PER_ARTIST)
    tracks = track_results["tracks"]["items"]
    if not tracks:
        log.warning(f"  No tracks found for {artist['name']!r}")
        return []
    log.info(f"  {artist['name']!r}: {[t['name'] for t in tracks]}")
    return [t["uri"] for t in tracks]


def update_playlist():
    sp = get_spotify_client()

    lunatico = scrape_lunatico()
    barbes = scrape_barbes()

    if not lunatico and not barbes:
        log.error("Both scrapers returned nothing — aborting to preserve existing playlist")
        return

    # Deduplicate while preserving order
    seen: set[str] = set()
    all_artists: list[str] = []
    for name in lunatico + barbes:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            all_artists.append(name)

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
