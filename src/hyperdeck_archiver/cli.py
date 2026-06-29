"""Command-line entry point: ingest | prune | probe."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from . import ingest, prune
from .bmd_client import BmdClient
from .config import load_config
from .ftp_client import FtpDeck
from .notifier import send_summary
from .util import human_bytes, setup_logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hyperdeck-archiver")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Plan only; write/clear nothing")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Archive decks to NAS, optionally clear cards")
    p_ingest.add_argument("--date", help="Run date folder YYYY-MM-DD (default: today)")
    p_ingest.add_argument("--deck", action="append", default=[], help="Limit to deck name(s)")
    p_ingest.add_argument("--no-clear", action="store_true", help="Do not format cards this run")

    p_prune = sub.add_parser("prune", help="Delete NAS date-folders older than retention")
    p_prune.add_argument("--retention-days", type=int, help="Override retention.days")

    sub.add_parser("probe", help="Read-only reachability check of configured decks")
    return parser


def _cmd_probe(cfg, log: logging.Logger) -> int:
    for deck in cfg.enabled_decks():
        print(f"\n=== {deck.name} ({deck.host}) ===")
        ftp_ok = clips = bmd_ok = None
        try:
            with FtpDeck(deck.host) as ftp:
                total = 0
                count = 0
                for slot in deck.slots:
                    for c in ftp.list_clips(slot, cfg.skip_metadata):
                        count += 1
                        total += c.size or 0
                ftp_ok = True
                clips = f"{count} clips, {human_bytes(total)}"
        except Exception as e:  # noqa: BLE001
            ftp_ok = False
            clips = f"FTP error: {e}"
        try:
            with BmdClient(deck.host) as bmd:
                bmd_ok = bmd.ping()
        except Exception:  # noqa: BLE001
            bmd_ok = False
        print(f"  FTP : {'OK' if ftp_ok else 'FAIL'} - {clips}")
        print(f"  BMD : {'OK' if bmd_ok else 'FAIL'} (9993)")
        log.info("probe %s ftp=%s bmd=%s clips=%s", deck.host, ftp_ok, bmd_ok, clips)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = load_config(args.config)
    log = setup_logging(cfg.log_file, cfg.log_level)

    if args.command == "ingest":
        when = datetime.strptime(args.date, "%Y-%m-%d") if args.date else None
        deck_filter = set(args.deck) if args.deck else None
        summary = ingest.run(
            cfg, when=when, dry_run=args.dry_run, no_clear=args.no_clear, deck_filter=deck_filter
        )
        send = cfg.notify_on_success if summary.succeeded else cfg.notify_on_failure
        if send:
            send_summary(cfg, summary)
        return 0 if summary.succeeded and not summary.error else 1

    if args.command == "prune":
        summary = prune.run(cfg, dry_run=args.dry_run, retention_days=args.retention_days)
        send = cfg.notify_on_success if not summary.error else cfg.notify_on_failure
        if send:
            send_summary(cfg, summary)
        return 0 if not summary.error else 1

    if args.command == "probe":
        return _cmd_probe(cfg, log)

    return 2


if __name__ == "__main__":
    sys.exit(main())
