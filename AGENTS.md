# AGENTS.md

Notes for OpenCode sessions working in this repo. `README.md` is empty; the real
docs are `config.example.yaml` (heavily commented) and `SETUP-RASPBERRY-PI.md`
(Linux/Pi reference — production is now a macOS iMac; see Deploy below).

## Run / dev entrypoint

The package uses a `src/` layout and is **not installed** in dev — there is no
`pip install -e .` step. Always launch via the shim, which puts `src/` on
`sys.path`:

```bash
python run.py --config config.yaml <command>        # NOT the console script
```

Before any command works, copy `config.example.yaml` → `config.yaml` and
`.env.example` → `.env`. Both are gitignored (site-specific). The config loader
(`config.py:load_config`) reads `.env` from the **config file's parent dir**, so
keep `.env` next to `config.yaml`.

CLI subcommands (`cli.py`): `ingest`, `prune`, `probe`, `space` (NAS free-space
preflight vs `min_free_gb`). Global flags:
`--config` (default `config.yaml`), `--dry-run`. `ingest` also takes `--no-clear`,
`--deck <name>` (repeatable), `--limit N` (smoke-test cap), `--date YYYY-MM-DD`.

## Checks

```bash
ruff check .          # line-length 100, py310, rules E/F/I/N/W (pyproject.toml)
pytest                # testpaths=["tests"]; no integration tests, all offline
pytest tests/test_core.py::test_parse_file_line   # single test
```

No typecheck step is configured. `step0_probe.py` / `step0b_ftp_probe.py` at the
repo root are one-off discovery scripts (used to capture real HyperDeck
responses for the test fixtures), not part of the shipped package — they carry a
known lint error, so don't be surprised when `ruff check .` flags them.

Tests reach into private `_*` helpers in
`ingest.py` (`_ingest_deck`, `_ingest_slot`, `_clip_dest`, `_max_seq_in`) to pin
the FTP reconnect-after-failure contract — do not rename or rewrite those
without updating `tests/test_core.py`.

## Architecture

Archives Blackmagic HyperDeck SD cards to a Synology NAS over the network, then
prunes by age. Pure-Python (streams in 256 KB chunks; footage never touches the
host disk). One orchestrator entry per command: `ingest.run`, `prune.run`.

Each HyperDeck speaks **two protocols** — don't conflate them:
- FTP (`ftp_client.py`) — lists and downloads clips per slot.
- BMD 9993 TCP control (`bmd_client.py`) — slot info + `format prepare/confirm`,
  used only to clear a card after its clips verify.

FTP slot directory names are **model-dependent**: the Studio HD Mini exposes
`/1`/`/2`, newer decks expose `/sd1`/`/sd2`. Set per-deck `slot_path` (a `{}`
template filled with the slot id) in config; default `"{}"` gives the legacy
`/1`/`/2`. BMD slot ids stay numeric regardless of model.

State/resumability lives in `manifest.py`: a JSON file per date under
`<footage_dir>/.hyperdeck-archiver/`. Re-runs skip verified clips and retry
failed ones automatically; `slot_cleared` is tracked per slot.

## Safety / destructive ops

- `ingest.clear_cards` (config) defaults **false**; `--no-clear` forces it off.
  When true, a slot is BMD-formatted only after every clip on it is downloaded +
  hash-verified. Never flip on without a clean full run first.
- `prune` deletes whole NAS date-folders older than `retention.days`. Test with
  `--dry-run` first.

## Dependencies / quirks

- `python-dotenv` is imported with a **silent fallback** if missing — if `.env`
  values (SMTP creds, NAS creds) aren't loading, check it's installed.
- `xxhash` is optional (`pip install xxhash` or the `[fast]` extra); set
  `hash.algo: xxhash` in config. Default is stdlib `blake2b`.
- Logs write to `logs/hyperdeck-archiver.log` (`logs/` is gitignored).
- **Don't use `shutil.disk_usage` for NAS free space** — use `nas.disk_usage()`,
  which shells out to `df -Pk`. macOS `statvfs()` truncates block counts to
  32 bits, so `shutil` wraps every `2**32 * f_frsize` bytes and reported the
  34.9 TiB share as 3.1 TB, false-tripping the `min_free_gb` gate.

## Deploy (reference, don't reproduce)

Production is a macOS iMac (`Sanctuary-VPR-Utility-iMac-2018`, repo at
`~/Dev/hyperdeck-archiver`, venv at `.venv/`, SMB share mounted at
`/Volumes/Video Archive`, scheduler is **launchd**). A full macOS setup doc is
TBD; the known production state is recorded in `HANDOFF.md`.

`SETUP-RASPBERRY-PI.md` is the **Linux/Pi reference** for that alternative
target (apt / `cifs-utils` / `/etc/fstab` / systemd) — not current production,
but kept current in case of a future host move. `schedulers/` ships templated
units (`@@INSTALL_DIR@@`, `@@VENV_PYTHON@@`, `@@USER@@`) for both systemd and
launchd. Code is identical across hosts; only `nas.mount_root` and the
scheduler choice differ.
