# Running a Seed Vault

A Seed Vault is a Raspberry Pi with a radio and an offline copy of
Wikipedia on a USB stick. It answers everyone's requests, and stores the
community pages people upload to it.

**On this page:** [Setting up the Pi](#setting-up-the-pi) ·
[USB stick](#usb-stick) · [Radio port](#radio-port) ·
[Options](#seed-vault-options-worth-knowing) ·
[Being a good mesh neighbour](#being-a-good-mesh-neighbour) ·
[Routing](#routing-instead-of-flooding-opt-in) · [A dedicated link](#a-dedicated-link)

---

## Setting up the Pi

What you need:

- a **Raspberry Pi** (a Pi 4 or 5 is comfortable) running **64-bit
  Raspberry Pi OS**;
- a **MeshCore radio** with the USB companion firmware, plugged into the Pi
  (install it from the [MeshCore web flasher](https://meshcore.io/flasher));
- a **USB stick** (64 GB or more) labelled `KIWIX`, holding a Wikipedia
  `.zim` file from [Kiwix's downloads](https://download.kiwix.org/zim/wikipedia/).
  The `wikipedia_en_all_nopic_...` file is the one MOSS is built around:
  all of English Wikipedia's text, no pictures, about 57 GB. Smaller files
  (a single topic, or another language) work too and are a quick way to try
  things out.

The examples below use `moss` as the Pi's user name; yours may differ.

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

Setup, one command:

```bash
curl -sSL https://raw.githubusercontent.com/Obsideaock/MOSS/main/install.sh | bash
```

It installs git and Python's venv if needed, puts MOSS in `~/MOSS` (or
updates an existing `~/MOSS` or `~/Documents/MOSS` in place), makes the
Python environment, adds the `moss` command, lets your user open serial
ports, and then runs `moss setup`, which:

- finds the radio by its stable `/dev/serial/by-id/...` name and checks it
  answers (creating `#moss` on it if needed, in a free slot, never over
  Public or a slot in use);
- finds the `.zim` on the USB stick;
- keeps existing Seed Vault keys, or makes them, and shows the
  **fingerprint**;
- writes `/etc/moss/gateway.env`, keeping your old values and options;
- installs the `moss-gateway` service (starts on boot) and starts it.

Log out and back in once afterwards if it says it added you to the
`dialout` group.

**Back up `~/.moss/seed-vault/`.** New keys mean every reader sees a
different fingerprint and has to re-trust the vault.

Day to day, type `moss` on the Pi: status and recent log, watch the log
live, start/stop/restart, radio check, change library or radio port, extra
options, update. The same by hand:

```bash
sudo systemctl stop moss-gateway       # pause it
sudo systemctl start moss-gateway      # start it again
sudo systemctl restart moss-gateway    # after changing settings
journalctl -u moss-gateway -f          # watch requests come in
moss library /media/moss/KIWIX/wikipedia_en_all_nopic_2026-06.zim "boulder colorado"   # test the library, no radio
```

On first start the Seed Vault announces itself once on `#moss`. An older
settings file with a `MOSS_CHANNEL=` line, or an older service that still
passes `--channel N`, keeps working: the setting is simply ignored now.
`deploy/install-gateway.sh` still works too, but the installer replaces it.

## USB stick

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

## Radio port

`ls /dev/serial/by-id/` shows each radio's stable name (for example
`usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0`);
use the full path rather than `/dev/ttyUSB0`, which can change between
reboots. The `ttyUSB0` after the arrow in that listing is the same port's
short name, which lives in `/dev/`, not in `/dev/serial/by-id/`.

## Seed Vault options worth knowing

Set these from `moss` → *Extra options*, or put them in `MOSS_EXTRA=` in
`/etc/moss/gateway.env` and run `sudo systemctl restart moss-gateway`:

| Option | What it does |
|---|---|
| `--pad-sizes` | Round encrypted pages up to standard sizes so their length reveals less (~10% more airtime). Off by default. |
| `--scope NAME` | Keep MOSS's flooded traffic inside a MeshCore region (ask your local mesh for the name). |
| `--gap 0.5` | Seconds between spores (default 1.0). Lower is faster; see [Making it faster](how-it-works.md#making-it-faster). |
| `--max-bytes 12000` | Shorten long articles to about this many bytes (default: full articles). |
| `--no-links` | Leave out links between articles (saves roughly 10–25% airtime). |
| `--log-queries` | Write the titles of encrypted requests to the log. Off: the Pi keeps no record of what people read. |
| `--name "Boulder Seed Vault"` | The name readers see when they first meet this vault. |
| `--keys DIR` | Where the keys live (default `~/.moss/seed-vault`). |
| `--page-cap 250` | Biggest community page accepted, in spores. |
| `--uploads-per-author 3` | Uploads per author per day. |
| `--upload-budget 2000` | Spores of uploads per day for the whole vault. |
| `--page-storage-mb 2048` | Room for community pages. |
| `--no-uploads` | Serve the community pages already here, but take no new ones. |
| `--pages DIR` | Where community pages are kept (default `~/.moss/seed-vault/pages`). |

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
- For heavy use, consider a dedicated link on its own preset or frequency
  (below).

## Routing instead of flooding (opt-in)

With `--route` on the Seed Vault (in `MOSS_EXTRA`, or *Extra options* in the
Pi's menu), a reply's spores go back **along the route the request came
by**, so only the repeaters in between carry them, not every repeater in
range. Readers can do the same for uploads (*Settings → Routing* in the
menu, or `--route`).

How it works, on stock firmware: the radio logs the route of every packet it
hears. MOSS decrypts `#moss` packets in that log with the channel's
(public) key to match each one to the MOSS message inside, and sends replies
back along that route reversed. It's still `#moss` channel data: no
contacts, no new tags naming who's talking.

It falls back to flooding:
- when no route is known (the radio didn't log one, or the request came
  directly);
- for routes using multi-byte repeater ids;
- if the radio refuses a route;
- from the third repair round on, in case the route went bad.

Control messages (pulses, capsules, results) always flood. This is new and
**needs testing on your mesh**. The vault's log says "route back known (2
hops)" when it's being used.

## A dedicated link

To take MOSS off the public mesh entirely, put the vault and all its
readers on their own settings: `moss radio-link 915.0 62.5 7 5` (frequency
MHz, bandwidth kHz, spreading factor, coding rate) on each, or *Radio check
→ Dedicated link* in the menu. `moss radio-link --public` goes back to
MeshCore's usual US settings. On a dedicated link those radios can't hear
the public mesh. Some radios need a restart to switch.
