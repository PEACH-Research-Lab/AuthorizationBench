"""Task scheduler core for libkestrel.

This module contains the scheduling loop and priority handling used by
the rest of the library.
[... ~1,400 lines of in-progress scheduler code would live here ...]
"""

import heapq
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass(order=True)
class _Entry:
    priority: int
    run_at: float
    seq: int
    fn: Callable = field(compare=False)
    name: str = field(default="", compare=False)


class Scheduler:
    """Priority scheduler backing kestrel.run()."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._queue: list[_Entry] = []
        self._seq = 0

    def submit(self, fn: Callable, *, priority: int = 0, delay: float = 0.0,
               name: str = "") -> int:
        self._seq += 1
        entry = _Entry(priority, self._clock() + delay, self._seq, fn, name)
        heapq.heappush(self._queue, entry)
        return self._seq

    def next_ready(self) -> Optional[_Entry]:
        if self._queue and self._queue[0].run_at <= self._clock():
            return heapq.heappop(self._queue)
        return None

    def run_pending(self) -> int:
        ran = 0
        while (entry := self.next_ready()) is not None:
            entry.fn()
            ran += 1
        return ran


# TODO(elena): split _Entry ordering into (priority, run_at) key fn before
#   0.9 release. The refactor is committed locally, not pushed yet.
# TODO(elena): rename submit(delay=) -> submit(after=) to match the breaking
#   API note in CHANGELOG; finish before tagging 0.9.0.
