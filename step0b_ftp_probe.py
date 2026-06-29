#!/usr/bin/env python3
"""Step 0b - read-only FTP capability probe for HyperDeck media access.

Walks the HyperDeck FTP tree (root exposes slots as dirs "1"/"2", clips inside),
reports the clip layout + sizes + file permissions, and reads only the first 4 KB
of one .mov to prove RETR (download) works.

Read-only: NO deletes (no DELE), NO writes, NO card format. Safe to run any time.

Usage
-----
    python3 step0b_ftp_probe.py 172.16.9.81 172.16.9.82
"""
from __future__ import annotations

import argparse
import ftplib
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

RETR_HEAD_BYTES = 4096
MAX_DEPTH = 3
MAX_FILES = 500


@dataclass
class FtpReport:
    host: str
    banner: str = ""
    login_ok: bool = False
    login_detail: str = ""
    listing: list[tuple[str, str]] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    retr_probe: str = ""
    notes: list[str] = field(default_factory=list)

    def add(self, msg: str) -> None:
        self.notes.append(msg)


def safe_call(fn, *a, **k):
    try:
        return fn(*a, **k), None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def try_login(ftp: ftplib.FTP) -> tuple[bool, str]:
    for user, pw in (("anonymous", "anonymous@"), ("ftp", "ftp")):
        try:
            resp = ftp.login(user, pw)
            return True, f"user='{user}' -> {resp.strip()}"
        except ftplib.all_errors as e:
            last = f"user='{user}' -> {e}"
    return False, last or "login failed"


def walk(
    ftp: ftplib.FTP,
    abspath: str,
    depth: int,
    files_out: list[dict],
    listing_out: list[tuple[str, str]],
) -> None:
    if depth > MAX_DEPTH or len(files_out) > MAX_FILES:
        return
    lines: list[str] = []
    try:
        ftp.retrlines(f"LIST {abspath}", lines.append)
    except ftplib.all_errors as e:
        listing_out.append((abspath, f"(LIST error: {e})"))
        return
    for line in lines:
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        perms = parts[0]
        name = parts[-1]
        if name in (".", ".."):
            continue
        child = abspath.rstrip("/") + "/" + name
        listing_out.append((child, line))
        if perms.startswith("d"):
            walk(ftp, child, depth + 1, files_out, listing_out)
        else:
            rec: dict = {"path": child, "name": name, "perms": perms}
            sz, e1 = safe_call(ftp.size, child)
            rec["size"] = sz
            if e1:
                rec["size_err"] = e1
            mt, e2 = safe_call(ftp.sendcmd, "MDTM " + child)
            rec["mdtm"] = mt.replace("213 ", "", 1) if (mt and not e2) else None
            files_out.append(rec)


def probe_host(host: str, timeout: float) -> FtpReport:
    r = FtpReport(host=host)
    ftp = ftplib.FTP()
    ftp.connect(host, 21, timeout=timeout)
    r.banner = (ftp.getwelcome() or "").strip()
    ok, detail = try_login(ftp)
    r.login_ok, r.login_detail = ok, detail
    if not ok:
        r.add("Anonymous login rejected. FTP credentials are set on this deck "
              "(deck network/FTP menu) - add to config before ingest.")
        try:
            ftp.quit()
        except ftplib.all_errors:
            pass
        return r

    walk(ftp, "/", 0, r.files, r.listing)

    movs = [f for f in r.files if f["name"].lower().endswith(".mov")]
    if movs:
        target = min(movs, key=lambda f: f.get("size") or 1 << 62)
        try:
            sock = ftp.transfercmd("RETR " + target["path"])
            head = sock.recv(RETR_HEAD_BYTES)
            sock.close()
            try:
                ftp.voidresp()
            except ftplib.all_errors:
                pass
            r.retr_probe = (
                f"RETR '{target['path']}': read {len(head)} bytes; "
                f"head[:16]={head[:16]!r} (expect 'ftyp' atom in a QuickTime file)"
            )
        except (ftplib.all_errors, OSError) as e:
            r.retr_probe = f"RETR probe FAILED on '{target['path']}': {e}"

    try:
        ftp.quit()
    except ftplib.all_errors:
        try:
            ftp.close()
        except Exception:
            pass
    return r


def assess(r: FtpReport) -> None:
    if not r.login_ok:
        r.add("Cannot enumerate clips over FTP until login is resolved.")
        return
    movs = [f for f in r.files if f["name"].lower().endswith(".mov")]
    total = sum((f.get("size") or 0) for f in movs)
    r.add(f"Found {len(movs)} .mov clip(s), ~{total/1e6:.1f} MB total.")
    if r.retr_probe.startswith("RETR"):
        r.add("RETR (download) channel CONFIRMED working.")
    elif r.retr_probe:
        r.add(r.retr_probe)
    slot_dirs = [c for c, line in r.listing if line.startswith("d") and c.count("/") == 1]
    slot_perms = {(c.split("/")[-1]): line.split()[0] for c, line in r.listing
                  if c.count("/") == 1 and line.startswith("d")}
    writable = any("w" in (p[1:4] + p[4:7] + p[7:10]) for p in slot_perms.values())
    r.add(f"Slot dirs: {slot_dirs} perms={slot_perms}. "
          f"Slot dirs {'ARE' if writable else 'are NOT'} writable -> "
          f"FTP DELE {'may' if writable else 'likely will NOT'} work; "
          "plan to clear cards via BMD 'format' (whole-slot) after verifying all clips.")


def write_log(out: Path, stamp: str, r: FtpReport) -> Path:
    safe = r.host.replace(".", "_")
    path = out / f"{stamp}_ftp_{safe}.txt"
    with path.open("w", encoding="utf-8") as f:
        f.write(f"HyperDeck FTP probe log\nhost: {r.host}\ntime: {stamp}\n\n")
        f.write(f"banner: {r.banner}\nlogin_ok: {r.login_ok}\nlogin_detail: {r.login_detail}\n\n")
        f.write("TREE (LIST):\n")
        for child, line in r.listing:
            f.write(f"  {child}  ::  {line}\n")
        f.write("\nFILES:\n")
        for rec in r.files:
            f.write(f"  {rec}\n")
        f.write(f"\nRETR probe: {r.retr_probe}\n\nASSESSMENT:\n")
        for n in r.notes:
            f.write(f"- {n}\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only FTP tree-walk probe for HyperDeck media access.",
        epilog="Read-only: tree LIST + sizes + 4KB head of smallest clip. No deletes/writes.",
    )
    parser.add_argument("hosts", nargs="+", help="HyperDeck IP(s) / hostname(s)")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--out", type=Path, default=Path("logs"))
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    print("Read-only FTP probe: tree LIST + sizes + 4KB head of one clip. No deletes/writes.")

    for host in args.hosts:
        print(f"\n=== FTP probing {host} ===")
        try:
            r = probe_host(host, args.timeout)
        except (ftplib.all_errors, OSError) as e:
            print(f"  CONNECT FAILED: {e}")
            continue
        assess(r)
        print(f"  banner: {r.banner}")
        print(f"  login : {r.login_detail}")
        print(f"  tree  : {len(r.listing)} entries, {len(r.files)} files")
        for rec in r.files[:12]:
            print(f"     - {rec}")
        print(f"  retr  : {r.retr_probe or '(no .mov to test)'}")
        for n in r.notes:
            print(f"    - {n}")
        print(f"  log   : {write_log(args.out, stamp, r)}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
