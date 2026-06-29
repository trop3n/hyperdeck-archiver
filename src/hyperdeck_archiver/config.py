"""Configuration loading and validation (YAML + .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional; env vars can be set directly instead
    def load_dotenv(_path):  # type: ignore[misc]
        return False


@dataclass(frozen=True)
class DeckConfig:
    name: str
    host: str
    enabled: bool = True
    slots: tuple[int, ...] = (1, 2)


@dataclass(frozen=True)
class Config:
    decks: list[DeckConfig]
    mount_root: Path
    footage_root: str
    share: str
    smb_url: str
    min_free_gb: int
    concurrency: int
    clear_cards: bool
    date_folder_format: str
    skip_metadata: tuple[str, ...]
    retention_enabled: bool
    retention_days: int
    smtp_host: str
    smtp_port: int
    smtp_starttls: bool
    smtp_from: str
    smtp_to: list[str]
    smtp_user: str
    smtp_pass: str
    notify_on_success: bool
    notify_on_failure: bool
    hash_algo: str
    log_file: Path
    log_level: str
    raw: dict

    @property
    def footage_dir(self) -> Path:
        return self.mount_root / self.footage_root

    @property
    def manifest_dir(self) -> Path:
        return self.footage_dir / ".hyperdeck-archiver"

    def enabled_decks(self) -> list[DeckConfig]:
        return [d for d in self.decks if d.enabled]

    def dest_for(self, deck: DeckConfig, when) -> Path:
        folder = when.strftime(self.date_folder_format)
        return self.footage_dir / folder / deck.name


def _require(key: str, data: dict, context: str) -> object:
    if key not in data:
        raise ValueError(f"config: missing required key '{context}.{key}'")
    return data[key]


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    env_path = path.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config: top level must be a mapping")
    return _build(raw)


def _build(raw: dict) -> Config:
    decks_data = _require("decks", raw, "")
    decks: list[DeckConfig] = []
    for i, d in enumerate(decks_data):
        name = _require("name", d, f"decks[{i}]")
        host = _require("host", d, f"decks[{i}]")
        slots = tuple(int(s) for s in d.get("slots", [1, 2]))
        decks.append(
            DeckConfig(
                name=str(name), host=str(host), enabled=bool(d.get("enabled", True)), slots=slots
            )
        )

    nas = _require("nas", raw, "")
    ingest = raw.get("ingest", {}) or {}
    retention = raw.get("retention", {}) or {}
    smtp = raw.get("smtp", {}) or {}
    notify = raw.get("notify", {}) or {}
    hashing = raw.get("hash", {}) or {}
    log = raw.get("log", {}) or {}

    smtp_from = os.environ.get("SMTP_FROM") or str(smtp.get("from", ""))
    smtp_user = os.environ.get("SMTP_USER", smtp.get("user", smtp_from))
    smtp_pass = os.environ.get("SMTP_PASS", smtp.get("pass", ""))

    return Config(
        decks=decks,
        mount_root=Path(str(_require("mount_root", nas, "nas"))),
        footage_root=str(nas.get("footage_root", "footage")),
        share=str(nas.get("share", "")),
        smb_url=str(nas.get("smb_url", "")),
        min_free_gb=int(nas.get("min_free_gb", 100)),
        concurrency=int(ingest.get("concurrency", 4)),
        clear_cards=bool(ingest.get("clear_cards", False)),
        date_folder_format=str(ingest.get("date_folder_format", "%Y-%m-%d")),
        skip_metadata=tuple(
            ingest.get("skip_metadata", [".fseventsd", ".Spotlight-V100", ".Trashes", "._"])
        ),
        retention_enabled=bool(retention.get("enabled", True)),
        retention_days=int(retention.get("days", 30)),
        smtp_host=str(smtp.get("host", "smtp-mail.outlook.com")),
        smtp_port=int(smtp.get("port", 587)),
        smtp_starttls=bool(smtp.get("starttls", True)),
        smtp_from=smtp_from,
        smtp_to=list(smtp.get("to", [])),
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        notify_on_success=bool(notify.get("on_success", True)),
        notify_on_failure=bool(notify.get("on_failure", True)),
        hash_algo=str(hashing.get("algo", "blake2b")),
        log_file=Path(str(log.get("file", "logs/hyperdeck-archiver.log"))),
        log_level=str(log.get("level", "INFO")),
        raw=raw,
    )
