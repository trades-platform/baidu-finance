"""Sector tests: unit coverage for bulk US fetch, plus live Baidu tests.

Live sector tests are name-agnostic: list first, then drill into the first row
so they survive changes to the underlying universe.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pandas as pd

from baidu_finance import Period
from baidu_finance.cache import MemoryCache
from baidu_finance.sector import SectorAPI
import baidu_finance.sector as sector_mod

from tests.markers import live

OHLCV = ["open", "high", "low", "close", "volume"]


def _query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class _FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.urls: list[str] = []

    def get_json(self, url: str):
        self.urls.append(url)
        return self.handler(url)


class TestUsAllConstituentsUnit:
    def test_constituents_paginate_by_offset(self, monkeypatch):
        monkeypatch.setattr(sector_mod, "_SAPI_PAGE", 2)
        pages = {
            0: [{"code": "A", "name": "a"}, {"code": "B", "name": "b"}],
            2: [{"code": "C", "name": "c"}],
        }

        def handler(url):
            pn = int(_query(url)["pn"])
            return {"Result": {"list": {"body": pages.get(pn, [])}}}

        transport = _FakeTransport(handler)
        api = SectorAPI(transport, MemoryCache())
        df = api._sapi_constituents("US1305", "us")
        assert list(df["code"]) == ["A", "B", "C"]
        assert [_query(u)["pn"] for u in transport.urls] == ["0", "2"]

    def test_constituents_stop_at_page_cap(self, monkeypatch):
        monkeypatch.setattr(sector_mod, "_SAPI_PAGE", 2)
        monkeypatch.setattr(sector_mod, "_SAPI_MAX_PAGES", 3)

        def handler(url):
            pn = _query(url)["pn"]
            return {"Result": {"list": {"body": [
                {"code": f"X{pn}a", "name": "a"},
                {"code": f"X{pn}b", "name": "b"},
            ]}}}

        transport = _FakeTransport(handler)
        api = SectorAPI(transport, MemoryCache())
        df = api._sapi_constituents("US1305", "us")
        assert len(transport.urls) == 3
        assert list(df["code"]) == ["X0a", "X0b", "X2a", "X2b", "X4a", "X4b"]

    def test_all_constituents_joins_public_sector_name(self):
        def handler(url):
            if "style=heatmap" in url:
                return {"Result": {"list": {"body": [
                    {"name": "半导体", "code": "US1305", "market": "us", "pxChangeRate": "1%"},
                    {"name": "汽车", "code": "US1405", "market": "us", "pxChangeRate": "0%"},
                ]}}}
            code = _query(url)["code"]
            body = {
                "US1305": [{
                    "code": "NVDA", "name": "英伟达",
                    "rawData": {"marketValue": 4.5e12},
                }],
                "US1405": [{
                    "code": "TSLA", "name": "特斯拉",
                    "rawData": {"marketValue": 1.1e12},
                }],
            }[code]
            return {"Result": {"list": {"body": body}}}

        api = SectorAPI(_FakeTransport(handler), MemoryCache())
        df = api.us_all_constituents()
        assert list(df.columns) == ["code", "name", "sector", "market_value"]
        assert set(zip(df["code"], df["sector"])) == {("NVDA", "半导体"), ("TSLA", "汽车")}
        assert "real_code" not in df.columns
        nvda = df.loc[df["code"] == "NVDA", "market_value"].iloc[0]
        assert nvda == 4.5e12

    def test_us_sector_constituents_include_market_value(self):
        def handler(url):
            if "style=heatmap" in url:
                return {"Result": {"list": {"body": [
                    {"name": "半导体", "code": "US1305", "market": "us", "pxChangeRate": "1%"},
                ]}}}
            return {"Result": {"list": {"body": [
                {"code": "UMC", "name": "联电", "rawData": {"marketValue": 47775658429}},
                {"code": "MISS", "name": "无市值"},
            ]}}}

        api = SectorAPI(_FakeTransport(handler), MemoryCache())
        df = api.us_sector_constituents("半导体")
        assert list(df.columns) == ["code", "name", "market_value"]
        assert df.loc[df["code"] == "UMC", "market_value"].iloc[0] == 47775658429
        assert pd.isna(df.loc[df["code"] == "MISS", "market_value"].iloc[0])


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


@live
class TestUnitedStates:
    def test_list_us_sectors(self, client):
        df = client.list_us_sectors()
        assert list(df.columns) == ["name", "ratio"]
        assert len(df) > 0
        name = df.iloc[0]["name"]
        cached = client._cache.get(name, tag="baidu:us_sector")
        assert cached and "real_code" in cached and "market" in cached
        assert cached["market"] == "us"

    def test_constituents(self, client):
        df = client.list_us_sectors()
        name = df.iloc[0]["name"]
        members = client.us_sector_constituents(name)
        assert list(members.columns) == ["code", "name", "market_value"]
        assert len(members) > 0
        assert members["market_value"].notna().any()

    def test_kline(self, client):
        df = client.list_us_sectors()
        name = df.iloc[0]["name"]
        end = datetime.now()
        start = end - timedelta(days=30)
        k = client.us_sector_kline(name, Period.DAILY, start, end)
        assert isinstance(k.index, pd.DatetimeIndex)
        assert list(k.columns) == OHLCV

    def test_all_constituents(self, client):
        df = client.us_all_constituents()
        assert list(df.columns) == ["code", "name", "sector", "market_value"]
        assert len(df) > 0
        assert df["code"].ne("").all()
        assert df["sector"].ne("").all()
        assert df["market_value"].notna().any()
        sectors = client.list_us_sectors()
        assert set(df["sector"]).issubset(set(sectors["name"]))
