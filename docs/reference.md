# Reference

Look-up material: problems and fixes, every command, where files are
kept, and what's in the repository.

**On this page:** [Troubleshooting](#troubleshooting) · [Command reference](#command-reference) ·
[Where things are stored](#where-things-are-stored) · [Repository layout](#repository-layout)

---

## Troubleshooting

| You see | Likely cause | Fix |
|---|---|---|
| `no pulse from the Seed Vault` | The vault isn't running, the radios can't hear each other, or every try was lost | Check the vault's log; move radios closer; try again. `journalctl -u moss-gateway -f` on the Pi. |
| `your Seed Vault didn't answer, but one with a different key did` | The Pi's keys changed (reflash, new SD card) | If expected: `moss trust --forget`, then check the new fingerprint. |
| `a Seed Vault answered, but it isn't the one you pinned` | A different vault, or a typo in the pinned fingerprint | Check with `moss trust`; re-pin or `--forget`. |
| `page came through blighted` | Damaged or tampered page | Try again. Repeated blight with no interference around is worth investigating. |
| `all N channel slots on this radio are in use` | No free slot to create #moss in | Remove a channel in the MeshCore app, or add `#moss` there yourself. |
| `slot N is a channel called #moss with a different key` | Someone's private channel shares the name | Harmless; MOSS uses its own slot. Rename that channel if it's confusing. |
| `this computer remembers an old pairing with the radio` (on a Mac: `Peer removed pairing information`) | The radio was reflashed, reset or paired elsewhere, but the computer kept the old pairing, so it never asks for the PIN | Forget the radio in the computer's Bluetooth settings (Mac: ⓘ → Forget This Device; Windows: Remove device; Linux: `bluetoothctl remove ADDRESS`), then try again and enter the PIN when asked. |
| `couldn't reach the radio ... over Bluetooth` | Radio off, too far, or still connected to the phone app | Disconnect the phone app, bring it closer, try again; if asked, the PIN is in the MeshCore app (often 123456). |
| No Bluetooth radios found in setup | Computer's Bluetooth off, or radio not advertising | Turn Bluetooth on; restart the radio; make sure it runs the Bluetooth companion firmware. |
| `moss` isn't recognised | The terminal was open before installing | Open a new terminal (on Linux, or log out and in). |
| `this copy has commits GitHub doesn't` on update | Local commits in the MOSS folder | Push them, or `git status` to sort it out; nothing was changed. |
| `no such port` | Wrong `--port` | The message lists the ports found; on the Pi use the full `/dev/serial/by-id/...` path. |
| `... stayed busy` / `Access is denied` at start | Another program (MeshCore app, another terminal) holds the port | Close it, or `python tools/reset_radio.py --who`. |
| Status bar red, *reconnecting…* | Radio unplugged or laptop just woke | Wait or replug; it recovers by itself. |
| `radio link lost: ...` and it never comes back | The reason is in the message; `still can't reopen COM12` lines say why reopening fails | Close anything else using the port; replug; run with `--debug` and look at the last lines. |
| `the signing key doesn't own this address` / `signature doesn't match` | A page that was forged or altered | Don't trust it. |
| `too big: N spores, this Seed Vault's cap is 250` | The page is over the vault's size cap | Split it into two pages linked to each other, or ask the vault's owner. |
| `uploads for today are used up` / `airtime for today is used up` | A daily upload limit | Try tomorrow; the page stays in the outbox. |
| `already on this Seed Vault` | That version was uploaded before | Nothing to do. |
| `doesn't have a page at that address` | Not on this vault (or a typo) | Check the address; it may be on another vault. |
| `older version ... refused` | Someone (or a vault) offered an older copy of a page you've seen newer | Nothing to do; you keep the newer one. |
| `reading a .zim needs libzim` | libzim not installed | `pip install -r requirements.txt` (or `pip install libzim`). |
| `library not found` in the Pi's service log | USB stick not mounted | Plug it in; see [USB stick](seed-vault.md#usb-stick). |
| First search on the Pi takes several seconds | Cold USB stick | Use a USB 3 port; later searches are faster. |
| Many repairs on every transfer | Gap too short, interference, or weak signal | Raise `--gap`, move radios, check antennas. |
| `the radio refused to send a message 3 times` | The radio's send queue is full or it's throttling itself | Occasional: harmless, the protocol repeats it. Constant: raise `--gap`; if it says it's reopening the link, it's recovering by itself. |
| Stuck on *Waiting for the capsule* | The capsule was lost and the reader's asks aren't getting out | It now catches a repeated capsule within 50 spores. If it still gives up, check the laptop's terminal for "refused to send" lines. |
| Article shows `shortened to save airtime` | The vault runs with `--max-bytes` | Remove it from `MOSS_EXTRA`, then refresh the article. |
| A saved article lacks links, math or tables | Saved by an older version | Refresh it (circular arrow). |

---

## Command reference

Run `moss <command> --help` for every option.

| Command | Where | What it does |
|---|---|---|
| *(nothing)* / `menu` | Both | The menu. |
| `--version` | Both | Which MOSS this is. |
| `setup [--role reader/vault] [--port P] [--zim Z] [--yes]` | Both | Set this machine up (the installers run this). |
| `update [--branch main/experimental]` | Both | Newest MOSS from GitHub. |
| `radio-check send/listen/info` | Both | Range test with pings on `#moss`; `info` shows the radio's settings. |
| `gateway` | Pi | Run the Seed Vault (`--source` a .zim, `wikipedia`, or a folder of .md). |
| `vault-key` | Pi | Show (creating if needed) the Seed Vault's fingerprint. |
| `library <source> "query"` | Pi | Search the library directly, no radio; shows an article and its airtime cost. |
| `web` | Laptop | The browser reader. |
| `search "query"` | Laptop | Search, pick, read in the terminal. |
| `read "Title"` | Laptop | Fetch one article by title. |
| `cache [show/forget] ["Title"]` | Laptop | List, read or delete saved articles (no radio). |
| `export <vault>` | Laptop | Write saved articles into an Obsidian vault. |
| `trust [FINGERPRINT] [--forget]` | Laptop | Show, pin or forget the trusted Seed Vault. |
| `author [list/new/use/rename/backup/restore]` | Laptop | Your author identities for community pages. |
| `page make FILE.md [--unlisted] [--anonymous] [--as NAME]` | Laptop | Sign a new page into the outbox. |
| `page update/restore/delete/history ADDRESS` | Laptop | New versions of your pages. |
| `page check FILE.mosspage [--show]` | Anywhere | Verify a page file's signature and print it. |
| `page mine` | Laptop | Everything you've published. |
| `page upload [ADDRESS or FILE]` | Laptop | Upload what's waiting in the outbox (or one page). |
| `page open ADDRESS [--refresh]` | Laptop | Read a community page by its address. |
| `selftest` | Laptop | End-to-end test with the Seed Vault: search, specs, upload, read back, replay, delete. |
| `vault-info` | Laptop | The Seed Vault's specs: version, library, free storage, spore print (and trades spore prints). |
| `spore-print ["NOTE"]` | Pi | Show the vault's spore print, or start the original one. |
| `radio-link FREQ BW SF CR` / `--public` | Both | Put this radio on a dedicated link, or back on the public mesh. |

Options shared by radio commands: `--port`, `--ble [ADDRESS]` and `--pin`
(Bluetooth instead of USB), `--channel-name` (default `#moss`), `--scope`,
`--route` (opt-in routing), `--cache`, `--debug`. Reader options: `--refresh` (ask over radio
even if saved), `--quiet` (seconds of silence before asking for bare
patches, default 6), `--idle`, `--plain`, `--no-pager`, and `--sim FOLDER_OR_ZIM`
(with `--sim-loss`, `--sim-airtime`, `--pad-sizes`, `--no-links`) for a
simulated radio. Seed Vault options are in the table [Running a Seed Vault](seed-vault.md#seed-vault-options-worth-knowing),
plus `--rounds` (repair rounds per wait, default 10) and `--wait` (seconds to
listen for bare patches, default 30). The capsule repeat interval (50 spores)
is a `Gateway` setting in code (`capsule_every`).

---

## Where things are stored

| Path | Machine | Contents |
|---|---|---|
| `~/.moss/cache/articles/*.md` | Laptop | Saved articles, exactly as received |
| `~/.moss/cache/index.json` | Laptop | Titles, sizes, when fetched |
| `~/.moss/cache/searches.json` | Laptop | Past searches and their results |
| `~/.moss/vault.json` | Laptop | The trusted Seed Vault's public keys and fingerprint |
| `~/.moss/config.json` | Both | What setup remembered: reader or vault, radio port or Bluetooth radio (and PIN), channel, scope, update branch |
| Browser storage | Laptop | The *Read later* list and light/dark theme |
| `~/.moss/authors/<id>/key.pem` | Laptop | An author identity's private signing key (readable only by you; back it up) |
| `~/.moss/authors/<id>/pages.json`, `history/` | Laptop | Pages you published with it, and every version's text |
| `~/.moss/authors/default` | Laptop | Which identity `page make` uses |
| `~/.moss/outbox/*.mosspage` | Laptop | Signed pages waiting to be uploaded |
| `~/.moss/outbox/sent/` | Laptop | Pages already uploaded |
| `~/.moss/cache/pages/` | Laptop | Community pages you've opened, as signed by their authors |
| `~/.moss/seed-vault/pages/<author id>/*.mosspage` | Pi | Community pages the vault carries (newest version of each) |
| `~/.moss/seed-vault/pages/uploads.json` | Pi | Today's upload counts per author id, for the limits (nothing older than yesterday) |
| `~/.moss/social.json` | Laptop | Who you follow, favourites, how often you open pages (never sent anywhere) |
| `~/.moss/spore-prints/` | Laptop | Spore prints this reader carries, and its own (`mine.json`) |
| `~/.moss/seed-vault/spore-print.json` | Pi | The vault's spore print (never changes once set) |
| `~/.moss/seed-vault/pages/downloads.json` | Pi | Download count per public page (no who, no when) |
| `~/.moss/seen-pages.json` | Laptop | Newest version seen of each page, so old copies can't be replayed |
| `~/.moss/seed-vault/x25519.pem`, `ed25519.pem` | Pi | The Seed Vault's private keys (readable only by its user) |
| `/etc/moss/gateway.env` | Pi | Service settings |

`~` is `C:\Users\<you>` on Windows and `/home/<you>` on the Pi. Set
`MOSS_HOME` to keep identities, outbox and seen pages somewhere other than
`~/.moss`. Saved
articles aren't encrypted on the laptop's disk.

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
│   ├── pages.py               community pages: author identities, addresses, signed page files
│   ├── pages_cli.py           moss author ... / page ...
│   ├── pagestore.py           the Seed Vault's community pages: limits, search and ranking
│   ├── sporeprint.py          spore prints: a vault's signed family tree
│   ├── social.py              follows and favourites (local only)
│   ├── selftest.py            end-to-end test against a Seed Vault (real or simulated)
│   ├── menu.py + ui.py        `moss` with no arguments: the menu
│   ├── setup.py               `moss setup`: reader or Seed Vault, radio, library, service
│   ├── config.py              what setup remembers (~/.moss/config.json)
│   ├── service.py             the Pi's background service and its settings
│   ├── radiocheck.py          range test: pings on #moss
│   ├── updater.py             `moss update` from GitHub
│   ├── web.py + index.html    the browser reader (localhost only)
│   ├── static/katex/          formula drawing, bundled so it works offline
│   └── cli.py                 moss ...
├── tests/                     python -m unittest discover -s tests
│   ├── make_test_zim.py       builds TestExamples/mini_wikipedia.zim
│   └── fake_radio.py          a fake MeshCore radio for tests
├── tools/                     the earlier hardware scripts
│   ├── page_transfer.py       send one file between two radios (the original demo)
│   ├── ping_test.py, datagram_test.py, diagnose.py, reset_radio.py, fault_test.py, ...
│   └── fetch_page.py          used by the live-Wikipedia source
├── TestExamples/              fixtures, plus mini_wikipedia.zim (about 80 KB)
├── deploy/                    Pi service template and settings template (used by setup)
├── install.sh, install.ps1    the one-line installers (Linux/Pi/Mac, Windows)
├── pyproject.toml             makes `pip install -e .` provide the moss command
├── requirements.txt           reader (laptop)
└── requirements-gateway.txt   Seed Vault
```
