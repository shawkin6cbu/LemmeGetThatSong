"""
mogg_check.py — detect encrypted .mogg audio in Rock Band CON / ex-CON songs
and hand it off to an in-process decryptor.

Drop next to yarg_gui.py and `import mogg_check`.

Mogg header: 4-byte LE version word.
    0x0A  = plain, Ogg stream starts at header_size     -> YARG OK
    0x0B+ = encrypted (0x0D is typical for Wii-sourced) -> YARG rejects the song

Decryption is not implemented here. Provide a sibling module `mogg_crypt.py`
exposing:

    decrypt(data: bytes) -> bytes     # returns a plain mogg (starts 0a 00 00 00)

If it isn't present, scanning still works and process_con() reports the
problem instead of fixing it.
"""

from __future__ import annotations

import logging
import os
import shutil
import struct
from dataclasses import dataclass

log = logging.getLogger("yarg.mogg")

PLAIN_MOGG = 0x0A


# ── mogg header ────────────────────────────────────────────────────────────

def mogg_version(data: bytes) -> int | None:
    """Version word from the first 8 bytes of a mogg. None if not a mogg."""
    if len(data) < 8:
        return None
    ver, hdr = struct.unpack_from("<II", data, 0)
    if not (0 < ver < 0x100) or hdr < 8 or hdr >= 1 << 24:
        return None
    return ver


def mogg_version_at(path: str) -> int | None:
    with open(path, "rb") as f:
        return mogg_version(f.read(8))


def is_encrypted(data_or_ver) -> bool:
    ver = data_or_ver if isinstance(data_or_ver, int) else mogg_version(data_or_ver)
    return ver is not None and ver != PLAIN_MOGG


# ── STFS (CON / LIVE / PIRS) reader ────────────────────────────────────────

@dataclass
class StfsEntry:
    name: str
    path: str
    is_dir: bool
    size: int
    start: int
    blocks: int


class Stfs:
    """Minimal read-only STFS parser. Enough to list files and pull them out."""

    MAGICS = (b"CON ", b"LIVE", b"PIRS")

    def __init__(self, path: str):
        with open(path, "rb") as f:
            self.d = f.read()
        if self.d[:4] not in self.MAGICS:
            raise ValueError("not an STFS package")
        self.sex = (~self.d[0x37B]) & 1          # 0 = female, 1 = male
        self.first_table = 0xB000 if self.sex == 0 else 0xC000
        self.step0 = 0xAB if self.sex == 0 else 0xAC

    # block arithmetic (hash tables are interleaved with data blocks)
    def _backing(self, b: int) -> int:
        r = (((b + 0xAA) // 0xAA) << self.sex) + b
        if b < 0xAA:
            return r
        r = (((b + 0x70E4) // 0x70E4) << self.sex) + r
        if b < 0x70E4:
            return r
        return (1 << self.sex) + r

    def _block_addr(self, b: int) -> int:
        return (self._backing(b) << 0xC) + self.first_table

    def _l0_table(self, b: int) -> int:
        if b < 0xAA:
            return 0
        n = (b // 0xAA) * self.step0
        n += ((b // 0x70E4) + 1) << self.sex
        return n if b // 0x70E4 == 0 else n + (1 << self.sex)

    def _next_block(self, b: int) -> int:
        o = (self._l0_table(b) << 0xC) + self.first_table + (b % 0xAA) * 0x18
        return struct.unpack(">I", b"\x00" + self.d[o + 0x15:o + 0x18])[0]

    def _read_chain(self, start: int, count: int) -> bytes:
        out, b = bytearray(), start
        for _ in range(count):
            o = self._block_addr(b)
            out += self.d[o:o + 0x1000]
            b = self._next_block(b)
            if b >= 0xFFFFFF:
                break
        return bytes(out)

    def files(self) -> list[StfsEntry]:
        count = struct.unpack_from("<h", self.d, 0x37C)[0]
        first = struct.unpack("<I", self.d[0x37E:0x381] + b"\x00")[0]
        table = self._read_chain(first, count)

        raw = []
        for i in range(0, len(table) - 0x3F, 0x40):
            e = table[i:i + 0x40]
            nlen = e[0x28] & 0x3F
            name = e[:nlen].decode("latin1")
            if not name:
                raw.append(None)
                continue
            raw.append(dict(
                name=name,
                is_dir=bool(e[0x28] & 0x80),
                blocks=struct.unpack("<I", e[0x29:0x2C] + b"\x00")[0],
                start=struct.unpack("<I", e[0x2F:0x32] + b"\x00")[0],
                parent=struct.unpack(">h", e[0x32:0x34])[0],
                size=struct.unpack(">I", e[0x34:0x38])[0],
            ))

        def full(i, depth=0):
            r = raw[i]
            if r is None:
                return ""
            if depth > 16 or r["parent"] < 0 or r["parent"] >= len(raw) \
                    or raw[r["parent"]] is None:
                return r["name"]
            return full(r["parent"], depth + 1) + "/" + r["name"]

        return [StfsEntry(r["name"], full(i), r["is_dir"], r["size"],
                          r["start"], r["blocks"])
                for i, r in enumerate(raw) if r]

    def read(self, e: StfsEntry) -> bytes:
        return self._read_chain(e.start, e.blocks)[:e.size]

    @staticmethod
    def _safe_join(dest: str, entry_path: str) -> str | None:
        """
        Resolve an entry path inside `dest`, or None if it escapes.

        File names in an STFS package are attacker-controlled bytes. Without
        this, an entry named "../../../.ssh/authorized_keys" or an absolute
        path writes anywhere the process can reach.
        """
        parts = []
        for raw in entry_path.replace("\\", "/").split("/"):
            part = raw.strip().strip(".")
            if not part or raw in (".", ".."):
                continue
            # Drop drive letters and any residual separators.
            part = part.split(":")[-1].replace("/", "").replace("\\", "")
            if part:
                parts.append(part)
        if not parts:
            return None

        out = os.path.normpath(os.path.join(dest, *parts))
        root = os.path.abspath(dest)
        if os.path.commonpath([root, os.path.abspath(out)]) != root:
            return None
        return out

    def extract_all(self, dest: str) -> None:
        os.makedirs(dest, exist_ok=True)
        for e in self.files():
            out = self._safe_join(dest, e.path)
            if out is None:
                log.warning(f"Skipping unsafe STFS entry: {e.path!r}")
                continue
            if e.is_dir:
                os.makedirs(out, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "wb") as f:
                    f.write(self.read(e))


# ── scanning ───────────────────────────────────────────────────────────────

@dataclass
class MoggReport:
    path: str          # container path (the CON, or the mogg itself)
    inner: str         # path inside the CON, or "" for a loose file
    version: int
    encrypted: bool


def scan(path: str) -> list[MoggReport]:
    """Report on every mogg in a CON, an ex-CON folder, or a single mogg."""
    out: list[MoggReport] = []

    if os.path.isdir(path):
        for root, _, names in os.walk(path):
            for n in names:
                if n.lower().endswith(".mogg"):
                    fp = os.path.join(root, n)
                    v = mogg_version_at(fp)
                    if v is not None:
                        out.append(MoggReport(fp, "", v, v != PLAIN_MOGG))
        return out

    with open(path, "rb") as f:
        head = f.read(8)

    if head[:4] in Stfs.MAGICS:
        s = Stfs(path)
        for e in s.files():
            if e.is_dir or not e.name.lower().endswith(".mogg"):
                continue
            o = s._block_addr(e.start)
            v = mogg_version(s.d[o:o + 8])
            if v is not None:
                out.append(MoggReport(path, e.path, v, v != PLAIN_MOGG))
        return out

    v = mogg_version(head)
    if v is not None:
        out.append(MoggReport(path, "", v, v != PLAIN_MOGG))
    return out


def has_encrypted_audio(path: str) -> bool:
    try:
        return any(r.encrypted for r in scan(path))
    except Exception:
        return False


# ── decryption ─────────────────────────────────────────────────────────────

class DecryptError(RuntimeError):
    pass


def decryptor_available() -> bool:
    try:
        import mogg_crypt
    except ImportError:
        return False
    # An adapter may wrap an external binary that isn't installed. If it
    # exposes available(), trust it — otherwise assume the import is enough.
    check = getattr(mogg_crypt, "available", None)
    if callable(check):
        try:
            return bool(check())
        except Exception:
            return False
    return True


def decrypt_file(src: str, dst: str) -> None:
    """Decrypt one mogg via mogg_crypt.decrypt(). Validates the result."""
    try:
        import mogg_crypt
    except ImportError:
        raise DecryptError("mogg_crypt.py not found — drop a decryptor "
                           "implementation next to mogg_check.py")

    with open(src, "rb") as f:
        data = f.read()

    try:
        out = mogg_crypt.decrypt(data)
    except Exception as e:
        raise DecryptError(f"mogg_crypt.decrypt failed: {e}")

    v = mogg_version(out[:8]) if out else None
    if v != PLAIN_MOGG:
        raise DecryptError(f"output still encrypted (version 0x{v:02X})"
                           if v else "decrypt() returned no usable output")

    hdr = struct.unpack_from("<I", out, 4)[0]
    if out[hdr:hdr + 4] != b"OggS":
        raise DecryptError("no OggS signature at header offset — bad decrypt")

    with open(dst, "wb") as f:
        f.write(out)


def decrypt_in_place(folder: str, progress=None) -> tuple[int, list[str]]:
    """Decrypt every encrypted mogg under `folder`. Returns (n_done, errors)."""
    todo = [r for r in scan(folder) if r.encrypted]
    done, errs = 0, []
    for i, r in enumerate(todo):
        if progress:
            progress(i, len(todo), r.path)
        tmp = r.path + ".dec"
        try:
            decrypt_file(r.path, tmp)
            os.replace(tmp, r.path)
            done += 1
        except Exception as e:
            errs.append(f"{os.path.basename(r.path)}: {e}")
            if os.path.exists(tmp):
                os.remove(tmp)
    return done, errs


def process_con(con_path: str, songs_dir: str, name: str) -> tuple[str, str]:
    """
    If a CON's audio is encrypted, extract it to an ex-CON folder and decrypt
    in place (YARG reads ex-CONs). Returns (final_path, status_message).
    """
    reports = scan(con_path)
    enc = [r for r in reports if r.encrypted]
    if not enc:
        return con_path, f"Saved:\n{con_path}\n\nAudio OK (mogg 0x0A)."

    vers = ", ".join(sorted({f"0x{r.version:02X}" for r in enc}))
    if not decryptor_available():
        return con_path, (f"Saved:\n{con_path}\n\n"
                          f"Encrypted audio ({vers}) — YARG will skip this song.\n"
                          f"No decryptor available: add mogg_crypt.py.")

    dest = os.path.join(songs_dir, name)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    Stfs(con_path).extract_all(dest)

    done, errs = decrypt_in_place(dest)
    if errs:
        return dest, (f"Extracted to:\n{dest}\n\n"
                      f"Decryption failed ({vers}):\n" + "\n".join(errs))

    os.remove(con_path)
    return dest, f"Extracted and decrypted {done} mogg(s):\n{dest}"


def autofix(path: str, songs_dir: str | None = None, name: str | None = None,
            progress=None) -> tuple[str, str]:
    """
    Detect and repair encrypted audio for anything a download produced.

    `path` may be a CON/LIVE/PIRS file, or a folder (from a ZIP) that may
    itself contain loose moggs and/or nested CON files.

    Returns (final_path, status_message).
    """
    if os.path.isfile(path):
        head = b""
        try:
            with open(path, "rb") as f:
                head = f.read(4)
        except OSError:
            pass
        if head in Stfs.MAGICS:
            sd = songs_dir or os.path.dirname(path)
            nm = name or os.path.splitext(os.path.basename(path))[0]
            return process_con(path, sd, nm)
        return path, ""

    if not os.path.isdir(path):
        return path, ""

    notes: list[str] = []

    # Nested CONs inside an extracted archive: expand each in place.
    for root, _, names in os.walk(path):
        for n in names:
            fp = os.path.join(root, n)
            try:
                with open(fp, "rb") as f:
                    if f.read(4) not in Stfs.MAGICS:
                        continue
            except OSError:
                continue
            try:
                Stfs(fp).extract_all(root)
                os.remove(fp)
                notes.append(f"Expanded CON: {n}")
            except Exception as e:
                notes.append(f"Could not expand {n}: {e}")

    enc = [r for r in scan(path) if r.encrypted]
    if not enc:
        return path, "\n".join(notes)

    vers = ", ".join(sorted({f"0x{r.version:02X}" for r in enc}))
    if not decryptor_available():
        notes.append(f"Encrypted audio ({vers}) — YARG will skip this song.\n"
                     f"No decryptor available: set ONYX_BIN.")
        return path, "\n".join(notes)

    done, errs = decrypt_in_place(path, progress=progress)
    if errs:
        notes.append(f"Decryption failed ({vers}):\n" + "\n".join(errs))
    elif done:
        notes.append(f"Decrypted {done} mogg(s) ({vers}).")
    return path, "\n".join(notes)


def fix_library(songs_dir: str, progress=None) -> tuple[int, int, list[str]]:
    """
    Sweep an existing songs folder: repair every encrypted CON and ex-CON.
    Returns (n_songs_fixed, n_moggs_decrypted, errors).
    """
    targets: list[str] = []
    for entry in sorted(os.listdir(songs_dir)):
        fp = os.path.join(songs_dir, entry)
        if os.path.isdir(fp):
            targets.append(fp)
        elif os.path.isfile(fp):
            try:
                with open(fp, "rb") as f:
                    if f.read(4) in Stfs.MAGICS:
                        targets.append(fp)
            except OSError:
                pass

    songs, moggs, errs = 0, 0, []
    for i, t in enumerate(targets):
        if progress:
            progress(i, len(targets), t)
        try:
            if not any(r.encrypted for r in scan(t)):
                continue
            before = len([r for r in scan(t) if r.encrypted])
            _, msg = autofix(t, songs_dir, os.path.basename(t))
            after = 0
            try:
                after = len([r for r in scan(t) if r.encrypted])
            except Exception:
                pass
            if after < before:
                songs += 1
                moggs += before - after
            elif msg:
                errs.append(f"{os.path.basename(t)}: {msg.splitlines()[-1]}")
        except Exception as e:
            errs.append(f"{os.path.basename(t)}: {e}")
    return songs, moggs, errs


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("usage: mogg_check.py <file.con | ex-con folder | file.mogg>")
    for r in scan(sys.argv[1]):
        tag = f"ENCRYPTED 0x{r.version:02X}" if r.encrypted else "plain 0x0A"
        print(f"{tag:18} {r.inner or r.path}")