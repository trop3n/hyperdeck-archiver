"""Data models (plain dataclasses) shared across modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Clip:
    """A single clip on a deck slot."""

    slot: int
    clip_id: int
    name: str
    size: int | None = None
    start_tc: str = ""
    duration_tc: str = ""

    @property
    def is_video(self) -> bool:
        return self.name.lower().endswith((".mov", ".mp4", ".mxf"))


@dataclass(frozen=True)
class SlotInfo:
    slot: int
    status: str = "unknown"
    volume_name: str = ""
    video_format: str = ""
    blocked: bool = False

    @property
    def mounted(self) -> bool:
        return self.status.lower() == "mounted"


@dataclass
class ClipResult:
    clip: Clip
    status: str = "pending"          # pending|downloaded|verified|failed|skipped
    dest_path: str = ""
    bytes_copied: int = 0
    hash_algo: str = ""
    hash_value: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "verified"


@dataclass
class SlotResult:
    deck: str
    slot: int
    clips: list[ClipResult] = field(default_factory=list)
    cleared: bool = False
    clear_skipped: bool = False
    error: str = ""

    @property
    def all_clips_verified(self) -> bool:
        verified = [c for c in self.clips if c.clip.is_video]
        return bool(verified) and all(c.ok for c in self.clips if c.clip.is_video)

    @property
    def bytes_copied(self) -> int:
        return sum(c.bytes_copied for c in self.clips)


@dataclass
class DeckResult:
    deck: str
    host: str
    slots: list[SlotResult] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and all(not s.error for s in self.slots)

    @property
    def bytes_copied(self) -> int:
        return sum(s.bytes_copied for s in self.slots)


@dataclass
class RunSummary:
    command: str
    started_at: datetime
    finished_at: datetime | None = None
    decks: list[DeckResult] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    dry_run: bool = False
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return not self.error and all(d.ok for d in self.decks)

    @property
    def total_bytes(self) -> int:
        return sum(d.bytes_copied for d in self.decks)

    def clip_counts(self) -> tuple[int, int, int]:
        verified = failed = skipped = 0
        for d in self.decks:
            for s in d.slots:
                for c in s.clips:
                    if not c.clip.is_video:
                        continue
                    if c.status == "verified":
                        verified += 1
                    elif c.status == "failed":
                        failed += 1
                    elif c.status == "skipped":
                        skipped += 1
        return verified, failed, skipped
