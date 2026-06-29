"""Per-run manifest on the NAS for resumable, idempotent ingests.

Stored as <footage_dir>/.hyperdeck-archiver/<YYYY-MM-DD>.json. Records each clip's
size/hash/status so a rerun skips already-verified clips and never re-clears a slot.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def manifest_dir_for(config) -> Path:
    return config.manifest_dir


def manifest_path(config, date_str: str) -> Path:
    return manifest_dir_for(config) / f"{date_str}.json"


def load(config, date_str: str) -> dict:
    path = manifest_path(config, date_str)
    if not path.exists():
        return _skeleton(date_str)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _skeleton(date_str)
    data.setdefault("decks", {})
    return data


def _skeleton(date_str: str) -> dict:
    return {"date": date_str, "updated": None, "decks": {}}


def save(config, date_str: str, data: dict) -> None:
    path = manifest_path(config, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def clip_entry(data: dict, deck: str, slot: int, name: str) -> dict | None:
    slots = data.get("decks", {}).get(deck, {}).get("slots", {})
    for clip in slots.get(str(slot), {}).get("clips", []):
        if clip.get("name") == name:
            return clip
    return None


def record_clip(data: dict, deck: str, slot: int, entry: dict) -> None:
    decks = data.setdefault("decks", {})
    deck_entry = decks.setdefault(deck, {"slots": {}})
    slot_entry = deck_entry["slots"].setdefault(str(slot), {"cleared": False, "clips": []})
    clips = slot_entry["clips"]
    for i, clip in enumerate(clips):
        if clip.get("name") == entry.get("name"):
            clips[i] = entry
            return
    clips.append(entry)


def slot_cleared(data: dict, deck: str, slot: int) -> bool:
    return (
        data.get("decks", {})
        .get(deck, {})
        .get("slots", {})
        .get(str(slot), {})
        .get("cleared", False)
    )


def mark_slot_cleared(data: dict, deck: str, slot: int) -> None:
    decks = data.setdefault("decks", {})
    deck_entry = decks.setdefault(deck, {"slots": {}})
    slot_entry = deck_entry["slots"].setdefault(str(slot), {"cleared": False, "clips": []})
    slot_entry["cleared"] = True
