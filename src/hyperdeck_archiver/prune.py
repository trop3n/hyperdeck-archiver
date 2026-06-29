"""Retention pruner: delete NAS date-folders older than the retention window."""
from __future__ import annotations

import logging
from datetime import datetime

from .config import Config
from .models import RunSummary
from .nas import ensure_mount, remove_tree, select_prune_targets

log = logging.getLogger("hyperdeck_archiver.prune")


def run(
    cfg: Config,
    dry_run: bool = False,
    retention_days: int | None = None,
) -> RunSummary:
    days = cfg.retention_days if retention_days is None else retention_days
    started = datetime.now()
    summary = RunSummary(command="prune", started_at=started, dry_run=dry_run)

    if not cfg.retention_enabled:
        log.info("retention pruning disabled in config; nothing to do.")
        summary.finished_at = datetime.now()
        return summary

    try:
        footage_dir = ensure_mount(cfg.mount_root, cfg.footage_root)
    except Exception as e:  # noqa: BLE001
        summary.error = f"NAS not ready: {e}"
        summary.finished_at = datetime.now()
        log.error(summary.error)
        return summary

    targets = select_prune_targets(footage_dir, days)
    log.info("prune: %d folder(s) older than %d days", len(targets), days)
    for path in targets:
        rel = str(path.relative_to(cfg.mount_root))
        log.info("  %s %s", "would remove" if dry_run else "removing", rel)
        summary.pruned.append(rel)
        if not dry_run:
            try:
                remove_tree(path)
            except Exception as e:  # noqa: BLE001
                log.error("failed to remove %s: %s", path, e)
                summary.error = f"{summary.error}; {e}".strip("; ")

    summary.finished_at = datetime.now()
    return summary
