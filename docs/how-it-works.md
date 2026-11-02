# How it works

The technical side: the radio channel, what a transfer looks like on
the air, message formats, speed, and recovering from a dropped radio.
None of this is needed to use MOSS.

**On this page:** [The two machines](#the-two-machines) · [Glossary](#glossary) ·
[The #moss channel](#the-moss-channel) · [The radio side](#the-radio-side) ·
[A transfer, step by step](#a-transfer-step-by-step) · [Message formats](#message-formats) ·
[What a page looks like on the air](#what-a-page-looks-like-on-the-air) · [Search](#search) ·
[Speed and airtime](#speed-and-airtime) · [If the radio drops out](#if-the-radio-drops-out)

## The two machines

```
 laptop (reader)                                       Raspberry Pi (Seed Vault)
 ┌──────────────────────────┐                         ┌───────────────────────────────┐
 │ browser: the reader      │   LoRa radio, #moss     │ moss gateway                  │
 │ moss web                 │◄───────────────────────►│ wikipedia_en_all_nopic.zim    │
 │ ~/.moss  saved articles, │  163-byte encrypted     │ (57 GB, on a USB stick)       │
 │          trusted vault   │  messages, km of range  │ ~/.moss/seed-vault  its keys  │
 └────────────┬─────────────┘                         └───────────────┬───────────────┘
           USB│MeshCore radio                                      USB│MeshCore radio
```

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

## The radio side

MeshCore radios carry two kinds of channel traffic:

- **text messages**: what the MeshCore app shows as chat;
- **channel data**: raw bytes ("binary") tagged with a *data type* number.
  MOSS uses `0xFF01`, from the range set aside for experiments. Chat apps
  ignore data types they don't recognize, which is why MOSS traffic never
  shows up as garbage in anyone's chat.

That tag lives in MeshCore's own packet wrapping. MeshCore allows up to 165
bytes of channel data per message; MOSS uses 163 per message for itself.

## A transfer, step by step

1. **First contact** (once per reader): the reader sends **WHOIS**; the Seed
   Vault answers **HELLO** with its public keys; the reader remembers them.
2. **Request**: the reader sends a sealed request (search or article title).
   If nothing comes back, it asks again (and also asks WHOIS, in case the
   vault's key changed).
3. **Pulses**: the vault answers *dormant* (queued, with your place in line),
   then *looking it up*, then *sending* (with an estimated time).
4. **Capsule**: how many spores, their total size, and a hash of the page.
   It's sent twice at the start and again every 50 spores (about 2–3% extra
   airtime), so a reader that missed it can pick it up without asking.
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

- If the **capsule** is lost, spores that arrive anyway are kept; the reader
  asks for it again and also catches the next repeat (every 50 spores), which
  works even if the reader's radio can't transmit for a while.
- If a **bare-patch list** is lost several times and the vault gives up
  waiting, the reader's next list makes it pick the transfer back up.
- The reader only gives up after **15 asks in a row that bring nothing
  new**; asks that make progress don't count.
- If an **error** or capsule is lost, the reader asks again and the vault
  repeats its last answer.
- If the radio **refuses** to send something (its queue is full or it's
  busy), the send is retried, logged, and if the radio keeps refusing, the
  link is reopened.
- One transfer at a time on the air: other requests wait (*dormant*) at the
  vault, up to six.

Tested over a simulated radio losing 25% and 35% of messages, with 240-spore
(Quantum-mechanics-sized) articles: every transfer completed intact.

## Message formats

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
| 13 | SACCEPT (upload accepted) | type 1, id 2, one-time key 32, sealed spores and length 6, check 16 | vault → reader |
| 14 | UCHUNK (upload spore) | type 1, id 2, number 2, data up to 158 | reader → vault |
| 15 | UNACK (upload bare patch) | type 1, id 2, count 1, numbers 2 each | vault → reader |
| 16 | SRESULT (upload result) | type 1, id 2, sealed stored-or-not and message, check 16 | vault → reader |

A sealed request carries a *kind*:

| Kind | Request |
|---|---|
| 1 | Search (Wikipedia only; older readers) |
| 2 | Article |
| 3 | Community page (the query is its address) |
| 4 | Upload (the query is the offer: address, version, size; address `spore-print` offers a spore print) |
| 5 | Vault specs |
| 6 | Search, Wikipedia and community (query: offset, `\x1f`, the text; or `author:<id>`) |
| 7 | The vault's spore print |

A vault that doesn't know a kind answers "bad request", which the reader
turns into "the Seed Vault is running an older MOSS; moss update on the
Pi". HELLO also carries the vault's MOSS version, after a zero byte.

With encryption on (the normal case) only types 2, 3, 4, 6 and 8–16 go out.
Types 1, 5 and 7 are kept for `--plain` and for the older tools.

The 5-byte spore header (type, ID, number) is what leaves 158 of the 163
bytes for the page.

## What a page looks like on the air

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

## Search

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

`moss library <zim> "title" --pick 1` (on the Pi, no radio) prints
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

Polls can also go unanswered on a perfectly healthy radio (for example when
it passes along a kind of message the MeshCore library can't read). So
before declaring the link lost, MOSS asks the radio a simple "what device are
you?" question, and only reconnects if that goes unanswered too. Every
disconnect message says why (`radio link lost: the radio stopped answering
(...)`), and if reopening keeps failing, the error is shown on the first try
and every fifth. `--debug` prints everything the radio and the MeshCore
library say, for chasing anything stranger.

`tools/reset_radio.py` is still there for a radio that's genuinely stuck:
`--who` shows what's holding the port, `--hard` reboots the board over serial.
