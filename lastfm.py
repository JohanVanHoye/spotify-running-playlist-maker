"""
Last.fm-based artist discovery.

Replaces Spotify's deprecated genre:artist search and artist_related_artists endpoint.

Two complementary strategies are combined:
  A) Tag pagination  — fetches the top artists for a given Last.fm tag across
     multiple pages. Pages 1-3 surface well-known names; later pages reach the
     long tail of lesser-known but genre-authentic artists.
  B) Similarity graph — for a set of seed artists (auto-picked from the tag
     results + optional user-defined overrides), fetches similar artists from
     Last.fm and merges them into the pool. Similarity is scored by Last.fm's
     algorithm, not by popularity, so it surfaces artists regardless of audience size.

The combined artist-name pool is then resolved to Spotify artist IDs via name
search, ready for the existing album/track enumeration pipeline.
"""
from __future__ import annotations
import time
from typing import Optional
import requests

LASTFM_API_BASE = "https://ws.audioscrobbler.com/2.0/"
_REQUEST_DELAY = 0.25   # seconds between Last.fm calls — well under 5 req/sec limit
_SPOTIFY_DELAY = 0.05   # seconds between Spotify name-search calls


def _lastfm_get(params: dict, timeout: float = 8.0) -> Optional[dict]:
    """GET a Last.fm API endpoint; return parsed JSON or None on error.
    Retries once on HTTP 429 after honouring the Retry-After header."""
    try:
        params["format"] = "json"
        r = requests.get(LASTFM_API_BASE, params=params, timeout=timeout)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 10))
            print(f"  … Last.fm rate limited, waiting {wait}s...")
            time.sleep(wait + 1)
            r = requests.get(LASTFM_API_BASE, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None


def get_top_artists_for_tag(api_key: str, tag: str, pages: int = 7) -> list[str]:
    """
    Fetch artist names for a Last.fm tag across `pages` pages (50 per page).
    Returns a list of artist name strings ordered by Last.fm popularity descending.
    """
    artist_names: list[str] = []
    for page in range(1, pages + 1):
        data = _lastfm_get({
            "method": "tag.gettopartists",
            "tag": tag,
            "api_key": api_key,
            "limit": 50,
            "page": page,
        })
        if not data:
            break
        artists = data.get("topartists", {}).get("artist", [])
        if not artists:
            break
        for a in artists:
            name = a.get("name")
            if name:
                artist_names.append(name)
        time.sleep(_REQUEST_DELAY)
    return artist_names


def get_similar_artists(api_key: str, artist_name: str, limit: int = 50) -> list[str]:
    """
    Fetch similar artists for a given artist from Last.fm.
    Returns a list of artist name strings ordered by similarity score descending.
    """
    data = _lastfm_get({
        "method": "artist.getsimilar",
        "artist": artist_name,
        "api_key": api_key,
        "limit": limit,
    })
    if not data:
        return []
    similar = data.get("similarartists", {}).get("artist", [])
    time.sleep(_REQUEST_DELAY)
    return [a.get("name") for a in similar if a.get("name")]


def _names_match(lastfm_name: str, spotify_name: str) -> bool:
    """
    Loose case-insensitive match to avoid obvious false positives from Spotify search.
    Handles common differences like 'Sisters of Mercy' vs 'The Sisters of Mercy'.
    """
    a = lastfm_name.lower().strip()
    b = spotify_name.lower().strip()
    return a == b or a in b or b in a


def discover_artists_via_lastfm(settings, spotify_client) -> dict:
    """
    Discover artists using Last.fm tag pagination + similarity graph traversal,
    then resolve each to a Spotify artist ID via name search.

    Args:
        settings:        Settings instance (needs lastfm_api_key, genre_searchstring,
                         lastfm_tag_pages, lastfm_seed_artists).
        spotify_client:  Authenticated Spotipy client.

    Returns:
        dict of {spotify_artist_id: (artist_name, artist_uri)}
    """
    api_key = settings.lastfm_api_key
    tag = settings.genre_searchstring
    tag_pages = getattr(settings, "lastfm_tag_pages", 7)
    seed_overrides = list(getattr(settings, "lastfm_seed_artists", []) or [])

    # ---- Strategy A: tag pagination ----
    print(f"♫ Last.fm: fetching top artists for tag '{tag}' ({tag_pages} pages × 50)...")
    tag_artist_names = get_top_artists_for_tag(api_key, tag, pages=tag_pages)
    print(f"  → {len(tag_artist_names)} artists from tag pagination")

    # ---- Strategy B: similarity graph ----
    # Auto-seed: top 5 from tag results (well-known = dense similarity neighbourhoods)
    # + any user-defined seed overrides from settings.toml
    auto_seeds = tag_artist_names[:5]
    all_seeds = list(dict.fromkeys(auto_seeds + seed_overrides))  # deduplicate, preserve order

    similar_names: list[str] = []
    print(f"♫ Last.fm: similarity expansion from {len(all_seeds)} seed artist(s)...")
    for seed in all_seeds:
        found = get_similar_artists(api_key, seed)
        similar_names.extend(found)
    print(f"  → {len(similar_names)} artists from similarity graph")

    # ---- Merge and deduplicate (tag results first, then similar) ----
    all_names = list(dict.fromkeys(tag_artist_names + similar_names))
    print(f"♫ Last.fm: {len(all_names)} unique artist names — resolving on Spotify...")

    # ---- Resolve names to Spotify artist IDs ----
    discovered: dict = {}
    resolved = 0
    total = len(all_names)
    for i, name in enumerate(all_names):
        try:
            result = spotify_client.search(q=name, type="artist", limit=1)
            items = result.get("artists", {}).get("items", [])
            if items:
                artist = items[0]
                if _names_match(name, artist.get("name", "")):
                    artist_id = artist["uri"].split(":")[-1]
                    discovered[artist_id] = (artist["name"], artist["uri"])
                    resolved += 1
            time.sleep(_SPOTIFY_DELAY)
        except Exception as ex:
            print(f"\n  ⚠ Spotify lookup failed for '{name}': {ex}")
        print(f"  → resolving {i + 1}/{total} — {resolved} matched so far...", end="\r", flush=True)

    print(f"  → {resolved}/{total} artists resolved on Spotify{' ' * 20}")
    return discovered
