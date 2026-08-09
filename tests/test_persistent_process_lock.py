from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.persistent_process_lock import acquire_persistent_live_process_lock


@unittest.skipUnless(os.name == "nt", "Windows named mutex behavior")
class PersistentLiveProcessLockTest(unittest.TestCase):
    def test_only_one_process_lock_owner_is_allowed(self):
        name = f"Global\\StockBotLiveSchedulerTest-{uuid4().hex}"
        first = acquire_persistent_live_process_lock(name=name)
        try:
            with self.assertRaisesRegex(RuntimeError, "already active"):
                acquire_persistent_live_process_lock(name=name)
        finally:
            first.close()

        replacement = acquire_persistent_live_process_lock(name=name)
        replacement.close()
        replacement.close()


if __name__ == "__main__":
    unittest.main()
