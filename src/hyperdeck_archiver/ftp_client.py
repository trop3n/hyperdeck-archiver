"""FTP client for HyperDeck media: list clips per slot and stream downloads.

The HyperDeck FTP server (port 21, anonymous) exposes slots as directories named
"1".."N" at the root, with .mov clips directly inside each slot directory. This
module lists clips and streams them to disk while hashing in one pass.
"""
from __future__ import annotations

import ftplib
from contextlib import contextmanager
from fnmatch import fnmatch

from .models import Clip


class FtpError(RuntimeError):
    pass


def parse_list_line(line: str) -> tuple[str, bool, int | None, str] | None:
    """Parse a UNIX-style LIST line -> (name, is_dir, size, perms) or None."""
    parts = line.split(None, 8)
    if len(parts) < 9:
        return None
    perms, size_s, name = parts[0], parts[4], parts[8]
    is_dir = perms.startswith("d")
    size = int(size_s) if size_s.isdigit() else None
    return name, is_dir, size, perms


def is_metadata(name: str, skip_patterns: tuple[str, ...]) -> bool:
    if name in (".", ".."):
        return True
    return any(fnmatch(name, pat) for pat in skip_patterns)


class FtpDeck:
    def __init__(self, host: str, timeout: float = 30.0):
        self.host = host
        self.timeout = timeout
        self._ftp: ftplib.FTP | None = None

    def connect(self) -> None:
        self._ftp = ftplib.FTP()
        self._ftp.connect(self.host, 21, timeout=self.timeout)
        self._ftp.login()  # anonymous
        self._ftp.sendcmd("TYPE I")

    def close(self) -> None:
        if self._ftp is not None:
            try:
                self._ftp.quit()
            except ftplib.all_errors:
                try:
                    self._ftp.close()
                except OSError:
                    pass
            self._ftp = None

    def reconnect(self) -> None:
        """Replace the control connection with a fresh one.

        A timed-out FTP control socket is permanently unusable: ftplib's internal
        file object raises 'cannot read from timed out object' on every subsequent
        read. Any transfer/list timeout MUST be followed by reconnect() before this
        FtpDeck is reused, or the next operation will fail spuriously even though
        the deck/slot itself is fine.
        """
        self.close()
        self.connect()

    def __enter__(self) -> "FtpDeck":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _require(self) -> ftplib.FTP:
        if self._ftp is None:
            raise FtpError("not connected")
        return self._ftp

    def list_clips(self, slot: int, skip_metadata: tuple[str, ...] = ()) -> list[Clip]:
        ftp = self._require()
        lines: list[str] = []
        ftp.retrlines(f"LIST /{slot}", lines.append)
        clips: list[Clip] = []
        clip_id = 0
        for line in lines:
            parsed = parse_list_line(line)
            if parsed is None:
                continue
            name, is_dir, size, _perms = parsed
            if is_dir or is_metadata(name, skip_metadata):
                continue
            clip_id += 1
            clips.append(Clip(slot=slot, clip_id=clip_id, name=name, size=size))
        return clips

    def size(self, slot: int, name: str) -> int:
        ftp = self._require()
        return int(ftp.size(f"/{slot}/{name}"))

    def download(self, slot: int, name: str, dest_path: str, hasher, progress=None) -> int:
        """Stream RETR /<slot>/<name> to dest_path, updating `hasher` per chunk.

        Returns bytes written. `progress(written)` is called per chunk if given.
        """
        ftp = self._require()
        remote = f"/{slot}/{name}"
        sock = ftp.transfercmd("RETR " + remote)
        total = 0
        try:
            with open(dest_path, "wb") as f:
                while True:
                    chunk = sock.recv(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    total += len(chunk)
                    if progress is not None:
                        progress(total)
        finally:
            sock.close()
            try:
                ftp.voidresp()
            except ftplib.all_errors:
                pass
        return total


@contextmanager
def connect(host: str, timeout: float = 30.0):
    deck = FtpDeck(host, timeout)
    try:
        deck.connect()
        yield deck
    finally:
        deck.close()
