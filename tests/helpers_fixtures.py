"""Synthetic powermetrics output for tests.

The key names mirror what `powermetrics --show-process-energy --show-process-io
--show-process-netstats -f plist` documents; the parser accepts several
spellings because they drift between macOS releases.
"""

import plistlib


def make_plist(tasks, elapsed_ns=1_000_000_000) -> bytes:
    return plistlib.dumps({"is_delta": True, "elapsed_ns": elapsed_ns, "tasks": list(tasks)})
