"""Tests built from the real HyperDeck responses captured in step0/step0b logs."""
from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hyperdeck_archiver import ingest as ingest_mod  # noqa: E402
from hyperdeck_archiver import manifest as manifest_mod  # noqa: E402
from hyperdeck_archiver import nas  # noqa: E402
from hyperdeck_archiver.bmd_client import parse_slot_info, parse_token  # noqa: E402
from hyperdeck_archiver.config import DeckConfig, load_config  # noqa: E402
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


# ---- Config: deck number auto-derive + uniqueness validation ----

def _write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_load_config_auto_derives_number_from_name(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        """
decks:
  - name: Deck3
    host: 10.0.0.3
nas:
  mount_root: /nas
""",
    )
    cfg = load_config(p)
    assert cfg.decks[0].number == 3


def test_load_config_explicit_number_wins(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        """
decks:
  - name: Deck3
    host: 10.0.0.3
    number: 99
nas:
  mount_root: /nas
""",
    )
    cfg = load_config(p)
    assert cfg.decks[0].number == 99


def test_load_config_number_falls_back_to_position(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        """
decks:
  - name: Alpha
    host: 10.0.0.1
  - name: Bravo
    host: 10.0.0.2
nas:
  mount_root: /nas
""",
    )
    cfg = load_config(p)
    assert [d.number for d in cfg.decks] == [1, 2]


def test_load_config_rejects_duplicate_deck_names(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        """
decks:
  - name: Deck3
    host: 10.0.0.3
  - name: Deck3
    host: 10.0.0.4
nas:
  mount_root: /nas
""",
    )
    with pytest.raises(ValueError, match="duplicate deck name"):
        load_config(p)


def test_load_config_rejects_duplicate_deck_numbers(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        """
decks:
  - name: Deck3
    host: 10.0.0.3
  - name: Other
    host: 10.0.0.4
    number: 3
nas:
  mount_root: /nas
""",
    )
    with pytest.raises(ValueError, match="duplicate deck number"):
        load_config(p)


# ---- FTP connection resilience (reconnect on failure / per-slot boundary) ----
#
# Regression: a single timed-out download used to leave the shared FTP control
# socket permanently unusable (ftplib: "cannot read from timed out object"), so
# every later clip and the NEXT slot's listing failed spuriously. These tests pin
# the contract that a dead connection is replaced before further use.

class _FakeFtp:
    """Stand-in FtpDeck that records control-connection lifecycle for assertions."""

    def __init__(self, host="h"):
        self.host = host
        self.connects = 0
        self.closes = 0
        self.reconnects = 0
        self.list_calls: list[int] = []
        self.clips_by_slot: dict[int, list[Clip]] = {}
        self.reconnect_raises = False

    def connect(self):
        self.connects += 1

    def close(self):
        self.closes += 1

    def reconnect(self):
        self.reconnects += 1
        if self.reconnect_raises:
            raise OSError("cannot read from timed out object")

    def list_clips(self, slot, skip=()):
        self.list_calls.append(slot)
        return list(self.clips_by_slot.get(slot, []))


class _FakeBmd:
    def __init__(self, host="h", **kw):
        self.host = host
        self.connected = False
        self.closed = False
        self.formatted: list[int] = []

    def connect(self):
        self.connected = True

    def close(self):
        self.closed = True

    def format_slot(self, slot, *a, **kw):
        self.formatted.append(slot)
        return True


class _IngestCfg:
    """Minimal Config surface exercised by _ingest_deck / _ingest_slot."""

    skip_metadata = (".fseventsd", "._*")
    hash_algo = "blake2b"
    rename_enabled = False

    def __init__(self, manifest_dir: Path):
        self.manifest_dir = manifest_dir


def _download_returning(results: dict[str, str]):
    """Stub for transfer.download_and_verify keyed by clip name -> status."""

    def _stub(deck, clip, dest_path, algo, logger=None):
        return ClipResult(clip=clip, status=results.get(clip.name, "verified"),
                          dest_path=str(dest_path))

    return _stub


def test_ingest_deck_reconnects_between_slots(tmp_path: Path, monkeypatch):
    ftp = _FakeFtp()
    ftp.clips_by_slot = {1: [Clip(1, 1, "a.mov", 10)], 2: [Clip(2, 1, "b.mov", 10)]}
    monkeypatch.setattr(ingest_mod, "FtpDeck", lambda host, **kw: ftp)
    monkeypatch.setattr(ingest_mod, "BmdClient", lambda host, **kw: _FakeBmd(host))
    monkeypatch.setattr(ingest_mod, "download_and_verify", _download_returning({}))

    cfg = _IngestCfg(tmp_path)
    deck = DeckConfig(name="Deck2", host="172.16.9.82", slots=(1, 2))
    mdata = manifest_mod.load(cfg, "2026-07-13")

    result = ingest_mod._ingest_deck(
        cfg, deck, tmp_path, mdata, threading.Lock(), "2026-07-13",
        datetime(2026, 7, 13), dry_run=False, do_clear=False,
    )
    # slot 1 uses the initial connect; exactly one reconnect happens before slot 2.
    assert ftp.connects == 1
    assert ftp.reconnects == 1
    assert ftp.list_calls == [1, 2]
    assert len(result.slots) == 2
    assert all(s.clips[0].status == "verified" for s in result.slots)


def test_ingest_slot_reconnects_after_failed_clip(tmp_path: Path, monkeypatch):
    ftp = _FakeFtp()
    ftp.clips_by_slot = {1: [Clip(1, 1, "a.mov", 10), Clip(1, 2, "b.mov", 10)]}
    monkeypatch.setattr(ingest_mod, "download_and_verify",
                        _download_returning({"a.mov": "failed"}))

    cfg = _IngestCfg(tmp_path)
    deck = DeckConfig(name="Deck2", host="172.16.9.82", slots=(1,))
    mdata = manifest_mod.load(cfg, "2026-07-13")

    sr = ingest_mod._ingest_slot(
        cfg, deck, 1, ftp, None, tmp_path, mdata, threading.Lock(), "2026-07-13",
        datetime(2026, 7, 13), dry_run=False, do_clear=False, max_clips=None, counter=[0],
    )
    # first clip failed -> one reconnect; second clip is still attempted on a fresh link.
    assert ftp.reconnects == 1
    assert len(sr.clips) == 2
    assert sr.clips[0].status == "failed"
    assert sr.clips[1].status == "verified"
    assert sr.error == ""


def test_ingest_slot_breaks_when_reconnect_fails(tmp_path: Path, monkeypatch):
    ftp = _FakeFtp()
    ftp.reconnect_raises = True
    ftp.clips_by_slot = {1: [Clip(1, 1, "a.mov", 10), Clip(1, 2, "b.mov", 10)]}
    monkeypatch.setattr(ingest_mod, "download_and_verify",
                        _download_returning({"a.mov": "failed"}))

    cfg = _IngestCfg(tmp_path)
    deck = DeckConfig(name="Deck2", host="172.16.9.82", slots=(1,))
    mdata = manifest_mod.load(cfg, "2026-07-13")

    sr = ingest_mod._ingest_slot(
        cfg, deck, 1, ftp, None, tmp_path, mdata, threading.Lock(), "2026-07-13",
        datetime(2026, 7, 13), dry_run=False, do_clear=False, max_clips=None, counter=[0],
    )
    # reconnect failed after the first clip -> stop; the second clip is never tried.
    assert ftp.reconnects == 1
    assert len(sr.clips) == 1
    assert sr.clips[0].status == "failed"
    assert sr.error.startswith("FTP connection lost")


def test_ingest_deck_stops_when_reconnect_between_slots_fails(tmp_path: Path, monkeypatch):
    ftp = _FakeFtp()
    ftp.reconnect_raises = True
    ftp.clips_by_slot = {1: [Clip(1, 1, "a.mov", 10)], 2: [Clip(2, 1, "b.mov", 10)]}
    monkeypatch.setattr(ingest_mod, "FtpDeck", lambda host, **kw: ftp)
    monkeypatch.setattr(ingest_mod, "BmdClient", lambda host, **kw: _FakeBmd(host))
    monkeypatch.setattr(ingest_mod, "download_and_verify", _download_returning({}))

    cfg = _IngestCfg(tmp_path)
    deck = DeckConfig(name="Deck2", host="172.16.9.82", slots=(1, 2))
    mdata = manifest_mod.load(cfg, "2026-07-13")

    result = ingest_mod._ingest_deck(
        cfg, deck, tmp_path, mdata, threading.Lock(), "2026-07-13",
        datetime(2026, 7, 13), dry_run=False, do_clear=False,
    )
    # slot 1 processed; reconnect before slot 2 failed -> stop with a deck error.
    assert ftp.reconnects == 1
    assert len(result.slots) == 1
    assert result.error.startswith("FTP reconnect failed before slot 2")


def test_bmd_not_opened_when_not_clearing(tmp_path: Path, monkeypatch):
    made = {"n": 0}

    def make_bmd(host, **kw):
        made["n"] += 1
        return _FakeBmd(host)

    ftp = _FakeFtp()
    ftp.clips_by_slot = {1: [Clip(1, 1, "a.mov", 10)], 2: []}
    monkeypatch.setattr(ingest_mod, "FtpDeck", lambda host, **kw: ftp)
    monkeypatch.setattr(ingest_mod, "BmdClient", make_bmd)
    monkeypatch.setattr(ingest_mod, "download_and_verify", _download_returning({}))

    cfg = _IngestCfg(tmp_path)
    deck = DeckConfig(name="Deck2", host="172.16.9.82", slots=(1, 2))
    mdata = manifest_mod.load(cfg, "2026-07-13")

    ingest_mod._ingest_deck(
        cfg, deck, tmp_path, mdata, threading.Lock(), "2026-07-13",
        datetime(2026, 7, 13), dry_run=False, do_clear=False,
    )
    assert made["n"] == 0


def test_bmd_opened_and_closed_when_clearing(tmp_path: Path, monkeypatch):
    bmd = _FakeBmd("172.16.9.82")
    ftp = _FakeFtp()
    ftp.clips_by_slot = {1: [Clip(1, 1, "a.mov", 10)], 2: []}
    monkeypatch.setattr(ingest_mod, "FtpDeck", lambda host, **kw: ftp)
    monkeypatch.setattr(ingest_mod, "BmdClient", lambda host, **kw: bmd)
    monkeypatch.setattr(ingest_mod, "download_and_verify", _download_returning({}))

    cfg = _IngestCfg(tmp_path)
    deck = DeckConfig(name="Deck2", host="172.16.9.82", slots=(1, 2))
    mdata = manifest_mod.load(cfg, "2026-07-13")

    ingest_mod._ingest_deck(
        cfg, deck, tmp_path, mdata, threading.Lock(), "2026-07-13",
        datetime(2026, 7, 13), dry_run=False, do_clear=True,
    )
    assert bmd.connected is True
    assert bmd.closed is True
    assert bmd.formatted == [1]
