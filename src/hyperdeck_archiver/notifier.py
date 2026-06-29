"""Outlook SMTP notifier. Renders a RunSummary into an email summary."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .models import RunSummary
from .util import human_bytes

log = logging.getLogger("hyperdeck_archiver.notifier")


def render_summary(summary: RunSummary) -> tuple[str, str]:
    """Return (subject, body) for a RunSummary."""
    verb = summary.command
    state = "OK" if summary.succeeded else "FAILED" if summary.error or any(
        not d.ok for d in summary.decks
    ) else "OK"
    date = summary.started_at.strftime("%Y-%m-%d %H:%M")
    subject = f"[HyperDeck Archiver] {verb} {state} - {date}"

    lines: list[str] = []
    if summary.dry_run:
        lines.append("** DRY RUN - no files were written and no cards were cleared **")
        lines.append("")

    if summary.decks:
        if summary.dry_run:
            total_clips = sum(
                1
                for d in summary.decks
                for s in d.slots
                for c in s.clips
                if c.clip.is_video
            )
            total_bytes = sum(
                c.clip.size or 0
                for d in summary.decks
                for s in d.slots
                for c in s.clips
                if c.clip.is_video
            )
            lines.append(f"Plan: {total_clips} clip(s) to archive, {human_bytes(total_bytes)}.")
        else:
            verified, failed, skipped = summary.clip_counts()
            lines.append(
                f"Ingest: {verified} verified, {failed} failed, {skipped} skipped, "
                f"{human_bytes(summary.total_bytes)} copied."
            )
        lines.append("")
        for deck in summary.decks:
            tag = "OK" if deck.ok else "ERROR"
            lines.append(f"[{tag}] {deck.deck} ({deck.host})")
            if deck.error:
                lines.append(f"    deck error: {deck.error}")
            for slot in deck.slots:
                if summary.dry_run:
                    planned = sum(c.clip.size or 0 for c in slot.clips if c.clip.is_video)
                    n = sum(1 for c in slot.clips if c.clip.is_video)
                    lines.append(
                        f"    slot {slot.slot}: {n} clip(s), "
                        f"{human_bytes(planned)} (would archive)"
                    )
                else:
                    cleared = (
                        "cleared" if slot.cleared
                        else ("clear skipped" if slot.clear_skipped else "no clear")
                    )
                    verr = sum(1 for c in slot.clips if c.clip.is_video and c.status == "verified")
                    vfail = sum(1 for c in slot.clips if c.clip.is_video and c.status == "failed")
                    lines.append(
                        f"    slot {slot.slot}: {verr} ok / {vfail} failed "
                        f"({human_bytes(slot.bytes_copied)}) - {cleared}"
                    )
                if slot.error:
                    lines.append(f"      slot error: {slot.error}")
                for c in slot.clips:
                    if c.status == "failed":
                        lines.append(f"      FAILED clip: {c.clip.name} - {c.error}")
            lines.append("")

    if summary.pruned:
        lines.append(f"Pruned {len(summary.pruned)} date folder(s):")
        for p in summary.pruned:
            lines.append(f"  - {p}")
        lines.append("")

    if summary.error:
        lines.append(f"Run error: {summary.error}")

    if not lines:
        lines.append("Nothing to report.")
    return subject, "\n".join(lines)


def send_summary(config, summary: RunSummary) -> bool:
    if not config.smtp_to:
        log.warning("SMTP recipients not configured; skipping notification.")
        return False
    if not config.smtp_user or not config.smtp_pass:
        log.warning(
            "SMTP credentials not set (SMTP_USER/SMTP_PASS in .env); skipping notification."
        )
        return False

    subject, body = render_summary(summary)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.smtp_from or config.smtp_user
    msg["To"] = ", ".join(config.smtp_to)
    msg.set_content(body)

    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as server:
            if config.smtp_starttls:
                server.starttls()
            server.login(config.smtp_user, config.smtp_pass)
            server.send_message(msg)
        log.info("Notification sent: %s", subject)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Notification failed: %s", e)
        return False
