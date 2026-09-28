# TROUBLESHOOTING.md — HyperDecks

First-responder guide for "a deck isn't showing up." For repo mechanics see
`AGENTS.md`; for current site state see `HANDOFF.md` (gitignored).

Everything in the triage sections below is **read-only**. Nothing here formats a
card, deletes a clip, or changes a deck setting.

## Ground rules

- **Check `transport info` before touching a deck.** If `status: record`, stop —
  a service is being recorded. Come back later.
- **Be gentle with FTP: one session at a time, always `quit()`.** Deck3 went
  unreachable for part of 2026-09-08, likely from probing that left a dangling
  PASV socket. It recovered on its own. Prefer the BMD port (9993) for
  diagnostics; it is much better behaved.
- **Never run `format`** by hand. Card clearing is the archiver's job, gated
  behind `ingest.clear_cards` (default off) and a guard that refuses unless the
  deck's own clip list matches what was just verified.
- The archiver runs on the **iMac**, where the decks are L2-local (no gateway).
  Reachability from any other host proves the deck is up, but only the iMac's
  view decides whether the nightly ingest will work.

## Fast triage

Run these in order. Substitute the deck's IP.

```bash
ping -c 3 172.16.9.81                              # 1. is it on the network?
nc -z -w2 172.16.9.81 21 && echo "FTP open"        # 2. can we list clips?
nc -z -w2 172.16.9.81 9993 && echo "control open"  # 3. can we read status?
printf 'device info\n' | nc -w3 172.16.9.81 9993   # 4. what is it?
```

Then, from the iMac, the authoritative check:

```bash
cd ~/Dev/hyperdeck-archiver && .venv/bin/python run.py --config config.yaml probe
```

`probe` is read-only (FTP list + BMD ping, no NAS access). Run it **once** — do
not loop it.

## Symptom → cause

### Ping fails, "No route to host" or "Destination Host Unreachable"

The router or host could not ARP the deck. On the iMac, where decks are L2-local,
this means the deck is **not in the broadcast domain**. Three causes, in the order
worth checking:

1. **No power.** LCD dark. Check the PSU, or the PoE injector/switch port if the
   deck is powered that way.
2. **No link.** LCD lit but no green LED on the NIC. Swap the cable, then swap the
   switch port against a port a known-good deck uses — that separates "deck NIC
   dead" from "drop dead."
3. **Wrong IP.** LCD lit, link LED green, still no answer at its expected address.
   Sweep the subnet (below). If it is nowhere on the subnet, read its address over
   USB-C with HyperDeck Setup, or plug a laptop straight into the deck and watch
   for its ARP/DHCP chatter in Wireshark.

A power cycle resolves more of these than it has any right to — that is what
brought Deck1 back on 2026-09-28 after three weeks dark.

### Deck answers ping but the archiver can't reach it

Check tcp/21 specifically. FTP is the archiver's data path; tcp/9993 being open
proves only that the control plane is alive. A deck that answers 9993 but refuses
21 usually clears after a power cycle.

### Deck is up, but clips don't get archived

Check `slot_path` in `config.yaml` against what the deck actually exposes over
FTP. See the model table below — getting this wrong yields an empty clip list
with no error.

### "I can't see it in Blackmagic HyperDeck Setup"

Expected on the **Studio Mini** — it has no network discovery and only appears in
Setup over USB-C. This is not a fault and not a reason to reach for a firmware
update. The Studio HD Plus decks do appear over the network.

## Sweeping the subnet

Finds every HyperDeck on `172.16.8.0/23` regardless of expected address, and
prints each one's model and protocol version:

```bash
for o in 8 9; do for h in $(seq 1 254); do
  (printf '' | nc -w1 172.16.$o.$h 9993 2>/dev/null | head -3 \
    | grep -q model && echo "172.16.$o.$h") &
done; done; wait
```

A deck present here but not at its configured IP has drifted; one absent
everywhere is powered off, unlinked, or on another subnet entirely.

## Read-only commands worth knowing (tcp/9993)

| command | tells you |
|---------|-----------|
| `device info` | model, protocol version, **unique id (= the MAC)**, slot count |
| `slot info: slot id: N` | `mounted`/`empty`, volume name, video format, `blocked` |
| `disk list: slot id: N` | the clips the deck itself sees, with codec and duration |
| `configuration` | record codec, input, record prefix |
| `transport info` | **`status: record` means hands off** |
| `help` | the full command set for that firmware |

The `unique id` is the deck's MAC — that is how you get a MAC for a DHCP
reservation without touching the switch. Known: `Sanc Deck1`
`7c:2e:0d:08:ae:94`, `Sanc Deck2` `7c:2e:0d:0d:3d:d2`. Blackmagic's OUI is
`7c:2e:0d` (macOS drops leading zeros and prints it `7c:2e:d:…`).

## Model differences that matter

| | Studio Mini (`.81`, `.82`) | Studio HD Plus (`.144`, `.183`, `.218`) |
|---|---|---|
| protocol | 1.11 | 1.19 |
| FTP slot dirs | `/1`, `/2` → `slot_path: "{}"` | `/sd1`, `/sd2` → `slot_path: "sd{}"` |
| web UI (tcp/80) | none | yes |
| HyperDeck Setup | **USB-C only** | over the network |
| `format` token reply | `token: <value>` | bare token under `216 format ready:` |

BMD slot ids stay numeric on both; only the FTP directory names differ.

## Firmware

**Neither deck can report its firmware over the network.** The HyperDeck Ethernet
Protocol has no software-version command — `device info` returns the *protocol*
version (1.11 / 1.19), which is not the firmware version. The Studio Minis have no
web UI either. The only ways to read it are HyperDeck Setup over USB-C, or the
deck's own menu.

To update a Studio Mini: install Blackmagic HyperDeck Setup, connect USB-C, launch
it. It prompts if an update exists; no prompt means it is current. Older units can
need a **short, good-quality USB-C cable** to enumerate at all.

**Before updating, know that it may move the deck across the table above.** If a
Studio Mini's FTP layout changes from `/1`,`/2` to `/sd1`,`/sd2`, `ingest` will
silently find zero clips until `slot_path` is changed to `"sd{}"`. After any
firmware change:

1. Re-run `probe` and confirm the clip counts are non-zero.
2. Re-check the protocol version with `device info`.
3. Do **not** update firmware and enable `ingest.clear_cards` in the same week —
   the format-token reply shape differs between 1.11 and 1.19, and that code path
   is what card clearing depends on.

Update only for a specific fault you can name. Both decks have run for months on
1.11, and the one real outage so far was fixed by a power cycle.

## Known quirks in this fleet

- Clip names can contain **leading spaces** (Piro recorded a clip literally named
  `" .mov"`), and macOS leaves **AppleDouble `._` files** on cards. Both are
  handled in code — don't "fix" them on the card.
- Hasley's card is named `LUMIX` (formatted in a Panasonic camera). Harmless; it
  gets renamed on first clear.
- `Smith  Deck3` has a **double space** in its configured name, and that name
  becomes the NAS folder name. Don't tidy it without moving the folders.
- A same-day ingest re-run clears nothing — already-archived clips come back
  `skipped`, and only clips `verified` *in that run* count toward clearing.
