"""Unit tests for enums, models, and cache (no network)."""

from __future__ import annotations

import time

import pytest

from baidu_finance import Adjust, Period, StockInfo
from baidu_finance.cache import MemoryCache


class TestPeriod:
    def test_parse_enum_passthrough(self):
        assert Period.parse(Period.DAILY) is Period.DAILY

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1d", Period.DAILY), ("daily", Period.DAILY), ("day", Period.DAILY),
            ("5", Period.MIN_5), ("5m", Period.MIN_5),
            ("120", Period.MIN_120), ("2h", Period.MIN_120),
        ],
    )
    def test_parse_aliases(self, value, expected):
        assert Period.parse(value) is expected

    def test_parse_invalid(self):
        with pytest.raises(ValueError):
            Period.parse("nope")

    @pytest.mark.parametrize(
        "period,ktype",
        [
            (Period.DAILY, "day"), (Period.WEEKLY, "week"), (Period.MONTHLY, "month"),
            (Period.MIN_5, "min5"), (Period.MIN_120, "min120"),
        ],
    )
    def test_baidu_ktype(self, period, ktype):
        assert period.baidu_ktype == ktype


class TestAdjust:
    @pytest.mark.parametrize(
        "value,expected",
        [("qfq", Adjust.QFQ), ("forward", Adjust.QFQ), ("backward", Adjust.HFQ),
         ("none", Adjust.NONE)],
    )
    def test_parse(self, value, expected):
        assert Adjust.parse(value) is expected


class TestStockInfo:
    def test_fields(self):
        info = StockInfo(code="600519", name="贵州茅台", market_id="ab")
        assert info.code == "600519"
        assert info.name == "贵州茅台"
        assert info.market_id == "ab"


class TestMemoryCache:
    def test_set_get(self):
        c = MemoryCache()
        c.set("k", {"a": 1}, tag="t")
        assert c.get("k", tag="t") == {"a": 1}

    def test_tag_namespacing(self):
        c = MemoryCache()
        c.set("k", "v1", tag="t1")
        c.set("k", "v2", tag="t2")
        assert c.get("k", tag="t1") == "v1"
        assert c.get("k", tag="t2") == "v2"

    def test_missing_returns_none(self):
        assert MemoryCache().get("absent", tag="t") is None

    def test_expiry(self):
        c = MemoryCache()
        c.set("k", "v", tag="t", expire=0.05)
        assert c.get("k", tag="t") == "v"
        time.sleep(0.1)
        assert c.get("k", tag="t") is None


class TestDiskCache:
    def test_parity_with_memory(self, tmp_path):
        pytest.importorskip("diskcache_rs")
        from baidu_finance.cache import DiskCache

        c = DiskCache(str(tmp_path / "cache"))
        c.set("600519", {"name": "x", "market_id": "ab"}, tag="baidu:stock")
        assert c.get("600519", tag="baidu:stock") == {"name": "x", "market_id": "ab"}
        # Different tag, same key is isolated.
        assert c.get("600519", tag="baidu:industry") is None

