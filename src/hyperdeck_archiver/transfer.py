"""Download + verify engine: stream a clip from FTP to the NAS while hashing,
then re-hash the on-disk file to confirm it landed intact.

A clip is VERIFIED only when:
  - bytes written == FTP-reported source size (completeness), AND
  - hash computed during download == hash computed by re-reading the NAS file.
There is no source-side checksum from the deck, so this is the strongest practical
integrity guarantee and is what gates card clearing.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .ftp_client import FtpDeck
from .models import Clip, ClipResult

log = logging.getLogger("hyperdeck_archiver.transfer")


def make_hasher(algo: str):
    algo = (algo or "blake2b").lower()
    if algo == "xxhash":
        try:
            import xxhash  # type: ignore

            return xxhash.xxh3_128(), "xxh3_128"
        except ImportError:
            log.warning("xxhash not installed; falling back to blake2b")
            algo = "blake2b"
    if algo == "sha256":
        return hashlib.sha256(), "sha256"
    if algo == "blake2b":
        return hashlib.blake2b(), "blake2b"
    return hashlib.blake2b(), "blake2b"


def _hash_file(path: str | Path, algo_name: str) -> str:
    if algo_name.startswith("xxh"):
        import xxhash  # type: ignore

        h = xxhash.xxh3_128()
    elif algo_name == "sha256":
        h = hashlib.sha256()
    else:
        h = hashlib.blake2b()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(262144), b""):
            h.update(chunk)
    return h.hexdigest()


def download_and_verify(
    deck: FtpDeck,
    clip: Clip,
    dest_path: Path,
    algo: str,
    logger: logging.Logger | None = None,
) -> ClipResult:
    log = logger or logging.getLogger("hyperdeck_archiver.transfer")
    result = ClipResult(clip=clip, dest_path=str(dest_path))
    try:
        src_size = deck.size(clip.slot, clip.name)
    except Exception as e:  # noqa: BLE001
        result.status = "failed"
        result.error = f"size query failed: {e}"
        return result

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    hasher, algo_name = make_hasher(algo)
    result.hash_algo = algo_name

    try:
        written = deck.download(clip.slot, clip.name, str(dest_path), hasher)
    except Exception as e:  # noqa: BLE001
        result.status = "failed"
        result.error = f"download failed: {e}"
        return result
    result.bytes_copied = written
    result.hash_value = hasher.hexdigest()

    if src_size is not None and written != src_size:
        result.status = "failed"
        result.error = f"size mismatch: wrote {written}, deck reports {src_size}"
        log.error("[%s] %s %s", result.error, clip.name, clip.slot)
        return result

    try:
        disk_hash = _hash_file(dest_path, algo_name)
    except Exception as e:  # noqa: BLE001
        result.status = "failed"
        result.error = f"verify re-read failed: {e}"
        return result

    if disk_hash != result.hash_value:
        result.status = "failed"
        result.error = "verify hash mismatch (NAS file differs from streamed bytes)"
        return result

    result.status = "verified"
    log.info("verified %s (%d bytes, %s)", clip.name, written, algo_name)
    return result
