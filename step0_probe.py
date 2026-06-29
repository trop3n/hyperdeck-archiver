#!/usr/bin/env python3
"""Step 0 - read-only capability probe for Blackmagic HyperDeck recorders.

Purpose
-------
Determine, per HyperDeck, whether footage is reachable over the network and via
what mechanism, so the ingest source classes can be built on fact, not guesses.

What it does
------------
1. TCP-connect scans a set of candidate ports (default includes 9993 = BMD
   HyperDeck Ethernet Protocol, plus 21/80/443/445/etc.).
2. If 9993 is open, speaks the text protocol just enough to read the banner,
   answer `ping`, and request a clip list with `clips get`. All raw traffic is
   hex+ascii dumped to a log file for manual inspection.
3. If an HTTP port is open, issues `GET /` and records status/headers/snippet.
4. Prints a per-host summary and a recommendation, and writes a timestamped
   raw transcript under ./logs/.

What it does NOT do
-------------------
- It NEVER writes to, deletes from, or formats any card on the deck.
- It NEVER downloads clip content (listing only). The deck is treated read-only.
- It makes no claim about the download mechanism; it only gathers evidence.

Usage
-----
    python3 step0_probe.py 172.16.9.81 172.16.9.82
    python3 step0_probe.py 172.16.9.81 --timeout 2 --ports 21,80,445,9993
"""
from __future__ import annotations

import argparse
import re
import socket
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_PORTS = [21, 22, 23, 80, 443, 445, 5000, 8000, 8080, 9000, 9993]
BMD_PORT = 9993
RECV_WINDOW = 1.0
RECV_CAP = 5.0
MAX_BURST_BYTES = 65536
CLIP_RE = re.compile(rb"\.mov\b|\.mp4\b|\.mxf\b", re.IGNORECASE)


@dataclass
class HostReport:
    host: str
    ports_open: dict[int, bool] = field(default_factory=dict)
    bmd_transcript: list[tuple[str, bytes]] = field(default_factory=list)
    bmd_answered_ping: bool = False
    bmd_clips_bytes: int = 0
    bmd_clip_hints: int = 0
    http_snippets: dict[int, dict[str, str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def add(self, msg: str) -> None:
        self.notes.append(msg)


def port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def recv_burst(sock: socket.socket, cap_seconds: float = RECV_CAP) -> bytes:
    sock.settimeout(RECV_WINDOW)
    buf = bytearray()
    start = datetime.now()
    while (datetime.now() - start).total_seconds() < cap_seconds:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) >= MAX_BURST_BYTES:
            break
    return bytes(buf)


def count_clip_hints(data: bytes) -> int:
    return len(CLIP_RE.findall(data))


def probe_bmd(r: HostReport, port: int, timeout: float) -> None:
    try:
        with socket.create_connection((r.host, port), timeout=timeout) as sock:
            banner = recv_burst(sock)
            r.bmd_transcript.append(("RECV banner", banner))
            sock.sendall(b"\n")
            nudge = recv_burst(sock, cap_seconds=2.0)
            if nudge:
                r.bmd_transcript.append(("RECV after blank line", nudge))
            for cmd in ("ping", "clips get", "disk list", "slot info", "help"):
                try:
                    sock.sendall(cmd.encode() + b"\n")
                except OSError as e:
                    r.bmd_transcript.append((f"SEND {cmd} FAILED", str(e).encode()))
                    continue
                resp = recv_burst(sock)
                r.bmd_transcript.append((f"RECV for '{cmd}'", resp))
                if cmd == "ping" and resp:
                    r.bmd_answered_ping = True
                if cmd == "clips get":
                    r.bmd_clips_bytes = len(resp)
                    r.bmd_clip_hints = count_clip_hints(resp)
            try:
                sock.sendall(b"quit\n")
            except OSError:
                pass
    except OSError as e:
        r.bmd_transcript.append(("CONNECT FAILED", str(e).encode()))


def probe_http(host: str, port: int, timeout: float) -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"GET / HTTP/1.0\r\nHost: " + host.encode() + b"\r\n\r\n")
            data = recv_burst(sock, cap_seconds=3.0)
            text = data.decode("latin-1", errors="replace")
            head, _, body = text.partition("\r\n\r\n")
            info["status"] = head.splitlines()[0] if head else "(empty)"
            info["headers"] = head[:1000]
            info["body_snippet"] = body[:500]
            for line in head.splitlines():
                low = line.lower()
                if low.startswith("server:"):
                    info["server"] = line.split(":", 1)[1].strip()
                elif low.startswith("content-type:"):
                    info["content_type"] = line.split(":", 1)[1].strip()
    except OSError as e:
        info["error"] = str(e)
    return info


def assess(r: HostReport) -> None:
    open_ports = [p for p, ok in r.ports_open.items() if ok]
    if BMD_PORT in open_ports:
        if r.bmd_clips_bytes > 0:
            r.add(
                f"9993 OPEN and 'clips get' returned {r.bmd_clips_bytes} bytes "
                f"(~{r.bmd_clip_hints} clip-name hints). Network LISTING looks viable. "
                "Open the log and confirm the clip format + whether a download command exists."
            )
        elif r.bmd_answered_ping:
            r.add(
                "9993 OPEN and answered 'ping', but 'clips get' returned nothing. "
                "Protocol is present but media listing is unavailable on this model/firmware "
                "(likely control-only). Network download probably NOT viable -> USB fallback."
            )
        else:
            r.add(
                "9993 OPEN but did not answer 'ping'. Protocol uncertain - inspect the raw log."
            )
    else:
        r.add(
            "9993 (HyperDeck protocol) is NOT open. Network ingest via BMD protocol is NOT "
            "available on this deck -> use USB-C mass storage or a card reader."
        )
    for p, info in r.http_snippets.items():
        if info.get("error"):
            r.add(f"HTTP {p}: error - {info['error']}")
        elif info.get("status"):
            r.add(
                f"HTTP {p}: {info['status']} "
                f"server={info.get('server', '?')} type={info.get('content_type', '?')}"
            )


def hexdump(label: str, data: bytes) -> str:
    out = [f"--- {label} ({len(data)} bytes) ---"]
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        ascpart = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{i:04x}  {hexpart:<48}  {ascpart}")
    return "\n".join(out)


def print_summary(r: HostReport) -> None:
    open_ports = [p for p, ok in r.ports_open.items() if ok]
    print(f"  summary: open={open_ports or 'none'}")
    for n in r.notes:
        print(f"    - {n}")


def write_log(out: Path, stamp: str, r: HostReport) -> Path:
    safe = r.host.replace(".", "_")
    path = out / f"{stamp}_{safe}.txt"
    with path.open("w", encoding="utf-8") as f:
        f.write(f"HyperDeck probe log\nhost: {r.host}\ntime: {stamp}\n\n")
        f.write("PORT SCAN\n")
        for p, ok in r.ports_open.items():
            f.write(f"  {p}: {'OPEN' if ok else 'closed'}\n")
        f.write("\nHYPERDECK PROTOCOL (9993) TRANSCRIPT\n")
        for label, data in r.bmd_transcript:
            f.write(hexdump(label, data) + "\n")
        if r.http_snippets:
            f.write("\nHTTP PROBES\n")
            for p, info in r.http_snippets.items():
                f.write(f"--- port {p} ---\n")
                for k, v in info.items():
                    f.write(f"{k}: {v}\n")
                f.write("\n")
        f.write("\nASSESSMENT\n")
        for n in r.notes:
            f.write(f"- {n}\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only probe to check HyperDeck network media reachability.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Read-only: lists only, never downloads or deletes anything.",
    )
    parser.add_argument("hosts", nargs="+", help="HyperDeck IP(s) / hostname(s)")
    parser.add_argument(
        "--ports",
        default=",".join(str(p) for p in DEFAULT_PORTS),
        help="Comma-separated ports to scan (default: %(default)s)",
    )
    parser.add_argument("--timeout", type=float, default=3.0, help="Per-connect timeout seconds")
    parser.add_argument("--out", type=Path, default=Path("logs"), help="Log output directory")
    args = parser.parse_args()

    ports = [int(p) for p in args.ports.split(",")]
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    print("Read-only probe: no downloads, no deletes, no card writes.")
    for host in args.hosts:
        print(f"\n=== Probing {host} ===")
        report = HostReport(host=host)
        for p in ports:
            ok = port_open(host, p, args.timeout)
            report.ports_open[p] = ok
            print(f"  port {p:>5}: {'OPEN' if ok else 'closed'}")
        open_ports = [p for p, ok in report.ports_open.items() if ok]
        if not open_ports:
            report.add("No ports open. Network ingest NOT viable -> USB/card-reader fallback.")
            print_summary(report)
            write_log(args.out, stamp, report)
            continue
        if BMD_PORT in open_ports:
            print(f"  -> speaking HyperDeck protocol on {BMD_PORT} ...")
            probe_bmd(report, BMD_PORT, args.timeout)
        for p in (80, 443, 5000, 8080):
            if p in open_ports:
                print(f"  -> probing HTTP on {p} ...")
                report.http_snippets[p] = probe_http(host, p, args.timeout)
        assess(report)
        print_summary(report)
        log = write_log(args.out, stamp, report)
        print(f"  log: {log}")

    print("\nDone. Inspect the files under ./logs/ to confirm clip listing and spot any download path.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
