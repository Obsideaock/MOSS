# MOSS

**Wikipedia over a MeshCore radio, with no internet on either end.**

A **Seed Vault** (a Raspberry Pi with a LoRa radio and an offline copy of
Wikipedia on a USB stick) answers requests. A **reader** (a laptop with a
radio) searches, picks an article, and watches it arrive *spore by spore* in
a browser, readable from the top while the rest is still coming in. Articles
keep their links, formulas, tables and key facts; everything read is saved
for offline reading and can be exported to Obsidian. What you search for and
read is encrypted: other people on the mesh can't tell what you're reading.

```
 laptop (reader)                                       Raspberry Pi (Seed Vault)
 ┌──────────────────────────┐                         ┌───────────────────────────────┐
 │ browser: the reader      │   LoRa radio, #moss     │ python -m moss gateway        │
 │ python -m moss web       │◄───────────────────────►│ wikipedia_en_all_nopic.zim    │
 │ ~/.moss  saved articles, │  163-byte encrypted     │ (57 GB, on a USB stick)       │
 │          trusted vault   │  messages, km of range  │ ~/.moss/seed-vault  its keys  │
 └────────────┬─────────────┘                         └───────────────┬───────────────┘
           USB│MeshCore radio                                      USB│MeshCore radio
```

---

## Contents

1. [Glossary](#glossary)
2. [Quick start](#quick-start)
3. [Repository layout](#repository-layout)
4. [The Raspberry Pi (Seed Vault)](#the-raspberry-pi-seed-vault)
5. [The laptop (reader)](#the-laptop-reader)
6. [Using the reader](#using-the-reader)
7. [The #moss channel](#the-moss-channel)
8. [Private reading (encryption)](#private-reading-encryption)
9. [How it works](#how-it-works)
10. [Speed and airtime](#speed-and-airtime)
11. [Being a good mesh neighbour](#being-a-good-mesh-neighbour)
12. [If the radio drops out](#if-the-radio-drops-out)
13. [Troubleshooting](#troubleshooting)
14. [Command reference](#command-reference)
15. [Where things are stored](#where-things-are-stored)
16. [Testing](#testing)
17. [FAQ](#faq)
18. [Not built yet](#not-built-yet)
19. [Credits, licences and further reading](#credits-licences-and-further-reading)

---

## Glossary

MOSS has its own names for a few networking terms. The code keeps the
standard terms (`MANIFEST`, `CHUNK`, `NACK`, `STATUS` in `protocol.py`) so it
reads like any other networking code.

| MOSS | Standard term | What it is |
|---|---|---|
| **Seed Vault** | gateway / server | The Pi holding the Wikipedia library. Its command is `moss gateway` and its service `moss-gateway`. |
| **reader** | client | The laptop side: the browser reader or the `moss` command line. |
| **spore** | packet / chunk | One radio message carrying up to 158 bytes of a page. |
| **capsule** | manifest | The first message of a transfer: how many spores, sizes, a hash. |
| **bare patch** | NACK / missing list | The spores the reader is still missing, sent back so only those are re-sent. |
| **pulse** | status message | The Seed Vault saying "got it, working on it" (queued, looking it up, sending). |
| **dormant** | queued | Waiting behind other requests at the Seed Vault. |
| **blight** | failed integrity check | The page arrived damaged or tampered with and was thrown away. |
| **fingerprint** | key fingerprint | Sixteen characters (`3F9A-12C4-77E0-5B21`) identifying a Seed Vault, to check by eye. |
| **#moss** | channel | The MeshCore hashtag channel all MOSS traffic uses. |

---

## Quick start

**Pi (Seed Vault)**, once set up (see below):

```bash
cd ~/MOSS
.venv/bin/python -m moss gateway --port /dev/serial/by-id/usb-... \
    --source /media/moss/KIWIX/wikipedia_en_all_nopic_2026-06.zim
# or, as a service: sudo systemctl restart moss-gateway
```

**Laptop (reader)**, in PowerShell:

```powershell
cd C:\Users\<you>\Documents\MOSS
.venv\Scripts\activate
python -m moss web --port COM12          # opens http://127.0.0.1:8765
```

Start the Pi first. No channel setting is needed: both sides find (or
create) the `#moss` channel on their radio. If only one radio is plugged in,
`--port` can be left out.

**No radios at hand?** `python -m moss web --sim TestExamples/mini_wikipedia.zim`
runs the whole thing on one laptop over a simulated, lossy radio.

---

## Repository layout

```
MOSS/
├── moss/                      the app, the same code on both machines
│   ├── protocol.py            message formats: capsule, spore, bare patch, done, request, pulse, error
│   ├── seal.py                encryption: Seed Vault keys, sealed requests and replies, trust
│   ├── channel.py             finds or creates the #moss channel on the radio
│   ├── transport.py           the real MeshCore radio (with reconnect), or a simulated lossy one
│   ├── gateway.py             the Seed Vault: answers requests one at a time, with repair rounds
│   ├── client.py              the reader's side: asks, receives spores, repairs, checks for blight
│   ├── sources.py             where answers come from: live Wikipedia, a folder of .md files
│   ├── zim.py                 ...or a Kiwix .zim file, fully offline (the Pi uses this)
│   ├── tables.py              rebuilds tables sent "disassembled"
│   ├── cache.py               saved articles on the laptop (~/.moss/cache)
│   ├── vault.py               saved articles -> Obsidian notes
│   ├── web.py + index.html    the browser reader (localhost only)
│   ├── static/katex/          formula drawing, bundled so it works offline
│   └── cli.py                 python -m moss ...
├── tests/                     python -m unittest discover -s tests
│   ├── make_test_zim.py       builds TestExamples/mini_wikipedia.zim
│   └── fake_radio.py          a fake MeshCore radio for tests
├── tools/                     the earlier hardware scripts
│   ├── page_transfer.py       send one file between two radios (the original demo)
│   ├── ping_test.py, datagram_test.py, diagnose.py, reset_radio.py, fault_test.py, ...
│   └── fetch_page.py          used by the live-Wikipedia source
├── TestExamples/              fixtures, plus mini_wikipedia.zim (about 80 KB)
├── deploy/                    Pi service: install-gateway.sh, moss-gateway.service, gateway.env
├── requirements.txt           reader (laptop)
└── requirements-gateway.txt   Seed Vault
```

---

## The Raspberry Pi (Seed Vault)

Needs 64-bit Raspberry Pi OS. The 57 GB `.zim` stays on a USB stick (label
`KIWIX`); nothing is copied off it. Plug it into a blue USB 3 port: searches
jump around the file, and a stick on USB 2 makes the first search after boot
noticeably slow (later ones are faster once parts of the index are cached).

Where things live on the Pi:

```
~/MOSS/                                    this repo, with .venv/ inside
~/.moss/seed-vault/                        the Seed Vault's keys (back these up)
/media/moss/KIWIX/wikipedia_en_all_nopic_2026-06.zim
                                           the library, on the USB stick
/etc/moss/gateway.env                      port, .zim path, extra options
/etc/systemd/system/moss-gateway.service   starts the Seed Vault on boot
```

Setup:

```bash
git clone https://github.com/Obsideaock/MOSS.git ~/MOSS
cd ~/MOSS
sudo bash deploy/install-gateway.sh        # venv, libzim, serial permission, service
# log out and back in once, so your user can open the radio's serial port

# plug in the KIWIX stick, then check the library, no radio needed:
.venv/bin/python -m moss library /media/moss/KIWIX/wikipedia_en_all_nopic_2026-06.zim "boulder colorado"

ls -l /dev/serial/by-id/              # find the radio's stable name
sudo nano /etc/moss/gateway.env       # set MOSS_PORT

# run it by hand first, to watch it:
.venv/bin/python -m moss gateway --port /dev/serial/by-id/usb-... \
    --source /media/moss/KIWIX/wikipedia_en_all_nopic_2026-06.zim

# then as a service:
sudo systemctl start moss-gateway
journalctl -u moss-gateway -f         # watch requests come in
```

On first start the Seed Vault:

- creates `#moss` on its radio if it isn't there (in a free slot; never over
  Public or a slot in use),
- creates its keys in `~/.moss/seed-vault/` and prints its **fingerprint**
  (`python -m moss vault-key` shows it again later),
- announces itself once on `#moss`.

**Back up `~/.moss/seed-vault/`.** New keys mean every reader sees a
different fingerprint and has to re-trust the vault.

`sudo bash deploy/install-gateway.sh` is safe to re-run; it never overwrites
an edited `/etc/moss/gateway.env`. An older settings file with a
`MOSS_CHANNEL=` line, or an older service that still passes `--channel N`,
keeps working: the setting is simply ignored now.

### USB stick

`/media/moss/KIWIX` is where the desktop mounts the stick when user `moss`
is logged in. The service waits up to two minutes for the file, so a Pi that
boots to the desktop (auto-login, the default) just works. If the Pi runs
without a desktop, or nobody logs in, make it mount at boot instead:

```bash
lsblk -f                              # find the KIWIX stick and its FSTYPE
sudo mkdir -p /media/moss/KIWIX
sudo nano /etc/fstab                  # add ONE line, for the FSTYPE you saw:

# exfat or vfat:
LABEL=KIWIX  /media/moss/KIWIX  exfat  defaults,nofail,uid=moss,gid=moss,x-systemd.device-timeout=15s  0  0
# ext4:
LABEL=KIWIX  /media/moss/KIWIX  ext4   defaults,nofail,x-systemd.device-timeout=15s  0  0

sudo systemctl daemon-reload && sudo mount -a && ls /media/moss/KIWIX
```

`nofail` means the Pi still boots if the stick is unplugged.

### Radio port

`ls /dev/serial/by-id/` shows each radio's stable name (for example
`usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0`);
use the full path rather than `/dev/ttyUSB0`, which can change between
reboots. The `ttyUSB0` after the arrow in that listing is the same port's
short name, which lives in `/dev/`, not in `/dev/serial/by-id/`.

### Seed Vault options worth knowing

Put these in `MOSS_EXTRA=` in `/etc/moss/gateway.env` (then
`sudo systemctl restart moss-gateway`):

| Option | What it does |
|---|---|
| `--pad-sizes` | Round encrypted pages up to standard sizes so their length reveals less (~10% more airtime). Off by default. |
| `--scope NAME` | Keep MOSS's flooded traffic inside a MeshCore region (ask your local mesh for the name). |
| `--gap 0.5` | Seconds between spores (default 1.0). Lower is faster; see [Making it faster](#making-it-faster). |
| `--max-bytes 12000` | Shorten long articles to about this many bytes (default: full articles). |
| `--no-links` | Leave out links between articles (saves roughly 10–25% airtime). |
| `--log-queries` | Write the titles of encrypted requests to the log. Off: the Pi keeps no record of what people read. |
| `--name "Boulder Seed Vault"` | The name readers see when they first meet this vault. |
| `--keys DIR` | Where the keys live (default `~/.moss/seed-vault`). |

---

## The laptop (reader)

Where things live:

```
C:\Users\<you>\Documents\MOSS\             this repo, with .venv\ inside
C:\Users\<you>\.moss\cache\                saved articles and searches
C:\Users\<you>\.moss\vault.json            which Seed Vault this laptop trusts
<your Obsidian vault>\Wikipedia\           only if you run `moss export`
```

Setup (PowerShell, Python 3.11+):

```powershell
git clone https://github.com/Obsideaock/MOSS.git
cd MOSS
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python -m moss web --port COM12      # opens http://127.0.0.1:8765
```

Find the COM port in Device Manager under *Ports (COM & LPT)*, or leave
`--port` out when only one radio is plugged in.

The command line works too:

```powershell
python -m moss search "shrubbies band" --port COM12     # search, pick, read
python -m moss read "Boulder, Colorado" --port COM12    # one article by title
python -m moss cache                                    # what's saved (no radio)
python -m moss cache show "Boulder, Colorado"           # read a saved article offline
python -m moss export C:\path\to\ObsidianVault          # saved articles -> notes
python -m moss trust                                    # which Seed Vault is trusted
```

---

## Using the reader

`python -m moss web` opens a reader in your browser at
`http://127.0.0.1:8765`. It only listens on your own computer, works with no
internet (fonts, formulas and everything else are local), and looks and
behaves much like Obsidian.

- **Search** with the box at the top left (or Ctrl K). Results come back over
  the radio as a short list with a one-line description each.
- **Open** a result to request it. You'll see what's happening ("Asking the
  Seed Vault…", "Dormant behind 1 other request", "37 of 120 spores
  received", "4 spores didn't land. Asking again for the bare patches"), a
  grid of spores filling in, a time estimate, and the text itself appearing
  as it arrives.
- **Saved articles** are listed on the left and open instantly, no radio
  needed. The refresh button (circular arrow) fetches a fresh copy.
- **Links** in an article:
  - to an article you've saved: opens it straight away;
  - to anything else (dotted underline): click for **Get it now** or **Read
    later**. *Read later* collects titles in the left sidebar; click one to
    fetch it, and it drops off the list once it's saved.
- **Key facts**: the article's infobox, shown in a box beside the text.
- **Tables** come through in full, merged cells included.
- **Formulas** are drawn properly (KaTeX, bundled).
- **Outline** on the right lists the headings; **Links to saved notes**
  lists saved articles this one mentions.
- The **status bar** shows the radio (green), whether reading is encrypted
  (hover the lock for the Seed Vault's fingerprint), and turns red with
  *Radio disconnected, reconnecting…* if the radio drops out.

### What's kept from Wikipedia, and what isn't

| Kept | Left out (to save airtime) |
|---|---|
| All the article text, headings, lists (numbered lists keep their numbers) | Images (the nopic library has none) |
| Links between articles (as `[[wikilinks]]`) | Citations, footnote markers [1], References |
| Formulas (as LaTeX) | External links, Further reading, Notes sections |
| The infobox, as *Key facts* | Navigation boxes and sidebars |
| Data tables, in full | Kiwix's licence footer (the source line says the same) |
| *Italic*, **bold**, sub/superscripts (CO₂, 10⁻⁶, km²) | Invisible characters (zero-width spaces, soft hyphens) |
| "Main article: …" / "See also: …" notes, the *See also* list | |

Every article starts with a source line linking to the original on
wikipedia.org and stating its licence (CC BY-SA 4.0).

### Obsidian

`python -m moss export <vault>` writes every saved article into
`<vault>\Wikipedia\` as a note: properties (title, source, licence, fetched
date, tags), a source callout, `[[links]]` that work in Obsidian's graph,
formulas Obsidian draws natively, tables as markdown tables, and *Key facts*
as a collapsible callout. It's safe to re-run; it only rewrites MOSS's own
notes. `--link-searches` also links titles that only appeared in search
results (they show as unresolved notes in the graph).

---

## The #moss channel

All MOSS traffic uses one MeshCore **hashtag channel**, `#moss`.

- **No setup.** On every connect, MOSS looks through the radio's channel
  slots for `#moss` and creates it in a free slot if it's missing. It never
  touches slot 0 (Public) or any slot already in use. If someone already has
  a private channel *named* `#moss` with a different key, MOSS leaves it alone
  and uses a separate slot.
- **Nothing to hand out.** A hashtag channel's key is derived from its name
  (the first 16 bytes of SHA-256 of `"#moss"`), exactly as the MeshCore app
  does when someone types `#moss` into "join channel". Anyone can join, which
  is the point: no niche channel to hunt down.
- **Only #moss.** MOSS sends only on its channel and ignores data arriving on
  any other.
- **Not Public.** MOSS stays off the Public channel people chat on.
- **The channel key isn't what keeps your reading private.** Because anyone
  can derive it, MOSS encrypts everything again on top (see the next section).
- **Different name for testing:** `--channel-name #moss-test` keeps test
  traffic separate. Names follow the MeshCore app's rules (a `#`, then
  lowercase letters, digits and dashes, 30 bytes at most), so they match what
  people create in the app.

### Region scope

Every message on a channel is repeated by every repeater in range, up to 64
hops by default. A long article is a few minutes of airtime per repeater.
MeshCore firmware 1.12+ supports **scopes**: named regions that repeaters
keep flooded traffic inside. `--scope NAME` (on both sides) asks the radio to
mark MOSS's traffic with that region. Ask your local mesh which region name
their repeaters use. Older firmware ignores the setting (MOSS logs that it's
sending unscoped).

---

## Private reading (encryption)

Anyone in radio range can hear MOSS traffic, and the `#moss` key is public.
So MOSS encrypts end to end on top of the channel: **what you search for and
read is readable only by you and the Seed Vault.**

### What's protected

- **Requests**: your search or the title you asked for. Each request is
  encrypted with a fresh one-time key and is always exactly 163 bytes, so its
  length says nothing about what was asked.
- **Replies**: the capsule, every spore and every error ("no such article:
  …"), encrypted with a key made for that one transfer and then discarded.
  Even someone who later gets hold of the Pi can't decrypt recorded replies.
- **Authenticity**: only the real Seed Vault can produce a reply your reader
  accepts. A fake vault can't answer, and a forged or altered spore makes the
  page **blight**; it's refused and never saved.
- **The Pi's log**: the Seed Vault doesn't log what people search for or
  read ("article (encrypted; not logged)"). `--log-queries` turns that on for
  debugging.

### How it works

It's the standard HPKE pattern (RFC 9180) built from standard pieces:
X25519 key exchange, HKDF-SHA256 and ChaCha20-Poly1305, from the
`pycryptodome` library MeshCore already uses. Nothing home-made.

1. The Seed Vault has a long-term key pair. Readers learn its public key
   once and remember it.
2. For each request the reader makes a one-time key, combines it with the
   vault's public key, and encrypts the request with the result.
3. The vault makes its own one-time key for the reply. The reply key mixes
   both one-time keys with the vault's long-term key, then the one-time keys
   are thrown away.

On the radio this costs almost nothing: 48 bytes of a request go to the key
and the tamper check (leaving 110 bytes for the search text or title, which
is plenty), and a whole page gets one 16-byte tamper check at the end. Text
still appears as it arrives; it's shown as received and only saved once the
whole page passes its check.

### Trust (fingerprints)

The first Seed Vault a reader hears is trusted and remembered in
`~/.moss/vault.json`, like SSH does. Check its fingerprint (status bar, hover
the lock) against the one the Pi prints. To be strict from the start, pin it
before first contact:

```
python -m moss trust 3F9A-12C4-77E0-5B21     # only trust this vault
python -m moss trust                         # show who is trusted
python -m moss trust --forget                # after the Pi's keys were reset on purpose
```

If a reader's vault stops answering and a vault with a different key answers
instead (say the Pi was reflashed), the reader says so and suggests
`moss trust --forget` rather than just "no answer". A second Seed Vault on the
same channel can't read requests meant for yours, so it can't answer them
either.

### What a listener can still see

| Hidden | Still visible |
|---|---|
| What you searched for | That someone is using MOSS |
| Which article came back | Roughly when |
| What the page says | Roughly how big the page was (its spore count) |
| Error messages | Roughly where the radio is, from its signal |

**Page size** is the one real leak: articles differ in length, so a spore
count narrows down what was read. On its own it's a weak clue (thousands of
articles share any given count), but it adds up across many reads. Turn on
`--pad-sizes` on the Seed Vault to round pages up to standard spore counts
(1 2 3 4 5 6 8 10 13 16 20 25 32 40 50 63 79 100 …) for about 10% more
airtime. **Off by default.** (Resent spores don't hide the count: each
carries its number, and a resent spore is identical to the original.)

**Public things stay public.** Announcements or status pages meant for
everyone don't need encrypting, since receiving a broadcast says nothing
about you; they'd be signed by the Seed Vault so nobody can fake one (not
built yet).

`--plain` on the reader turns encryption off, for debugging only.

---

## How it works

### The radio side

MeshCore radios carry two kinds of channel traffic:

- **text messages**: what the MeshCore app shows as chat;
- **channel data**: raw bytes ("binary") tagged with a *data type* number.
  MOSS uses `0xFF01`, from the range set aside for experiments. Chat apps
  ignore data types they don't recognize, which is why MOSS traffic never
  shows up as garbage in anyone's chat.

That tag lives in MeshCore's own packet wrapping. MeshCore allows up to 165
bytes of channel data per message; MOSS uses 163 per message for itself.

### A transfer, step by step

1. **First contact** (once per reader): the reader sends **WHOIS**; the Seed
   Vault answers **HELLO** with its public keys; the reader remembers them.
2. **Request**: the reader sends a sealed request (search or article title).
   If nothing comes back, it asks again (and also asks WHOIS, in case the
   vault's key changed).
3. **Pulses**: the vault answers *dormant* (queued, with your place in line),
   then *looking it up*, then *sending* (with an estimated time).
4. **Capsule**: how many spores, their total size, and a hash of the page.
5. **Spores**: the page, compressed and encrypted, cut into numbered
   158-byte slices, one per message, paced by `--gap`.
6. **Bare patches**: when the radio goes quiet, the reader sends the numbers
   of the spores it's missing; the vault re-sends only those. This repeats
   until the page is complete.
7. **Check**: the reader decrypts, verifies and decompresses the whole page
   and checks its hash. Any damage means **blight**: the page is refused.
8. **Done**: the reader tells the vault it has everything (twice, in case
   one is lost), and saves the article.

Built in for a lossy radio:

- If the **capsule** is lost, spores that arrive anyway are kept, and the
  reader asks for the capsule again.
- If a **bare-patch list** is lost several times and the vault gives up
  waiting, the reader's next list makes it pick the transfer back up.
- The reader only gives up after **15 asks in a row that bring nothing
  new**; asks that make progress don't count.
- If an **error** or capsule is lost, the reader asks again and the vault
  repeats its last answer.
- One transfer at a time on the air: other requests wait (*dormant*) at the
  vault, up to six.

Tested over a simulated radio losing 25% and 35% of messages, with 240-spore
(Quantum-mechanics-sized) articles: every transfer completed intact.

### Message formats

Every message fits in one 163-byte MeshCore datagram. The first byte says
what it is; most then carry the 2-byte transfer ID so a reader ignores other
people's transfers.

| Byte 1 | Name | Layout (bytes) | Direction |
|---|---|---|---|
| 1 | MANIFEST (unencrypted capsule) | type 1, id 2, total 2, length 4, flags 1, hash 8, title | vault → reader |
| 2 | CHUNK (spore) | type 1, id 2, number 2, data up to 158 | vault → reader |
| 3 | NACK (bare patch) | type 1, id 2, count 1, numbers 2 each (up to 79) | reader → vault |
| 4 | DONE | type 1, id 2 | reader → vault |
| 5 | REQUEST (unencrypted) | type 1, id 2, kind 1, text | reader → vault |
| 6 | STATUS (pulse) | type 1, id 2, state 1, value 2 | vault → reader |
| 7 | ERROR (unencrypted) | type 1, id 2, message | vault → reader |
| 8 | HELLO | type 1, encryption key 32, signing key 32, signature 64, name | vault → all |
| 9 | WHOIS | type 1, id 2 | reader → all |
| 10 | SREQUEST (sealed request) | type 1, id 2, one-time key 32, sealed 112, check 16 (always 163) | reader → vault |
| 11 | SCAPSULE (sealed capsule) | type 1, id 2, one-time key 32, sealed sizes and hash 19, check 16 | vault → reader |
| 12 | SERROR (sealed error) | type 1, id 2, sealed message, check 16 | vault → reader |

With encryption on (the normal case) only types 2, 3, 4, 6 and 8–12 go out.
Types 1, 5 and 7 are kept for `--plain` and for the older tools.

The 5-byte spore header (type, ID, number) is what leaves 158 of the 163
bytes for the page.

### What a page looks like on the air

Before compression and encryption, an article is plain markdown with a few
conventions the reader understands:

- `[[Target]]` or `[[Target|words shown]]` for links between articles;
- `$...$` and `$$...$$` for formulas (LaTeX); a literal `$` or `*` is written
  `\$` / `\*`;
- `**bold**`, `*italic*`, `1.` / `-` lists, `#`–`######` headings;
- tables "disassembled": one line per row, cells split by tabs, nothing
  repeated. `^` = same cell as the one above, `<` = same cell as the one to
  the left, a leading `!` = header row. The infobox uses the same format
  under `:::facts`:

```
:::table Best-selling games
!Rank	Title	Sales	Platform(s)
1	*[[Minecraft]]*	300,000,000	Multi-platform
2	*Grand Theft Auto V*	210,000,000	^
:::
```

The reader rebuilds real tables from this (merged cells included). It only
saves about 2% compared with ordinary markdown tables, because compression
already removes repetition; the real win is keeping merged cells.

The page is then compressed with zlib (typically to about a third of its
size) before encryption.

### Search

The Pi searches the ZIM's own indexes: the full-text index (by content) and
the title index (by name, so "boulder col" finds "Boulder, Colorado"). An
exact title match comes first, then the two kinds of result alternate. Each
result carries the article's first sentence as a short description. Search
results travel exactly like a page (a small one: usually 2–4 spores).

---

## Speed and airtime

### Why it's so much slower than Wi-Fi

The limit isn't message size; it's how fast bits can move through the air,
and that's a deliberate trade between **speed and range**.

- **Wi-Fi** spreads its signal across a wide slice of spectrum (20–160 MHz)
  and packs many bits into each moment. That needs a strong signal, so it
  reaches tens of metres, at hundreds of megabits per second.
- **LoRa** uses a narrow slice (62.5 kHz on the US MeshCore preset, hundreds
  of times narrower) and stretches each bit out over time. That lets it decode
  signals weaker than the background noise, kilometres away, on very little
  power. The price is speed: about **2,700 bits per second** on the US preset
  (910.525 MHz, SF7, 62.5 kHz bandwidth, coding rate 4/5), tens of thousands
  of times slower than Wi-Fi.

### Where the time goes, per spore

| | Time | Can it change? |
|---|---|---|
| Transmitting the message (163 bytes + MeshCore's wrapping) | about 0.56–0.63 s | Only with a different radio preset |
| The pause before the next spore (`--gap`) | 1.0 s (default) | Yes: it's a setting |

So a spore costs about 1.6 seconds, roughly **100 bytes of page per second**.

The pause isn't wasted. A radio can't listen while it transmits, so the pause
is when bare-patch lists from the reader can get through. A repeater
rebroadcasting a spore needs the air too, and pushing messages faster than
the radio can transmit fills its message queue (which makes radios
sluggish or unresponsive).

### Typical costs

| Article | Text | Spores | On air (default gap) |
|---|---|---|---|
| Short (e.g. Cardboard) | 5 KB | ~15 | ~25 s |
| Typical | 20–40 KB | 45–90 | 1–2 min |
| Long (e.g. Quantum mechanics) | ~100 KB | ~220 | ~5–6 min |
| Search results | — | 2–4 | a few seconds |

What adds to it:

- **Links**: roughly 10–25% (`--no-links` drops them).
- **Tables and infoboxes**: chemistry and element articles carry big data
  boxes; "List of …" articles are mostly tables.
- **Padding**: about 10% when `--pad-sizes` is on.
- **Repairs**: each lost spore is sent again.

`python -m moss library <zim> "title" --pick 1` (on the Pi, no radio) prints
the real cost of any article before anyone requests it, and `--max-bytes
12000` caps article size if you need to.

### Making it faster

**1. Tune the gap (free, no code changes).** One second is a cautious
default, not a measured number. To find the right value for your radios:

1. Fetch the same article a few times with the Seed Vault at `--gap 1.0`,
   then `0.5`, then `0.25`.
2. Note the time each took (shown in the reader) and how many repairs the
   vault's log shows ("repair 1: bare patch of N spores").
3. Keep the smallest gap that doesn't cause many repairs.

| Gap | Page speed | vs default |
|---|---|---|
| 1.0 s | ~100 bytes/s | — |
| 0.5 s | ~145 bytes/s | ~45% faster |
| 0.25 s | ~185 bytes/s | ~85% faster |
| 0 (back to back) | ~265 bytes/s | not recommended |

With the two radios in direct range, 0.5 is likely fine. With a repeater in
between, keep the gap above about 0.6 s so its rebroadcast of one spore
doesn't collide with your next.

**2. A faster radio preset (big gain, real trade-offs).** Each doubling of
bandwidth halves transmit time: a spore takes ~0.6 s at 62.5 kHz, ~0.3 s at
125 kHz, ~0.15 s at 250 kHz, at the cost of range. But every radio on a mesh
must use the same preset to hear each other, so changing it takes your radios
off the shared MeshCore mesh. That could suit a dedicated MOSS link between
two radios, not a shared mesh.

**3. Send fewer spores.** A shared compression dictionary (both sides
pre-loaded with common Wikipedia words and markup) would shrink short
articles by an estimated 10–25% with no radio changes. Not built yet.

---

## Being a good mesh neighbour

MeshCore channel messages **flood**: every repeater that hears one repeats it,
up to 64 hops by default. MOSS sends many messages in a row, so on a shared
mesh it can crowd out everyone else. In the Netherlands, a single node
sending about 1.4 kB per minute of flooded traffic made the public channel
unusable and people started muting it; a Seed Vault sending spores at the
default gap puts out roughly 6 kB per minute while it's sending.

What MOSS does about it:

- uses its own `#moss` channel, never Public;
- sends binary channel data, so chat apps don't display it;
- paces spores with a gap, and re-sends only what's missing;
- serves one transfer at a time;
- sends only small messages for discovery (WHOIS/HELLO) and requests.

What you can do:

- **Talk to your local MeshCore group before running a Seed Vault on a
  shared mesh.** Repeater operators set their own rules, and asking first is
  how you avoid making anyone angry.
- Use `--scope` with your region's name so distant repeaters don't carry
  MOSS traffic.
- Cap article size (`--max-bytes`) or drop links (`--no-links`) if airtime
  is tight.
- For heavy use, consider a dedicated link on its own preset or frequency.

---

## If the radio drops out

Laptop sleep or a pulled USB cable doesn't need `reset_radio.py` any more.
The reader and the Seed Vault both watch the link: a disconnect, a failed
write, or several polls in a row with no answer all count as lost. They then
release the old port handle (on Windows, a dead handle is what causes
"Access is denied") and keep reopening the port until the radio is back,
re-detecting the COM port if it changed (when `--port` was left out). The
`#moss` channel and scope are set up again on reconnect. The status bar turns
red with *Radio disconnected, reconnecting…* until then, and a transfer in
progress carries on afterwards.

`tools/reset_radio.py` is still there for a radio that's genuinely stuck:
`--who` shows what's holding the port, `--hard` reboots the board over serial.

---

## Troubleshooting

| You see | Likely cause | Fix |
|---|---|---|
| `no pulse from the Seed Vault` | The vault isn't running, the radios can't hear each other, or every try was lost | Check the vault's log; move radios closer; try again. `journalctl -u moss-gateway -f` on the Pi. |
| `your Seed Vault didn't answer, but one with a different key did` | The Pi's keys changed (reflash, new SD card) | If expected: `python -m moss trust --forget`, then check the new fingerprint. |
| `a Seed Vault answered, but it isn't the one you pinned` | A different vault, or a typo in the pinned fingerprint | Check with `python -m moss trust`; re-pin or `--forget`. |
| `page came through blighted` | Damaged or tampered page | Try again. Repeated blight with no interference around is worth investigating. |
| `all N channel slots on this radio are in use` | No free slot to create #moss in | Remove a channel in the MeshCore app, or add `#moss` there yourself. |
| `slot N is a channel called #moss with a different key` | Someone's private channel shares the name | Harmless; MOSS uses its own slot. Rename that channel if it's confusing. |
| `no such port` | Wrong `--port` | The message lists the ports found; on the Pi use the full `/dev/serial/by-id/...` path. |
| `... stayed busy` / `Access is denied` at start | Another program (MeshCore app, another terminal) holds the port | Close it, or `python tools/reset_radio.py --who`. |
| Status bar red, *reconnecting…* | Radio unplugged or laptop just woke | Wait or replug; it recovers by itself. |
| `reading a .zim needs libzim` | libzim not installed | `pip install -r requirements.txt` (or `pip install libzim`). |
| `library not found` in the Pi's service log | USB stick not mounted | Plug it in; see [USB stick](#usb-stick). |
| First search on the Pi takes several seconds | Cold USB stick | Use a USB 3 port; later searches are faster. |
| Many repairs on every transfer | Gap too short, interference, or weak signal | Raise `--gap`, move radios, check antennas. |
| Article shows `shortened to save airtime` | The vault runs with `--max-bytes` | Remove it from `MOSS_EXTRA`, then refresh the article. |
| A saved article lacks links, math or tables | Saved by an older version | Refresh it (circular arrow). |

---

## Command reference

Run `python -m moss <command> --help` for every option.

| Command | Where | What it does |
|---|---|---|
| `gateway` | Pi | Run the Seed Vault (`--source` a .zim, `wikipedia`, or a folder of .md). |
| `vault-key` | Pi | Show (creating if needed) the Seed Vault's fingerprint. |
| `library <source> "query"` | Pi | Search the library directly, no radio; shows an article and its airtime cost. |
| `web` | Laptop | The browser reader. |
| `search "query"` | Laptop | Search, pick, read in the terminal. |
| `read "Title"` | Laptop | Fetch one article by title. |
| `cache [show/forget] ["Title"]` | Laptop | List, read or delete saved articles (no radio). |
| `export <vault>` | Laptop | Write saved articles into an Obsidian vault. |
| `trust [FINGERPRINT] [--forget]` | Laptop | Show, pin or forget the trusted Seed Vault. |

Options shared by radio commands: `--port`, `--channel-name` (default
`#moss`), `--scope`, `--cache`. Reader options: `--refresh` (ask over radio
even if saved), `--quiet` (seconds of silence before asking for bare
patches, default 6), `--idle`, `--plain`, `--no-pager`, and `--sim FOLDER_OR_ZIM`
(with `--sim-loss`, `--sim-airtime`, `--pad-sizes`, `--no-links`) for a
simulated radio. Seed Vault options are in the table [above](#seed-vault-options-worth-knowing),
plus `--rounds` (repair rounds per wait, default 10) and `--wait` (seconds to
listen for bare patches, default 30).

---

## Where things are stored

| Path | Machine | Contents |
|---|---|---|
| `~/.moss/cache/articles/*.md` | Laptop | Saved articles, exactly as received |
| `~/.moss/cache/index.json` | Laptop | Titles, sizes, when fetched |
| `~/.moss/cache/searches.json` | Laptop | Past searches and their results |
| `~/.moss/vault.json` | Laptop | The trusted Seed Vault's public keys and fingerprint |
| Browser storage | Laptop | The *Read later* list and light/dark theme |
| `~/.moss/seed-vault/x25519.pem`, `ed25519.pem` | Pi | The Seed Vault's private keys (readable only by its user) |
| `/etc/moss/gateway.env` | Pi | Service settings |

`~` is `C:\Users\<you>` on Windows and `/home/<you>` on the Pi. Saved
articles aren't encrypted on the laptop's disk.

---

## Testing

```bash
python -m unittest discover -s tests -v     # everything, no hardware or internet needed
python tools/test_page_transfer.py          # the original file-transfer script
python tests/make_test_zim.py               # rebuild TestExamples/mini_wikipedia.zim
```

The tests run real exchanges over a simulated radio that drops messages,
including:

- full transfers at 20–35% loss, long (240-spore) articles, lost capsules,
  lost bare-patch lists, transfers that pause and resume;
- an **eavesdropper** recording everything sent and trying to rebuild pages:
  unencrypted it recovers everything, encrypted it recovers nothing;
- a fake Seed Vault, a forged spore, a changed vault key, pinned
  fingerprints, padding;
- a fake MeshCore radio for reconnects (sleep, unplug, failed writes) and the
  `#moss` channel (create, reuse, skip Public, private name clashes, other
  channels ignored, scopes);
- the article converter against real Wikipedia pages from Kiwix's own test
  library (math, links, tables, infoboxes) and the mini test library.

Hardware scripts in `tools/` (they predate `#moss` and take a slot number):
`ping_test.py` (send numbered chat messages; `--channels` lists slots),
`datagram_test.py` (binary messages), `diagnose.py`, `reset_radio.py`,
`fault_test.py` (break real transfers on purpose), `compare_files.py`.

---

## FAQ

**Can other people see what I'm reading?**
Not the content or titles: requests, pages and errors are encrypted end to
end. They can see that someone used MOSS, when, roughly how big the page was,
and roughly where the radio is. See [Private reading](#private-reading-encryption).

**Can the Seed Vault's owner see what I read?**
The Pi has to decrypt your request to answer it, so in principle yes. By
default it doesn't log it, but you're trusting whoever runs the vault.

**What if the Pi is stolen?**
The thief gets its keys. They can't decrypt past replies (one-time keys), but
they could decrypt recorded *requests*, and could pretend to be that vault.
Make a new vault key and have readers `moss trust --forget` and re-check the
fingerprint.

**Why not just use the Public channel?**
It floods the whole mesh, carries everyone's chat, and its key is public.
`#moss` is just as easy to join, keeps MOSS traffic separate, and can be
scoped to a region.

**Do both sides need internet?**
No. The Pi serves from the `.zim` on its USB stick; the reader's page,
fonts and formula drawing are all local. (`--source wikipedia` makes a vault
fetch live from wikipedia.org instead, which does need internet.)

**Can several people use one Seed Vault?**
Yes, one transfer at a time; others wait their turn (*dormant*, up to six in
line), and the reader shows their place.

**Can there be two Seed Vaults in range?**
Yes. Each reader trusts one; the other can't read its requests.

**What about images?**
The nopic library has none, and a single photo would be dozens to hundreds of
spores. Not planned for now.

**How big can an article be?**
No hard limit; the longest Wikipedia articles take several minutes. Text
appears as it arrives, and `--max-bytes` caps the size if needed.

**Does it work with other Kiwix libraries?**
Any Kiwix `.zim` should work (Wiktionary, Wikivoyage, other languages);
search uses the file's own indexes. Only the English Wikipedia nopic file and
Kiwix's test files have been tried.

**Why "spores"?**
Small, numerous, carried on the air, and only a full set grows into the whole
thing.

---

## Not built yet

Ideas discussed for later:

- **Adding your own pages** to the Seed Vault from a laptop (same transfer,
  the other direction), with moderation, signed authors, and a separate shelf
  so Wikipedia can't be overwritten.
- **Page types**: boards/forums (fetch only what's new), status pages
  broadcast once to everyone (signed, unencrypted), maps (base maps
  preloaded, small layers of points over the air).
- **Overheard spores**: readers keeping public broadcast spores they overhear.
- **Shared-dictionary compression** for 10–25% smaller short articles.
- **A "Seed Vault here, join #moss" note** on Public, very rarely, for
  discovery.

---

## Credits, licences and further reading

- Article text is from **Wikipedia**, under CC BY-SA 4.0; every article
  carries its source link and licence.
- Offline Wikipedia comes from **Kiwix** `.zim` files, read with **libzim**
  (GPL-3.0).
- Radios run **MeshCore**; MOSS talks to them with the `meshcore` Python
  library.
- Formulas are drawn by **KaTeX** (MIT licence), bundled in `moss/static/katex/`.
- Encryption uses **pycryptodome**.
- Icons are from **Lucide** (ISC licence).

Check each component's licence before redistributing MOSS.

Further reading:

- [MeshCore FAQ](https://docs.meshcore.io/faq/): presets, flooding, hop limits, the Public key
- [MeshCore companion protocol](https://docs.meshcore.io/companion_protocol/): channel slots, channel data, limits
- [MeshCore scopes](https://blog.meshcore.io/2026/04/17/default-scope): keeping floods within a region
- [Hashtag channel keys](https://github.com/TwinRocket/meshloom/issues/71): how a channel's key comes from its name
- [Repeater flood control discussion](https://github.com/meshcore-dev/MeshCore/issues/3278): what heavy flooding does to a mesh
- [RFC 9180 (HPKE)](https://www.rfc-editor.org/rfc/rfc9180): the encryption pattern MOSS follows
- [Kiwix](https://kiwix.org/): offline Wikipedia and other libraries
