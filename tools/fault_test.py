"""Break a real transfer on purpose, over real radios, in chosen ways.

The simulated tests prove the state machines are correct. This proves the
whole stack is — real firmware, real serial link, real airtime — while
causing specific failures instead of waiting for luck to produce them.

It wraps the radio and interferes with what goes out: spores that never get
sent, spores sent with a flipped byte, a manifest that goes missing, a
transfer that dies halfway. The receiver on the other machine runs the
ordinary `page_transfer.py --recv` and knows nothing about any of it.

    python fault_test.py --list
    python fault_test.py --port COM16 --channel 1 --scenario drop-every-5
    python fault_test.py --port COM16 --channel 1 --all

On the other machine, for every scenario:

    python page_transfer.py --port COM12 --channel 1 --recv --dir received

Each fault hits a spore ONCE, the way real interference does, so repair
rounds can still recover. Every scenario should end with a file whose hash
matches; `corrupt-one` and `truncate-one` must visibly reject the bad data
first, and `die-midway` needs a second run to finish. Pass --persist to
break the same spore forever and watch the give-up path instead.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import struct
import time
from pathlib import Path

import page_transfer as pt


class FaultyRadio:
    """Wraps a real radio and breaks what goes out, by rule."""

    def __init__(self, inner, rules: dict, persist: bool = False,
                 verbose: bool = False):
        self._inner = inner
        self.rules = rules
        self.verbose = verbose
        self.persist = persist      # keep breaking the same spore forever?
        self.commands = self
        self.counts = {"sent": 0, "dropped": 0, "corrupted": 0,
                       "duplicated": 0, "truncated": 0}
        self.chunk_seen = 0
        self.already: set[tuple[str, int]] = set()

    def once(self, kind: str, index: int) -> bool:
        """True the first time a given spore is broken this way.

        Real interference hits a transmission, not a spore forever — if the
        fault repeated on every retry, no repair round could ever succeed
        and we'd be testing nothing but the give-up path.
        """
        if self.persist:
            return True
        key = (kind, index)
        if key in self.already:
            return False
        self.already.add(key)
        return True

    def __getattr__(self, name):
        """Forward anything we don't override.

        A real MeshCore keeps commands on `mc.commands`, while this wrapper
        sets `self.commands = self` so sends come through here. So a lookup
        has to try the radio first AND its command handler — otherwise
        `get_msg` goes missing and the sender never hears the receiver.
        """
        inner = object.__getattribute__(self, "_inner")
        try:
            return getattr(inner, name)
        except AttributeError:
            commands = getattr(inner, "commands", None)
            if commands is not None and hasattr(commands, name):
                return getattr(commands, name)
            raise

    async def get_msg(self, *args, **kwargs):
        return await self._inner.commands.get_msg(*args, **kwargs)

    async def send_channel_data(self, chan, data_type, payload):
        kind = payload[0]
        index = None
        if kind == pt.CHUNK:
            index = struct.unpack("<BHH", payload[:5])[2]
            self.chunk_seen += 1

        for rule in self.rules.get("drop_kinds", []):
            if kind == rule:
                self.counts["dropped"] += 1
                print(f"    [fault] dropped {name_of(kind)}")
                return ok()

        if kind == pt.CHUNK:
            if index in self.rules.get("drop_indices", ()) and self.once("drop", index):
                self.counts["dropped"] += 1
                print(f"    [fault] dropped spore {index}")
                return ok()
            if (self.rules.get("drop_every")
                    and index % self.rules["drop_every"] == 0
                    and self.once("drop", index)):
                self.counts["dropped"] += 1
                print(f"    [fault] dropped spore {index}")
                return ok()
            if (random.random() < self.rules.get("drop_chance", 0.0)
                    and self.once("drop", index)):
                self.counts["dropped"] += 1
                print(f"    [fault] dropped spore {index}")
                return ok()
            if (index in self.rules.get("corrupt_indices", ())
                    and self.once("corrupt", index)):
                body = bytearray(payload)
                body[-1] ^= 0xFF              # flip a bit in the content
                payload = bytes(body)
                self.counts["corrupted"] += 1
                print(f"    [fault] corrupted spore {index}")
            if (index in self.rules.get("truncate_indices", ())
                    and self.once("truncate", index)):
                payload = payload[:pt.CHUNK_HEADER + 10]
                self.counts["truncated"] += 1
                print(f"    [fault] truncated spore {index}")

        result = await self._inner.commands.send_channel_data(chan, data_type, payload)
        self.counts["sent"] += 1
        status = getattr(result, "type", result)
        if self.verbose or str(status).endswith("ERROR"):
            extra = f"  <- {getattr(result, 'payload', None)!r}" if \
                str(status).endswith("ERROR") else ""
            label = name_of(kind) + (f" {index}" if index is not None else "")
            print(f"    [radio] {label:<14} {len(payload):3d} bytes  "
                  f"{status}{extra}")

        if kind == pt.CHUNK and index in self.rules.get("duplicate_indices", ()):
            await asyncio.sleep(0.2)
            await self._inner.commands.send_channel_data(chan, data_type, payload)
            self.counts["duplicated"] += 1
            print(f"    [fault] sent spore {index} twice")

        if (self.rules.get("die_after") is not None
                and self.chunk_seen >= self.rules["die_after"]):
            raise PowerCut(f"simulated power loss after {self.chunk_seen} spores")
        return result


class PowerCut(Exception):
    """Stands in for the transmitter losing power mid-transfer."""


def ok():
    import types
    return types.SimpleNamespace(type="ok", payload={})


def name_of(kind: int) -> str:
    return {pt.MANIFEST: "MANIFEST", pt.CHUNK: "CHUNK", pt.NACK: "NACK",
            pt.DONE: "DONE", pt.ROUND_END: "ROUND_END",
            pt.READY: "READY"}.get(kind, str(kind))


SCENARIOS: dict[str, tuple[str, dict]] = {
    "clean": (
        "no faults — the control run", {}),
    "drop-every-5": (
        "lose every 5th spore; repair rounds must recover them",
        {"drop_every": 5}),
    "drop-random-30": (
        "lose 30% of spores at random",
        {"drop_chance": 0.3}),
    "drop-first-manifest": (
        "the first manifest never goes out; the retry must save it",
        {"drop_kinds": [], "drop_first_manifest": True}),
    "drop-round-end": (
        "ROUND_END is lost; the receiver's quiet timer must cover for it",
        {"drop_kinds": [pt.ROUND_END]}),
    "corrupt-one": (
        "one spore arrives with a flipped byte; the hash MUST reject it",
        {"corrupt_indices": {3}}),
    "truncate-one": (
        "one spore arrives short; reassembly must notice",
        {"truncate_indices": {4}}),
    "duplicate-some": (
        "some spores arrive twice; duplicates must be harmless",
        {"duplicate_indices": {1, 5, 9}}),
    "die-midway": (
        "the transmitter loses power partway; rerun to resume",
        {"die_after": 8}),
    "lose-last": (
        "the final spore never arrives on the first pass",
        {"drop_indices": {-1}}),
}


class FirstManifestDropper(FaultyRadio):
    """Swallows only the first manifest, letting retries through."""

    def __init__(self, inner, rules, persist: bool = False,
                 verbose: bool = False):
        super().__init__(inner, rules, persist, verbose)
        self.manifests = 0

    async def send_channel_data(self, chan, data_type, payload):
        if payload[0] == pt.MANIFEST:
            self.manifests += 1
            if self.manifests == 1:
                self.counts["dropped"] += 1
                print("    [fault] dropped the first MANIFEST")
                return ok()
        return await super().send_channel_data(chan, data_type, payload)


async def run_scenario(mc, name: str, args) -> None:
    description, rules = SCENARIOS[name]
    rules = dict(rules)

    path = Path(args.file)
    blob, _ = pt.pack(path.read_bytes(), not args.no_zlib)
    total = max(1, -(-len(blob) // pt.CHUNK_BYTES))
    if -1 in rules.get("drop_indices", ()):          # "the last one"
        rules["drop_indices"] = {total - 1}

    print(f"\n{'=' * 66}\n{name}: {description}\n{'=' * 66}")
    print(f"{path.name}: {total} spores, about "
          f"{pt.fmt_time(pt.estimate_seconds(total, args.gap))}")

    wrapper_class = (FirstManifestDropper if rules.pop("drop_first_manifest", False)
                     else FaultyRadio)
    faulty = wrapper_class(mc, rules, persist=args.persist,
                           verbose=args.verbose)

    started = time.monotonic()
    try:
        await pt.send_page(faulty, args.channel, path, args.gap, args.rounds,
                           args.data_type, not args.no_zlib, args.wait,
                           args.handshake)
    except PowerCut as exc:
        print(f"\n  *** {exc}")
        print("  Run this scenario again (or any normal send) — the receiver "
              "kept its spores and the sender will ask what's still needed.")

    elapsed = time.monotonic() - started
    c = faulty.counts
    print(f"\n  {elapsed:.0f}s, {c['sent']} datagrams actually transmitted, "
          f"{c['dropped']} dropped, {c['corrupted']} corrupted, "
          f"{c['truncated']} truncated, {c['duplicated']} duplicated")
    if name in ("corrupt-one", "truncate-one"):
        print("  Check the receiving machine: it must REJECT the bad spore "
              "(hash mismatch), then recover on a later round and end with a "
              "matching file. A written file with a wrong hash is a failure.")
    else:
        print("  Check the receiving machine: the file should be written with "
              "a matching hash.")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--channel", type=int)
    # Use forward slashes in paths: "TestExamples/sample_page.md" works on
    # Windows too, and avoids backslash escape warnings.
    ap.add_argument("--file", default="sample_page.md")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--all", action="store_true", help="run every scenario in turn")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--gap", type=float, default=1.0)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--wait", type=float, default=15.0)
    ap.add_argument("--handshake", type=float, default=4.0)
    ap.add_argument("--pause", type=float, default=20.0,
                    help="seconds between scenarios in --all")
    ap.add_argument("--no-zlib", action="store_true")
    ap.add_argument("--data-type", type=lambda s: int(s, 0), default=pt.MOSS_DATA_TYPE)
    ap.add_argument("--verbose", action="store_true",
                    help="print the radio's response to every datagram sent")
    ap.add_argument("--persist", action="store_true",
                    help="keep breaking the same spore on every retry, so the "
                         "transfer can never finish (tests the give-up path)")
    ap.add_argument("--seed", type=int,
                    help="fix the random seed so a run is repeatable; "
                         "omit it and both the scenario choice and the "
                         "random drops vary each time")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    if args.list:
        print("scenarios:\n")
        for name, (description, _) in sorted(SCENARIOS.items()):
            print(f"  {name:22} {description}")
        print("\nOmit --scenario and one is picked at random.")
        print("\nStart the receiver on the other machine first:")
        print("  python page_transfer.py --channel 1 --recv --dir received")
        return

    if args.channel is None:
        raise SystemExit("--channel is required")

    # No scenario named? Pick one. Testing what you didn't choose is how you
    # find the failure you weren't expecting.
    if not args.scenario and not args.all:
        pool = [n for n in sorted(SCENARIOS) if n != "clean"]
        args.scenario = random.choice(pool)
        print(f"no scenario given — picked {args.scenario} at random "
              f"(--scenario to choose, --list to see them all)")
    if not Path(args.file).exists():
        raise SystemExit(f"no such file: {args.file}")

    port = args.port or pt.pick_port()
    mc = await pt.open_radio(port, args.baud)
    print(f"connected to {port}")

    names = sorted(SCENARIOS) if args.all else [args.scenario]
    try:
        for n, name in enumerate(names):
            await run_scenario(mc, name, args)
            if n < len(names) - 1:
                print(f"\n  pausing {args.pause:.0f}s before the next scenario — "
                      f"restart the receiver if it exited")
                await asyncio.sleep(args.pause)
    finally:
        await mc.disconnect()
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")