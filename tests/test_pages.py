"""Community pages, stage 1: identities, addresses, signed records.

Most of these try to break something: forge a page, alter one after
signing, replay an old version, or link an anonymous page to its author.

    python -m unittest discover -s tests -v      (from the repo root)
"""

import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from Crypto.PublicKey import ECC                     # noqa: E402
from Crypto.Signature import eddsa                   # noqa: E402

from moss import pages as PG                         # noqa: E402

MD = """# Water filters

A [[Sand filter|sand filter]] cleans water slowly. It works without power.

## Building one

Stack gravel, then sand, then charcoal.
"""


def split(blob: bytes):
    n = struct.unpack_from("<I", blob, len(PG.MAGIC))[0]
    start = len(PG.MAGIC) + 4
    return blob[start:start + n], blob[start + n:start + n + 64], blob[start + n + 64:]


def build(record_bytes: bytes, sig: bytes, body: bytes) -> bytes:
    return PG.MAGIC + struct.pack("<I", len(record_bytes)) + record_bytes + sig + body


def sign_with(key, record_bytes: bytes) -> bytes:
    return eddsa.new(key, "rfc8032").sign(PG.SIGN_CONTEXT + record_bytes)


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.authors = PG.Authors(self.home)
        self.stern = self.authors.create("Stern")


class AddressTests(Base):
    def test_author_id_is_128_bits_of_lowercase_base32(self):
        self.assertRegex(self.stern.id, r"^[a-z2-7]{26}$")
        self.assertRegex(self.stern.fingerprint, r"^[0-9A-F]{4}(-[0-9A-F]{4}){3}$")
        self.assertEqual(PG.author_id(self.stern.pub), self.stern.id)

    def test_names(self):
        self.assertEqual(PG.check_name("water-filters", PG.PUBLIC), "water-filters")
        for bad in ("Water", "a b", "-x", "x" * 49, "", "../etc"):
            with self.assertRaises(PG.PageError, msg=bad):
                PG.check_name(bad, PG.PUBLIC)
        r = PG.random_name()
        self.assertRegex(r, r"^~[a-z2-7]{16}$")
        PG.check_name(r, PG.UNLISTED)
        with self.assertRaises(PG.PageError):          # random names are for unlisted pages
            PG.check_name(r, PG.PUBLIC)

    def test_parse_address(self):
        aid, name = PG.parse_address(f"moss:{self.stern.id}/water-filters")
        self.assertEqual((aid, name), (self.stern.id, "water-filters"))
        for bad in ("moss:abc/x", f"{self.stern.id}/x", f"moss:{self.stern.id}/", f"moss:{self.stern.id}/A"):
            with self.assertRaises(PG.PageError, msg=bad):
                PG.parse_address(bad)


class RoundTripTests(Base):
    def test_sign_and_load(self):
        page = self.stern.sign("water-filters", MD)
        again = PG.load(page.to_bytes())
        self.assertEqual(again.text(), MD)
        r = again.record
        self.assertEqual(r["address"], f"moss:{self.stern.id}/water-filters")
        self.assertEqual(r["title"], "Water filters")
        self.assertEqual(r["about"], "A sand filter cleans water slowly.")
        self.assertEqual((r["author"], r["version"], r["visibility"], r["deleted"]),
                         ("Stern", 1, "public", False))
        self.assertLess(again.spores, 10)

    def test_versions_count_up_and_history_is_kept(self):
        self.stern.sign("water-filters", MD)
        v2 = self.stern.sign("water-filters", "# Water filters\n\nNew text.\n")
        self.assertEqual(v2.version, 2)
        self.assertEqual(self.stern.history("water-filters"), [1, 2])
        with self.assertRaises(PG.PageError):
            self.stern.sign("water-filters", MD, version=2)

    def test_restore_puts_old_text_back_as_a_new_version(self):
        self.stern.sign("water-filters", MD)
        self.stern.sign("water-filters", "# Water filters\n\nWrong advice.\n")
        v3 = self.stern.sign("water-filters", self.stern.old_text("water-filters", 1))
        self.assertEqual(v3.version, 3)
        self.assertEqual(v3.text(), MD)

    def test_delete_is_a_signed_higher_version(self):
        self.stern.sign("water-filters", MD)
        gone = PG.load(self.stern.sign("water-filters", None).to_bytes())
        self.assertTrue(gone.deleted)
        self.assertEqual((gone.version, gone.body, gone.text()), (2, b"", ""))

    def test_unlisted_page_can_be_deleted(self):
        name = PG.random_name()
        self.stern.sign(name, MD, visibility=PG.UNLISTED)
        gone = self.stern.sign(name, None)                 # no visibility given
        self.assertTrue(gone.deleted)
        self.assertEqual(gone.record["visibility"], PG.UNLISTED)

    def test_visibility_is_kept_for_a_name(self):
        name = PG.random_name()
        self.stern.sign(name, MD, visibility=PG.UNLISTED)
        with self.assertRaises(PG.PageError):
            self.stern.sign(name, MD, visibility=PG.PUBLIC)


class ForgeryTests(Base):
    """Nobody can publish as another author, and nobody can change a page
    after it's signed without readers noticing."""

    def setUp(self):
        super().setUp()
        self.page = self.stern.sign("water-filters", MD)
        self.blob = self.page.to_bytes()
        self.rec, self.sig, self.body = split(self.blob)

    def assertRefused(self, blob, why=""):
        with self.assertRaises(PG.PageError, msg=why):
            PG.load(blob)

    def test_every_single_byte_change_is_caught(self):
        for i in range(len(PG.MAGIC), len(self.blob)):
            bad = bytearray(self.blob)
            bad[i] ^= 0x01
            self.assertRefused(bytes(bad), f"byte {i}")

    def test_edited_record_with_old_signature(self):
        rec = json.loads(self.rec)
        rec["title"] = "Something else"
        self.assertRefused(build(PG._canonical(rec), self.sig, self.body))

    def test_mallory_resigns_sterns_address(self):
        mallory = ECC.generate(curve="Ed25519")
        rec = json.loads(self.rec)
        rec["key"] = mallory.public_key().export_key(format="raw").hex()
        rb = PG._canonical(rec)
        self.assertRefused(build(rb, sign_with(mallory, rb), self.body), "key must own the address")

    def test_mallory_signs_with_sterns_public_key_listed(self):
        mallory = ECC.generate(curve="Ed25519")
        self.assertRefused(build(self.rec, sign_with(mallory, self.rec), self.body))

    def test_body_swapped(self):
        other = zlib.compress(b"# Water filters\n\nDrink it straight from the river.\n")
        self.assertRefused(build(self.rec, self.sig, other))

    def test_signature_without_the_context_is_refused(self):
        raw_sig = eddsa.new(self.stern.key, "rfc8032").sign(self.rec)
        self.assertRefused(build(self.rec, raw_sig, self.body))

    def test_non_canonical_record_is_refused_even_if_signed(self):
        rec = json.loads(self.rec)
        rb = json.dumps(rec, sort_keys=True, indent=1).encode()
        self.assertRefused(build(rb, sign_with(self.stern.key, rb), self.body))

    def test_duplicate_or_unknown_fields_are_refused(self):
        dup = self.rec.replace(b'"title":', b'"title":"x","title":', 1)
        self.assertRefused(build(dup, sign_with(self.stern.key, dup), self.body))
        rec = json.loads(self.rec)
        rec["extra"] = 1
        rb = PG._canonical(rec)
        self.assertRefused(build(rb, sign_with(self.stern.key, rb), self.body))

    def test_locked_records_are_not_accepted_yet(self):
        rec = json.loads(self.rec)
        rec["lock"] = {"kdf": "scrypt"}
        rb = PG._canonical(rec)
        self.assertRefused(build(rb, sign_with(self.stern.key, rb), self.body))

    def test_truncated_and_junk(self):
        for cut in (0, 5, len(PG.MAGIC) + 2, len(self.blob) - 1):
            self.assertRefused(self.blob[:cut], f"cut at {cut}")
        self.assertRefused(b"hello")
        self.assertRefused(PG.MAGIC + struct.pack("<I", 10 ** 9))

    def test_compression_bomb_is_refused(self):
        bomb = zlib.compress(b"a" * (PG.MAX_TEXT + 10), 9)
        rec = json.loads(self.rec)
        rec["size"], rec["hash"] = len(bomb), hashlib.sha256(bomb).hexdigest()
        rb = PG._canonical(rec)
        self.assertRefused(build(rb, sign_with(self.stern.key, rb), bomb))


class ReplayTests(Base):
    """Old versions can't be replayed."""

    def test_vault_side_check(self):
        v1 = self.stern.sign("water-filters", MD)
        v2 = self.stern.sign("water-filters", "# Water filters\n\nFixed.\n")
        self.assertEqual(PG.check_newer(None, v1), "new")
        self.assertEqual(PG.check_newer(v1, v2), "newer")
        self.assertEqual(PG.check_newer(v2, PG.load(v2.to_bytes())), "same")
        with self.assertRaises(PG.PageError):
            PG.check_newer(v2, v1)

    def test_two_different_pages_with_one_version_are_refused(self):
        a = self.stern.sign("water-filters", MD)
        b = self.stern.sign("water-filters", "# Other\n", version=None)
        rec = json.loads(b.record_bytes)
        rec["version"] = 1
        rb = PG._canonical(rec)
        forked = PG.load(build(rb, sign_with(self.stern.key, rb), b.body))
        with self.assertRaises(PG.PageError):
            PG.check_newer(a, forked)

    def test_reader_remembers_the_newest_it_saw(self):
        v1 = self.stern.sign("water-filters", MD)
        v2 = self.stern.sign("water-filters", "# Water filters\n\nFixed.\n")
        seen = PG.SeenVersions(self.home)
        seen.accept(v2)
        seen.accept(v2)                                  # downloading it again is fine
        again = PG.SeenVersions(self.home)               # survives a restart
        with self.assertRaises(PG.PageError):
            again.accept(v1)


class AnonymityTests(Base):
    def test_anonymous_page_carries_nothing_of_the_main_identity(self):
        anon = self.authors.create(anonymous=True, note="clinic")
        page = anon.sign(PG.random_name(), MD, visibility=PG.UNLISTED)
        blob = page.to_bytes()
        record = page.record_bytes.decode()
        for trace in (self.stern.pub.hex(), self.stern.id, self.stern.fingerprint, "Stern"):
            self.assertNotIn(trace, record)
        self.assertNotIn(self.stern.pub, blob)
        self.assertEqual(page.record["author"], "")
        self.assertNotIn("clinic", record, "the private note must stay on the device")

    def test_anonymous_identities_are_separate_and_never_default(self):
        a1 = self.authors.create(anonymous=True)
        a2 = self.authors.create(anonymous=True)
        self.assertEqual(len({a1.id, a2.id, self.stern.id}), 3)
        self.assertEqual(self.authors.default().id, self.stern.id)
        with self.assertRaises(PG.PageError):
            self.authors.set_default(a1)

    def test_owner_found_by_address_for_updates(self):
        anon = self.authors.create(anonymous=True)
        name = PG.random_name()
        page = anon.sign(name, MD, visibility=PG.UNLISTED)
        self.assertEqual(self.authors.owner_of(page.address).id, anon.id)


class KeyStorageTests(Base):
    def test_backup_and_restore(self):
        f = self.home / "backup.json"
        self.authors.backup(self.stern, f, "correct horse battery staple")
        text = f.read_text()
        self.assertNotIn("BEGIN PRIVATE KEY", text)
        self.assertIn("BEGIN ENCRYPTED PRIVATE KEY", text)
        other = PG.Authors(Path(tempfile.mkdtemp()))
        with self.assertRaises(PG.PageError):
            other.restore(f, "wrong passphrase entirely")
        back = other.restore(f, "correct horse battery staple")
        self.assertEqual((back.id, back.name), (self.stern.id, "Stern"))
        page = back.sign("from-backup", MD)               # can still publish as Stern
        self.assertEqual(PG.load(page.to_bytes()).record["author"], "Stern")

    def test_short_backup_passphrase_refused(self):
        with self.assertRaises(PG.PageError):
            self.authors.backup(self.stern, self.home / "b.json", "short")

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_keys_and_history_are_private(self):
        self.stern.sign("water-filters", MD)
        for p in (self.stern.folder / "key.pem", self.stern.folder / "pages.json",
                  self.stern.folder / "history" / "water-filters" / "v1.md"):
            self.assertEqual(p.stat().st_mode & 0o077, 0, p)
        for d in (self.authors.folder, self.stern.folder, self.stern.folder / "history"):
            self.assertEqual(d.stat().st_mode & 0o077, 0, d)

    def test_find(self):
        self.assertEqual(self.authors.find("stern").id, self.stern.id)
        self.assertEqual(self.authors.find(self.stern.id[:6]).id, self.stern.id)
        self.assertEqual(self.authors.find(self.stern.fingerprint).id, self.stern.id)
        with self.assertRaises(PG.PageError):
            self.authors.find("nobody")


class CommandLineTests(unittest.TestCase):
    def test_make_update_restore_delete(self):
        import contextlib
        import io
        from moss.cli import main
        home = tempfile.mkdtemp()
        old = os.environ.get("MOSS_HOME")
        os.environ["MOSS_HOME"] = home
        self.addCleanup(lambda: os.environ.__setitem__("MOSS_HOME", old) if old
                        else os.environ.pop("MOSS_HOME", None))
        md = Path(home) / "wf.md"
        md.write_text(MD, encoding="utf-8")

        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                try:
                    main(list(argv))
                except SystemExit as exc:
                    code = exc.code
            return code, out.getvalue()

        self.assertEqual(run("author", "new", "Stern")[0], 0)
        code, out = run("page", "make", str(md))
        self.assertEqual(code, 0, out)
        address = next(w for w in out.split() if w.startswith("moss:"))
        self.assertTrue(address.endswith("/water-filters"))
        self.assertEqual(run("page", "update", address, str(md))[0], 0)
        self.assertEqual(run("page", "restore", address, "1")[0], 0)
        self.assertEqual(run("page", "delete", address)[0], 0)
        files = sorted((Path(home) / "outbox").glob("*.mosspage"))
        self.assertEqual(len(files), 4)
        code, out = run("page", "check", str(files[-1]))
        self.assertIn("signature OK", out)
        code, out = run("page", "make", str(md), "--unlisted", "--anonymous")
        self.assertEqual(code, 0, out)
        self.assertNotIn("heads up", out)
        code, out = run("page", "make", str(md), "--name", "other", "--unlisted")
        self.assertIn("heads up", out)


if __name__ == "__main__":
    unittest.main()
