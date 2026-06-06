"""Live tests for stock data (real Baidu requests)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from baidu_finance import Period

from tests.markers import live

OHLCV = ["open", "high", "low", "close", "volume"]


@live
class TestGetInfo:
    @pytest.mark.parametrize("code", ["600519", "000001", "00700", "AAPL"])
    def test_info_non_empty(self, client, code):
        info = client.get_info(code)
        assert info.code == code
        assert info.name and isinstance(info.name, str)
        assert info.market_id and isinstance(info.market_id, str)

    def test_info_is_cached(self, client):
        client.get_info("600519")
        cached = client._cache.get("600519", tag="baidu:stock")
        assert cached is not None
        assert cached["name"] and cached["market_id"]


@live
class TestGetKline:
    def test_daily_shape(self, client):
        end = datetime.now()
        start = end - timedelta(days=30)
        df = client.get_kline("000001", Period.DAILY, start, end)
        assert isinstance(df, pd.DataFrame)
        assert isinstance(df.index, pd.DatetimeIndex)
        assert list(df.columns) == OHLCV
        assert len(df) > 0
        assert df.index.is_monotonic_increasing

    @pytest.mark.parametrize(
        "period", [Period.MIN_5, Period.MIN_30, Period.MIN_60, Period.DAILY,
                   Period.WEEKLY, Period.MONTHLY],
    )
    def test_periods(self, client, period):
        end = datetime.now()
        start = end - timedelta(days=30)
        df = client.get_kline("000001", period, start, end)
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == OHLCV

    def test_future_range_empty(self, client):
        start = datetime.now() + timedelta(days=365)
        end = start + timedelta(days=7)
        df = client.get_kline("000001", Period.DAILY, start, end)
        assert df.empty
        assert list(df.columns) == OHLCV
