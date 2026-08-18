"""
preview.py — 30-second song previews.

Looks the track up on Deezer first (previews are MP3, which pygame can play
directly) and falls back to iTunes (M4A, which most backends cannot decode,
so it is offered as an "open externally" link instead).

Playback needs pygame. Without it, lookup still works and callers can hand
the URL to a browser.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from urllib.parse import quote

log = logging.getLogger("yarg")

DEEZER_API = "https://api.deezer.com/search"
ITUNES_API = "https://itunes.apple.com/search"

try:
    import pygame
    pygame.mixer.init()
    HAS_AUDIO = True
except Exception as _e:                       # pygame missing or no audio dev
    HAS_AUDIO = False
    log.info(f"Audio preview playback unavailable: {_e}")


@dataclass
class Preview:
    url: str
    source: str          # deezer | itunes
    playable: bool       # can we play it in-process?
    title: str = ""
    artist: str = ""


_cache: dict[tuple[str, str], Preview | None] = {}
_cache_lock = threading.Lock()


def lookup(artist: str, title: str, session) -> Preview | None:
    """Find a 30s preview. Returns None if nothing matches."""
    key = (artist.lower().strip(), title.lower().strip())
    with _cache_lock:
        if key in _cache:
            return _cache[key]

    result = _deezer(artist, title, session) or _itunes(artist, title, session)
    with _cache_lock:
        _cache[key] = result
    return result


def _deezer(artist: str, title: str, session) -> Preview | None:
    try:
        q = quote(f'artist:"{artist}" track:"{title}"')
        r = session.get(f"{DEEZER_API}?q={q}&limit=1", timeout=8)
        if r.status_code != 200:
            return None
        data = (r.json() or {}).get("data") or []
        if not data:
            return None
        t = data[0]
        url = t.get("preview")
        if not url:
            return None
        return Preview(url, "deezer", HAS_AUDIO,
                       t.get("title", ""), (t.get("artist") or {}).get("name", ""))
    except Exception as e:
        log.debug(f"Deezer preview lookup failed: {e}")
        return None


def _itunes(artist: str, title: str, session) -> Preview | None:
    try:
        q = quote(f"{artist} {title}")
        r = session.get(f"{ITUNES_API}?term={q}&media=music&limit=1", timeout=8)
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
        if not results:
            return None
        t = results[0]
        url = t.get("previewUrl")
        if not url:
            return None
        # iTunes previews are M4A; pygame cannot decode them.
        return Preview(url, "itunes", False,
                       t.get("trackName", ""), t.get("artistName", ""))
    except Exception as e:
        log.debug(f"iTunes preview lookup failed: {e}")
        return None


# ── Playback ───────────────────────────────────────────────────────────────

_current: str | None = None


def play(url: str, session) -> bool:
    """Stream a preview into memory and play it. False if unsupported."""
    global _current
    if not HAS_AUDIO:
        return False
    try:
        import io
        r = session.get(url, timeout=15)
        if r.status_code != 200:
            return False
        stop()
        pygame.mixer.music.load(io.BytesIO(r.content))
        pygame.mixer.music.play()
        _current = url
        return True
    except Exception as e:
        log.warning(f"Preview playback failed: {e}")
        return False


def stop() -> None:
    global _current
    if not HAS_AUDIO:
        return
    try:
        pygame.mixer.music.stop()
    except Exception:
        pass
    _current = None


def is_playing() -> bool:
    if not HAS_AUDIO:
        return False
    try:
        return bool(pygame.mixer.music.get_busy())
    except Exception:
        return False
