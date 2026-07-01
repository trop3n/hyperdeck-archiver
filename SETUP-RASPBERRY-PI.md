# Deploy on Raspberry Pi 5 (8 GB)

A Pi 5 is a good always-on host for this: pure-Python, streams in 256 KB chunks
(footage never touches the Pi's SD card), and the HyperDecks - not the Pi - are
the throughput limit (~17.7 MB/s each).

Tested target: Raspberry Pi OS (64-bit, Bookworm) or Ubuntu 24.04. Python 3.11+
ships with the OS and works as-is.

## 0. Base setup

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip cifs-utils
timedatectl set-ntp true        # the Pi has no RTC; keep the clock right for date folders
timedatectl status
```

## 1. Get the project onto the Pi

```bash
git clone <this repo url> ~/hyperdeck-archiver
cd ~/hyperdeck-archiver
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Sanity check it imports and the CLI runs:

```bash
.venv/bin/python run.py --help
```

## 2. Configure

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit `config.yaml`:
- `nas.mount_root: /mnt/footage`  (Pi path, not the Mac `/Volumes/Footage`)
- `decks:` - hosts + `number:` for each; set `enabled: true/false`
- `ingest.clear_cards: false`  (leave OFF until after the first successful archive)
- `retention.days: 30`

Edit `.env` with `SMTP_USER` / `SMTP_PASS` (Outlook app password).

## 3. Mount the Synology share (persistent, boot-safe)

Create a credentials file (so secrets stay out of `/etc/fstab`):

```bash
sudo mkdir -p /etc/samba
sudo tee /etc/samba/.nas-creds >/dev/null <<'EOF'
username=archiver
password=YOUR_NAS_PASSWORD
domain=WORKGROUP
EOF
sudo chmod 600 /etc/samba/.nas-creds
sudo chown root:root /etc/samba/.nas-creds
```

Add the mount (replace `footage` with your actual share name):

```bash
sudo mkdir -p /mnt/footage
echo '//192.168.6.52/footage  /mnt/footage  cifs  credentials=/etc/samba/.nas-creds,uid=$(id -u),gid=$(id -g),iocharset=utf8,vers=3.0,seal,nofail,x-systemd.automount,x-systemd.idle-timeout=60  0  0' | sudo tee -a /etc/fstab
sudo mount -a
ls -l /mnt/footage        # confirm it lists the share
```

`nofail` + `x-systemd.automount` mean the Pi will still boot if the NAS is down,
and will mount the share on first access.

## 4. First run - WATCHED, no card wipe

```bash
.venv/bin/python run.py --config config.yaml probe          # FTP+BMD reachability
.venv/bin/python run.py --config config.yaml ingest --no-clear
```

Inspect files on the NAS, spot-check a clip. Only after a clean full run, flip
`ingest.clear_cards: true` in `config.yaml` so cards are formatted (verified-only)
on the next run.

## 5. Schedule it (systemd)

Substitute the placeholders in the unit files, install, and enable:

```bash
cd ~/hyperdeck-archiver
export INSTALL_DIR="$PWD"
export VENV_PYTHON="$INSTALL_DIR/.venv/bin/python"
export USER="$(whoami)"
for f in schedulers/hyperdeck-archiver-ingest.service schedulers/hyperdeck-archiver-prune.service; do
  sed -e "s|@@INSTALL_DIR@@|$INSTALL_DIR|g" \
      -e "s|@@VENV_PYTHON@@|$VENV_PYTHON|g" \
      -e "s|@@USER@@|$USER|g" "$f" | sudo tee /etc/systemd/system/$(basename "$f") >/dev/null
done
sudo cp schedulers/hyperdeck-archiver-ingest.timer schedulers/hyperdeck-archiver-prune.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hyperdeck-archiver-ingest.timer hyperdeck-archiver-prune.timer
systemctl list-timers | grep hyperdeck
```

Run once by hand to confirm:

```bash
sudo systemctl start hyperdeck-archiver-ingest.service
journalctl -u hyperdeck-archiver-ingest.service -e     # see the run
```

Timers fire: **ingest Mondays 01:00**, **prune daily 03:00** (30-day retention).
`Persistent=true` catches a missed run after a reboot/power loss.

## 6. Notes / gotchas

- **Network ceiling:** the Pi's single 1 GbE NIC carries deck-in + NAS-out at the
  same time. 4 decks (~70 MB/s in + ~70 MB/s out) fits 1 GbE full-duplex but is
  near the ceiling. Weekly job for ~320 GB is ~75 min.
- **Verify re-reads the file from the NAS** (the integrity guarantee) - extra SMB
  traffic but kept on for safety.
- **No RTC by default** - keep NTP on (step 0) or add a cheap RTC HAT, else a
  long-offline Pi will mislabel date folders.
- **Power:** use the official 5 V/5 A PSU; brown-outs corrupt SD cards.
- **Moving host later:** the only Mac-vs-Pi differences are `nas.mount_root` and
  which scheduler you install (launchd vs systemd). Code is identical.
