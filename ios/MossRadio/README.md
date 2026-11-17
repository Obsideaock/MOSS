# MOSS Radio (iPhone)

The iPhone side of [MOSS](../../README.md). Through a MeshCore radio on
Bluetooth (a T1000-E, a T-Beam, any radio with the **companion Bluetooth**
firmware), it:

- **range-tests** the link to a Seed Vault: pings every few seconds, the
  vault answers each with how well it heard you, and every ping lands on a
  map with its signal both ways and its distance. Export as CSV.
- **searches and reads Wikipedia** from the Seed Vault, encrypted end to
  end like the laptop reader, with spores filling in as they arrive.
  Articles you read stay on the phone and open with no radio.
- shows everything MOSS-related the radio hears on `#moss`, with its signal.
- runs a **self-check** that replays bytes made by the Python MOSS, to prove
  the phone speaks MOSS exactly.

Not in this version yet: opening community pages, uploading pages, vault
specs and spore prints. The laptop reader does those.

## Build and install (xtool)

Unzip this folder into `~/ios-development/projects/` (so it's
`~/ios-development/projects/MossRadio`), plug the iPhone into the PC (or have
it on the same Wi-Fi), unlock it, and run:

```bash
cd ~/ios-development/projects/MossRadio
./run-on-iphone.sh
```

That builds the app, registers it with the renewal kit, and installs it.
`./run-on-iphone.sh --no-install` builds and registers only.

The first build takes a while (the SDK's modules compile once). If it stops
with compiler errors, send them over: this was written without a Mac, so the
first build is the real compile test.

- Bundle ID: `com.openlonehollow.MossRadio`. Don't change it: a new ID is a
  new app to iOS, and uses another of the free account's three app slots.
- No extensions, no paid-account capabilities. Bluetooth and location
  (with their background modes) are all it asks for.
- Everything it keeps (the trusted vault, range tests, saved articles,
  settings) is saved as it changes, so a renewal reinstalling it loses nothing.

## First run

1. **Radio tab → Find radios**, and tap your radio. Disconnect the MeshCore
   phone app from it first: a radio takes one Bluetooth connection at a time.
   The first time, iOS asks for the radio's **PIN** (in the MeshCore app, or on
   the radio's screen). The app then finds or creates `#moss` on the radio.
2. **Vault tab → Look for the Seed Vault.** The first vault heard is trusted;
   check its fingerprint matches the Pi's (`moss vault-info`).
3. **Vault tab → Run the self-check.** Everything should pass.

## Range testing

1. Stand by the Seed Vault and tap **I'm at the Seed Vault: set its spot
   here** (Range tab), so pings get a distance.
2. **Start a range test**, put the phone in your pocket and walk or drive.
   It keeps going with the screen off.
3. Each dot on the map is a ping: **green** answered, **red** no answer,
   **grey** never sent. Tap **Earlier tests** to see a finished test and
   **Export as CSV**.

**Direct only (no repeaters)**, on by default, sends pings that only radios in
direct range hear, so the test measures the link itself and doesn't load
the shared mesh. Turn it off to test reaching the vault through repeaters.

The Seed Vault needs MOSS 0.6.1 or newer to answer pings (`moss update` on
the Pi). It answers at most one ping every 3 seconds, whoever sends them, and
`--no-pings` turns answers off.

## How it fits together

| File | What it does |
|---|---|
| `RadioLink.swift` | Bluetooth link to the radio, the `#moss` channel, fetching queued messages |
| `MeshCore.swift` | The MeshCore companion protocol: commands and replies |
| `MossProtocol.swift` | MOSS datagrams: spores, repairs, pings, search results |
| `Seal.swift` | Encryption: sealed requests, capsules, the trusted vault (CryptoKit) |
| `MossClient.swift` | One request start to finish: retries, repairs, checks |
| `RangeTester.swift`, `Location.swift` | Range tests and GPS |
| `Library.swift` | Saved articles |
| `ArticleText.swift` | Showing MOSS articles: facts boxes, tables, formulas, links |
| `SelfCheck.swift`, `TestVectors.swift` | The self-check; vectors made by `tools/make_ios_vectors.py` |
| `*View.swift`, `App.swift` | The four tabs |

Licence: GPL-3.0-or-later, like the rest of MOSS.
