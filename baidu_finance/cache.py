"""Cache layer: protocol, in-memory default, and optional disk adapter.

Values stored in the cache MUST be plain JSON-serializable structures
(dicts/lists/scalars), never pandas objects, so that the in-memory and disk
implementations behave identically.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class Cache(Protocol):
    """Cache contract used by the SDK.

    ``tag`` namespaces the keyspace; clearing one namespace must not affect
    another. The effective stored key is ``f"{tag}:{key}"``.
    """

    def get(self, key: str, *, tag: str = "") -> Optional[Any]:
        """Return the value for ``(key, tag)`` or ``None`` if absent/expired."""
        ...

    def set(
        self, key: str, value: Any, *, tag: str = "", expire: Optional[float] = None
    ) -> None:
        """Store ``value`` under ``(key, tag)`` with an optional TTL in seconds."""
        ...


def _compose(key: str, tag: str) -> str:
    """Compose the flat stored key from ``key`` and ``tag``."""
    return f"{tag}:{key}" if tag else key


class MemoryCache:
    """Zero-dependency in-memory cache with per-entry expiry.

    Entries persist for the lifetime of the instance. Used by default when no
    cache is injected into a ``Client``.
    """

    def __init__(self) -> None:
        # stored_key -> (value, expire_at | None)
        self._store: dict[str, tuple[Any, Optional[float]]] = {}

    def get(self, key: str, *, tag: str = "") -> Optional[Any]:
        item = self._store.get(_compose(key, tag))
        if item is None:
            return None
        value, expire_at = item
        if expire_at is not None and time.time() >= expire_at:
            self._store.pop(_compose(key, tag), None)
            return None
        return value

    def set(
        self, key: str, value: Any, *, tag: str = "", expire: Optional[float] = None
    ) -> None:
        expire_at = time.time() + expire if expire is not None else None
        self._store[_compose(key, tag)] = (value, expire_at)


class DiskCache:
    """Persistent cache backed by ``diskcache_rs``.

    Requires the optional ``[disk]`` extra (``diskcache-rs>=0.4.10``). Preserves
    the same tag-prefix key scheme as :class:`MemoryCache` so behavior is
    identical across both implementations.
    """

    def __init__(self, directory: Optional[str] = None) -> None:
        import os

        import diskcache_rs

        if directory is None:
            directory = os.path.join(os.path.expanduser("~"), ".baidu_finance_cache")
        directory = os.path.expanduser(directory)
        os.makedirs(directory, exist_ok=True)
        self._cache = diskcache_rs.Cache(directory)
        self.directory = directory

    def get(self, key: str, *, tag: str = "") -> Optional[Any]:
        return self._cache.get(_compose(key, tag))

    def set(
        self, key: str, value: Any, *, tag: str = "", expire: Optional[float] = None
    ) -> None:
        self._cache.set(_compose(key, tag), value, expire=expire)
