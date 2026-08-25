"""Live tests for the transport layer and parsing (real Baidu requests)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

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


class TestRequestsTransportSessions:
    def test_lazy_until_first_use(self):
        t = RequestsTransport()
        assert t._session is None
        t.close()

    def test_threads_get_distinct_sessions(self):
        t = RequestsTransport()
        barrier = threading.Barrier(4)
        by_thread: dict[int, int] = {}

        def worker(_):
            barrier.wait()
            first = t._get_session()
            second = t._get_session()
            assert first is second
            by_thread[threading.get_ident()] = id(first)

        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(worker, range(4)))
            assert len(by_thread) == 4
            assert len(set(by_thread.values())) == 4
            assert t._session is None
        finally:
            t.close()
            assert t._session is None

    def test_close_drops_sessions_for_later_use(self):
        t = RequestsTransport()
        first = t._get_session()
        t.close()
        assert t._session is None
        second = t._get_session()
        assert second is not first
        t.close()


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
