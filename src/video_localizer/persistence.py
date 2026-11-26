"""Atomic replacement tolerant of brief Windows reader/file-scanner locks."""

import os
import time


def atomic_replace(source, target):
    """Replace without deleting the destination, retrying only Windows locks.

    A reader can briefly deny deletion while a checkpoint is being inspected.
    Ten attempts bound the extra wait to 450 ms; persistent permissions errors
    and other filesystem failures still reach the caller unchanged.
    """
    for attempt in range(10):
        try:
            os.replace(source, target)
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 9:
                raise
            time.sleep(0.05)
