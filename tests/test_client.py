"""Client facade tests: construction, lifecycle, and the public-handle contract."""

from __future__ import annotations

from datetime import datetime, timedelta

from baidu_finance import Client, Period
from baidu_finance.cache import MemoryCache

from tests.markers import live


class TestConstruction:
    def test_default_construction_no_io(self):
        c = Client()
        # Default transport session is lazy: nothing created yet.
        assert c._transport._session is None
        c.close()

    def test_injected_dependencies(self):
        cache = MemoryCache()
        c = Client(cache=cache)
        assert c._cache is cache
        c.close()


@live
class TestLifecycleLive:
    def test_lazy_then_active(self, client):
        assert client._transport._session is None
        client.get_info("600519")
        assert client._transport._session is not None

    def test_close_recreates(self):
        c = Client()
        c.get_info("600519")
        assert c._transport._session is not None
        c.close()
        assert c._transport._session is None
        # get_kline is never cached, so it forces a fresh network request.
        end = datetime.now()
        start = end - timedelta(days=10)
        c.get_kline("600519", Period.DAILY, start, end)
        assert c._transport._session is not None
        c.close()


@live
class TestPublicHandleContract:
    def test_sector_returns_hide_internal_codes(self, client):
        industries = client.list_industries()
        # Public listing exposes only name + ratio, never an internal code.
        assert list(industries.columns) == ["name", "ratio"]

    def test_constituents_expose_public_stock_codes(self, client):
        name = client.list_industries().iloc[0]["name"]
        members = client.industry_constituents(name)
        assert list(members.columns) == ["code", "name"]
        end = datetime.now()
        start = end - timedelta(days=10)
        # A returned constituent code is itself a valid public stock handle.
        code = members.iloc[0]["code"]
        df = client.get_kline(code, Period.DAILY, start, end)
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
