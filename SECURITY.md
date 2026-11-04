# Security

MOSS is an early release. Its encryption is built from standard,
well-reviewed pieces (X25519, HKDF-SHA256, ChaCha20-Poly1305, Ed25519,
scrypt), and the tests actively try to break it, but it has **not had an
independent security review yet**.

What MOSS protects, and what it can't, is set out in
[Privacy and encryption](docs/privacy.md).

## Reporting a problem

If you find a way to read someone's traffic, forge or alter a page, pretend
to be a Seed Vault, track a reader, or run code through the reader, please
report it **privately**, not in a public issue:

- on GitHub, open the **Security** tab of this repository and choose
  **Report a vulnerability**.

Please include what you did, what happened, and the MOSS version
(`moss --version` prints it). We'll reply, fix it,
and credit you if you'd like.

Ordinary bugs that aren't about security are welcome as normal
[issues](https://github.com/Obsideaock/MOSS/issues).

## Supported versions

Only the newest version on the `main` branch gets fixes. `moss update`
gets it.
