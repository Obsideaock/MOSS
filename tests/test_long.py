"""Full-length articles and the repair paths they lean on.

    python -m unittest discover -s tests -v      (from the repo root)
"""

import asyncio
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from moss import protocol as P                  # noqa: E402
from moss.cache import Cache                    # noqa: E402
from moss.client import MossClient              # noqa: E402
from moss.gateway import Gateway                # noqa: E402
from moss.sources import LocalSource            # noqa: E402
from moss.transport import SimNetwork           # noqa: E402

FIXTURES = ROOT / "TestExamples"


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 90))


def long_article_folder() -> Path:
    """~110 KB of text (about 240 packets), like Quantum mechanics."""
    words = " ".join((FIXTURES / f).read_text(encoding="utf-8")
                     for f in ("Shrubbies.md", "Cardboard.md")).split()
    rng = random.Random(3)
    paras: list[str] = []
    while sum(map(len, paras)) < 110_000:
        paras.append(" ".join(rng.choice(words) for _ in range(rng.randint(40, 120))) + ".")
    folder = Path(tempfile.mkdtemp())
    body = "\n\n".join(f"## Section {i}\n\n{p}" if i % 6 == 0 else p
                       for i, p in enumerate(paras))
    (folder / "long.md").write_text("# Long article\n\n" + body + "\n", encoding="utf-8")
    return folder


async def fetch(folder, title, loss=0.0, seed=1, drop_client=None, log=None):
    net = SimNetwork(loss=loss, seed=seed)
    gtx, ctx = net.endpoint("gw"), net.endpoint("client")
    if drop_client:
        ctx.drop_next.extend(drop_client)
    # Same ratios as the real defaults: gateway wait ~5x the client's quiet.
    gw = Gateway(gtx, LocalSource(folder, None), gap=0.0, wait=2.5, settle=0.1,
                 log=(log.append if log is not None else lambda m: None))
    await gw.start()
    client = MossClient(ctx, Cache(tempfile.mkdtemp()), quiet=0.4, request_timeout=1.0,
                        fetch_timeout=5.0, idle=8.0, manifest_retry=0.2, nack_spacing=0.05)
    try:
        return await client.article(title)
    finally:
        await gw.stop()


class LongArticleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = long_article_folder()
        cls.expected = run(LocalSource(cls.folder, None).article("Long article"))[1]

    def test_full_article_is_not_shortened(self):
        page = run(fetch(self.folder, "Long article"))
        self.assertEqual(page.text, self.expected)
        self.assertNotIn("shortened", page.text)

    def test_full_article_survives_heavy_loss(self):
        for seed in (1, 2, 12):                  # 12 used to strand the last few packets
            page = run(fetch(self.folder, "Long article", loss=0.25, seed=seed))
            self.assertEqual(page.text, self.expected, f"seed {seed}")


class LostManifestTests(unittest.TestCase):
    def test_manifest_lost_twice_keeps_early_chunks(self):
        """The first manifest and its re-send both vanish. The client must keep
        the chunks it heard meanwhile and only ask for the ones it lacks."""
        is_manifest = lambda raw: raw[0] == P.MANIFEST   # noqa: E731
        log: list[str] = []
        page = run(fetch(FIXTURES, "Shrubbies",
                         drop_client=[is_manifest, is_manifest], log=log))
        self.assertIn("The Shrubbies were an English pop group", page.text)
        repairs = [line for line in log if "repair" in line]
        self.assertFalse(repairs, f"chunks heard before the manifest were thrown away: {repairs}")


if __name__ == "__main__":
    unittest.main()
