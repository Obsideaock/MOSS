"""Private reading: a listener on the channel learns nothing about what's read.

Every test runs a real exchange over the simulated radio with a third
station, the listener, recording everything sent. It is then given its best
shot: reassemble the spores, decompress them, search for the words.

    python -m unittest discover -s tests -v      (from the repo root)
"""

import asyncio
import shutil
import sys
import tempfile
import unittest
import zlib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from moss import protocol as P                  # noqa: E402
from moss import seal as S                      # noqa: E402
from moss.cache import Cache                    # noqa: E402
from moss.client import MossClient, MossError   # noqa: E402
from moss.gateway import Gateway                # noqa: E402
from moss.sources import LocalSource            # noqa: E402
from moss.transport import SimNetwork           # noqa: E402

FIXTURES = ROOT / "TestExamples"
SECRET_QUERY = "shrubbies band memphis"
SECRET_TITLE = "Shrubbies"
SECRET_WORDS = ["Shrubbies", "Memphis in Texas", "Craig Fortnam", "shrubbies band"]
CLIENT_TIMING = dict(quiet=0.4, request_timeout=1.0, fetch_timeout=5.0, idle=8.0,
                     manifest_retry=0.2, nack_spacing=0.05)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


def listener_recovers(heard: list[bytes]) -> str:
    """What an eavesdropper can reconstruct: every chunk stream it heard,
    reassembled in order and decompressed if possible, plus the raw bytes."""
    streams = defaultdict(dict)
    for raw in heard:
        if raw and raw[0] == P.CHUNK and len(raw) > P.CHUNK_HEADER:
            tid, index, payload = P.parse_chunk(raw)
            streams[tid][index] = payload
    out = [raw.decode("latin-1") for raw in heard]
    for chunks in streams.values():
        blob = b"".join(chunks[i] for i in sorted(chunks))
        for candidate in (blob, blob[2:]):
            try:
                out.append(zlib.decompress(candidate).decode("utf-8", "replace"))
            except zlib.error:
                pass
        out.append(blob.decode("latin-1"))
    return "\n".join(out)


class Station:
    """A Seed Vault, a reader and a listener on one simulated channel."""

    def __init__(self, sealed=True, loss=0.0, seed=1, pad_sizes=False, log_queries=False):
        self.tmp = Path(tempfile.mkdtemp())
        self.net = SimNetwork(loss=loss, seed=seed)
        self.vault_tx, self.reader_tx = self.net.endpoint("vault"), self.net.endpoint("reader")
        self.heard: list[bytes] = []
        self.net.endpoint("listener").subscribe(self.heard.append)
        self.keys = S.VaultKeys.load_or_create(self.tmp / "vault")
        self.log: list[str] = []
        self.vault = Gateway(self.vault_tx, LocalSource(FIXTURES, None), gap=0.0, wait=2.5,
                             settle=0.1, log=self.log.append, keys=self.keys,
                             pad_sizes=pad_sizes, log_queries=log_queries)
        self.keyring = S.Keyring(self.tmp / "reader") if sealed else None
        self.reader = MossClient(self.reader_tx, Cache(self.tmp / "cache"), keyring=self.keyring,
                                 **CLIENT_TIMING)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class EavesdropperTests(unittest.TestCase):
    def exchange(self, sealed, **kw):
        st = Station(sealed=sealed, **kw)

        async def go():
            await st.vault.start()
            try:
                found = await st.reader.search(SECRET_QUERY)
                page = await st.reader.article(SECRET_TITLE)
                with self.assertRaises(MossError) as missing:
                    await st.reader.article("Secret Missing Topic")
                return found, page, str(missing.exception)
            finally:
                await st.vault.stop()

        try:
            return (*run(go()), st)
        finally:
            st.close()

    def test_plain_mode_leaks_everything(self):
        """Sanity check of the listener: without encryption it reads it all."""
        _, page, _, st = self.exchange(sealed=False)
        recovered = listener_recovers(st.heard)
        for word in SECRET_WORDS + ["Secret Missing Topic"]:
            self.assertIn(word, recovered)

    def test_sealed_mode_leaks_nothing(self):
        found, page, missing, st = self.exchange(sealed=True, loss=0.2, seed=3)
        self.assertEqual(found.results[0].title, "Shrubbies")
        self.assertIn("Memphis in Texas", page.text)
        self.assertIn("no such article", missing)            # the reader still gets the error
        recovered = listener_recovers(st.heard)
        for word in SECRET_WORDS + ["Secret Missing Topic", "no such article"]:
            self.assertNotIn(word, recovered, f"{word!r} leaked to the listener")
            self.assertNotIn(word.lower(), recovered.lower(), f"{word!r} leaked to the listener")

    def test_sealed_traffic_uses_only_sealed_messages(self):
        _, _, _, st = self.exchange(sealed=True)
        kinds = {raw[0] for raw in st.heard}
        self.assertFalse(kinds & {P.REQUEST, P.MANIFEST, P.ERROR}, "a plain message went out")
        requests = [raw for raw in st.heard if raw[0] == S.SREQUEST]
        self.assertTrue(requests)
        self.assertEqual({len(r) for r in requests}, {P.MAX_DATAGRAM},
                         "every request must be the same size, whatever was asked")

    def test_vault_log_keeps_no_titles(self):
        _, _, _, st = self.exchange(sealed=True)
        log = "\n".join(st.log)
        self.assertIn("encrypted; not logged", log)
        for word in SECRET_WORDS + ["Secret Missing Topic"]:
            self.assertNotIn(word, log)
        _, _, _, st = self.exchange(sealed=True, log_queries=True)
        self.assertIn("Shrubbies", "\n".join(st.log))


class TrustTests(unittest.TestCase):
    def test_first_vault_is_trusted_and_remembered(self):
        st = Station()

        async def go():
            await st.vault.start()
            try:
                await st.reader.article("Cardboard")
            finally:
                await st.vault.stop()

        run(go())
        self.assertEqual(S.Keyring(st.tmp / "reader").fingerprint, st.keys.fingerprint)
        st.close()

    def test_pinned_fingerprint_refuses_other_vaults(self):
        st = Station()
        st.keyring.pin("0000-0000-0000-0000")

        async def go():
            await st.vault.start()
            try:
                await st.reader.article("Cardboard")
            finally:
                await st.vault.stop()

        with self.assertRaisesRegex(MossError, "isn't the one you pinned"):
            run(go())
        self.assertNotIn(S.SREQUEST, {raw[0] for raw in st.heard}, "nothing may be sent to it")
        st.close()

    def test_changed_vault_key_is_explained(self):
        st = Station()

        async def go():
            await st.vault.start()
            await st.reader.article("Cardboard")
            await st.vault.stop()
            # The Pi is reflashed: same place, new keys.
            st.vault.keys = S.VaultKeys.load_or_create(st.tmp / "new-vault")
            await st.vault.start()
            try:
                await st.reader.article("Shrubbies", refresh=True)
            finally:
                await st.vault.stop()

        with self.assertRaisesRegex(MossError, "different key.*trust --forget"):
            run(go())
        st.close()

    def test_impostor_cannot_serve_pages(self):
        """A second vault on the channel can't read requests meant for the
        trusted one, so it can't answer them."""
        st = Station()
        impostor_log: list[str] = []
        impostor = Gateway(st.net.endpoint("impostor"), LocalSource(FIXTURES, None), gap=0.0,
                           wait=2.5, settle=0.1, log=impostor_log.append,
                           keys=S.VaultKeys.load_or_create(st.tmp / "impostor"))

        async def go():
            await st.vault.start()
            st.keyring.offer(st.keys.x_pub, st.keys.ed_pub, "Seed Vault")   # already trusted
            await impostor.start()
            try:
                return await st.reader.article("Cardboard")
            finally:
                await st.vault.stop()
                await impostor.stop()

        page = run(go())
        self.assertIn("Cardboard", page.text)
        self.assertFalse([m for m in impostor_log if "article" in m], "the impostor read a request")
        st.close()

    def test_forged_spore_is_blight(self):
        """Someone swaps in a fake spore: the page is refused, not shown."""
        st = Station()
        forger = st.net.endpoint("forger")

        async def forge(raw):
            if raw and raw[0] == S.SCAPSULE:     # the transfer is starting: inject spore 0
                await forger.send(P.build_chunk(P.peek_tid(raw), 0, b"\x00" * P.CHUNK_BYTES))
        forger.subscribe(forge)
        # The real spore 0 doesn't reach the reader, so the forged one is what it keeps.
        st.reader_tx.drop_next.append(
            lambda raw: raw[0] == P.CHUNK and P.parse_chunk(raw)[1] == 0 and len(raw) > 6
            and raw[P.CHUNK_HEADER:] != b"\x00" * P.CHUNK_BYTES)

        async def go():
            await st.vault.start()
            try:
                await st.reader.article("Shrubbies")
            finally:
                await st.vault.stop()

        with self.assertRaisesRegex(MossError, "blighted"):
            run(go())
        self.assertIsNone(Cache(st.tmp / "cache").get_article("Shrubbies"), "a forged page was saved")
        st.close()


class PaddingTests(unittest.TestCase):
    def test_off_by_default(self):
        st = Station()
        self.assertFalse(st.vault.pad_sizes)
        st.close()

    def test_padded_sizes_are_standard_and_page_intact(self):
        counts = {}
        for pad in (False, True):
            st = Station(pad_sizes=pad)

            async def go():
                await st.vault.start()
                try:
                    return await st.reader.article("Shrubbies")
                finally:
                    await st.vault.stop()

            page = run(go())
            self.assertIn("Memphis in Texas", page.text)
            counts[pad] = len({P.parse_chunk(r)[1] for r in st.heard if r[0] == P.CHUNK})
            st.close()
        self.assertEqual(counts[True], S.padded_spores(counts[False]))
        self.assertGreaterEqual(counts[True], counts[False])

    def test_size_steps(self):
        steps = [S.padded_spores(n) for n in range(1, 60)]
        self.assertEqual(sorted(set(steps))[:12], [1, 2, 3, 4, 5, 6, 8, 10, 13, 16, 20, 25])
        overhead = sum(S.padded_spores(n) / n for n in range(1, 400)) / 399 - 1
        self.assertLess(overhead, 0.15)


class KeyTests(unittest.TestCase):
    def test_keys_persist_and_fingerprint_is_stable(self):
        tmp = Path(tempfile.mkdtemp())
        a = S.VaultKeys.load_or_create(tmp)
        b = S.VaultKeys.load_or_create(tmp)
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertRegex(a.fingerprint, r"^[0-9A-F]{4}(-[0-9A-F]{4}){3}$")
        shutil.rmtree(tmp)

    def test_fingerprint_typing_is_forgiving(self):
        self.assertEqual(S.normalize_fingerprint("3f9a 12c4 77e0 5b21"), "3F9A-12C4-77E0-5B21")
        with self.assertRaises(ValueError):
            S.normalize_fingerprint("3F9A")

    def test_long_titles_fit(self):
        keys = S.VaultKeys.load_or_create(tempfile.mkdtemp())
        title = "List of " + "very " * 40 + "long titles"
        r = S.ReaderSession(keys.x_pub, 7, P.KIND_ARTICLE, title)
        self.assertEqual(len(r.request), P.MAX_DATAGRAM)
        _, _, query, _ = keys.open_request(r.request)
        self.assertEqual(query, title.encode()[:S.MAX_SEALED_QUERY].decode())


if __name__ == "__main__":
    unittest.main()
