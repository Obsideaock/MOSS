"""Hardware test: this laptop's radio against your real Seed Vault.

    python tools/hardware_test.py                 # uses the port `moss setup` saved
    python tools/hardware_test.py --port COM12

The same as `moss selftest`: search, upload a throwaway unlisted test page,
read it back, upload a second version, check an old copy is refused, delete
it. Takes a few minutes of airtime. The Seed Vault must be running the same
MOSS version (moss update on both).

For radio range alone, use `moss radio-check listen` / `send` instead.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moss.cli import main  # noqa: E402

if __name__ == "__main__":
    main(["selftest", *sys.argv[1:]])
