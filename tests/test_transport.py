"""Live tests for the transport layer and parsing (real Baidu requests)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from baidu_finance.parsing import empty_kline, parse_market_data
from baidu_finance.transport import RequestsTransport, TransportError

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

    def test_null_new_market_data_returns_empty(self):
        assert parse_market_data({"newMarketData": None}).empty
        assert parse_market_data({"newMarketData": {"marketData": None}}).empty
        assert parse_market_data([]).empty


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


class _FakeResponse:
    status_code = 200
    text = '{"ok": 1}'

    def json(self):
        return {"ok": 1}

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self):
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        return _FakeResponse()


class _BoomSession:
    def get(self, url, timeout=None):
        raise RuntimeError("connection reset")


def _offline_transport(monkeypatch, session=None, **kwargs) -> RequestsTransport:
    transport = RequestsTransport(**kwargs)
    monkeypatch.setattr(
        transport, "_get_session", lambda: session if session is not None else _FakeSession()
    )
    return transport


class TestRateLimitAvoidance:
    def test_default_headers_look_like_browser(self):
        t = RequestsTransport()
        try:
            headers = t._make_session().headers
            for key in ("User-Agent", "Accept", "Accept-Language", "Referer", "Origin"):
                assert headers[key]
            assert "gushitong.baidu.com" in headers["Referer"]
        finally:
            t.close()

    def test_custom_headers_override_defaults(self):
        t = RequestsTransport(headers={"Accept": "application/vnd.finance-web.v1+json"})
        try:
            assert (
                t._make_session().headers["Accept"]
                == "application/vnd.finance-web.v1+json"
            )
        finally:
            t.close()

    def test_429_and_5xx_are_retried(self):
        t = RequestsTransport()
        try:
            retry = t._make_session().get_adapter("https://").max_retries
            assert 429 in retry.status_forcelist
            assert {500, 502, 503, 504} <= set(retry.status_forcelist)
            assert retry.backoff_factor > 0
        finally:
            t.close()

    def test_sustained_rate_spaces_requests(self, monkeypatch):
        session = _FakeSession()
        t = _offline_transport(monkeypatch, session, rate=20, burst=1)
        try:
            t0 = time.monotonic()
            for _ in range(3):
                assert t.get_json("https://x/y") == {"ok": 1}
            # 3 requests -> 2 enforced gaps of >= 1/20s.
            assert time.monotonic() - t0 >= 0.10
            assert session.calls == 3
        finally:
            t.close()

    def test_burst_fires_without_waiting(self, monkeypatch):
        t = _offline_transport(monkeypatch, rate=1, burst=3)
        try:
            t0 = time.monotonic()
            for _ in range(3):
                t.get_json("https://x/y")
            assert time.monotonic() - t0 < 0.5
            assert t._bucket.tokens < 0.01
        finally:
            t.close()

    def test_zero_rate_disables_pacing(self, monkeypatch):
        t = _offline_transport(monkeypatch, rate=0)
        try:
            t0 = time.monotonic()
            for _ in range(5):
                t.get_json("https://x/y")
            assert time.monotonic() - t0 < 0.4
        finally:
            t.close()

    def test_pacing_is_global_across_threads(self, monkeypatch):
        t = _offline_transport(monkeypatch, rate=20, burst=1)
        try:
            t0 = time.monotonic()
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda _: t.get_json("https://x/y"), range(6)))
            # 6 requests from 3 threads -> 5 enforced gaps, bursts impossible.
            assert time.monotonic() - t0 >= 0.25
        finally:
            t.close()

    def test_failures_halve_rate_to_floor(self, monkeypatch):
        monkeypatch.setattr(
            "baidu_finance.transport.subprocess.check_output",
            lambda command, text=True: '{"ok": 2}',
        )
        t = _offline_transport(monkeypatch, _BoomSession(), rate=10, burst=10)
        try:
            for _ in range(5):
                assert t.get_json("https://x/y") == {"ok": 2}
            # 10 -> 5 -> 2.5 -> 1.25 -> 0.625 -> clamped at the 0.5 floor.
            assert t._bucket.rate == pytest.approx(0.5)
        finally:
            t.close()

    def test_successes_recover_rate(self, monkeypatch):
        t = _offline_transport(monkeypatch, rate=10, burst=10)
        try:
            t._note_outcome(ok=False)
            t._note_outcome(ok=False)
            assert t._bucket.rate == pytest.approx(2.5)
            for expected in (3.75, 5.625, 8.4375, 10.0, 10.0):
                t.get_json("https://x/y")
                assert t._bucket.rate == pytest.approx(expected)
        finally:
            t.close()

    def test_wget_fallback_carries_headers(self, monkeypatch):
        seen = {}

        def fake_check_output(command, text=True):
            seen["command"] = command
            return '{"ok": 2}'

        monkeypatch.setattr(
            "baidu_finance.transport.subprocess.check_output", fake_check_output
        )
        t = _offline_transport(monkeypatch, _BoomSession(), rate=0)
        try:
            assert t.get_json("https://x/y") == {"ok": 2}
        finally:
            t.close()
        command = seen["command"]
        assert command[0] == "wget"
        assert "https://x/y" in command
        headers = [a for a in command if ":" in a and a not in ("https://x/y",)]
        assert any(a.startswith("User-Agent: Mozilla/5.0") for a in headers)
        assert any(a.startswith("Referer: https://gushitong.baidu.com") for a in headers)

    def test_wget_failure_raises_transport_error(self, monkeypatch):
        def boom(command, text=True):
            raise RuntimeError("wget missing")

        monkeypatch.setattr(
            "baidu_finance.transport.subprocess.check_output", boom
        )
        t = _offline_transport(monkeypatch, _BoomSession(), rate=0)
        try:
            with pytest.raises(TransportError, match="Failed to fetch"):
                t.get_json("https://x/y")
        finally:
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
