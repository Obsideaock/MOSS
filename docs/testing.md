# Testing

How MOSS is tested, and how to test your own radios and Seed Vault.

---

## Running the tests

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
- **uploads and community pages** over a lossy simulated radio:
  - round trips at 25–35% loss, including lost acceptances, lost results
    and every spore lost once;
  - an eavesdropper who must not see the text, title, address, author or
    key of an uploaded or read page;
  - vault logs that don't name pages;
  - every limit (size, per author, daily budget across many identities,
    storage, uploads off);
  - a forged page and a page that doesn't match its offer, both refused
    after transfer;
  - replays of old versions and deleted pages refused;
  - a vault serving an older copy, or a tampered one, refused by the reader;
  - the hardware self-test run end to end, including catching a vault that
    wrongly accepts replays;
- **finding pages and stage 3**:
  - ranking (linked pages first, link farms counting once, unlisted and
    deleted pages never shown), paging and author listings;
  - your follows moving results up without ever appearing in a request;
  - vault specs (including free storage) that never contain unlisted
    pages, authors or reads;
  - an older vault explained, with search falling back to Wikipedia only;
  - spore prints: chains growing, forgeries (edited, cut, renamed,
    re-rooted) refused, readers never named, a reader carrying a print from
    one vault to another;
  - routing learned from the radio's packet log, off by default, never
    matching other channels, falling back to flooding;
  - the dedicated-link settings;
- **install and update**: settings files rewritten without losing comments,
  `moss update` against real git repositories (moving forward, stashing
  local edits, refusing local commits, switching branches), setup keeping
  existing vault keys and settings, and the radio check on a fake radio
  (including pausing and restarting the vault);
- **signed pages** under attack: every single byte flipped, records edited
  or re-signed by another key, signatures without their context, non-canonical
  and duplicate-field records, replayed old versions, two pages signed with
  one version, compression bombs, and anonymous pages checked for any trace
  of the author's other identities;
- a fake MeshCore radio for reconnects (sleep, unplug, failed writes, a
  healthy radio sending messages the library can't read) and the
  `#moss` channel (create, reuse, skip Public, private name clashes, other
  channels ignored, scopes);
- the article converter against real Wikipedia pages from Kiwix's own test
  library (math, links, tables, infoboxes) and the mini test library.

## Testing on your hardware

Two levels, both from the laptop with the Seed Vault running as normal (and
both machines on the same MOSS version: `moss update` on each):

1. **Can the radios hear each other?** `moss radio-check listen` on the Pi
   (it pauses the vault for the test) and `moss radio-check send` on the
   laptop, or *Radio check* in the menu on both. Ten numbered pings; the Pi
   shows each one that arrives with its signal-to-noise ratio.
2. **Does everything work end to end?** `moss selftest` on the laptop (or
   `python tools/hardware_test.py`, or *Radio check → Full test with the
   Seed Vault*). Over the real radios it:
   1. searches;
   2. uploads a throwaway unlisted test page from a throwaway identity;
   3. reads it back and compares it byte for byte;
   4. uploads and reads back a second version;
   5. checks the vault refuses the old version when sent again;
   6. deletes the page.

   It also checks the vault's specs, for 8 steps in all.

   Each step prints PASS or FAIL with how long it took. Expect a few minutes.

`moss selftest --sim TestExamples` runs the same steps over the simulated
radio, no hardware needed; the unit tests run it too.

Hardware scripts in `tools/` (they predate `#moss` and take a slot number):
`ping_test.py` (send numbered chat messages; `--channels` lists slots),
`datagram_test.py` (binary messages), `diagnose.py`, `reset_radio.py`,
`fault_test.py` (break real transfers on purpose), `compare_files.py`.
