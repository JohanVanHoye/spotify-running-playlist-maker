"""
BPM provider interfaces and helpers.

Spotify's Audio Features / Audio Analysis endpoints are no longer available
for third-party apps created after Nov 27, 2024. Spotify's preview_url fields
are also now widely null across their catalogue.

Provider implemented:
- LocalAudioBpmProvider: resolves a 30-second preview audio clip and analyses
  tempo locally using librosa. No external API key required.

  Preview URL resolution order:
    1. Spotify preview_url (passed in from the track object, often null now)
    2. Deezer public search API (free, no key, good catalogue coverage)
    3. Give up — return bpm=None for this track

  The source field in BpmResult reflects where the preview came from:
  "librosa/spotify" or "librosa/deezer".
"""
from __future__ import annotations
import os
import tempfile
import urllib.parse
from dataclasses import dataclass
from typing import Optional, List, Iterable
import numpy as np


@dataclass
class BpmResult:
    bpm: Optional[float]
    source: str
    confidence: Optional[float] = None
    notes: str = ""


class BpmProvider:
    def get_bpm(self, artist_name: str, track_name: str,
                isrc: Optional[str] = None,
                preview_url: Optional[str] = None) -> BpmResult:
        raise NotImplementedError


def _get_deezer_preview(artist_name: str, track_name: str) -> Optional[str]:
    """
    Search Deezer for a track and return its 30-second preview MP3 URL.
    Uses Deezer's public search API — no API key required.
    Returns None if no match is found or on any error.
    """
    try:
        import requests
        # Simple combined query — more robust than strict field:value syntax
        query = urllib.parse.quote(f"{artist_name} {track_name}")
        url = f"https://api.deezer.com/search?q={query}&limit=5"
        response = requests.get(url, timeout=8)
        if response.status_code == 200:
            for item in response.json().get("data", []):
                preview = item.get("preview")
                if preview:
                    return preview
    except Exception:
        pass
    return None


def _analyse_preview(audio_url: str, source_label: str) -> BpmResult:
    """
    Download an MP3 preview URL to a temp file and estimate BPM with librosa.
    source_label is embedded in the returned BpmResult for traceability.
    C-level stderr is suppressed during loading to silence mpg123/libsndfile
    noise (e.g. benign ID3v2 tag warnings) that cannot be caught via Python.
    """
    tmp_path = None
    try:
        import requests
        import librosa

        response = requests.get(audio_url, timeout=15)
        if response.status_code != 200:
            return BpmResult(bpm=None, source=source_label,
                             notes=f"download failed (HTTP {response.status_code})")

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name

        # Suppress C-level stderr during load (mpg123 ID3 tag warnings etc.)
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        saved_stderr_fd = os.dup(2)
        os.dup2(devnull_fd, 2)
        try:
            y, sr = librosa.load(tmp_path, sr=None, mono=True)
        finally:
            os.dup2(saved_stderr_fd, 2)
            os.close(saved_stderr_fd)
            os.close(devnull_fd)

        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        # librosa >= 0.10 returns tempo as a 1-element ndarray rather than a scalar
        bpm = float(np.atleast_1d(tempo)[0])
        return BpmResult(bpm=bpm, source=source_label)

    except Exception as ex:
        return BpmResult(bpm=None, source=source_label, notes=f"analysis error: {ex}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


class LocalAudioBpmProvider(BpmProvider):
    """
    Estimates BPM from a 30-second audio preview using librosa.

    Tries Spotify's preview_url first; if absent (now common after Spotify's
    Nov 2024 API changes), falls back to Deezer's public search API to obtain
    a preview clip for the same track.
    """

    def get_bpm(self, artist_name: str, track_name: str,
                isrc: Optional[str] = None,
                preview_url: Optional[str] = None) -> BpmResult:

        # 1. Try Spotify preview URL
        if preview_url:
            return _analyse_preview(preview_url, source_label="librosa/spotify")

        # 2. Fall back to Deezer
        deezer_url = _get_deezer_preview(artist_name, track_name)
        if deezer_url:
            return _analyse_preview(deezer_url, source_label="librosa/deezer")

        # 3. No preview available from either source
        return BpmResult(bpm=None, source="librosa", notes="no preview URL (Spotify or Deezer)")


# ----------------- Helper functions -----------------

def build_bpm_providers(settings) -> List[BpmProvider]:
    return [LocalAudioBpmProvider()]


def get_bpm_from_providers(providers: Iterable[BpmProvider], artist_name: str,
                            track_name: str,
                            preview_url: Optional[str] = None) -> Optional[float]:
    for p in providers:
        res = p.get_bpm(artist_name=artist_name, track_name=track_name,
                        preview_url=preview_url)
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
