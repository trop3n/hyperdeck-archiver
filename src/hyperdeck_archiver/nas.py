"""NAS-side helpers: mount preflight, free-space, and dated-folder pruning."""
from __future__ import annotations

import shutil
import tempfile
from datetime import datetime
from pathlib import Path

DATE_FORMAT = "%Y-%m-%d"


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


def free_space_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(str(path)).free / 1e9
    except OSError:
        return -1.0


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
