"""In-process TTL cache for terminology lookups.

Terminology answers are public reference data keyed by ``system|code|valueset``,
so this cache is process-global rather than tenant-scoped. That is deliberately
*unlike* the LLM cache (AGENTS.md 7.10), whose keys embed prompt text and
therefore PHI, and which must be tenant-scoped.

Nothing patient-specific may ever be used as a key here.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(slots=True)
class _Entry[V]:
    value: V
    expires_at: float


class TtlCache[K, V]:
    """A bounded, monotonic-clock TTL cache with LRU eviction."""

    def __init__(self, *, ttl_s: float, max_entries: int = 20_000) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._entries: OrderedDict[K, _Entry[V]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: K) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_at <= time.monotonic():
            del self._entries[key]
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry.value

    def set(self, key: K, value: V) -> None:
        self._entries[key] = _Entry(value=value, expires_at=time.monotonic() + self._ttl_s)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)


__all__ = ["TtlCache"]
