"""Ingest orchestrator: archive every enabled deck to the NAS in parallel.

Per slot state machine: list clips -> download+verify each (resumable via manifest)
-> only if every video clip on the slot is verified, BMD-format that slot. Any
failed clip blocks that slot's clear and is surfaced in the run summary.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import manifest as manifest_mod
from .bmd_client import BmdClient
from .config import Config, DeckConfig
from .ftp_client import FtpDeck
from .models import ClipResult, DeckResult, RunSummary, SlotResult
from .nas import ensure_mount, free_space_gb
from .transfer import download_and_verify

log = logging.getLogger("hyperdeck_archiver.ingest")

_SEQ_RE = re.compile(r"\s(\d+)\.[A-Za-z0-9]+$")


def _max_seq_in(dest_root: Path) -> int:
    """Highest existing sequence number among renamed files in dest_root (0 if none)."""
    if not dest_root.exists():
        return 0
    best = 0
    for p in dest_root.iterdir():
        if not p.is_file():
            continue
        m = _SEQ_RE.search(p.name)
        if m:
            best = max(best, int(m.group(1)))
    return best


def _clip_dest(
    cfg: Config,
    deck: DeckConfig,
    slot: int,
    original_name: str,
    dest_root: Path,
    when: datetime,
    seq: int | None,
) -> Path:
    if cfg.rename_enabled:
        date_str = when.strftime(cfg.rename_date_format)
        stem = cfg.rename_pattern.format(date=date_str, deck=deck.number, slot=slot, seq=seq)
        ext = os.path.splitext(original_name)[1]
        return dest_root / (stem + ext)
    return dest_root / f"slot{slot}" / original_name


def _ingest_slot(
    cfg: Config,
    deck: DeckConfig,
    slot: int,
    ftp: FtpDeck,
    bmd: BmdClient | None,
    dest_root: Path,
    mdata: dict,
    mlock: threading.Lock,
    date_str: str,
    when: datetime,
    dry_run: bool,
    do_clear: bool,
    max_clips: int | None = None,
    counter: list[int] | None = None,
) -> SlotResult:
    sr = SlotResult(deck=deck.name, slot=slot)
    try:
        clips = ftp.list_clips(slot, cfg.skip_metadata)
    except Exception as e:  # noqa: BLE001
        sr.error = f"list clips failed: {e}"
        log.error("[%s slot %d] %s", deck.name, slot, sr.error)
        return sr

    processed = 0
    for clip in clips:
        if not clip.is_video:
            continue
        if max_clips is not None and processed >= max_clips:
            break
        processed += 1
        with mlock:
            existing = manifest_mod.clip_entry(mdata, deck.name, slot, clip.name)
        if existing and existing.get("status") == "verified" and existing.get("size") == clip.size:
            sr.clips.append(
                ClipResult(
                    clip=clip,
                    status="skipped",
                    dest_path=existing.get("dest", ""),
                    bytes_copied=clip.size or 0,
                    hash_algo=existing.get("hash_algo", ""),
                    hash_value=existing.get("hash", ""),
                )
            )
            log.info("[%s slot %d] skip (already verified): %s", deck.name, slot, clip.name)
            continue

        seq = None
        if cfg.rename_enabled and counter is not None:
            counter[0] += 1
            seq = counter[0]
        dest = _clip_dest(cfg, deck, slot, clip.name, dest_root, when, seq)

        if dry_run:
            sr.clips.append(ClipResult(clip=clip, status="pending", dest_path=str(dest)))
            continue

        cr = download_and_verify(ftp, clip, dest, cfg.hash_algo)
        sr.clips.append(cr)
        entry = {
            "name": clip.name,
            "slot": slot,
            "seq": seq,
            "size": clip.size,
            "hash_algo": cr.hash_algo,
            "hash": cr.hash_value,
            "status": cr.status,
            "dest": str(dest),
        }
        with mlock:
            manifest_mod.record_clip(mdata, deck.name, slot, entry)
            manifest_mod.save(cfg, date_str, mdata)

        if cr.status == "failed":
            # A failed download/size-query typically leaves the control socket
            # poisoned ('cannot read from timed out object'). Reconnect so the
            # next clip on this slot gets a clean connection instead of failing
            # spuriously. If the reconnect itself fails the deck has likely hung.
            try:
                ftp.reconnect()
            except Exception as e:  # noqa: BLE001
                sr.error = f"FTP connection lost after failed clip {clip.name}: {e}"
                log.error("[%s slot %d] %s", deck.name, slot, sr.error)
                break

    _maybe_clear(cfg, deck, slot, bmd, sr, mdata, mlock, date_str, dry_run, do_clear)
    return sr


def _maybe_clear(
    cfg: Config,
    deck: DeckConfig,
    slot: int,
    bmd: BmdClient | None,
    sr: SlotResult,
    mdata: dict,
    mlock: threading.Lock,
    date_str: str,
    dry_run: bool,
    do_clear: bool,
) -> None:
    video = [c for c in sr.clips if c.clip.is_video]
    if not video:
        return
    if not do_clear or dry_run:
        if do_clear and not sr.all_clips_verified:
            sr.clear_skipped = True
        return
    with mlock:
        already = manifest_mod.slot_cleared(mdata, deck.name, slot)
    if already:
        sr.cleared = True
        return
    if not sr.all_clips_verified:
        sr.clear_skipped = True
        log.warning("[%s slot %d] not all clips verified; skipping clear", deck.name, slot)
        return
    if bmd is None:
        sr.error = "cannot clear: BMD control connection unavailable"
        log.error("[%s slot %d] %s", deck.name, slot, sr.error)
        return
    try:
        ok = bmd.format_slot(slot)
        sr.cleared = ok
        if ok:
            with mlock:
                manifest_mod.mark_slot_cleared(mdata, deck.name, slot)
                manifest_mod.save(cfg, date_str, mdata)
            log.info("[%s slot %d] card formatted (cleared)", deck.name, slot)
    except Exception as e:  # noqa: BLE001
        sr.error = f"format failed: {e}"
        log.error("[%s slot %d] %s", deck.name, slot, sr.error)


def _ingest_deck(
    cfg: Config,
    deck: DeckConfig,
    dest_root: Path,
    mdata: dict,
    mlock: threading.Lock,
    date_str: str,
    when: datetime,
    dry_run: bool,
    do_clear: bool,
    max_clips: int | None = None,
) -> DeckResult:
    result = DeckResult(deck=deck.name, host=deck.host)
    try:
        ftp = FtpDeck(deck.host)
        ftp.connect()
    except Exception as e:  # noqa: BLE001
        result.error = f"FTP connect failed: {e}"
        log.error("[%s] %s", deck.name, result.error)
        return result

    bmd: BmdClient | None = None
    if do_clear and not dry_run:
        try:
            bmd = BmdClient(deck.host)
            bmd.connect()
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] BMD connect failed; slot status/format unavailable: %s", deck.name, e)
            bmd = None

    dest_root.mkdir(parents=True, exist_ok=True)
    counter = [_max_seq_in(dest_root)] if cfg.rename_enabled else [0]
    try:
        for i, slot in enumerate(deck.slots):
            # Fresh FTP control connection at each slot boundary. A timed-out
            # transfer leaves the control socket permanently unusable (ftplib:
            # 'cannot read from timed out object'); without this, a failure on an
            # earlier slot would cascade into every later slot's clip listing.
            if i > 0:
                try:
                    ftp.reconnect()
                except Exception as e:  # noqa: BLE001
                    result.error = f"FTP reconnect failed before slot {slot}: {e}"
                    log.error("[%s] %s", deck.name, result.error)
                    break
            result.slots.append(
                _ingest_slot(
                    cfg,
                    deck,
                    slot,
                    ftp,
                    bmd,
                    dest_root,
                    mdata,
                    mlock,
                    date_str,
                    when,
                    dry_run,
                    do_clear,
                    max_clips,
                    counter,
                )
            )
    finally:
        ftp.close()
        if bmd is not None:
            bmd.close()
    return result


def run(
    cfg: Config,
    when: datetime | None = None,
    dry_run: bool = False,
    no_clear: bool = False,
    deck_filter: set[str] | None = None,
    max_clips_per_slot: int | None = None,
) -> RunSummary:
    when = when or datetime.now()
    date_str = when.strftime(cfg.date_folder_format)
    started = datetime.now()
    summary = RunSummary(command="ingest", started_at=started, dry_run=dry_run)

    do_clear = cfg.clear_cards and not no_clear

    try:
        footage_dir = ensure_mount(cfg.mount_root, cfg.footage_root)
    except Exception as e:  # noqa: BLE001
        summary.error = f"NAS not ready: {e}"
        summary.finished_at = datetime.now()
        log.error(summary.error)
        return summary

    free_gb = free_space_gb(footage_dir)
    log.info("NAS free space: %.1f GB (min %d GB)", free_gb, cfg.min_free_gb)
    if not dry_run and 0 <= free_gb < cfg.min_free_gb:
        summary.error = (
            f"NAS free space {free_gb:.1f} GB below minimum {cfg.min_free_gb} GB; aborting."
        )
        summary.finished_at = datetime.now()
        log.error(summary.error)
        return summary

    mdata = manifest_mod.load(cfg, date_str)
    mlock = threading.Lock()

    enabled = [d for d in cfg.enabled_decks() if not deck_filter or d.name in deck_filter]
    if not enabled:
        summary.error = "no enabled decks selected"
        summary.finished_at = datetime.now()
        return summary

    workers = max(1, min(cfg.concurrency, len(enabled)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _ingest_deck,
                cfg,
                deck,
                cfg.dest_for(deck, when),
                mdata,
                mlock,
                date_str,
                when,
                dry_run,
                do_clear,
                max_clips_per_slot,
            ): deck
            for deck in enabled
        }
        for fut in futures:
            deck = futures[fut]
            try:
                summary.decks.append(fut.result())
            except Exception as e:  # noqa: BLE001
                summary.decks.append(
                    DeckResult(deck=deck.name, host=deck.host, error=f"worker crashed: {e}")
                )

    with mlock:
        manifest_mod.save(cfg, date_str, mdata)
    summary.finished_at = datetime.now()
    return summary
