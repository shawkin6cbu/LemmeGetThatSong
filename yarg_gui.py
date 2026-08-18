#!/usr/bin/env python3
"""
LemmeGetThatSong
========================
Search Chorus Encore, RhythmVerse, and YARG Charts simultaneously.
Download charts directly into your YARG Songs folder.
Real-time taste profiling with personalized recommendations.

Requirements:
    pip install requests Pillow customtkinter

Optional (taste engine discovery):
    ListenBrainz API (no key needed, auto-detected)
"""

import customtkinter as ctk
from tkinter import ttk, messagebox, filedialog
import tkinter as tk
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import os
import sys
import subprocess
import zipfile
import io
import threading
import re
import shutil
import tempfile
import logging
import math
import time
import webbrowser
from urllib.parse import quote
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import mogg_check
    HAS_MOGG_CHECK = True
except ImportError:
    HAS_MOGG_CHECK = False

try:
    import preview as preview_mod
    HAS_PREVIEW = True
except ImportError:
    HAS_PREVIEW = False

# ═══════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("yarg")

# UI size. 1.0 = correct size on every display regardless of OS DPI setting.
# Persisted in settings.json; adjustable in-app. LGTS_UI_SCALE overrides.
UI_SCALE_MIN, UI_SCALE_MAX, UI_SCALE_STEP = 0.6, 2.0, 0.05

# ── Font family ─────────────────────────────────────────────────────────
# Resolved against Tk's own family list once the root window exists, since
# Tk is the only authority on what it can actually render. fc-list may name
# fonts Tk cannot use, and requires fontconfig to be installed at all.

FONT = "TkDefaultFont"

FONT_CANDIDATES = [
    "Segoe UI",          # Windows
    "SF Pro Text",       # macOS
    "Helvetica Neue",
    "Inter",
    "Noto Sans",
    "DejaVu Sans",
    "Liberation Sans",
    "Ubuntu",
    "Cantarell",
    "Arial",
    "Helvetica",
]


def resolve_font() -> str:
    """Pick the first available UI font. Requires an existing Tk root."""
    try:
        import tkinter.font as tkfont
        have = {f.lower() for f in tkfont.families()}
        for cand in FONT_CANDIDATES:
            if cand.lower() in have:
                return cand
        # Nothing matched: fall back to Tk's own default, which always
        # resolves to something proportional rather than a fixed-width font.
        return "TkDefaultFont"
    except Exception as e:
        log.warning(f"Font detection failed: {e}")
        return "TkDefaultFont"


# ═══════════════════════════════════════════════════════════════════════════
#  THEME — Catppuccin Mocha
# ═══════════════════════════════════════════════════════════════════════════

BG       = "#1e1e2e"
BG2      = "#181825"
CRUST    = "#11111b"
SURF0    = "#313244"
SURF1    = "#45475a"
SURF2    = "#585b70"
OVERLAY0 = "#6c7086"
TEXT     = "#cdd6f4"
SUB      = "#a6adc8"
BLUE     = "#89b4fa"
GREEN    = "#a6e3a1"
YELLOW   = "#f9e2af"
PEACH    = "#fab387"
RED      = "#f38ba8"
MAUVE    = "#cba6f7"
TEAL     = "#94e2d5"
LAVENDER = "#b4befe"
FLAMINGO = "#f2cdcd"
PINK     = "#f5c2e7"
SKY      = "#89dceb"
SAPPHIRE = "#74c7ec"

DIFF_COLORS = {0: SURF2, 1: GREEN, 2: GREEN,
               3: YELLOW, 4: YELLOW, 5: PEACH, 6: RED, 7: RED}

TAG_COLORS = [BLUE, TEAL, MAUVE, PEACH, GREEN, PINK, SKY, LAVENDER, FLAMINGO, SAPPHIRE]

def diff_color(v: int) -> str:
    if v < 0: return SURF2
    return DIFF_COLORS.get(min(v, 7), PEACH)

# ═══════════════════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

ENCHOR_API = "https://api.enchor.us/search"
ENCHOR_HDR = {"Content-Type": "application/json",
              "Origin": "https://www.enchor.us", "Referer": "https://www.enchor.us/"}
RV_API     = "https://rhythmverse.co/api/all/songfiles/search/live"
RV_HDR     = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
              "X-Requested-With": "XMLHttpRequest",
              "Origin": "https://rhythmverse.co",
              "Referer": "https://rhythmverse.co/songfiles/game"}
YC_URL     = "https://www.yargcharts.com/"
MB_BASE    = "https://musicbrainz.org/ws/2"
LB_BASE    = "https://api.listenbrainz.org/1"

MAGIC_BYTES = [(b'SNGPKG', ".sng"), (b'PK\x03\x04', ".zip"),
               (b'CON ', ".con"), (b'LIVE', ".live"), (b'PIRS', ".pirs"),
               (b'Rar!\x1a\x07\x00', ".rar"), (b'Rar!\x1a\x07\x01', ".rar"),
               (b'7z\xbc\xaf\x27\x1c', ".7z")]

UA = "YARGChartDownloader/2.0 (https://github.com/placeholder)"
# Some CDNs / Drive reject non-browser agents on the actual file download.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:149.0) "
              "Gecko/20100101 Firefox/149.0")

# ═══════════════════════════════════════════════════════════════════════════
#  HTTP SESSION — connection pooling + retries
# ═══════════════════════════════════════════════════════════════════════════

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    retry = Retry(total=2, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=10))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s

SESSION = make_session()

# ═══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def detect_type(data: bytes) -> str:
    for magic, ext in MAGIC_BYTES:
        if data[:len(magic)] == magic:
            return ext
    if b'<!DOCTYPE' in data[:500] or b'<html' in data[:500].lower():
        return ".html"
    return ""

def safe_name(t: str) -> str:
    return re.sub(r'[^a-zA-Z0-9\s\-\(\)\[\]&!,\']', '', t).strip()

def fix_url(url: str, base: str = "") -> str:
    if not url: return ""
    if url.startswith("//"): return "https:" + url
    if url.startswith("/"): return base.rstrip("/") + url
    if not url.startswith("http"): return "https://" + url
    return url

def pdiff(val) -> int:
    if val is None: return -1
    try:
        v = int(val)
        return -1 if v < 0 else min(v, 6)
    except (ValueError, TypeError):
        return -1

def diff_str(v: int) -> str:
    return "·" if v < 0 else str(v)

# Legacy default, still probed by find_songs_path() on Linux.
SONGS_PATH = "/mnt/ml-data/yarg-songs"

CONFIG_DIR = os.path.expanduser("~/.config/LemmeGetThatSong")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        log.warning(f"Could not save settings: {e}")


def yarg_config_dirs() -> list[str]:
    """Unity persistentDataPath locations for YARG (company: YARC)."""
    home = os.path.expanduser("~")
    if os.name == "nt":
        low = os.path.join(home, "AppData", "LocalLow")
        return [os.path.join(low, "YARC", "YARG"),
                os.path.join(low, "YARC Official", "YARG")]
    if sys.platform == "darwin":
        sup = os.path.join(home, "Library", "Application Support")
        return [os.path.join(sup, "YARC", "YARG"),
                os.path.join(sup, "unity.YARC.YARG")]
    cfg = os.path.join(home, ".config", "unity3d")
    return [os.path.join(cfg, "YARC", "YARG"),
            os.path.join(cfg, "YARC Official", "YARG")]


def detect_yarg_songs_path() -> str | None:
    """
    Read YARG's own settings to find where it actually loads songs from.

    YARG keeps config under Unity's persistentDataPath. The layout varies by
    version and release channel, so rather than depend on a filename or key,
    collect every string that resolves to an existing directory from:
      * any .json anywhere under the config dir (recursive)
      * the Unity `prefs` PlayerPrefs file (XML, sometimes base64 values)
    then pick whichever looks like an actual song library.
    """
    found: list[str] = []

    def harvest(node):
        if isinstance(node, str):
            if len(node) > 3 and os.path.isdir(node):
                found.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                harvest(v)
        elif isinstance(node, list):
            for v in node:
                harvest(v)

    def scan_prefs(path: str):
        try:
            raw = open(path, "rb").read().decode("utf-8", "replace")
        except OSError:
            return
        # Unity prefs is XML: <pref name="..." type="string">value</pref>.
        # Some builds base64 the payload, so try both.
        for m in re.finditer(r">([^<>]{4,})<", raw):
            val = m.group(1).strip()
            for cand in (val, _maybe_b64(val)):
                if cand and os.path.isdir(cand):
                    found.append(cand)
        # Last resort: any absolute-looking path in the blob.
        for m in re.finditer(r"(?:[A-Za-z]:\\[^\"<>|*?\n]{3,}|/[^\"<>|*?\n:]{3,})",
                             raw):
            p = m.group(0).strip()
            if os.path.isdir(p):
                found.append(p)

    def _maybe_b64(s: str) -> str | None:
        try:
            import base64
            d = base64.b64decode(s, validate=True).decode("utf-8", "ignore")
            return d if d.strip() else None
        except Exception:
            return None

    for d in yarg_config_dirs():
        if not os.path.isdir(d):
            continue
        for root, dirs, names in os.walk(d):
            dirs[:] = [x for x in dirs if x.lower() not in ("cache", "logs")]
            for name in names:
                fp = os.path.join(root, name)
                low = name.lower()
                if low.endswith(".json"):
                    try:
                        with open(fp, errors="replace") as f:
                            harvest(json.load(f))
                    except Exception:
                        continue
                elif low == "prefs" or low.endswith(".prefs"):
                    scan_prefs(fp)

    if not found:
        return None

    def looks_like_songs(p: str) -> bool:
        try:
            entries = os.listdir(p)[:200]
        except OSError:
            return False
        if any(e.lower().endswith((".sng", ".con", ".rb3con")) for e in entries):
            return True
        for e in entries[:40]:
            sub = os.path.join(p, e)
            if os.path.isdir(sub):
                try:
                    if any(x.lower() in ("song.ini", "notes.chart", "notes.mid",
                                         "songs.dta")
                           for x in os.listdir(sub)[:50]):
                        return True
                except OSError:
                    pass
        return False

    # De-dupe, keep order.
    seen, ordered = set(), []
    for p in found:
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            ordered.append(p)

    for p in ordered:
        if looks_like_songs(p):
            log.info(f"Detected YARG songs folder: {p}")
            return p

    for p in ordered:
        if "song" in os.path.basename(p).lower():
            log.info(f"Guessed YARG songs folder by name: {p}")
            return p
    return None


def find_songs_path() -> str:
    """Saved setting > YARG's own config > per-OS default."""
    saved = load_settings().get("songs_path")
    if saved and os.path.isdir(saved):
        return saved

    detected = detect_yarg_songs_path()
    if detected:
        return detected

    home = os.path.expanduser("~")
    candidates = []

    if os.name == "nt":
        docs = os.path.join(home, "Documents")
        candidates += [
            os.path.join(docs, "YARG", "Songs"),
            os.path.join(docs, "My Games", "YARG", "Songs"),
            os.path.join(home, "Music", "Charts"),
        ]
        default = os.path.join(docs, "YARG", "Songs")
    elif sys.platform == "darwin":
        candidates += [os.path.join(home, "Music", "YARG", "Songs")]
        default = os.path.join(home, "Music", "YARG", "Songs")
    else:
        candidates += [
            os.path.join(home, "YARG", "Songs"),
            os.path.join(home, "Music", "YARG"),
            "/mnt/ml-data/yarg-songs",
        ]
        default = os.path.join(home, "YARG", "Songs")

    for c in candidates:
        if os.path.isdir(c):
            return c

    try:
        os.makedirs(default, exist_ok=True)
    except OSError as e:
        log.warning(f"Could not create songs folder {default}: {e}")
        return home
    return default

INSTRUMENTS = [("All", "all"), ("Guitar", "guitar"), ("Bass", "bass"),
               ("Drums", "drums"), ("Keys", "keys"), ("Vocals", "vocals")]
INST_ICONS = {"all": "", "guitar": "", "bass": "",
              "drums": "", "keys": "", "vocals": ""}

# Segmented-button captions, and the reverse map for the change callback.
# Ctrl+<first letter of the code> is the accelerator for each (a/g/b/d/k/v).
SEG_LABELS = {code: f"{INST_ICONS[code]} {label}".strip()
              for label, code in INSTRUMENTS}
SEG_CODES = {v: k for k, v in SEG_LABELS.items()}

# ═══════════════════════════════════════════════════════════════════════════
#  YARG CHARTS — dynamic Next-Action hash
# ═══════════════════════════════════════════════════════════════════════════

_yc_action_cache = {"hash": None, "ts": 0.0}

def get_yc_action() -> str:
    if _yc_action_cache["hash"] and time.time() - _yc_action_cache["ts"] < 3600:
        return _yc_action_cache["hash"]
    try:
        r = SESSION.get(YC_URL, timeout=10)
        # Next.js embeds action IDs in the page's JS chunks
        matches = re.findall(r'"([0-9a-f]{40,})"', r.text)
        if matches:
            _yc_action_cache["hash"] = matches[-1]
            _yc_action_cache["ts"] = time.time()
            log.info(f"YARG Charts action hash: {matches[-1][:16]}...")
            return matches[-1]
    except Exception as e:
        log.warning(f"Failed to scrape YC action hash: {e}")
    # fallback
    return "6064f1febf3ccdf67ed2763ddac3bf82c5d3fafb68"


# ═══════════════════════════════════════════════════════════════════════════
#  TASTE ENGINE — MusicBrainz TF-IDF + ListenBrainz similar artists
# ═══════════════════════════════════════════════════════════════════════════

IGNORE_TAGS = frozenset({
    "seen live", "favorites", "favourite", "favorite", "my favorite",
    "check out", "todo", "to listen", "spotify", "awesome",
})

GENERIC_TAGS = frozenset({
    "rock", "metal", "alternative", "indie", "electronic", "pop",
    "american", "british", "canadian", "australian", "male vocalists",
    "female vocalists", "singer-songwriter",
})


class TasteEngine:
    """
    Personal music taste profiler.
    - Tracks download history per artist
    - Resolves artists → MusicBrainz IDs → tags
    - Builds TF-IDF weighted taste vector
    - Discovers similar artists via ListenBrainz + MB tag search
    - Scores candidates against taste vector (cosine similarity)
    """

    def __init__(self):
        self.dir = os.path.expanduser("~/.config/LemmeGetThatSong")
        self.path = os.path.join(self.dir, "taste.json")
        self._mb_lock = threading.Lock()
        self._last_mb = 0.0

        # Persisted state
        self.downloads: dict[str, int] = {}          # artist_lower → count
        self.mbids: dict[str, str] = {}              # artist_lower → mbid
        self.artist_tags: dict[str, dict] = {}       # artist_lower → {tag: weight}
        self.taste_vector: dict[str, float] = {}     # tag → TF-IDF score
        self.recommendations: list[str] = []
        self.discovery: list[str] = []
        self.lb_available: bool | None = None        # None = untested

        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            self.downloads = d.get("downloads", {})
            self.mbids = d.get("mbids", {})
            self.artist_tags = d.get("artist_tags", {})
            self.taste_vector = d.get("taste_vector", {})
            self.recommendations = d.get("recommendations", [])
            self.discovery = d.get("discovery", [])
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"Failed to load taste profile: {e}")

    def save(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.path, "w") as f:
                json.dump({
                    "downloads": self.downloads,
                    "mbids": self.mbids,
                    "artist_tags": self.artist_tags,
                    "taste_vector": self.taste_vector,
                    "recommendations": self.recommendations,
                    "discovery": self.discovery,
                }, f, indent=2)
        except Exception as e:
            log.warning(f"Failed to save taste profile: {e}")

    def _mb_throttle(self):
        """MusicBrainz requires 1 req/sec."""
        with self._mb_lock:
            wait = max(0, 1.1 - (time.time() - self._last_mb))
            if wait > 0:
                time.sleep(wait)
            self._last_mb = time.time()

    def _mb_get(self, path: str, params: dict | None = None):
        self._mb_throttle()
        url = f"{MB_BASE}{path}"
        try:
            r = SESSION.get(url, params={**(params or {}), "fmt": "json"}, timeout=10)
            if r.status_code == 200:
                return r.json()
            log.debug(f"MB {r.status_code}: {url}")
        except Exception as e:
            log.debug(f"MB error: {e}")
        return None

    def _lb_get(self, path: str):
        """ListenBrainz GET — auto-detects if API is reachable."""
        if self.lb_available is False:
            return None
        try:
            r = SESSION.get(f"{LB_BASE}{path}", timeout=8)
            if r.status_code == 200:
                self.lb_available = True
                return r.json()
            log.debug(f"LB {r.status_code}: {path}")
        except Exception as e:
            if self.lb_available is None:
                log.info(f"ListenBrainz unavailable, using MB only: {e}")
                self.lb_available = False
        return None

    def resolve_mbid(self, name: str) -> str | None:
        low = name.lower().strip()
        if low in self.mbids:
            return self.mbids[low]
        data = self._mb_get("/artist/", {"query": f'artist:"{name}"', "limit": "3"})
        if not data or not data.get("artists"):
            return None
        best = data["artists"][0]
        mbid = best.get("id")
        if mbid:
            self.mbids[low] = mbid
        return mbid

    def fetch_tags(self, name: str) -> dict[str, float]:
        low = name.lower().strip()
        # Cached entries are authoritative, including an empty one ("MB has no
        # tags for this artist") — that is the negative cache.
        if low in self.artist_tags:
            return self.artist_tags[low]
        mbid = self.resolve_mbid(name)
        if not mbid:
            return {}
        data = self._mb_get(f"/artist/{mbid}", {"inc": "tags+genres"})
        if not data:
            return {}
        tags = {}
        # Genres (curated, higher quality)
        for g in data.get("genres", []):
            gn = g.get("name", "").lower().strip()
            c = g.get("count", 0)
            if gn and c > 0 and gn not in IGNORE_TAGS:
                tags[gn] = max(tags.get(gn, 0), c * 1.5)  # boost curated genres
        # Tags (user-submitted)
        for t in data.get("tags", []):
            tn = t.get("name", "").lower().strip()
            c = t.get("count", 0)
            if tn and c > 0 and tn not in IGNORE_TAGS:
                tags[tn] = max(tags.get(tn, 0), c)
        self.artist_tags[low] = tags
        return tags

    def fetch_similar_lb(self, name: str) -> list[tuple[str, str]]:
        """Get similar artists from ListenBrainz. Returns [(name, mbid), ...]"""
        mbid = self.mbids.get(name.lower().strip())
        if not mbid:
            return []
        data = self._lb_get(f"/artist/{mbid}/similar")
        if not data or "payload" not in data:
            return []
        results = []
        for item in data["payload"].get("artists", [])[:20]:
            n = item.get("name", "")
            m = item.get("artist_mbid", "")
            if n and m:
                results.append((n, m))
        return results

    def fetch_similar_mb(self, name: str) -> list[str]:
        """Get related artists from MusicBrainz relationships."""
        mbid = self.mbids.get(name.lower().strip())
        if not mbid:
            return []
        data = self._mb_get(f"/artist/{mbid}", {"inc": "artist-rels"})
        if not data:
            return []
        related = set()
        for rel in data.get("relations", []):
            target = rel.get("artist", {})
            rn = target.get("name", "")
            if rn and rn.lower() != name.lower():
                related.add(rn)
        return list(related)

    def rebuild_taste(self):
        """Build TF-IDF weighted taste vector from downloaded artists."""
        if not self.downloads:
            self.taste_vector = {}
            return

        # Document frequency: how many artists have each tag
        doc_freq: dict[str, int] = defaultdict(int)
        n_artists = len(self.downloads)
        for artist in self.downloads:
            for tag in self.artist_tags.get(artist, {}):
                doc_freq[tag] += 1

        # Weighted TF-IDF
        taste: dict[str, float] = defaultdict(float)
        for artist, dl_count in self.downloads.items():
            tags = self.artist_tags.get(artist, {})
            if not tags:
                continue
            max_w = max(tags.values())
            for tag, weight in tags.items():
                tf = weight / max_w
                idf = math.log(1 + n_artists / (1 + doc_freq.get(tag, 0)))
                # Generic tags get additional dampening
                penalty = 0.3 if tag in GENERIC_TAGS else 1.0
                taste[tag] += tf * idf * penalty * (1 + dl_count * 0.3)

        # Normalize to 0-100
        mx = max(taste.values()) if taste else 1
        self.taste_vector = {t: round(v / mx * 100, 1)
                            for t, v in taste.items() if v / mx > 0.05}

    def _cosine_sim(self, vec_a: dict, vec_b: dict) -> float:
        """Cosine similarity between two sparse tag vectors."""
        common = set(vec_a) & set(vec_b)
        if not common:
            return 0.0
        dot = sum(vec_a[t] * vec_b[t] for t in common)
        mag_a = math.sqrt(sum(v ** 2 for v in vec_a.values()))
        mag_b = math.sqrt(sum(v ** 2 for v in vec_b.values()))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    def _score_candidate(self, name: str) -> tuple[float, float]:
        """Score a candidate: (full_similarity, discovery_score).
        Discovery emphasizes rare/specific tags over generic ones."""
        tags = self.fetch_tags(name)
        if not tags or not self.taste_vector:
            return (0.0, 0.0)

        # Normalize candidate tags
        mx = max(tags.values())
        norm = {t: v / mx for t, v in tags.items()}

        full = self._cosine_sim(self.taste_vector, norm)

        # Discovery: filter out generic tags from both vectors
        taste_rare = {t: v for t, v in self.taste_vector.items() if t not in GENERIC_TAGS}
        cand_rare = {t: v for t, v in norm.items() if t not in GENERIC_TAGS}
        disc = self._cosine_sim(taste_rare, cand_rare)

        return (full, disc)

    def generate_recommendations(self):
        """Build recommendation + discovery lists."""
        if not self.taste_vector:
            self.recommendations = []
            self.discovery = []
            return

        known = set(self.downloads.keys())
        candidates: set[str] = set()

        # Source 1: ListenBrainz similar artists (best signal)
        top_artists = sorted(self.downloads, key=self.downloads.get, reverse=True)[:5]
        for artist in top_artists:
            for name, mbid in self.fetch_similar_lb(artist):
                low = name.lower().strip()
                if low not in known:
                    candidates.add(name)
                    self.mbids[low] = mbid

        # Source 2: MusicBrainz relationships
        for artist in top_artists[:3]:
            for name in self.fetch_similar_mb(artist):
                if name.lower().strip() not in known:
                    candidates.add(name)

        # Source 3: MB tag search on top taste tags
        top_tags = sorted(self.taste_vector, key=self.taste_vector.get, reverse=True)
        rare_tags = [t for t in top_tags if t not in GENERIC_TAGS][:4]
        for tag in rare_tags:
            data = self._mb_get("/artist/", {"query": f"tag:{quote(tag)}", "limit": "15"})
            if data:
                for a in data.get("artists", []):
                    n = a.get("name", "")
                    if n and n.lower().strip() not in known:
                        candidates.add(n)
                        mid = a.get("id")
                        if mid:
                            self.mbids[n.lower().strip()] = mid

        # Score all candidates
        scored = {}
        disc_scored = {}
        for name in list(candidates)[:40]:  # cap to avoid hammering MB
            full, disc = self._score_candidate(name)
            scored[name] = full
            disc_scored[name] = disc

        self.recommendations = sorted(
            [n for n, s in scored.items() if s > 0.05],
            key=scored.get, reverse=True
        )[:8]

        # Discovery: high rare-tag overlap, not already in recommendations
        rec_set = set(self.recommendations)
        self.discovery = sorted(
            [n for n, s in disc_scored.items() if s > 0.05 and n not in rec_set],
            key=disc_scored.get, reverse=True
        )[:4]

    def record_download(self, artist: str):
        low = artist.lower().strip()
        if not low:
            return
        self.downloads[low] = self.downloads.get(low, 0) + 1

    def background_update(self, artists: list[str], callback=None):
        def _run():
            try:
                for a in artists[:5]:
                    self.fetch_tags(a)
                self.rebuild_taste()
                self.generate_recommendations()
                self.save()
                log.info(f"Taste updated: {len(self.taste_vector)} tags, "
                         f"{len(self.recommendations)} recs")
            except Exception as e:
                log.error(f"Taste update failed: {e}")
            if callback:
                callback()
        threading.Thread(target=_run, daemon=True).start()

    def top_tags(self, n: int = 8) -> list[tuple[str, float]]:
        return sorted(self.taste_vector.items(),
                      key=lambda x: x[1], reverse=True)[:n]


# ═══════════════════════════════════════════════════════════════════════════
#  DUPLICATE DETECTION
# ═══════════════════════════════════════════════════════════════════════════

def scan_existing(songs_dir: str) -> set[str]:
    """Return set of 'artist - title' keys already in songs folder."""
    existing = set()
    if not os.path.isdir(songs_dir):
        return existing
    for name in os.listdir(songs_dir):
        # Most chart folders/files are "Artist - Title" format
        clean = re.sub(r'\.(sng|con|live|pirs|zip)$', '', name, flags=re.I)
        existing.add(clean.lower().strip())
    return existing


# ═══════════════════════════════════════════════════════════════════════════
#  APP
# ═══════════════════════════════════════════════════════════════════════════

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Tk root now exists, so the font list is queryable. Must happen
        # before any widget is constructed, since they read FONT at build time.
        global FONT
        FONT = resolve_font()
        log.info(f"UI font: {FONT}")

        ctk.set_appearance_mode("dark")

        # ── DPI scaling ─────────────────────────────────────────────────
        # CustomTkinter already applies DPI-aware scaling on Windows and
        # macOS. Overriding it there double-scales the whole UI. Only Linux
        # (especially KDE/Wayland) misreports scaling to Tk, so correct it
        # there and leave the other platforms alone.
        # ── UI scaling ──────────────────────────────────────────────────
        # CustomTkinter multiplies every dimension by the monitor's reported
        # DPI factor, on top of the scaling Tk already applies. Dividing by
        # that factor cancels it, leaving ui_scale as the only knob.
        try:
            from customtkinter.windows.widgets.scaling.scaling_tracker \
                import ScalingTracker
            self._dpi_factor = ScalingTracker.window_dpi_scaling_dict.get(self, 1.0)
        except Exception:
            self._dpi_factor = 1.0
        if not self._dpi_factor or self._dpi_factor <= 0:
            self._dpi_factor = 1.0

        env = os.environ.get("LGTS_UI_SCALE")
        saved = load_settings().get("ui_scale", 1.0)
        try:
            self.ui_scale = float(env) if env else float(saved)
        except (TypeError, ValueError):
            self.ui_scale = 1.0
        self.ui_scale = max(UI_SCALE_MIN, min(self.ui_scale, UI_SCALE_MAX))

        self._apply_ui_scale(self.ui_scale, persist=False)
        log.info(f"DPI factor {self._dpi_factor:.2f}, UI scale {self.ui_scale:.2f}")

        self.title("LemmeGetThatSong")
        # Fit the screen rather than assuming one. 1400x900 overflows a
        # 1366x768 laptop or a small VM display.
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1400, int(sw * 0.9)), min(900, int(sh * 0.9))
        self.geometry(f"{w}x{h}")
        self.minsize(min(1000, w), min(650, h))
        self.configure(fg_color=BG)

        self._results: list[dict] = []
        self._results_lock = threading.Lock()
        self.songs_path = ctk.StringVar(value=find_songs_path())
        self.songs_path.trace_add("write", lambda *_: self._persist_songs_path())
        self.active_filter = "all"
        self._last_status = ""
        self.sort_col = None
        self.sort_rev = False
        self._art_id = 0
        self._art_image = None
        self.art_cache: dict[str, ImageTk.PhotoImage] = {}
        self.existing_charts: set[str] = set()
        self.taste = TasteEngine()
        self._dl_queue: list[dict] = []
        self._dl_active = False

        self._setup_ttk_theme()
        self._build_ui()
        self._scan_existing_bg()
        self._setup_keybinds()
        # Focus the search bar on startup
        self.after(100, lambda: self.search_entry.focus_set())

    @property
    def results(self) -> list[dict]:
        return self._results

    @results.setter
    def results(self, val: list[dict]):
        with self._results_lock:
            self._results = val

    def _get_result(self, idx: int) -> dict | None:
        with self._results_lock:
            if 0 <= idx < len(self._results):
                return self._results[idx]
        return None

    def _setup_ttk_theme(self):
        """Style the ttk Treeview to match Catppuccin."""
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Treeview",
                     background=BG2, foreground=TEXT, fieldbackground=BG2,
                     rowheight=48, font=(FONT, 16), borderwidth=0)
        s.configure("Treeview.Heading",
                     background=SURF0, foreground=SUB,
                     font=(FONT, 14, "bold"), padding=(8, 10),
                     borderwidth=0, relief="flat")
        s.map("Treeview",
              background=[("selected", SURF1)],
              foreground=[("selected", TEXT)])
        s.map("Treeview.Heading",
              background=[("active", SURF1)],
              relief=[("active", "flat")])
        s.configure("Vertical.TScrollbar",
                     background=SURF0, troughcolor=BG2,
                     borderwidth=0, arrowsize=0)
        s.map("Vertical.TScrollbar", background=[("active", SURF1)])
        s.layout("Vertical.TScrollbar",
                 [('Vertical.Scrollbar.trough',
                   {'children': [('Vertical.Scrollbar.thumb',
                                  {'expand': '1', 'sticky': 'nswe'})],
                    'sticky': 'ns'})])

    def _build_ui(self):
        # ── Header ──────────────────────────────────────────────────────
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(18, 0))

        ctk.CTkLabel(header, text="LemmeGetThatSong",
                     font=ctk.CTkFont(FONT, 32, "bold"),
                     text_color=TEXT).pack(side="left")

        path_frame = ctk.CTkFrame(header, fg_color="transparent")
        path_frame.pack(side="right")
        ctk.CTkLabel(path_frame, text="Songs folder:",
                     font=ctk.CTkFont(FONT, 15),
                     text_color=SUB).pack(side="left", padx=(0, 8))
        ctk.CTkEntry(path_frame, textvariable=self.songs_path, width=280,
                     fg_color=SURF0, border_color=SURF1, text_color=TEXT,
                     font=ctk.CTkFont(FONT, 16)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(path_frame, text="...", width=36, height=36,
                      fg_color=SURF0, hover_color=SURF1, text_color=TEXT,
                      command=self._browse).pack(side="left")

        ctk.CTkButton(path_frame, text="Settings", width=80, height=36,
                      font=ctk.CTkFont(FONT, 14),
                      fg_color=SURF0, hover_color=SURF1, text_color=TEXT,
                      command=self._open_settings).pack(side="left", padx=(8, 0))

        # ── Search Bar ──────────────────────────────────────────────────
        search_frame = ctk.CTkFrame(self, fg_color="transparent")
        search_frame.pack(fill="x", padx=24, pady=(16, 0))

        self.search_var = ctk.StringVar()
        self.search_entry = ctk.CTkEntry(
            search_frame, textvariable=self.search_var,
            placeholder_text="Search for songs, artists, or charts...",
            height=56, font=ctk.CTkFont(FONT, 24),
            fg_color=SURF0, border_color=SURF1, text_color=TEXT,
            placeholder_text_color=OVERLAY0, corner_radius=12)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.search_entry.bind("<Return>", lambda e: self._start_search())

        self.search_btn = ctk.CTkButton(
            search_frame, text="SEARCH", height=56, width=130,
            font=ctk.CTkFont(FONT, 20, "bold"), corner_radius=12,
            fg_color=BLUE, hover_color=LAVENDER, text_color=CRUST,
            command=self._start_search)
        self.search_btn.pack(side="right")

        # ── Filters ─────────────────────────────────────────────────────
        filter_frame = ctk.CTkFrame(self, fg_color="transparent")
        filter_frame.pack(fill="x", padx=24, pady=(12, 0))

        self.inst_seg = ctk.CTkSegmentedButton(
            filter_frame,
            values=list(SEG_LABELS.values()),
            font=ctk.CTkFont(FONT, 15),
            fg_color=SURF0, selected_color=BLUE,
            selected_hover_color=LAVENDER,
            unselected_color=SURF0, unselected_hover_color=SURF1,
            text_color=TEXT, text_color_disabled=SUB,
            corner_radius=8, command=self._on_filter_change)
        self.inst_seg.set(SEG_LABELS["all"])
        self.inst_seg.pack(side="left")

        self.pro_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(filter_frame, text="Pro Drums Only",
                      variable=self.pro_var,
                      font=ctk.CTkFont(FONT, 15),
                      text_color=SUB, fg_color=SURF1,
                      progress_color=MAUVE, button_color=TEXT,
                      button_hover_color=LAVENDER).pack(side="right")

        self.scope_seg = ctk.CTkSegmentedButton(
            filter_frame,
            values=["All", "Artist", "Title"],
            font=ctk.CTkFont(FONT, 15),
            fg_color=SURF0, selected_color=MAUVE,
            selected_hover_color=LAVENDER,
            unselected_color=SURF0, unselected_hover_color=SURF1,
            text_color=TEXT, text_color_disabled=SUB,
            corner_radius=8, command=lambda _v: self._on_scope_change())
        self.scope_seg.set("All")
        self.scope_seg.pack(side="right", padx=(0, 24))

        ctk.CTkLabel(filter_frame, text="Search in:",
                     font=ctk.CTkFont(FONT, 14),
                     text_color=SUB).pack(side="right", padx=(0, 6))

        # ── Taste Chips ─────────────────────────────────────────────────
        self.taste_frame = ctk.CTkFrame(self, fg_color="transparent", height=32)
        self.taste_frame.pack(fill="x", padx=24, pady=(10, 0))
        self._render_taste()

        # ── Recommendation Chips ────────────────────────────────────────
        self.rec_frame = ctk.CTkFrame(self, fg_color="transparent", height=32)
        self.rec_frame.pack(fill="x", padx=24, pady=(4, 0))
        self._render_recs()

        # ── Status Bar ──────────────────────────────────────────────────
        self.status_var = ctk.StringVar(value="Ready — search for a song to get started")
        status_bar = ctk.CTkFrame(self, fg_color=CRUST, height=32, corner_radius=0)
        status_bar.pack(side="bottom", fill="x")
        ctk.CTkLabel(status_bar, textvariable=self.status_var,
                     font=ctk.CTkFont(FONT, 24),
                     text_color=SUB, anchor="w").pack(side="left", padx=16, pady=4)

        self.queue_label = ctk.CTkLabel(status_bar, text="",
                                        font=ctk.CTkFont(FONT, 24),
                                        text_color=TEAL, anchor="e")
        self.queue_label.pack(side="right", padx=16, pady=4)

        # ── Main Content ────────────────────────────────────────────────
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=24, pady=(12, 0))
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        # Table
        table_frame = ctk.CTkFrame(content, fg_color=BG2, corner_radius=12)
        table_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        inner_table = tk.Frame(table_frame, bg=BG2)
        inner_table.pack(fill="both", expand=True, padx=2, pady=2)

        cols = ("st", "artist", "title", "source", "charter", "G", "B", "D", "K", "V", "DLs")
        self.tree = ttk.Treeview(inner_table, columns=cols, show="headings", selectmode="browse")

        col_spec = {
            # name: (label, width, minwidth, stretch)
            "st": ("", 40, 40, False),
            "artist": ("Artist", 260, 140, True),
            "title": ("Title", 380, 200, True),
            "source": ("Source", 110, 90, False),
            "charter": ("Charter", 180, 120, True),
            "G": ("G", 46, 46, False), "B": ("B", 46, 46, False),
            "D": ("D", 46, 46, False), "K": ("K", 46, 46, False),
            "V": ("V", 46, 46, False), "DLs": ("DLs", 65, 65, False),
        }
        for c, (lbl, w, mw, stretch) in col_spec.items():
            self.tree.heading(c, text=lbl,
                              command=lambda _c=c: self._sort(_c))
            anchor = tk.W if c in ("artist", "title", "charter") else tk.CENTER
            self.tree.column(c, width=w, minwidth=mw, anchor=anchor,
                             stretch=stretch)

        sb = ttk.Scrollbar(inner_table, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda e: self._queue_dl())

        # Detail panel
        detail = ctk.CTkFrame(content, fg_color=BG2, width=340, corner_radius=12)
        detail.grid(row=0, column=1, sticky="nsew")
        detail.grid_propagate(False)

        pad = ctk.CTkFrame(detail, fg_color="transparent")
        pad.pack(fill="both", expand=True, padx=16, pady=16)

        self.art_label = tk.Label(pad, bg=BG2, width=280, height=300)
        self.art_label.pack(pady=(0, 14))
        self._set_art_placeholder()

        self.d_title = ctk.CTkLabel(pad, text="Select a chart",
                                    font=ctk.CTkFont(FONT, 20, "bold"),
                                    text_color=TEXT, wraplength=300, anchor="w")
        self.d_title.pack(anchor="w", pady=(0, 2))
        self.d_artist = ctk.CTkLabel(pad, text="", font=ctk.CTkFont(FONT, 16),
                                     text_color=SUB, wraplength=300, anchor="w")
        self.d_artist.pack(anchor="w", pady=(0, 2))
        self.d_album = ctk.CTkLabel(pad, text="", font=ctk.CTkFont(FONT, 15),
                                    text_color=OVERLAY0, wraplength=300, anchor="w")
        self.d_album.pack(anchor="w", pady=(0, 2))
        self.d_charter = ctk.CTkLabel(pad, text="", font=ctk.CTkFont(FONT, 15),
                                      text_color=OVERLAY0, wraplength=300, anchor="w")
        self.d_charter.pack(anchor="w", pady=(0, 2))
        self.d_source = ctk.CTkLabel(pad, text="", font=ctk.CTkFont(FONT, 15),
                                     text_color=OVERLAY0, wraplength=300, anchor="w")
        self.d_source.pack(anchor="w", pady=(0, 14))

        # Difficulty bars
        self.diff_frame = ctk.CTkFrame(pad, fg_color="transparent")
        self.diff_frame.pack(fill="x", pady=(0, 6))
        self.diff_bars: dict[str, tuple[ctk.CTkLabel, ctk.CTkProgressBar, ctk.CTkLabel]] = {}
        for code, name in [("G", "Guitar"), ("B", "Bass"), ("D", "Drums"),
                           ("K", "Keys"), ("V", "Vocals")]:
            row = ctk.CTkFrame(self.diff_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            lbl = ctk.CTkLabel(row, text=f"{name}", width=55,
                               font=ctk.CTkFont(FONT, 24),
                               text_color=SUB, anchor="w")
            lbl.pack(side="left")
            bar = ctk.CTkProgressBar(row, width=110, height=10,
                                     fg_color=SURF0, progress_color=SURF2,
                                     corner_radius=5)
            bar.set(0)
            bar.pack(side="left", padx=(4, 6))
            val = ctk.CTkLabel(row, text="·", width=20,
                               font=ctk.CTkFont(FONT, 16, "bold"),
                               text_color=SURF2)
            val.pack(side="left")
            self.diff_bars[code] = (lbl, bar, val)

        self.pro_badge = ctk.CTkLabel(pad, text="",
                                      font=ctk.CTkFont(FONT, 16, "bold"),
                                      text_color=MAUVE)
        self.pro_badge.pack(anchor="w", pady=(4, 14))

        self.dl_btn = ctk.CTkButton(
            pad, text="DOWNLOAD", height=50,
            font=ctk.CTkFont(FONT, 20, "bold"), corner_radius=10,
            fg_color=BLUE, hover_color=LAVENDER, text_color=CRUST,
            command=self._queue_dl)
        self.dl_btn.pack(fill="x", pady=(0, 6))

        self.dl_progress = ctk.CTkProgressBar(pad, height=4,
                                               fg_color=SURF0, progress_color=GREEN,
                                               corner_radius=2)
        self.dl_progress.pack(fill="x", pady=(0, 8))
        self.dl_progress.set(0)

        ctk.CTkButton(pad, text="Open Source Page", height=40,
                      font=ctk.CTkFont(FONT, 15), corner_radius=8,
                      fg_color=SURF0, hover_color=SURF1, text_color=TEXT,
                      command=self._open_page).pack(fill="x")

        if HAS_PREVIEW:
            self.preview_btn = ctk.CTkButton(
                pad, text="Preview (30s)", height=40,
                font=ctk.CTkFont(FONT, 15), corner_radius=8,
                fg_color=SURF0, hover_color=SURF1, text_color=TEXT,
                command=self._toggle_preview)
            self.preview_btn.pack(fill="x", pady=(8, 0))

        ctk.CTkButton(pad, text="Show in Folder", height=40,
                      font=ctk.CTkFont(FONT, 15), corner_radius=8,
                      fg_color=SURF0, hover_color=SURF1, text_color=TEXT,
                      command=self._show_in_folder).pack(fill="x", pady=(8, 0))

        if HAS_MOGG_CHECK:
            ctk.CTkButton(pad, text="Repair Library Audio", height=36,
                          font=ctk.CTkFont(FONT, 14), corner_radius=8,
                          fg_color=SURF0, hover_color=SURF1, text_color=SUB,
                          command=self._repair_library).pack(fill="x", pady=(8, 0))

    # ── Taste + Recommendation Rendering ────────────────────────────────

    def _chip_label(self, parent, text: str, color: str, padx):
        ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(FONT, 15, "bold"),
                     text_color=color).pack(side="left", padx=padx)

    def _chip_button(self, parent, text: str, color: str, query: str,
                     outlined: bool = False):
        ctk.CTkButton(
            parent, text=text, height=30, corner_radius=12,
            font=ctk.CTkFont(FONT, 24),
            fg_color=SURF0, hover_color=SURF1, text_color=color,
            border_width=1 if outlined else 0,
            border_color=color if outlined else SURF0,
            command=lambda q=query: self._chip(q)
        ).pack(side="left", padx=2)

    def _render_taste(self):
        for w in self.taste_frame.winfo_children():
            w.destroy()
        tags = self.taste.top_tags(8)
        if not tags:
            ctk.CTkLabel(self.taste_frame, text="Download songs to build your taste profile",
                         font=ctk.CTkFont(FONT, 24),
                         text_color=SURF2).pack(side="left")
            return
        self._chip_label(self.taste_frame, "Your taste:", SUB, (0, 8))
        for i, (tag, score) in enumerate(tags):
            self._chip_button(self.taste_frame, f"{tag} {int(score)}",
                              TAG_COLORS[i % len(TAG_COLORS)], tag, outlined=True)

    def _render_recs(self):
        for w in self.rec_frame.winfo_children():
            w.destroy()
        recs = self.taste.recommendations
        disc = self.taste.discovery
        if recs:
            self._chip_label(self.rec_frame, "Try:", TEAL, (0, 6))
            for name in recs:
                self._chip_button(self.rec_frame, name, TEAL, name)
        if disc:
            self._chip_label(self.rec_frame, "  Discover:", MAUVE, (8, 6))
            for name in disc:
                self._chip_button(self.rec_frame, name, MAUVE, name, outlined=True)

    def _chip(self, name: str):
        self.search_var.set(name)
        self._start_search()

    # ── UI Helpers ──────────────────────────────────────────────────────

    def _setup_keybinds(self):
        """Ctrl+<letter> switches the instrument filter without leaving the search bar."""
        for code, label in SEG_LABELS.items():
            def handler(event, c=code, l=label):
                self.active_filter = c
                self.inst_seg.set(l)
                return "break"  # stop Entry from eating the keypress
            key = code[0]
            # bind_all so it works even while the search Entry has focus
            self.bind_all(f"<Control-{key}>", handler)
            self.bind_all(f"<Control-{key.upper()}>", handler)

    def _on_filter_change(self, value: str):
        self.active_filter = SEG_CODES.get(value, self.active_filter)

    def _on_scope_change(self):
        # Scoping is applied to results, so re-filter without re-fetching.
        if self.results:
            self.after(0, lambda: self._fill(self._last_status))

    def _browse(self):
        p = filedialog.askdirectory(initialdir=self.songs_path.get())
        if p:
            self.songs_path.set(p)
            self._persist_songs_path()
            self._scan_existing_bg()

    def _persist_songs_path(self, *_):
        p = self.songs_path.get().strip()
        if not p or not os.path.isdir(p):
            return
        s = load_settings()
        if s.get("songs_path") == p:
            return
        s["songs_path"] = p
        save_settings(s)
        log.info(f"Saved songs folder: {p}")

    def _scan_existing_bg(self):
        def _run():
            self.existing_charts = scan_existing(self.songs_path.get())
            log.info(f"Found {len(self.existing_charts)} existing charts")
        threading.Thread(target=_run, daemon=True).start()

    def _set_art_placeholder(self):
        self.art_label.configure(image="", text="",
                                 font=(FONT, 64), fg=SURF1,
                                 width=18, height=8)
        self._art_image = None

    def _sort(self, col):
        if self.sort_col == col:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_col, self.sort_rev = col, False
        data = [(self.tree.set(iid, col), iid) for iid in self.tree.get_children()]
        try:
            data.sort(key=lambda x: int(x[0]) if x[0] not in ("·", "", "✓") else -1,
                      reverse=self.sort_rev)
        except ValueError:
            data.sort(key=lambda x: x[0].lower(), reverse=self.sort_rev)
        for i, (_, iid) in enumerate(data):
            self.tree.move(iid, "", i)

    def _on_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        c = self._get_result(int(sel[0]))
        if not c:
            return

        self.d_title.configure(text=c.get("title", ""))
        self.d_artist.configure(text=c.get("artist", ""))
        self.d_album.configure(text=c.get("album") or "")
        self.d_charter.configure(
            text=f"Charted by {c['charter']}" if c.get("charter") else "")
        dls = c.get("downloads", 0)
        src = c.get("source", "")
        self.d_source.configure(
            text=f"{src}  ·  {dls} downloads" if dls else src)

        for code, key in [("G", "dg"), ("B", "db"), ("D", "dd"),
                          ("K", "dk"), ("V", "dv")]:
            v = c.get(key, -1)
            _, bar, val_lbl = self.diff_bars[code]
            val_lbl.configure(text=diff_str(v), text_color=diff_color(v))
            if v >= 0:
                bar.configure(progress_color=diff_color(v))
                bar.set(v / 6.0)
            else:
                bar.configure(progress_color=SURF2)
                bar.set(0)

        self.pro_badge.configure(text="* Pro Drums" if c.get("has_pro") else "")

        art = c.get("art_url", "")
        if art and HAS_PIL:
            self._load_art(art)
        else:
            self._set_art_placeholder()

    def _load_art(self, url: str):
        self._art_id += 1
        lid = self._art_id
        if url in self.art_cache:
            self._show_art(self.art_cache[url])
            return
        threading.Thread(target=self._fetch_art, args=(url, lid), daemon=True).start()

    def _fetch_art(self, url: str, lid: int):
        try:
            r = SESSION.get(url, timeout=8)
            if r.status_code != 200 or lid != self._art_id:
                return
            img = Image.open(io.BytesIO(r.content)).resize((260, 260), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.art_cache[url] = photo
            if lid == self._art_id:
                self.after(0, lambda: self._show_art(photo))
        except Exception as e:
            log.debug(f"Art fetch failed: {e}")

    def _show_art(self, photo):
        self._art_image = photo
        self.art_label.configure(image=photo, text="", width=260, height=260)

    def _toggle_preview(self):
        if not HAS_PREVIEW:
            return
        if preview_mod.is_playing():
            preview_mod.stop()
            self.preview_btn.configure(text="Preview (30s)")
            return

        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Select a chart", "Click a song first.")
            return
        c = self._get_result(int(sel[0]))
        if not c:
            return

        self.preview_btn.configure(text="Loading…", state="disabled")

        def work():
            p = preview_mod.lookup(c.get("artist", ""), c.get("title", ""),
                                   SESSION)
            def done():
                self.preview_btn.configure(state="normal")
                if not p:
                    self.preview_btn.configure(text="Preview (30s)")
                    self.status_var.set("No preview found for this track")
                    return
                if p.playable and preview_mod.play(p.url, SESSION):
                    self.preview_btn.configure(text="Stop preview")
                    self.status_var.set(f"Preview: {p.artist} — {p.title}")
                    self._watch_preview()
                else:
                    self.preview_btn.configure(text="Preview (30s)")
                    if messagebox.askyesno(
                            "Open preview",
                            "Cannot play this preview in-app "
                            "(install pygame for built-in playback).\n\n"
                            "Open it in your browser?"):
                        webbrowser.open(p.url)
            self.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _watch_preview(self):
        """Reset the button once playback finishes on its own."""
        if preview_mod.is_playing():
            self.after(500, self._watch_preview)
        else:
            self.preview_btn.configure(text="Preview (30s)")

    def _apply_ui_scale(self, value: float, persist: bool = True):
        """Set UI size. 1.0 is 'correct' regardless of the display's DPI."""
        value = max(UI_SCALE_MIN, min(round(value, 2), UI_SCALE_MAX))
        self.ui_scale = value
        scale = value / self._dpi_factor
        ctk.set_widget_scaling(scale)
        ctk.set_window_scaling(scale)
        if persist:
            s = load_settings()
            s["ui_scale"] = value
            save_settings(s)
        if hasattr(self, "_scale_readout") and self._scale_readout.winfo_exists():
            self._scale_readout.configure(text=f"{int(value * 100)}%")

    def _nudge_ui_scale(self, delta: float):
        self._apply_ui_scale(self.ui_scale + delta)

    def _open_settings(self):
        if getattr(self, "_settings_win", None) and self._settings_win.winfo_exists():
            self._settings_win.focus()
            return

        win = ctk.CTkToplevel(self)
        self._settings_win = win
        win.title("Settings")
        win.configure(fg_color=BG)
        win.transient(self)
        win.resizable(False, False)

        pad = ctk.CTkFrame(win, fg_color="transparent")
        pad.pack(padx=24, pady=24, fill="both", expand=True)

        ctk.CTkLabel(pad, text="Interface size",
                     font=ctk.CTkFont(FONT, 17, "bold"),
                     text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(pad,
                     text="100% is the intended size. Lower it if the app "
                          "looks too large on your display.",
                     font=ctk.CTkFont(FONT, 13), text_color=SUB,
                     wraplength=320, justify="left").pack(anchor="w", pady=(2, 14))

        row = ctk.CTkFrame(pad, fg_color="transparent")
        row.pack(fill="x")

        ctk.CTkButton(row, text="−", width=44, height=38,
                      font=ctk.CTkFont(FONT, 20),
                      fg_color=SURF0, hover_color=SURF1, text_color=TEXT,
                      command=lambda: self._nudge_ui_scale(-UI_SCALE_STEP)
                      ).pack(side="left")

        self._scale_readout = ctk.CTkLabel(
            row, text=f"{int(self.ui_scale * 100)}%", width=90, height=38,
            font=ctk.CTkFont(FONT, 18, "bold"), text_color=TEXT)
        self._scale_readout.pack(side="left", padx=8)

        ctk.CTkButton(row, text="+", width=44, height=38,
                      font=ctk.CTkFont(FONT, 20),
                      fg_color=SURF0, hover_color=SURF1, text_color=TEXT,
                      command=lambda: self._nudge_ui_scale(UI_SCALE_STEP)
                      ).pack(side="left")

        entry = ctk.CTkEntry(row, width=70, height=38,
                             font=ctk.CTkFont(FONT, 15),
                             fg_color=SURF0, border_color=SURF1,
                             text_color=TEXT, placeholder_text="100")
        entry.pack(side="left", padx=(16, 6))

        def commit(_evt=None):
            raw = entry.get().strip().rstrip("%")
            try:
                pct = float(raw)
            except ValueError:
                entry.delete(0, tk.END)
                return
            # Accept either 90 or 0.9.
            self._apply_ui_scale(pct / 100 if pct > 3 else pct)
            entry.delete(0, tk.END)

        entry.bind("<Return>", commit)
        ctk.CTkButton(row, text="Set", width=56, height=38,
                      font=ctk.CTkFont(FONT, 14),
                      fg_color=SURF0, hover_color=SURF1, text_color=TEXT,
                      command=commit).pack(side="left")

        btns = ctk.CTkFrame(pad, fg_color="transparent")
        btns.pack(fill="x", pady=(18, 0))
        ctk.CTkButton(btns, text="Reset to 100%", height=36,
                      font=ctk.CTkFont(FONT, 14),
                      fg_color=SURF0, hover_color=SURF1, text_color=SUB,
                      command=lambda: self._apply_ui_scale(1.0)).pack(side="left")
        ctk.CTkButton(btns, text="Close", height=36, width=90,
                      font=ctk.CTkFont(FONT, 14),
                      fg_color=BLUE, hover_color=LAVENDER, text_color=BG,
                      command=win.destroy).pack(side="right")

        ctk.CTkLabel(pad,
                     text=f"Display reports {int(self._dpi_factor * 100)}% DPI",
                     font=ctk.CTkFont(FONT, 12),
                     text_color=SUB).pack(anchor="w", pady=(14, 0))

        win.after(120, win.lift)

    def _show_in_folder(self):
        path = self.songs_path.get()
        if not os.path.isdir(path):
            messagebox.showerror("Not found", f"Folder does not exist:\n{path}")
            return
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            log.error(f"Could not open folder: {e}")
            messagebox.showerror("Could not open", str(e))

    def _repair_library(self):
        if not mogg_check.decryptor_available():
            messagebox.showwarning(
                "No decryptor",
                "Encrypted Rock Band audio needs Onyx.\n\n"
                "Install it from github.com/mtolly/onyx/releases, then put it "
                "on PATH, in an 'onyx' folder next to this app, or set "
                "ONYX_BIN.")
            return

        path = self.songs_path.get()
        if not messagebox.askyesno(
                "Repair library audio",
                f"Scan {path} for encrypted Rock Band audio and repair it?\n\n"
                "This rewrites .mogg files in place and unpacks CON files. "
                "Back up first if you are unsure."):
            return

        def work():
            def prog(i, n, p):
                self.after(0, lambda: self.status_var.set(
                    f"Repairing {i + 1}/{n}: {os.path.basename(p)}"))
            try:
                songs, moggs, errs = mogg_check.fix_library(path, progress=prog)
            except Exception as e:
                log.error(f"Library repair failed: {e}")
                self.after(0, lambda: messagebox.showerror("Repair failed", str(e)))
                self.after(0, lambda: self.status_var.set("Repair failed"))
                return
            msg = f"Repaired {songs} song(s), {moggs} audio file(s)."
            if errs:
                msg += "\n\nProblems:\n" + "\n".join(errs[:10])
                if len(errs) > 10:
                    msg += f"\n…and {len(errs) - 10} more."
            self.after(0, lambda: self.status_var.set(msg.splitlines()[0]))
            self.after(0, lambda: messagebox.showinfo("Library repair", msg))

        self.status_var.set("Scanning library…")
        threading.Thread(target=work, daemon=True).start()

    def _open_page(self):
        sel = self.tree.selection()
        if not sel:
            return
        c = self._get_result(int(sel[0]))
        if not c:
            return
        url = c.get("page_url", "")
        if url:
            webbrowser.open(url)

    # ── Search ──────────────────────────────────────────────────────────

    def _start_search(self):
        q = self.search_var.get().strip()
        if not q:
            return
        self.search_btn.configure(state="disabled")
        self.status_var.set(f"Searching '{q}'…")
        self.tree.delete(*self.tree.get_children())
        self.results = []
        self._set_art_placeholder()
        for w in [self.d_title, self.d_artist, self.d_album,
                  self.d_charter, self.d_source]:
            w.configure(text="")
        self.pro_badge.configure(text="")
        for _, bar, val in self.diff_bars.values():
            bar.set(0)
            val.configure(text="·", text_color=SURF2)
        threading.Thread(target=self._search, args=(q,), daemon=True).start()

    def _search(self, q: str):
        all_r: list[dict] = []
        counts: dict[str, int] = {}
        inst = None if self.active_filter == "all" else self.active_filter
        pro = self.pro_var.get()

        for name, fn in [("Chorus", self._s_enchor),
                         ("RhythmVerse", self._s_rv),
                         ("YARG Charts", self._s_yc)]:
            try:
                r = fn(q, inst, pro)
                all_r.extend(r)
                counts[name] = len(r)
            except Exception as e:
                log.error(f"[{name}] Search failed: {e}")
                counts[name] = 0

        # Deduplicate
        seen: set[tuple] = set()
        unique: list[dict] = []
        for r in all_r:
            k = (r["artist"].lower().strip(),
                 r["title"].lower().strip(),
                 r.get("charter", "").lower().strip())
            if k not in seen:
                seen.add(k)
                unique.append(r)

        # Mark duplicates
        for r in unique:
            key = f"{safe_name(r['artist'])} - {safe_name(r['title'])}".lower().strip()
            r["_owned"] = key in self.existing_charts

        self.results = unique
        parts = [f"{k}: {v}" for k, v in counts.items()]
        status = f"{len(unique)} results ({', '.join(parts)})"

        # Feed artists to taste engine (from search, not just downloads)
        artists = list({r["artist"] for r in unique if r.get("artist")})
        if artists:
            self.taste.background_update(
                artists[:8],
                callback=lambda: self.after(0, self._refresh_taste_ui))

        self.after(0, lambda: self._fill(status))

    def _refresh_taste_ui(self):
        self._render_taste()
        self._render_recs()

    def _s_enchor(self, q, inst, pro, max_pages=15):
        body = {"search": q, "instrument": inst, "difficulty": None,
                "drumType": None, "drumsReviewed": False, "source": "website"}

        def fetch(pg):
            try:
                r = SESSION.post(ENCHOR_API, json={**body, "page": pg},
                                 headers=ENCHOR_HDR, timeout=8)
                return r.json() if r.status_code == 201 else None
            except Exception as e:
                log.warning(f"Chorus page {pg} failed: {e}")
                return None

        first = fetch(1)
        if not first:
            return []

        rows = list(first.get("data", []))
        found = first.get("found", 0)
        self._enchor_found = found

        per_page = len(rows) or 11
        n_pages = min(max_pages, -(-found // per_page)) if per_page else 1

        if n_pages > 1:
            with ThreadPoolExecutor(max_workers=6) as ex:
                for page in ex.map(fetch, range(2, n_pages + 1)):
                    if page:
                        rows.extend(page.get("data", []))

        out = []
        for it in rows:
            nd = it.get("notesData") or {}
            insts = set(nd.get("instruments") or [])
            drum_type = nd.get("drumType")

            dd = it.get("diff_drums", -1)
            ddr = it.get("diff_drums_real", -1)
            # notesData is authoritative: charters often leave diff_* at -1
            # even when the part exists.
            hp = (drum_type == "fourLanePro" or ddr >= 0
                  or it.get("pro_drums", False))

            if pro and not hp:
                continue
            # Client-side instrument filter, since the API's `instrument`
            # argument only biases relevance rather than excluding.
            if inst and insts and inst not in insts:
                continue

            def rating(key, val):
                """-1 when absent, 0 when present but unrated."""
                v = pdiff(val)
                return v if v >= 0 else (0 if key in insts else -1)

            md5 = it.get("albumArtMd5", "")
            dfid = it.get("driveFileId", "")
            smd5 = it.get("md5", "")
            # Encore hosts the .sng directly, keyed by chart md5. Prefer
            # it: no Drive throttling, no HTML confirm pages, and it does
            # not break when a charter moves their folder.
            if smd5:
                dl = f"https://files.enchor.us/{smd5}.sng"
            elif dfid:
                dl = f"https://drive.google.com/uc?id={dfid}&export=download"
            else:
                dl = ""
            out.append({
                "artist": it.get("artist", ""),
                "title": it.get("name", ""),
                "album": it.get("album", ""),
                "source": "Chorus",
                "charter": it.get("charter", ""),
                "dg": rating("guitar", it.get("diff_guitar")),
                "db": rating("bass", it.get("diff_bass")),
                "dd": rating("drums", dd),
                "dk": rating("keys", it.get("diff_keys")),
                "dv": rating("vocals", it.get("diff_vocals")),
                "has_pro": hp,
                "dl": dl,
                "art_url": f"https://files.enchor.us/{md5}.jpg" if md5 else "",
                "page_url": f"https://www.enchor.us/songs/{smd5}" if smd5 else "",
                "downloads": 0,
            })
        return out

    def _s_rv(self, q, inst, pro):
        out = []
        for pg in range(1, 3):
            body = (f"text={quote(q)}&page={pg}&records=25&data_type=full"
                    f"&sort%5B0%5D%5Bsort_by%5D=update_date"
                    f"&sort%5B0%5D%5Bsort_order%5D=DESC")
            if inst:
                body = f"instrument={inst}&{body}"
            r = SESSION.post(RV_API, data=body, headers=RV_HDR, timeout=10)
            if r.status_code != 200:
                break
            j = r.json()
            if j.get("status") != "success":
                break
            payload = j.get("data")
            if not isinstance(payload, dict):
                break          # RV returns data: false when there are no hits
            songs = payload.get("songs") or []
            total = (payload.get("records") or {}).get("total_filtered", 0)
            for s in songs:
                d = s.get("data", {})
                f = s.get("file", {})
                author = f.get("author")
                charter = author.get("name", "") if isinstance(author, dict) else ""
                hp = bool(f.get("pro_drums"))
                if pro and not hp:
                    continue
                dl = fix_url(f.get("download_url") or f.get("external_url") or "",
                             "https://rhythmverse.co")
                art = fix_url(d.get("album_art") or f.get("album_art") or "",
                              "https://rhythmverse.co")

                # Difficulty lives on `file`, not `data` — `data.diff_*` is
                # null on most records. `file.difficulties` is the authority
                # on which parts exist, e.g. {'drums': {'x': 1}, ...}.
                fd = f.get("difficulties")
                present = set(fd.keys()) if isinstance(fd, dict) else set()

                def rv_diff(key):
                    """-1 absent, 0 charted but unrated, else the rating."""
                    v = pdiff(f.get("diff_" + key, d.get("diff_" + key)))
                    if v >= 0:
                        return v
                    return 0 if key in present else -1

                diffs = {k: rv_diff(k) for k in
                         ("guitar", "bass", "drums", "keys", "vocals")}

                if inst and diffs.get(inst, -1) < 0:
                    continue

                out.append({
                    "artist": d.get("artist") or f.get("file_artist", ""),
                    "title": d.get("title") or f.get("file_title", ""),
                    "album": d.get("album") or f.get("file_album", ""),
                    "source": "RhythmVerse",
                    "charter": charter,
                    "dg": diffs["guitar"],
                    "db": diffs["bass"],
                    "dd": diffs["drums"],
                    "dk": diffs["keys"],
                    "dv": diffs["vocals"],
                    "has_pro": hp,
                    "dl": dl,
                    "art_url": art,
                    "page_url": f.get("file_url_full", ""),
                    "downloads": f.get("downloads", 0) or 0,
                })
            if pg * 25 >= total:
                break
        return out

    def _s_yc(self, q, inst, pro):
        out = []
        if pro:
            return out  # YARG Charts exposes no pro-drums flag — nothing can match
        action = get_yc_action()
        r = SESSION.post(
            f"{YC_URL}?query={quote(q)}",
            data=json.dumps([q, 1]),
            headers={
                "Accept": "text/x-component",
                "Content-Type": "text/plain;charset=UTF-8",
                "Next-Action": action,
                "Origin": "https://www.yargcharts.com",
                "Referer": f"https://www.yargcharts.com/?query={quote(q)}",
            },
            timeout=10)
        if r.status_code != 200:
            return out
        sd = None
        for line in r.text.split("\n"):
            if line.startswith("1:"):
                try:
                    sd = json.loads(line[2:])
                except json.JSONDecodeError:
                    pass
                break
        if not sd:
            return out
        for it in sd.get("songs", []):
            # code -> difficulty, matching the row keys used everywhere else
            d = {"guitar": pdiff(it.get("diffGuitar")),
                 "bass":   pdiff(it.get("diffBass")),
                 "drums":  pdiff(it.get("diffDrums")),
                 "keys":   pdiff(it.get("diffKeys")),
                 "vocals": pdiff(it.get("diffVocals"))}
            if inst and d.get(inst, -1) < 0:
                continue
            out.append({
                "artist": it.get("artist", ""),
                "title": it.get("title", ""),
                "album": it.get("album", ""),
                "source": "YARG Charts",
                "charter": "",
                "dg": d["guitar"], "db": d["bass"], "dd": d["drums"],
                "dk": d["keys"], "dv": d["vocals"],
                "has_pro": False,
                "dl": fix_url(it.get("downloadUrl", "")),
                "art_url": it.get("albumArt", ""),
                "page_url": f"https://www.yargcharts.com/?query={quote(it.get('artist', ''))}",
                "downloads": 0,
            })
        return out

    def _fill(self, status: str):
        self._last_status = status
        scope = self.scope_seg.get() if hasattr(self, "scope_seg") else "All"
        needle = self.search_var.get().strip().lower()

        self.tree.delete(*self.tree.get_children())
        shown = 0
        with self._results_lock:
            total = len(self._results)
            for i, r in enumerate(self._results):
                if scope == "Artist" and needle not in r["artist"].lower():
                    continue
                if scope == "Title" and needle not in r["title"].lower():
                    continue
                shown += 1
                dls = r.get("downloads", 0)
                dl_str = str(dls) if dls and dls > 0 else "·"
                owned = "✓" if r.get("_owned") else ""
                # iid stays the index into _results so selection still resolves
                self.tree.insert("", tk.END, iid=str(i), values=(
                    owned, r["artist"], r["title"], r["source"], r["charter"],
                    diff_str(r["dg"]), diff_str(r["db"]), diff_str(r["dd"]),
                    diff_str(r["dk"]), diff_str(r["dv"]), dl_str))
        self.search_btn.configure(state="normal")
        if scope != "All" and shown != total:
            status = f"{shown} of {status} — {scope.lower()} match"
        self.status_var.set(status)

    # ── Download Queue ──────────────────────────────────────────────────

    def _queue_dl(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Select a chart", "Click a song first.")
            return
        c = self._get_result(int(sel[0]))
        if not c:
            return
        if not c.get("dl"):
            page = c.get("page_url", "")
            if page and messagebox.askyesno(
                    "No Direct Link",
                    "No direct download link.\nOpen source page in browser?"):
                webbrowser.open(page)
            elif not page:
                messagebox.showerror("No link", "No download link available.")
            return

        self._dl_queue.append(c)
        self._update_queue_label()
        if not self._dl_active:
            self._process_queue()

    def _update_queue_label(self):
        n = len(self._dl_queue)
        self.queue_label.configure(text=f"{n} in queue" if n > 0 else "")

    def _process_queue(self):
        if not self._dl_queue:
            self._dl_active = False
            self.dl_btn.configure(state="normal", text="DOWNLOAD")
            self.dl_progress.set(0)
            return
        self._dl_active = True
        chart = self._dl_queue.pop(0)
        self._update_queue_label()
        self.dl_btn.configure(state="disabled",
                              text=f"Downloading… ({len(self._dl_queue)} queued)")
        self.status_var.set(f"Downloading {chart['artist']} — {chart['title']}…")
        self.dl_progress.set(0)
        threading.Thread(target=self._dl, args=(chart,), daemon=True).start()

    def _fetch(self, url: str, label: str) -> bytes:
        """Stream a URL to memory with progress. Retries once if truncated."""
        ua_hdr = {"User-Agent": BROWSER_UA}
        last_err = None

        for attempt in (1, 2):
            r = SESSION.get(url, headers=ua_hdr, timeout=120,
                            allow_redirects=True, stream=True)
            if r.status_code == 404:
                raise Exception(f"404 — dead link.\n{url}")
            if r.status_code != 200:
                raise Exception(f"HTTP {r.status_code}\n{url}")

            total = int(r.headers.get("content-length") or 0)
            buf, got, mark = io.BytesIO(), 0, 0.0
            try:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    buf.write(chunk)
                    got += len(chunk)
                    if total:
                        frac = 0.2 + 0.65 * (got / total)
                        if frac - mark >= 0.01:
                            mark = frac
                            self.after(0, lambda f=frac: self.dl_progress.set(f))
                            a, b = got / 1048576, total / 1048576
                            self.after(0, lambda x=a, y=b, l=label:
                                       self.status_var.set(
                                           f"{l} — {x:.1f} / {y:.1f} MB"))
                    elif got - mark >= 1048576:
                        mark = got
                        self.after(0, lambda x=got / 1048576, l=label:
                                   self.status_var.set(f"{l} — {x:.1f} MB"))
            except Exception as e:
                last_err = e
                got = -1

            if total and got != total:
                last_err = Exception(
                    f"Truncated: got {max(got, 0):,} of {total:,} bytes")
                if attempt == 1:
                    log.warning(f"{last_err} — retrying")
                    self.after(0, lambda: self.status_var.set(
                        "Connection dropped — retrying…"))
                    continue
                raise last_err
            return buf.getvalue()

        raise last_err or Exception("Download failed")

    def _dl(self, chart: dict):
        tmp = None
        try:
            url = chart["dl"]
            self.after(0, lambda: self.dl_progress.set(0.1))

            if "drive.google.com" in url or "docs.google.com" in url:
                # Drive serves an HTML confirm page for large files.
                r = SESSION.get(url, headers={"User-Agent": BROWSER_UA}, timeout=30)
                if r.headers.get("Content-Type", "").startswith("text/html"):
                    fid = re.search(r'id=([^&]+)', url)
                    if fid:
                        url = ("https://drive.usercontent.google.com/download"
                               f"?id={fid.group(1)}&export=download&confirm=t")
                    else:
                        cm = re.search(r'confirm=([0-9A-Za-z_-]+)', r.text)
                        if not cm:
                            raise Exception("Google Drive blocked.\nUse the source website.")
                        url = url + f"&confirm={cm.group(1)}"

            self.after(0, lambda: self.dl_progress.set(0.2))
            content = self._fetch(url, "Downloading")

            self.after(0, lambda: self.dl_progress.set(0.6))

            ft = detect_type(content)
            if ft == ".html":
                raise Exception("Got HTML instead of song file.\nLink may need browser auth.")

            sa, st = safe_name(chart["artist"]), safe_name(chart["title"])
            fn = f"{sa} - {st}"
            sd = self.songs_path.get()

            def _audio_fix(target, base_msg):
                """Scan for encrypted audio and repair. Returns status text."""
                if not HAS_MOGG_CHECK:
                    return base_msg
                try:
                    if not any(r.encrypted for r in mogg_check.scan(target)):
                        return base_msg
                except Exception as e:
                    log.error(f"mogg scan failed: {e}")
                    return base_msg

                self.after(0, lambda: self.status_var.set(
                    "Decrypting audio… this can take a moment"))

                def _prog(i, n, p):
                    self.after(0, lambda: self.status_var.set(
                        f"Decrypting audio {i + 1}/{n}…"))

                try:
                    final, note = mogg_check.autofix(target, sd, fn, progress=_prog)
                except Exception as e:
                    log.error(f"mogg autofix failed: {e}")
                    return f"{base_msg}\n\nAudio fix failed: {e}"

                if final != target:
                    base_msg = f"Extracted to:\n{final}"
                return f"{base_msg}\n\n{note}" if note else base_msg

            if ft == ".zip":
                tmp = tempfile.mkdtemp()
                try:
                    with zipfile.ZipFile(io.BytesIO(content)) as z:
                        z.extractall(tmp)
                except zipfile.BadZipFile:
                    raise Exception("Corrupted ZIP.")
                items = [i for i in os.listdir(tmp)
                         if i not in ('__MACOSX', '.DS_Store')]
                dest = os.path.join(sd, fn)
                if os.path.exists(dest):
                    shutil.rmtree(dest)
                if len(items) == 1 and os.path.isdir(os.path.join(tmp, items[0])):
                    shutil.move(os.path.join(tmp, items[0]), dest)
                else:
                    os.makedirs(dest, exist_ok=True)
                    for it in items:
                        shutil.move(os.path.join(tmp, it), os.path.join(dest, it))
                msg = _audio_fix(dest, f"Extracted to:\n{dest}")

            elif ft in (".sng", ".con", ".live", ".pirs"):
                fp = os.path.join(sd, f"{fn}{ft}")
                with open(fp, "wb") as f:
                    f.write(content)
                msg = _audio_fix(fp, f"Saved:\n{fp}")

            elif ft in (".rar", ".7z"):
                fp = os.path.join(sd, f"{fn}{ft}")
                with open(fp, "wb") as f:
                    f.write(content)
                msg = (f"Saved:\n{fp}\n\n"
                       f"{ft[1:].upper()} archive — YARG cannot read this.\n"
                       f"Extract it manually (unar / 7z), then rescan.")

            else:
                ul = url.lower()
                ext = (".sng" if ".sng" in ul else
                       ".con" if ("rb3con" in ul or "_con" in ul) else
                       ".chart" if ".chart" in ul else ".bin")
                fp = os.path.join(sd, f"{fn}{ext}")
                with open(fp, "wb") as f:
                    f.write(content)
                msg = _audio_fix(
                    fp, f"Saved:\n{fp}\n\n(Auto-detect failed — may need renaming)")

            self.after(0, lambda: self.dl_progress.set(1.0))

            # Update taste engine
            artist = chart.get("artist", "")
            if artist:
                self.taste.record_download(artist)
                self.taste.background_update(
                    [artist],
                    callback=lambda: self.after(0, self._refresh_taste_ui))

            # Update existing charts set
            key = fn.lower().strip()
            self.existing_charts.add(key)

            self.after(0, lambda: messagebox.showinfo("Done", msg))

        except Exception as e:
            log.error(f"Download failed: {e}")
            em = str(e)
            self.after(0, lambda: messagebox.showerror("Download Failed", em))
        finally:
            if tmp and os.path.exists(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
            self.after(0, self._process_queue)


if __name__ == "__main__":
    app = App()
    app.mainloop()