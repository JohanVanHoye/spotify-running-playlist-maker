
"""
BPM provider interfaces and helpers.

This module lets the app obtain BPM/tempo values without Spotify's
Audio Features / Audio Analysis endpoints, which are no longer available
for most third-party apps created after Nov 27, 2024. See Spotify's
announcement for context.

Providers implemented here:
- GetSongBpmProvider: looks up BPM by artist + title via an external API (stubbed).
- AcousticBrainzProvider: optional, MBID-based lookup (stub/starter).
- LocalFileProvider: placeholder for local file analysis (e.g., librosa/Essentia).

All providers return a BpmResult with (bpm, source, confidence, notes).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class BpmResult:
    bpm: Optional[float]
    source: str
    confidence: Optional[float] = None
    notes: str = ""


class BpmProvider:
    def get_bpm(self, artist_name: str, track_name: str, isrc: Optional[str] = None) -> BpmResult:
        raise NotImplementedError


class GetSongBpmProvider(BpmProvider):
    """
    A thin client around an external BPM catalog (e.g., getsongbpm.com).

    This is a *starter implementation*. You'll need to:
      - Provide your API key via settings.getsongbpm_api_key
      - Fill in the actual endpoint/params according to the vendor's docs
      - Map response JSON to a BPM float

    The code attempts to import 'requests'; if unavailable, it falls back to urllib.
    """
    def __init__(self, api_key: str, timeout: float = 8.0):
        self.api_key = api_key
        self.timeout = timeout

    def _http_get_json(self, url: str) -> Optional[dict]:
        try:
            try:
                import requests  # type: ignore
                r = requests.get(url, timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
                return None
            except Exception:
                # Fallback to stdlib
                import json
                import urllib.request
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    if resp.status == 200:
                        return json.loads(resp.read().decode('utf-8'))
                return None
        except Exception:
            return None

    def get_bpm(self, artist_name: str, track_name: str, isrc: Optional[str] = None) -> BpmResult:
        if not self.api_key:
            return BpmResult(bpm=None, source="GetSongBPM", notes="missing API key")

        # TODO: Replace with the provider's documented endpoint. The below is a placeholder.
        # For example purposes, we URL-encode a simple query built from artist + title.
        import urllib.parse
        query = urllib.parse.quote_plus(f"{artist_name} {track_name}")
        # Example placeholder URL (NOT the real API):
        url = f"https://api.getsongbpm.com/search/?api_key={self.api_key}&type=song&lookup={query}"
        data = self._http_get_json(url)

        bpm = None
        if isinstance(data, dict):
            # Map real structure here. Placeholder expects something like:
            # {'search': {'song': [{'tempo': '128.0', ...}, ...]}}
            try:
                songs = data.get('search', {}).get('song', [])
                if songs:
                    # naive: take first result tempo, if present
                    raw = songs[0].get('tempo')
                    if raw is not None:
                        bpm = float(raw)
            except Exception:
                bpm = None
        return BpmResult(bpm=bpm, source="GetSongBPM", confidence=None,
                         notes="stub mapping; adjust to real API response")


class AcousticBrainzProvider(BpmProvider):
    """
    Optional provider that queries AcousticBrainz by MBID (if you have it).
    Note: AcousticBrainz project ended in 2022; data quality/coverage can be uneven.
    This is a placeholder; wire an MBID resolver if you want to use it.
    """
    def __init__(self):
        pass

    def get_bpm(self, artist_name: str, track_name: str, isrc: Optional[str] = None) -> BpmResult:
        return BpmResult(bpm=None, source="AcousticBrainz", notes="not implemented")

class LocalFileProvider(BpmProvider):
    """
    Placeholder for local audio BPM estimation (e.g., librosa or Essentia) on files you own.
    """
    def get_bpm_for_file(self, audio_path: str) -> BpmResult:
        return BpmResult(bpm=None, source="LocalFile", notes="not implemented")

# ----------------- Helper functions -----------------

def build_bpm_providers(settings) -> List[BpmProvider]:
    providers: List[BpmProvider] = []
    api_key = getattr(settings, 'getsongbpm_api_key', None) or getattr(getattr(settings, 'bpm', None), 'getsongbpm_api_key', None)
    if api_key:
        providers.append(GetSongBpmProvider(api_key=api_key))
    # Enable AcousticBrainz if desired later
    # if getattr(settings, 'enable_acousticbrainz', False):
    #     providers.append(AcousticBrainzProvider())
    return providers


def get_bpm_from_providers(providers: Iterable[BpmProvider], artist_name: str, track_name: str) -> Optional[float]:
    for p in providers:
        res = p.get_bpm(artist_name=artist_name, track_name=track_name)
        if res and res.bpm:
            return float(res.bpm)
    return None


def normalize_bpm_for_settings(bpm: Optional[float], settings) -> (Optional[int], str):
    """Return (normalized_bpm, status) according to settings' bpm range and doubling rules."""
    if bpm is None:
        return None, 'missing'
    try:
        bpm = float(bpm)
    except Exception:
        return None, 'invalid'

    floor = int(getattr(settings, 'bpm_floor', 0) or 0)
    ceiling = int(getattr(settings, 'bpm_ceiling', 1000) or 1000)
    allow_double = bool(getattr(settings, 'allow_doubled_bpm', False))

    if floor <= bpm <= ceiling:
        return int(round(bpm)), 'exact'
    if allow_double:
        if floor <= (bpm / 2.0) <= ceiling:
            return int(round(bpm / 2.0)), 'halved'
        if floor <= (bpm * 2.0) <= ceiling:
            return int(round(bpm * 2.0)), 'doubled'
    return None, 'out_of_range'
