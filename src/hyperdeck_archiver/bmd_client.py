"""Blackmagic HyperDeck Ethernet Protocol client (TCP 9993).

Used for slot status and whole-card formatting. Clip LISTING is done over FTP
(see ftp_client.py); this module only covers what FTP cannot do: read slot
mount/blocked state and issue the two-step `format` that clears a card.

Response shape (from real captures): each reply is text lines ending in an empty
line; the first line is a status line like `202 slot info:` or `200 ok`.
"""
from __future__ import annotations

import re
import socket
import time
from contextlib import contextmanager

from .models import SlotInfo

BMD_PORT = 9993
RECV_WINDOW = 0.6
RECV_CAP = 6.0
TOKEN_RE = re.compile(r"token:\s*(\S+)", re.IGNORECASE)


class BmdError(RuntimeError):
    pass


def _parse_kv_lines(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def _status_code(status_line: str) -> int:
    try:
        return int(status_line.split()[0])
    except (IndexError, ValueError):
        return 0


def parse_slot_info(lines: list[str]) -> SlotInfo:
    kv = _parse_kv_lines(lines)
    slot = int(kv.get("slot id", "0") or 0)
    status = kv.get("status", "unknown")
    return SlotInfo(
        slot=slot,
        status=status,
        volume_name=kv.get("volume name", ""),
        video_format=kv.get("video format", ""),
        blocked=kv.get("blocked", "false").lower() == "true",
    )


def parse_token(lines: list[str]) -> str | None:
    for line in lines:
        m = TOKEN_RE.search(line)
        if m:
            return m.group(1)
    return None


class BmdClient:
    def __init__(self, host: str, port: int = BMD_PORT, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self.banner: str = ""

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.banner = self._read_block_text()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "BmdClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _read_block(self) -> bytes:
        """Read a reply: burst-read with a short quiet timeout, fast-exit on the
        blank line that terminates multi-line replies. Single-line replies (e.g.
        ping's '200 ok') have no blank line and simply end after a quiet gap."""
        assert self._sock is not None
        self._sock.settimeout(RECV_WINDOW)
        buf = bytearray()
        start = time.monotonic()
        while time.monotonic() - start < RECV_CAP:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)
            if buf.endswith(b"\r\n\r\n") or buf.endswith(b"\n\n"):
                break
        return bytes(buf)

    def _read_block_text(self) -> str:
        return self._read_block().decode("utf-8", "replace")

    def _cmd(self, command: str) -> list[str]:
        if self._sock is None:
            raise BmdError("not connected")
        self._sock.sendall(command.encode("utf-8") + b"\n")
        block = self._read_block_text()
        result = [ln for ln in block.split("\n") if ln.strip() != ""]
        status = _status_code(result[0]) if result else 0
        if status >= 500 and status != 500:
            raise BmdError(f"deck rejected '{command}': {block.strip()}")
        return result

    def ping(self) -> bool:
        try:
            return any("200 ok" in line for line in self._cmd("ping"))
        except (BmdError, OSError):
            return False

    def slot_info(self, slot: int) -> SlotInfo:
        lines = self._cmd(f"slot info: slot id: {slot}")
        return parse_slot_info(lines)

    def format_prepare(self, slot: int, filesystem: str = "exFAT", name: str = "Media") -> str:
        command = f"format: slot id: {slot} prepare: {filesystem} name: {name}"
        lines = self._cmd(command)
        token = parse_token(lines)
        if not token:
            raise BmdError(
                f"format prepare for slot {slot} returned no parseable token: "
                f"{' | '.join(lines)!r}"
            )
        return token

    def format_confirm(self, token: str) -> bool:
        lines = self._cmd(f"format: confirm: {token}")
        return _status_code(lines[0]) < 400 if lines else False

    def format_slot(self, slot: int, filesystem: str = "exFAT", name: str = "Media") -> bool:
        """Two-step format: prepare (returns token) then confirm (executes).

        DESTRUCTIVE: wipes the whole card in `slot`. Only call after every clip on
        the slot has been archived and verified. Aborts (returns False, no change)
        if the prepare token cannot be parsed.
        """
        token = self.format_prepare(slot, filesystem, name)
        return self.format_confirm(token)


@contextmanager
def connect(host: str, port: int = BMD_PORT, timeout: float = 10.0):
    client = BmdClient(host, port, timeout)
    try:
        client.connect()
        yield client
    finally:
        client.close()
