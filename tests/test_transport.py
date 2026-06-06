"""Live tests for the transport layer and parsing (real Baidu requests)."""

from __future__ import annotations

import pandas as pd

from baidu_finance.parsing import empty_kline, parse_market_data
from baidu_finance.transport import RequestsTransport

from tests.markers import live


class TestParsingPure:
    """parse_market_data is pure and exercised without network."""

    def test_empty_kline_shape(self):
        df = empty_kline()
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert isinstance(df.index, pd.DatetimeIndex)

    def test_falsy_result_returns_empty(self):
        assert parse_market_data(None).empty
        assert parse_market_data({}).empty


@live
class TestTransportLive:
    def test_get_json_returns_payload(self):
        t = RequestsTransport()
        try:
            data = t.get_json(
                "https://finance.pae.baidu.com/vapi/v1/stockrelatedobjects?code=600519"
            )
            assert isinstance(data, dict)
            assert "Result" in data
        finally:
            t.close()
