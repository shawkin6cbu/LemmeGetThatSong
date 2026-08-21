"""
mogg_crypt.py — adapter exposing mogg_check's expected interface:

    decrypt(data: bytes) -> bytes

Backed by Onyx (github.com/mtolly/onyx), a compiled Haskell binary, so this
shells out rather than importing anything. All cryptography is Onyx's; this
module only moves bytes and fixes up the container.

    onyx unwrap in.mogg --to out.ogg

Onyx emits a bare Ogg stream, but YARG wants a MOGG: a 4-byte LE version word
(0x0A = plain) plus a 4-byte LE offset to the Ogg data. So the Ogg gets
re-wrapped in an 8-byte plain-mogg header on the way back.

Setup:
    ./onyx-*.AppImage --appimage-extract
    export ONYX_BIN=$(realpath squashfs-root/usr/bin/onyx)
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile

PLAIN_MOGG = 0x0A
HEADER_SIZE = 8
TIMEOUT = 300


class MoggCryptError(RuntimeError):
    pass


# Directories searched for the Onyx binary. Deliberately narrow: this list
# feeds subprocess execution, so anywhere a drive-by download or an extracted
# archive could drop a file named "onyx" must NOT be here. That rules out
# ~/Downloads and similar. Users outside these paths set ONYX_BIN explicitly
# or point the app at the binary once, which is stored in CONFIG_PATH.
SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "onyx"),
    os.path.expanduser("~/.local/share/LemmeGetThatSong/onyx"),
    os.path.expanduser("~/.config/LemmeGetThatSong/onyx"),
]

CONFIG_PATH = os.path.expanduser("~/.config/LemmeGetThatSong/onyx_path")


def _candidates():
    """Yield plausible onyx paths, best first."""
    env = os.environ.get("ONYX_BIN")
    if env:
        yield env

    try:
        with open(CONFIG_PATH) as f:
            saved = f.read().strip()
        if saved:
            yield saved
    except OSError:
        pass

    found = shutil.which("onyx")
    if found:
        yield found

    for d in SEARCH_DIRS:
        for n in ("onyx", "onyx.exe"):
            yield os.path.join(d, n)
        # extracted AppImage laid down inside one of these dirs
        yield os.path.join(d, "squashfs-root", "usr", "bin", "onyx")


def _binary() -> str:
    for c in _candidates():
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c

    env = os.environ.get("ONYX_BIN")
    if env:
        raise MoggCryptError(f"ONYX_BIN is not an executable file: {env}")
    raise MoggCryptError(
        "Onyx not found. Install it from "
        "https://github.com/mtolly/onyx/releases, then either put `onyx` on "
        f"PATH, set ONYX_BIN, or write its path to {CONFIG_PATH}")


def set_binary(path: str) -> None:
    """Persist a user-chosen onyx path (used by the GUI's file picker)."""
    path = os.path.abspath(os.path.expanduser(path))
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise MoggCryptError(f"not an executable file: {path}")
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        f.write(path)


def available() -> bool:
    try:
        _binary()
        return True
    except MoggCryptError:
        return False


def wrap_ogg(ogg: bytes) -> bytes:
    """Wrap a bare Ogg stream in a plain (0x0A) MOGG header."""
    if ogg[:4] != b"OggS":
        raise MoggCryptError("expected an Ogg stream (no OggS signature)")
    return struct.pack("<II", PLAIN_MOGG, HEADER_SIZE) + ogg


def decrypt(data: bytes) -> bytes:
    """Decrypt a MOGG via `onyx unwrap`. Returns a plain (0x0A) MOGG."""
    if len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] == PLAIN_MOGG:
        return data  # already plain, nothing to do

    exe = _binary()
    tmp = tempfile.mkdtemp(prefix="moggcrypt-")
    src = os.path.join(tmp, "in.mogg")
    dst = os.path.join(tmp, "out.ogg")

    try:
        with open(src, "wb") as f:
            f.write(data)

        try:
            p = subprocess.run([exe, "unwrap", src, "--to", dst],
                               capture_output=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise MoggCryptError(f"onyx timed out after {TIMEOUT}s")
        except OSError as e:
            raise MoggCryptError(f"could not run onyx: {e}")

        if p.returncode != 0:
            err = (p.stderr or p.stdout).decode("utf-8", "replace")
            # Onyx warns about missing Wine on every run; not an error.
            err = "\n".join(l for l in err.splitlines()
                            if "Wine" not in l and "external program" not in l
                            and "Context (innermost" not in l).strip()
            raise MoggCryptError(
                f"onyx unwrap exited {p.returncode}: {err[:400] or '(no output)'}")

        if not os.path.exists(dst):
            raise MoggCryptError("onyx unwrap produced no output file")

        with open(dst, "rb") as f:
            ogg = f.read()

        if not ogg:
            raise MoggCryptError("onyx unwrap produced an empty file")

        return wrap_ogg(ogg)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        sys.exit("usage: mogg_crypt.py <in.mogg> <out.mogg>")

    with open(sys.argv[1], "rb") as f:
        raw = f.read()

    print(f"onyx:   {_binary()}")
    print(f"input:  version 0x{struct.unpack_from('<I', raw, 0)[0]:02X}, "
          f"{len(raw):,} bytes")

    out = decrypt(raw)
    ver, hdr = struct.unpack_from("<II", out, 0)
    print(f"output: version 0x{ver:02X}, header {hdr}, {len(out):,} bytes, "
          f"OggS at header: {out[hdr:hdr + 4] == b'OggS'}")

    with open(sys.argv[2], "wb") as f:
        f.write(out)
    print(f"wrote:  {sys.argv[2]}")