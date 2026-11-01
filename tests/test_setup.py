"""Install, setup, update and radio check: the parts behind `moss` and the
one-line installers.

    python -m unittest discover -s tests -v      (from the repo root)
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_radio import Factory                     # noqa: E402
from moss import config, radiocheck, service, setup, updater   # noqa: E402


class HomeMixin:
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        patcher = mock.patch.dict(os.environ, {"MOSS_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)


class ConfigTests(HomeMixin, unittest.TestCase):
    def test_defaults_then_saved_values(self):
        self.assertEqual(config.load()["channel"], "#moss")
        self.assertIsNone(config.load()["role"])
        config.update(role="reader", port="COM12", junk="ignored")
        cfg = config.load()
        self.assertEqual((cfg["role"], cfg["port"], cfg["branch"]), ("reader", "COM12", "main"))
        self.assertNotIn("junk", cfg)

    def test_command_line_uses_saved_port_but_flags_win(self):
        from moss.cli import build_parser
        config.update(port="COM12", scope="#colorado")
        args = build_parser().parse_args(["search", "boulder"])
        self.assertEqual((args.port, args.scope), ("COM12", "#colorado"))
        args = build_parser().parse_args(["search", "boulder", "--port", "COM5"])
        self.assertEqual(args.port, "COM5")

    def test_broken_config_file_is_ignored(self):
        config.path().parent.mkdir(parents=True, exist_ok=True)
        config.path().write_text("{not json")
        self.assertEqual(config.load()["channel"], "#moss")


class ServiceSettingsTests(unittest.TestCase):
    OLD = ("# MOSS settings\n# The radio.\nMOSS_PORT=/dev/ttyACM0\n\n# library\n"
           "MOSS_ZIM=\"/media/moss/KIWIX/old.zim\"\nMOSS_EXTRA=--gap 0.5\nOTHER=kept\n")

    def test_read(self):
        f = Path(tempfile.mkdtemp()) / "gateway.env"
        f.write_text(self.OLD)
        env = service.read_env(f)
        self.assertEqual(env["MOSS_PORT"], "/dev/ttyACM0")
        self.assertEqual(env["MOSS_ZIM"], "/media/moss/KIWIX/old.zim")
        self.assertEqual(env["MOSS_EXTRA"], "--gap 0.5")
        self.assertEqual(service.read_env(f.with_name("missing")), {})

    def test_render_keeps_comments_and_other_lines(self):
        new = service.render_env("/dev/serial/by-id/radio", "/media/x/new.zim", "--gap 0.5", self.OLD)
        self.assertIn("# MOSS settings", new)
        self.assertIn("MOSS_PORT=/dev/serial/by-id/radio\n", new)
        self.assertIn("MOSS_ZIM=/media/x/new.zim\n", new)
        self.assertIn("OTHER=kept", new)
        self.assertNotIn("ttyACM0", new)
        self.assertEqual(new.count("MOSS_PORT="), 1)

    def test_render_from_the_shipped_template(self):
        new = service.render_env("/dev/serial/by-id/radio", "/media/x/w.zim")
        self.assertIn("MOSS_PORT=/dev/serial/by-id/radio\n", new)
        self.assertIn("MOSS_EXTRA=\n", new)


class FindTests(unittest.TestCase):
    def test_find_zims_biggest_first_and_not_too_deep(self):
        root = Path(tempfile.mkdtemp())
        (root / "moss" / "KIWIX").mkdir(parents=True)
        big = root / "moss" / "KIWIX" / "wikipedia.zim"
        big.write_bytes(b"x" * 5000)
        small = root / "mini.zim"
        small.write_bytes(b"x" * 10)
        deep = root / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "too-deep.zim").write_bytes(b"x")
        self.assertEqual(setup.find_zims([root]), [big, small])


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


class UpdateTests(HomeMixin, unittest.TestCase):
    """moss update only ever moves forward, and never loses local edits."""

    def setUp(self):
        super().setUp()
        base = Path(tempfile.mkdtemp())
        self.src = base / "src"
        self.src.mkdir()
        git(self.src, "init", "-q", "-b", "main")
        (self.src / "a.txt").write_text("one\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-qm", "first")
        git(self.src, "branch", "experimental")
        self.origin = base / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.src), str(self.origin)], check=True)
        self.repo = base / "MOSS"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True)
        self.out = []

    def publish(self, branch, name, text):
        git(self.src, "checkout", "-q", branch)
        (self.src / name).write_text(text)
        git(self.src, "add", "-A")
        git(self.src, "commit", "-qm", f"change {name}")
        git(self.src, "push", "-q", str(self.origin), branch)
        git(self.src, "checkout", "-q", "main")

    def run_update(self, branch=None):
        return updater.update(branch, repo=self.repo, out=self.out.append,
                              install_deps=False, restart=False)

    def test_up_to_date(self):
        self.assertFalse(self.run_update())
        self.assertTrue(any("already up to date" in m for m in self.out))

    def test_moves_forward(self):
        self.publish("main", "b.txt", "new\n")
        self.assertTrue(self.run_update())
        self.assertEqual((self.repo / "b.txt").read_text(), "new\n")

    def test_local_edits_are_stashed_not_lost(self):
        self.publish("main", "b.txt", "new\n")
        (self.repo / "a.txt").write_text("my own edit\n")
        self.run_update()
        stash = subprocess.run(["git", "-C", str(self.repo), "stash", "list"],
                               capture_output=True, text=True).stdout
        self.assertIn("set aside by moss update", stash)
        git(self.repo, "stash", "pop")
        self.assertEqual((self.repo / "a.txt").read_text(), "my own edit\n")

    def test_refuses_when_this_copy_has_its_own_commits(self):
        self.publish("main", "b.txt", "new\n")
        (self.repo / "mine.txt").write_text("x")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "local only")
        with self.assertRaises(updater.UpdateError):
            self.run_update()
        self.assertTrue((self.repo / "mine.txt").exists())

    def test_switch_to_experimental_and_back(self):
        self.publish("experimental", "exp.txt", "testing\n")
        self.run_update("experimental")
        self.assertTrue((self.repo / "exp.txt").exists())
        self.assertEqual(config.load()["branch"], "experimental")
        self.run_update("main")
        self.assertFalse((self.repo / "exp.txt").exists())
        self.assertEqual(config.load()["branch"], "main")

    def test_not_a_checkout(self):
        with self.assertRaises(updater.UpdateError):
            updater.update(repo=Path(tempfile.mkdtemp()), out=self.out.append, install_deps=False)


class RadioCheckTests(HomeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.factory = Factory()
        self.addCleanup(self.factory.install())
        self.out = []

    def test_send_pings_on_moss(self):
        sent = asyncio.run(radiocheck.send("COM12", count=3, gap=0, out=self.out.append))
        self.assertEqual(sent, 3)
        radio = self.factory.radios[0]
        slot = next(i for i, (n, _) in enumerate(self.factory.slots) if n == "#moss")
        self.assertEqual([s for s, _ in radio.chat_sent], [slot] * 3)
        self.assertIn("moss ping 1/3 from Fake Heltec", radio.chat_sent[0][1])
        self.assertTrue(any("910.525 MHz" in m and "SF7" in m for m in self.out))

    def test_listen_hears_only_moss(self):
        async def go():
            task = asyncio.create_task(radiocheck.listen("COM12", seconds=5, out=self.out.append,
                                                         until=2))
            while not self.factory.radios:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            radio = self.factory.radios[0]
            slot = next(i for i, (n, _) in enumerate(self.factory.slots) if n == "#moss")
            radio.inbox += [(0, "public chatter"), (slot, "moss ping 1/2"), (slot, "moss ping 2/2")]
            return await task

        heard = asyncio.run(asyncio.wait_for(go(), 20))
        self.assertEqual(heard, ["moss ping 1/2", "moss ping 2/2"])
        self.assertTrue(any("SNR 6.5 dB" in m for m in self.out))

    def test_vault_is_paused_and_restarted(self):
        config.update(role="vault")
        calls = []
        with mock.patch.object(service, "available", return_value=True), \
                mock.patch.object(service, "is_active", return_value=True), \
                mock.patch.object(service, "control", side_effect=lambda a: calls.append(a) or True):
            asyncio.run(radiocheck.info("COM12", out=self.out.append))
        self.assertEqual(calls, ["stop", "start"])

    def test_describe(self):
        self.assertEqual(radiocheck.describe({"name": "H", "radio_freq": 910.525, "radio_bw": 62.5,
                                              "radio_sf": 7, "radio_cr": 5}),
                         "H · 910.525 MHz · BW 62.5 kHz · SF7 · CR5")


class SetupTests(HomeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import io
        from rich.console import Console
        from moss import ui
        quiet = mock.patch.object(ui, "console", Console(file=io.StringIO()))
        quiet.start()
        self.addCleanup(quiet.stop)
        self.factory = Factory()
        self.addCleanup(self.factory.install())

    def test_reader_without_questions(self):
        self.assertEqual(setup.run(role="reader", port="COM12", yes=True), 0)
        cfg = config.load()
        self.assertEqual((cfg["role"], cfg["port"]), ("reader", "COM12"))
        self.assertTrue(any(n == "#moss" for n, _ in self.factory.slots), "setup should create #moss")

    def test_vault_keeps_existing_keys(self):
        from moss.seal import VaultKeys
        before = VaultKeys.load_or_create(self.home / "seed-vault").fingerprint
        zim = self.home / "w.zim"
        zim.write_bytes(b"x")
        with mock.patch.object(service, "available", return_value=True), \
                mock.patch.object(service, "read_env", return_value={}):
            code = setup.run(role="vault", port="COM12", zim=str(zim), yes=True,
                             install_service=False)
        self.assertEqual(code, 0)
        self.assertEqual(VaultKeys.load_or_create(self.home / "seed-vault").fingerprint, before)
        self.assertEqual(config.load()["role"], "vault")

    def test_vault_uses_old_settings_as_defaults(self):
        old = {"MOSS_PORT": "COM12", "MOSS_ZIM": str(self.home / "old.zim"), "MOSS_EXTRA": "--gap 0.5"}
        saved = []
        with mock.patch.object(service, "available", return_value=True), \
                mock.patch.object(service, "read_env", return_value=old), \
                mock.patch.object(service, "is_active", return_value=False), \
                mock.patch.object(service, "save_settings", side_effect=lambda *a: saved.append(a) or True), \
                mock.patch.object(service, "install", return_value=True), \
                mock.patch.object(service, "control", return_value=True):
            self.assertEqual(setup.run(role="vault", yes=True), 0)
        self.assertEqual(saved, [("COM12", str(self.home / "old.zim"), "--gap 0.5")])

    def test_bad_radio_port_fails_cleanly(self):
        self.factory.port_held = True                     # someone else has it
        with mock.patch("moss.transport.asyncio.sleep", new=_no_sleep):
            code = setup.run(role="vault", port="COM12", zim="x.zim", yes=True, install_service=False)
        self.assertEqual(code, 1)


async def _no_sleep(*_a, **_k):
    return None


class InstallerScriptTests(unittest.TestCase):
    def test_shell_installer_parses(self):
        r = subprocess.run(["bash", "-n", str(ROOT / "install.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_installers_point_at_the_real_repo(self):
        for f in ("install.sh", "install.ps1"):
            self.assertIn("github.com/Obsideaock/MOSS", (ROOT / f).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
