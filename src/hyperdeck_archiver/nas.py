"""NAS-side helpers: mount preflight, free-space, and dated-folder pruning."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

DATE_FORMAT = "%Y-%m-%d"

# df -Pk data line: <filesystem> <1K-blocks> <used> <avail> <capacity>% <mount point>.
# Anchored on the '%' so a filesystem/mount name containing spaces or digits
# (e.g. '//user@host/Video%20Archive ... /Volumes/Video Archive') can't shift it.
_DF_RE = re.compile(r"\s(\d+)\s+(\d+)\s+(\d+)\s+\d+%\s")


class NasError(RuntimeError):
    pass


def ensure_mount(mount_root: Path, footage_root: str) -> Path:
    footage_dir = mount_root / footage_root
    if not mount_root.exists():
        raise NasError(
            f"NAS mount point {mount_root} does not exist. Mount the share first "
            f"(macOS autofs or Connect to Server; Linux /etc/fstab)."
        )
    footage_dir.mkdir(parents=True, exist_ok=True)
    _assert_writable(footage_dir)
    return footage_dir


def _assert_writable(path: Path) -> None:
    try:
        with tempfile.NamedTemporaryFile(prefix=".ha-probe-", dir=path, delete=True):
            pass
    except OSError as e:
        raise NasError(
            f"{path} is not writable by this user ({e}). Check SMB permissions "
            f"and that the share is mounted read/write."
        ) from e


def parse_df(output: str) -> tuple[int, int, int] | None:
    """(total, used, free) bytes from `df -Pk` output, or None if unparseable."""
    lines = [ln for ln in output.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    m = _DF_RE.search(lines[-1])
    if not m:
        return None
    total, used, free = (int(g) * 1024 for g in m.groups())
    return total, used, free


def disk_usage(path: Path) -> tuple[int, int, int] | None:
    """(total, used, free) bytes for the filesystem holding `path`; None if unmeasurable.

    Uses df(1), not shutil.disk_usage. macOS statvfs() truncates its block counts
    to 32 bits, so on a share larger than 2**32 * f_frsize bytes the numbers wrap:
    the 35 TiB Synology share read back as 3.1 TB total / 324 GB free, which
    false-tripped the min_free_gb gate. df(1) reads statfs(), whose counts are
    64-bit, and agrees with what DSM reports.
    """
    try:
        proc = subprocess.run(
            ["df", "-Pk", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_df(proc.stdout)


def free_space_gb(path: Path) -> float:
    usage = disk_usage(path)
    return -1.0 if usage is None else usage[2] / 1e9


def list_date_folders(footage_dir: Path) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    if not footage_dir.exists():
        return result
    for entry in footage_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        try:
            datetime.strptime(entry.name, DATE_FORMAT)
        except ValueError:
            continue
        result.append((entry.name, entry))
    result.sort(key=lambda t: t[0])
    return result


def select_prune_targets(
    footage_dir: Path, retention_days: int, today: datetime | None = None
) -> list[Path]:
    today = today or datetime.now()
    targets: list[Path] = []
    for name, path in list_date_folders(footage_dir):
        try:
            folder_date = datetime.strptime(name, DATE_FORMAT)
        except ValueError:
            continue
        age_days = (today - folder_date).days
        if age_days > retention_days:
            targets.append(path)
    return targets


def remove_tree(path: Path) -> None:
    if path.exists() and path.is_dir():
        shutil.rmtree(path)
