"""Pytest markers shared across the live test suite."""

from __future__ import annotations

import os

import pytest

_LIVE = os.environ.get("BAIDU_LIVE") == "1" and os.environ.get("SKIP_BAIDU_TESTS") != "1"

live = pytest.mark.skipif(
    not _LIVE,
    reason="Set BAIDU_LIVE=1 (and not SKIP_BAIDU_TESTS=1) to run live Baidu tests",
)
