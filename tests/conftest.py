"""Shared pytest fixtures and live-gating for baidu-finance tests.

Tests issue real Baidu requests (no mocks). They are skipped unless
``BAIDU_LIVE=1`` is set, and also when ``SKIP_BAIDU_TESTS=1`` is set.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def client():
    """A live ``Client`` with a fresh in-memory cache (never disk)."""
    from baidu_finance import Client
    from baidu_finance.cache import MemoryCache

    c = Client(cache=MemoryCache())
    yield c
    c.close()
