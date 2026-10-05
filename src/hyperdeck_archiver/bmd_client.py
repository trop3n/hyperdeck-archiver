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
# A deck erasing a card can stay silent well past RECV_CAP, so an unanswered
# `format: confirm` is resolved by watching the slot rather than assumed failed.
FORMAT_SETTLE_TIMEOUT = 120.0
FORMAT_POLL_INTERVAL = 3.0
TOKEN_RE = re.compile(r"token:\s*(\S+)", re.IGNORECASE)
BARE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DISK_LIST_ENTRY_RE = re.compile(r"^\d+: (.*)$")


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


def parse_disk_list(lines: list[str]) -> list[str]:
    """Clip entries from a `disk list` reply, each "<name> <format> <video> <duration>".

    Names can contain spaces (a deck recorded " .mov"; the Studio Mini writes
    "Blackmagic HyperDeck Studio Mini_0000.mov"), so entries are kept whole and
    matched by name prefix. An empty slot answers "105 no disk" and yields [].
    """
    entries = []
    for line in lines:
        m = DISK_LIST_ENTRY_RE.match(line.rstrip("\r"))
        if m:
            entries.append(m.group(1))
    return entries


def parse_token(lines: list[str]) -> str | None:
    """Token from a `format ... prepare` reply.

    Two shapes seen in the field: a `token: <token>` line, and (Studio HD Plus /
    Studio Mini, protocol 1.19) a `216 format ready:` status line with the bare
    token alone on the next line.
    """
    for i, line in enumerate(lines):
        m = TOKEN_RE.search(line)
        if m:
            return m.group(1)
        if _status_code(line) == 216 and i + 1 < len(lines):
            candidate = lines[i + 1].strip()
            if BARE_TOKEN_RE.match(candidate):
                return candidate
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

    def disk_list(self, slot: int) -> list[str]:
        return parse_disk_list(self._cmd(f"disk list: slot id: {slot}"))

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

    def format_confirm(self, token: str) -> bool | None:
        """Send `format: confirm`. True/False from the deck's status line, or
        **None when the deck did not answer** — which is not a failure: erasing a
        card routinely outlasts the read window, and treating silence as False
        reported real wipes as 'no clear'. Callers resolve None by observing the
        slot (see format_slot).
        """
        lines = self._cmd(f"format: confirm: {token}")
        if not lines:
            return None
        return _status_code(lines[0]) < 400

    def slot_emptied(self, slot: int) -> bool:
        """Watch the slot until it lists no clips (True) or the deck runs out of
        time to finish (False). A deck mid-format may refuse or drop commands, so
        errors here are retried rather than treated as an answer."""
        deadline = time.monotonic() + FORMAT_SETTLE_TIMEOUT
        while True:
            try:
                if not self.disk_list(slot):
                    return True
            except (BmdError, OSError):
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(FORMAT_POLL_INTERVAL)

    def format_slot(self, slot: int, filesystem: str = "exFAT", name: str = "Media") -> bool:
        """Two-step format: prepare (returns token) then confirm (executes).

        DESTRUCTIVE: wipes the whole card in `slot`. Only call after every clip on
        the slot has been archived and verified. Aborts (returns False, no change)
        if the prepare token cannot be parsed.

        The return value reports what the card actually did: a confirm the deck
        never answered is settled by watching the slot empty out, so a successful
        wipe is never reported as a failure (and vice versa).
        """
        token = self.format_prepare(slot, filesystem, name)
        confirmed = self.format_confirm(token)
        if confirmed is not None:
            return confirmed
        return self.slot_emptied(slot)


@contextmanager
def connect(host: str, port: int = BMD_PORT, timeout: float = 10.0):
    client = BmdClient(host, port, timeout)
    try:
        client.connect()
        yield client
    finally:
        client.close()
