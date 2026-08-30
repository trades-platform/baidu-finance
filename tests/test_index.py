"""Index tests: unit coverage for the fallback guard and pagination, plus live tests.

Payloads mirror real captures (2026-08-28): the selfselect kline rows carry 19
fields with a ragged MA tail, and the sapi constituents endpoint answers
``{"list": {"body": [...]}}`` for real indices, ``{"list": None}`` or ``{}``
for codes missing from Baidu's index library (932000), and a bare ``[]`` for
an exhausted page.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from baidu_finance import INDEX_NAMES, Period
from baidu_finance.cache import MemoryCache
from baidu_finance.index import IndexAPI, IndexNotFoundError
import baidu_finance.index as index_mod

from tests.markers import live

OHLCV = ["open", "high", "low", "close", "volume"]

# The full row is a real 000852 capture: timestamp,time,open,close,volume,
# high,low,amount,range,ratio,turnoverratio,preClose,ma5...,ma10...,ma20...
# The second mimics Baidu truncating the empty MA tail mid-sequence.
_ROW_FULL = (
    "1787846400,2026-08-28,7734.19,7705.03,23994948600,7802.55,7702.18,"
    "465794589248.00,-27.92,-0.36,2.41,7732.95,7600.33,232280316,"
    "7662.53,238684148,7619.93,249453850"
)
_ROW_RAGGED = (
    "1787760000,2026-08-27,7762.04,7733.87,22690154200,7766.30,7729.62,"
    "440832673224.00,-9.18,-0.12,2.30,7742.95,7612.20,232280316,7663.05"
)
# Real 000985 payload: a ¥16 stock (大庆华科), not the 中证全指 index.
_ROW_TRAP = (
    "1787846400,2026-08-28,16.51,16.72,558225000,17.90,16.37,"
    "95305265.00,+0.20,+1.21,4.31,16.52,16.20,2805379,16.11"
)


def _query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def _kline_result(*rows: str) -> dict:
    return {"newMarketData": {"marketData": ";".join(rows)}}


def _cons_result(items: list) -> dict:
    return {"Result": {"list": {"body": items}}}


def _route(kline_rows, probe=None):
    """Handler serving the sapi existence probe plus a selfselect kline."""
    probe_body = _cons_result([{"code": "300378", "name": "鼎捷数智"}]) if probe is None else probe

    def handler(url):
        if "selfselect" in url:
            return {"ResultCode": "0", "Result": _kline_result(*kline_rows)}
        return probe_body

    return handler


class _FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.urls: list[str] = []

    def get_json(self, url: str):
        self.urls.append(url)
        return self.handler(url)


class TestIndexUnit:
    def test_kline_parses_full_and_ragged_rows(self):
        def handler(url):
            if "selfselect" not in url:
                return _cons_result([{"code": "300378", "name": "鼎捷数智"}])
            q = _query(url)
            assert q["isIndex"] == "true" and q["market_type"] == "ab"
            assert q["ktype"] == "1" and q["group"] == "quotation_index_kline"
            return {"ResultCode": "0", "Result": _kline_result(_ROW_RAGGED, _ROW_FULL)}

        api = IndexAPI(_FakeTransport(handler), MemoryCache())
        df = api.get_kline(
            "000852", Period.DAILY, datetime(2018, 1, 1), datetime(2026, 12, 31)
        )
        assert list(df.columns) == OHLCV
        assert df.index.name == "datetime"
        assert list(df.index) == [pd.Timestamp("2026-08-27"), pd.Timestamp("2026-08-28")]
        full = df.loc["2026-08-28"]
        assert full["open"] == 7734.19 and full["close"] == 7705.03
        assert full["volume"] == 23994948600
        assert full["high"] == 7802.55 and full["low"] == 7702.18
        ragged = df.loc["2026-08-27"]
        assert ragged["close"] == 7733.87 and ragged["high"] == 7766.30
        assert df.attrs["code"] == "000852"
        assert df.attrs["name"] == INDEX_NAMES["000852"]

    def test_kline_window_is_calendar_inclusive(self):
        rows = ";".join(
            f"0,{d},1,2,3,4,0.5" for d in ("2026-01-05", "2026-02-02", "2026-03-02")
        )
        api = IndexAPI(
            _FakeTransport(_route([rows])), MemoryCache()
        )
        df = api.get_kline(
            "000300", Period.DAILY, datetime(2026, 1, 5), datetime(2026, 2, 2)
        )
        assert list(df.index) == [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-02-02")]

    @pytest.mark.parametrize("period", [Period.MIN_5, "5m", Period.MIN_60])
    def test_kline_rejects_minute_periods(self, period):
        api = IndexAPI(_FakeTransport(lambda url: {}), MemoryCache())
        with pytest.raises(ValueError, match="daily/weekly/monthly"):
            api.get_kline("000852", period, datetime(2026, 1, 1), datetime(2026, 2, 1))

    def test_kline_weekly_monthly_ktypes(self):
        seen = []

        def handler(url):
            if "selfselect" in url:
                seen.append(_query(url)["ktype"])
                return {"Result": _kline_result(_ROW_FULL)}
            return _cons_result([{"code": "300378", "name": "鼎捷数智"}])

        api = IndexAPI(_FakeTransport(handler), MemoryCache())
        for period in (Period.WEEKLY, Period.MONTHLY):
            api.get_kline("000300", period, datetime(2026, 1, 1), datetime(2026, 2, 1))
        assert seen == ["2", "3"]

    def test_kline_empty_result_returns_empty_frame(self):
        api = IndexAPI(_FakeTransport(_route([])), MemoryCache())
        df = api.get_kline("000852", Period.DAILY, datetime(2026, 1, 1), datetime(2026, 2, 1))
        assert df.empty
        assert list(df.columns) == OHLCV

    @pytest.mark.parametrize("probe", [
        {"Result": {"list": None}},   # 000985: colliding stock, no index data
        {"Result": {}},               # 932000: absent from the index library
        {"Result": []},
        {"ResultCode": "3203"},
    ])
    def test_unknown_index_never_requests_quotation(self, probe):
        def handler(url):
            if "selfselect" in url:
                return {"ResultCode": "0", "Result": _kline_result(_ROW_TRAP)}
            return probe

        transport = _FakeTransport(handler)
        api = IndexAPI(transport, MemoryCache())
        with pytest.raises(IndexNotFoundError, match="000852"):
            api.get_kline("000852", Period.DAILY, datetime(2026, 1, 1), datetime(2026, 2, 1))
        assert all("selfselect" not in url for url in transport.urls)

    def test_fallback_trap_regression_000985(self):
        """Baidu returns 大庆华科's price for 000985 — must raise, not return it."""

        def handler(url):
            if "selfselect" in url:
                return {"ResultCode": "0", "Result": _kline_result(_ROW_TRAP)}
            return {"Result": {"list": None}}

        api = IndexAPI(_FakeTransport(handler), MemoryCache())
        with pytest.raises(IndexNotFoundError, match="silently falls back"):
            api.get_kline("000985", Period.DAILY, datetime(2024, 1, 1), datetime(2026, 8, 30))

    def test_verification_is_cached(self):
        def handler(url):
            if "selfselect" in url:
                return {"Result": _kline_result(_ROW_FULL)}
            return _cons_result([{"code": "300378", "name": "鼎捷数智"}])

        transport = _FakeTransport(handler)
        cache = MemoryCache()
        api = IndexAPI(transport, cache)
        api.get_kline("000852", Period.DAILY, datetime(2026, 1, 1), datetime(2026, 2, 1))
        api.get_kline("000852", Period.DAILY, datetime(2026, 2, 1), datetime(2026, 3, 1))
        probes = [u for u in transport.urls if _query(u).get("rn") == "1"]
        assert len(probes) == 1
        assert cache.get("000852", tag="baidu:index") == {"verified": True}

    def test_constituents_paginate_by_offset(self, monkeypatch):
        monkeypatch.setattr(index_mod, "_CONS_PAGE", 2)
        pages = {
            0: [{"code": "A", "name": "a"}, {"code": "B", "name": "b"}],
            2: [{"code": "C", "name": "c"}],
        }

        def handler(url):
            pn = int(_query(url)["pn"])
            return _cons_result(pages.get(pn, []))

        transport = _FakeTransport(handler)
        api = IndexAPI(transport, MemoryCache())
        df = api.get_constituents("000300")
        assert list(df["code"]) == ["A", "B", "C"]
        assert [_query(u)["pn"] for u in transport.urls if _query(u)["rn"] != "1"] == ["0", "2"]

    def test_constituents_stop_at_page_cap(self, monkeypatch):
        monkeypatch.setattr(index_mod, "_CONS_PAGE", 2)
        monkeypatch.setattr(index_mod, "_CONS_MAX_PAGES", 3)

        def handler(url):
            pn = _query(url)["pn"]
            return _cons_result([
                {"code": f"X{pn}a", "name": "a"},
                {"code": f"X{pn}b", "name": "b"},
            ])

        transport = _FakeTransport(handler)
        api = IndexAPI(transport, MemoryCache())
        df = api.get_constituents("000852")
        pages = [u for u in transport.urls if _query(u)["rn"] != "1"]
        assert len(pages) == 3
        assert len(df) == 6

    def test_constituents_bare_list_page_terminates(self, monkeypatch):
        monkeypatch.setattr(index_mod, "_CONS_PAGE", 2)

        def handler(url):
            if _query(url)["rn"] == "1":
                return _cons_result([{"code": "A", "name": "a"}])
            return {"Result": {"list": []}}  # exhausted page shape

        transport = _FakeTransport(handler)
        api = IndexAPI(transport, MemoryCache())
        df = api.get_constituents("000300")
        assert df.empty
        assert list(df.columns) == ["code", "name"]
        assert len(transport.urls) == 2

    def test_constituents_skip_malformed_rows(self):
        def handler(url):
            if _query(url)["rn"] == "1":
                return _cons_result([{"code": "A", "name": "a"}])
            return _cons_result([
                {"code": "600519", "name": "贵州茅台"},
                {"code": "", "name": "no-code"},
                "junk",
                {"name": "no-code-either"},
            ])

        api = IndexAPI(_FakeTransport(handler), MemoryCache())
        df = api.get_constituents("000300")
        assert list(df["code"]) == ["600519"]

    def test_missing_index_constituents_raises(self):
        api = IndexAPI(
            _FakeTransport(lambda url: {"Result": {"list": None}}), MemoryCache()
        )
        with pytest.raises(IndexNotFoundError, match="932000"):
            api.get_constituents("932000")


@live
class TestIndices:
    def test_kline_csi300(self, client):
        df = client.index_kline(
            "000300", Period.DAILY, datetime(2020, 1, 1), datetime(2026, 8, 30)
        )
        assert list(df.columns) == OHLCV
        assert len(df) > 1000
        # Point magnitude: a colliding ¥-stock fallback would sit far below.
        assert df["close"].iloc[-1] > 1000
        assert df.attrs["name"] == "沪深300"

    def test_constituents_csi1000_exactly_1000(self, client):
        df = client.index_constituents("000852")
        assert list(df.columns) == ["code", "name"]
        assert len(df) == 1000
        assert df["code"].is_unique

    def test_constituents_kc_composite_range(self, client):
        df = client.index_constituents("000680")
        assert 500 < len(df) < 700

    def test_constituents_bj50(self, client):
        df = client.index_constituents("899050")
        assert len(df) == 50

    def test_fallback_trap_000985_raises(self, client):
        with pytest.raises(IndexNotFoundError):
            client.index_kline(
                "000985", Period.DAILY, datetime(2024, 1, 1), datetime(2026, 8, 30)
            )

    def test_missing_932000_raises(self, client):
        with pytest.raises(IndexNotFoundError):
            client.index_constituents("932000")
