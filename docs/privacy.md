# Privacy and encryption

What MOSS keeps private, how, and what a listener can still work out.
The short version is in the [README](../README.md#privacy-in-one-minute).

---

Anyone in radio range can hear MOSS traffic, and the `#moss` key is public.
So MOSS encrypts end to end on top of the channel: **what you search for and
read is readable only by you and the Seed Vault.**

## What's protected

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

## How the encryption works

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

## Trust (fingerprints)

The first Seed Vault a reader hears is trusted and remembered in
`~/.moss/vault.json`, like SSH does. Check its fingerprint (status bar, hover
the lock) against the one the Pi prints. To be strict from the start, pin it
before first contact:

```
moss trust 3F9A-12C4-77E0-5B21   # only trust this vault
moss trust                       # show who is trusted
moss trust --forget              # after the Pi's keys were reset on purpose
```

If a reader's vault stops answering and a vault with a different key answers
instead (say the Pi was reflashed), the reader says so and suggests
`moss trust --forget` rather than just "no answer". A second Seed Vault on the
same channel can't read requests meant for yours, so it can't answer them
either.

## What a listener can still see

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
