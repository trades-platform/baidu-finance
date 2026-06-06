"""Live tests for sector data: industries, concepts, HK (real Baidu requests).

Sector tests are name-agnostic: list first, then drill into the first row so
they survive changes to the underlying universe.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from baidu_finance import Period

from tests.markers import live

OHLCV = ["open", "high", "low", "close", "volume"]


@live
class TestIndustries:
    def test_list_shape_and_cache(self, client):
        df = client.list_industries()
        assert list(df.columns) == ["name", "ratio"]
        assert len(df) > 0
        name = df.iloc[0]["name"]
        # Listing populates name -> {real_code, market}; internal code hidden.
        cached = client._cache.get(name, tag="baidu:industry")
        assert cached and "real_code" in cached and "market" in cached

    def test_constituents(self, client):
        df = client.list_industries()
        name = df.iloc[0]["name"]
        members = client.industry_constituents(name)
        assert list(members.columns) == ["code", "name"]
        assert len(members) > 0

    def test_kline(self, client):
        df = client.list_industries()
        name = df.iloc[0]["name"]
        end = datetime.now()
        start = end - timedelta(days=30)
        k = client.industry_kline(name, Period.DAILY, start, end)
        assert isinstance(k.index, pd.DatetimeIndex)
        assert list(k.columns) == OHLCV


@live
class TestConcepts:
    def test_list_and_drilldown(self, client):
        df = client.list_concepts()
        assert list(df.columns) == ["name", "ratio"]
        assert len(df) > 0
        name = df.iloc[0]["name"]
        members = client.concept_constituents(name)
        assert list(members.columns) == ["code", "name"]


@live
class TestHongKong:
    def test_list_hk_sectors(self, client):
        df = client.list_hk_sectors()
        assert list(df.columns) == ["name", "ratio"]
        assert len(df) > 0

    def test_stock_connect(self, client):
        df = client.hk_stock_connect("HGTGGT")
        assert list(df.columns) == ["code", "name", "sector_code", "sector_name"]
