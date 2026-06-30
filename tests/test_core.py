"""Tests built from the real HyperDeck responses captured in step0/step0b logs."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hyperdeck_archiver import ingest as ingest_mod  # noqa: E402
from hyperdeck_archiver import manifest as manifest_mod  # noqa: E402
from hyperdeck_archiver import nas  # noqa: E402
from hyperdeck_archiver.bmd_client import parse_slot_info, parse_token  # noqa: E402
from hyperdeck_archiver.config import DeckConfig  # noqa: E402
from hyperdeck_archiver.ftp_client import is_metadata, parse_list_line  # noqa: E402
from hyperdeck_archiver.models import Clip, ClipResult, SlotResult  # noqa: E402

# ---- FTP LIST parsing (real lines from 172.16.9.81 / .82) ----

REAL_FILE_LINE = (
    "-rw-rw-rw- 1 root root 34986782632 Apr 27  2001 "
    "Blackmagic HyperDeck Studio Mini_0000.mov"
)
REAL_BRACKET_LINE = (
    "-rw-rw-rw- 1 root root 26146598760 Apr 27  2001 "
    "Blackmagic HyperDeck Studio Mini[0000].mov"
)
REAL_DIR_LINE = "dr-xr-xr-x 3 root root             0 Apr 27  2001 1"


def test_parse_file_line():
    parsed = parse_list_line(REAL_FILE_LINE)
    assert parsed is not None
    name, is_dir, size, perms = parsed
    assert name == "Blackmagic HyperDeck Studio Mini_0000.mov"
    assert is_dir is False
    assert size == 34986782632
    assert perms == "-rw-rw-rw-"


def test_parse_bracket_filename_preserved():
    name, is_dir, size, _ = parse_list_line(REAL_BRACKET_LINE)
    assert name == "Blackmagic HyperDeck Studio Mini[0000].mov"
    assert size == 26146598760 and is_dir is False


def test_parse_dir_line():
    name, is_dir, size, _ = parse_list_line(REAL_DIR_LINE)
    assert name == "1" and is_dir is True and size == 0


def test_parse_malformed_returns_none():
    assert parse_list_line("garbage line") is None


def test_metadata_filtering():
    pats = (".fseventsd", ".Spotlight-V100", ".Trashes", "._*")
    assert is_metadata(".fseventsd", pats)
    assert is_metadata("._foo", pats)
    assert is_metadata(".Spotlight-V100", pats)
    assert not is_metadata("Blackmagic HyperDeck Studio Mini_0000.mov", pats)
    assert is_metadata(".", pats)


# ---- BMD 9993 parsing (real slot-info + format token shapes) ----

SLOT_INFO_LINES = [
    "202 slot info:",
    "slot id: 1",
    "status: mounted",
    "volume name: Media",
    "recording time: 4893",
    "video format: 1080i5994",
    "blocked: false",
]


def test_parse_slot_info():
    info = parse_slot_info(SLOT_INFO_LINES)
    assert info.slot == 1
    assert info.status == "mounted"
    assert info.mounted is True
    assert info.volume_name == "Media"
    assert info.video_format == "1080i5994"
    assert info.blocked is False


def test_parse_slot_info_unmounted():
    lines = ["202 slot info:", "slot id: 2", "status: empty", "blocked: false"]
    info = parse_slot_info(lines)
    assert info.mounted is False and info.slot == 2


def test_parse_token_present():
    lines = ["250 format prepared:", "token: ABC-123-XYZ"]
    assert parse_token(lines) == "ABC-123-XYZ"


def test_parse_token_absent_returns_none():
    assert parse_token(["200 ok"]) is None
    assert parse_token([]) is None


# ---- Prune date logic ----

def test_select_prune_targets(tmp_path: Path):
    today = datetime(2026, 6, 29)
    for name in ("2026-05-01", "2026-05-28", "2026-06-28", "random", ".hidden"):
        (tmp_path / name).mkdir()
    targets = nas.select_prune_targets(tmp_path, retention_days=30, today=today)
    names = sorted(p.name for p in targets)
    assert names == ["2026-05-01", "2026-05-28"]


def test_select_prune_targets_empty(tmp_path: Path):
    assert nas.select_prune_targets(tmp_path, 30) == []


# ---- Manifest round-trip (resumability) ----

class _Cfg:
    def __init__(self, d: Path):
        self.manifest_dir = d


def test_manifest_record_and_lookup(tmp_path: Path):
    cfg = _Cfg(tmp_path)
    data = manifest_mod.load(cfg, "2026-06-29")
    manifest_mod.record_clip(data, "Deck1", 1, {
        "name": "clip_0000.mov", "size": 1000, "status": "verified",
        "hash": "deadbeef", "hash_algo": "blake2b",
    })
    manifest_mod.save(cfg, "2026-06-29", data)
    loaded = manifest_mod.load(cfg, "2026-06-29")
    entry = manifest_mod.clip_entry(loaded, "Deck1", 1, "clip_0000.mov")
    assert entry and entry["status"] == "verified" and entry["hash"] == "deadbeef"
    assert manifest_mod.slot_cleared(loaded, "Deck1", 1) is False
    manifest_mod.mark_slot_cleared(loaded, "Deck1", 1)
    assert manifest_mod.slot_cleared(loaded, "Deck1", 1) is True


def test_manifest_update_replaces_existing(tmp_path: Path):
    cfg = _Cfg(tmp_path)
    data = manifest_mod.load(cfg, "2026-06-29")
    manifest_mod.record_clip(data, "Deck1", 1, {"name": "x.mov", "status": "downloaded"})
    manifest_mod.record_clip(data, "Deck1", 1, {"name": "x.mov", "status": "verified", "hash": "h"})
    entry = manifest_mod.clip_entry(data, "Deck1", 1, "x.mov")
    assert entry["status"] == "verified"
    clips = data["decks"]["Deck1"]["slots"]["1"]["clips"]
    assert len(clips) == 1


# ---- Models: slot verification gate ----

def _clip(name="c.mov", size=10):
    return Clip(slot=1, clip_id=1, name=name, size=size)


def test_slot_all_verified_true():
    sr = SlotResult(deck="D", slot=1)
    sr.clips.append(ClipResult(clip=_clip("a.mov"), status="verified"))
    sr.clips.append(ClipResult(clip=_clip("b.mov"), status="verified"))
    assert sr.all_clips_verified is True


def test_slot_all_verified_false_when_failed():
    sr = SlotResult(deck="D", slot=1)
    sr.clips.append(ClipResult(clip=_clip("a.mov"), status="verified"))
    sr.clips.append(ClipResult(clip=_clip("b.mov"), status="failed", error="x"))
    assert sr.all_clips_verified is False


def test_slot_no_video_clips_not_verified():
    sr = SlotResult(deck="D", slot=1)
    assert sr.all_clips_verified is False


# ---- Rename / sequencing ----

class _RenameCfg:
    rename_enabled = True
    rename_pattern = "{date} {deck}-{slot} {seq:03d}"
    rename_date_format = "%m-%d-%Y"


class _NoRenameCfg:
    rename_enabled = False
    rename_pattern = "{date} {deck}-{slot} {seq:03d}"
    rename_date_format = "%m-%d-%Y"


def test_clip_dest_rename_format():
    deck = DeckConfig(name="Deck1", host="h", number=1)
    when = datetime(2026, 6, 29)
    dest = ingest_mod._clip_dest(
        _RenameCfg(), deck, slot=2, original_name="clip_0002.mov",
        dest_root=Path("/nas/footage/2026-06-29/Deck1"), when=when, seq=3,
    )
    assert dest.name == "06-29-2026 1-2 003.mov"
    assert dest.parent == Path("/nas/footage/2026-06-29/Deck1")


def test_clip_dest_rename_preserves_extension():
    deck = DeckConfig(name="Deck2", host="h", number=2)
    dest = ingest_mod._clip_dest(
        _RenameCfg(), deck, 1, "take.MP4", Path("/d"), datetime(2026, 1, 2), 10,
    )
    assert dest.name == "01-02-2026 2-1 010.MP4"


def test_clip_dest_rename_custom_pattern():
    deck = DeckConfig(name="Deck1", host="h", number=1)
    cfg = _RenameCfg()
    cfg.rename_pattern = "{deck:02d}_slot{slot}_{seq:04d}"
    dest = ingest_mod._clip_dest(cfg, deck, 2, "x.mov", Path("/d"), datetime(2026, 6, 29), 5)
    assert dest.name == "01_slot2_0005.mov"


def test_clip_dest_no_rename_uses_slot_subfolder():
    deck = DeckConfig(name="Deck1", host="h", number=1)
    dest = ingest_mod._clip_dest(
        _NoRenameCfg(), deck, 2, "orig.mov", Path("/d"), datetime(2026, 6, 29), None
    )
    assert dest == Path("/d/slot2/orig.mov")


def test_max_seq_in_finds_highest(tmp_path: Path):
    for name in ("06-29-2026 1 001.mov", "06-29-2026 1 003.mov", "06-29-2026 1 002.mov"):
        (tmp_path / name).write_bytes(b"")
    assert ingest_mod._max_seq_in(tmp_path) == 3


def test_max_seq_in_empty(tmp_path: Path):
    assert ingest_mod._max_seq_in(tmp_path) == 0
    assert ingest_mod._max_seq_in(tmp_path / "missing") == 0


def test_max_seq_in_ignores_unrelated(tmp_path: Path):
    (tmp_path / "random.mov").write_bytes(b"")
    (tmp_path / "06-29-2026 1 005.mov").write_bytes(b"")
    assert ingest_mod._max_seq_in(tmp_path) == 5
