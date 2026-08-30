"""Live tests for the transport layer and parsing (real Baidu requests)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from baidu_finance.parsing import empty_kline, parse_market_data
from baidu_finance.transport import (
    _AdaptiveBucket,
    _classify_response,
    _Circuit,
    RequestsTransport,
    TransportError,
)

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


class _PayloadResponse:
    def __init__(self, payload, status):
        self._payload = payload
        self.status_code = status

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _CannedSession:
    """Session returning a fixed ``(status, payload)`` on every call."""

    def __init__(self, payload, status=200):
        self.calls = 0
        self._response = _PayloadResponse(payload, status)

    def get(self, url, timeout=None):
        self.calls += 1
        return self._response


class _SuspectSession(_CannedSession):
    """Soft block on info-style endpoints: 200 with ``Result`` null."""

    def __init__(self):
        super().__init__({"ResultCode": 0, "ResultNum": 0, "Result": None})


# Risk-control samples observed in the wild (design.md D1 table).
_SOFT_INFO = {"ResultCode": 0, "ResultNum": 0, "Result": None}
_SOFT_QUOTATION = {
    "ResultCode": 0,
    "ResultNum": 0,
    "Result": {"newMarketData": {"headers": ["时间"], "keys": ["time"]}},
}
_HARD_RISK = {
    "ResultCode": 0,
    "Result": {"code": 403, "isCaptchaEnabled": True, "msg": "hit risk"},
}


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


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
        t._bucket._rand = lambda a, b: 1.0  # exact pacing; jitter has its own test
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
        t._bucket._rand = lambda a, b: 1.0
        try:
            t0 = time.monotonic()
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda _: t.get_json("https://x/y"), range(6)))
            # 6 requests from 3 threads -> 5 enforced gaps, bursts impossible.
            assert time.monotonic() - t0 >= 0.25
        finally:
            t.close()

    def test_same_url_failures_do_not_halve_rate(self, monkeypatch):
        monkeypatch.setattr(
            "baidu_finance.transport.subprocess.check_output",
            lambda command, text=True: '{"ok": 2}',
        )
        t = _offline_transport(monkeypatch, _BoomSession(), rate=10, burst=10)
        try:
            for _ in range(5):
                assert t.get_json("https://x/y") == {"ok": 2}
            # Isolated/same-URL failures are noise or per-code problems;
            # only sustained cross-URL failure confirmed by the breaker
            # gates the rate penalty.
            assert t._bucket.rate == pytest.approx(10.0)
        finally:
            t.close()

    def test_sustained_failures_halve_rate(self, monkeypatch):
        wget_calls = []

        def no_wget(command, text=True):
            wget_calls.append(command)
            raise RuntimeError("wget disabled in test")

        monkeypatch.setattr(
            "baidu_finance.transport.subprocess.check_output", no_wget
        )
        t = _offline_transport(
            monkeypatch,
            _SuspectSession(),
            rate=10,
            burst=10,
            suspect_retries=0,
        )
        try:
            for i in range(3):  # distinct URLs on one endpoint
                with pytest.raises(TransportError):
                    t.get_json(f"https://x/y?code={i}")
            # Third distinct-URL failure confirms the streak: rate halves.
            assert t._bucket.rate == pytest.approx(5.0)
            assert wget_calls == []
        finally:
            t.close()

    def test_successes_recover_rate(self, monkeypatch):
        t = _offline_transport(monkeypatch, rate=10, burst=10)
        try:
            t._bucket.penalize()
            t._bucket.penalize()
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


class TestRiskControlResilience:
    """Sniffer, circuit breaker, streak gating and jitter (offline)."""

    # ── 1.1 response classification ────────────────────────────────────

    def test_sniffer_anchors_observed_samples(self):
        assert _classify_response(200, _SOFT_INFO) == "suspect"
        assert _classify_response(200, _SOFT_QUOTATION) == "suspect"
        assert _classify_response(403, _HARD_RISK) == "hard_risk"

    def test_sniffer_passes_normal_shapes(self):
        assert _classify_response(200, {"Result": {"stockName": "贵州茅台"}}) == "normal"
        # A legitimately empty range still ships the data container.
        assert _classify_response(
            200, {"Result": {"newMarketData": {"headers": [], "keys": [], "data": []}}}
        ) == "normal"
        assert _classify_response(200, {"ok": 1}) == "normal"  # no Result key
        assert _classify_response(200, [{"Result": None}]) == "normal"  # non-dict
        assert _classify_response(200, None) == "normal"  # unparseable body
        assert _classify_response(200, {"Result": [{"code": "110200"}]}) == "normal"
        # A plain 403 without the risk marker is a generic failure.
        assert _classify_response(403, {"Result": {"code": 403}}) == "normal"

    # ── 2.1 circuit breaker states ─────────────────────────────────────

    def test_circuit_trips_after_threshold_across_urls(self):
        circuit = _Circuit(
            threshold=3, cooldown=10.0, max_cooldown=20.0, clock=_FakeClock()
        )
        assert circuit.record("https://x/a?1", ok=False) is False
        assert circuit.record("https://x/a?2", ok=False) is False
        assert circuit.record("https://x/a?3", ok=False) is True  # trips
        with pytest.raises(TransportError, match="cooling down"):
            circuit.guard()

    def test_same_url_failures_never_trip(self):
        circuit = _Circuit(threshold=3, cooldown=10.0, clock=_FakeClock())
        for _ in range(5):
            assert circuit.record("https://x/a?1", ok=False) is False
        circuit.guard()  # does not raise

    def test_half_open_success_closes_and_resets(self):
        clock = _FakeClock()
        circuit = _Circuit(threshold=3, cooldown=10.0, max_cooldown=20.0, clock=clock)
        for i in range(3):
            circuit.record(f"https://x/a?{i}", ok=False)
        with pytest.raises(TransportError):
            circuit.guard()
        clock.now += 10.0
        circuit.guard()  # expired into half-open probe
        assert circuit.record("https://x/a?9", ok=True) is False  # probe ok
        circuit.guard()  # closed again
        # Streak restarted from zero after the close.
        assert circuit.record("https://x/a?2", ok=False) is False

    def test_half_open_failure_doubles_cooldown_with_cap(self):
        clock = _FakeClock()
        circuit = _Circuit(threshold=1, cooldown=10.0, max_cooldown=20.0, clock=clock)
        circuit.record("https://x/a?1", ok=False)  # open, 10s
        clock.now += 10.0
        circuit.guard()  # half-open
        assert circuit.record("https://x/a?2", ok=False) is True  # reopen, 20s
        clock.now += 20.0
        circuit.guard()
        assert circuit.record("https://x/a?3", ok=False) is True  # capped at 20s
        clock.now += 20.0
        circuit.guard()
        assert circuit.record("https://x/a?4", ok=False) is True  # still capped

    def test_endpoints_are_isolated(self):
        clock = _FakeClock()
        a = _Circuit(threshold=1, cooldown=10.0, clock=clock)
        b = _Circuit(threshold=1, cooldown=10.0, clock=clock)
        a.record("https://pae/y?1", ok=False)
        with pytest.raises(TransportError):
            a.guard()
        b.guard()  # untouched

    # ── 3.2 jitter ─────────────────────────────────────────────────────

    def test_acquire_waits_carry_jitter(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
        factors = iter([0.75, 1.25])
        bucket = _AdaptiveBucket(
            rate=100.0, burst=1, min_rate=0.5, rand=lambda a, b: next(factors)
        )
        bucket.acquire()  # consumes the initial token, no wait
        bucket.acquire()
        bucket.acquire()
        # Nominal wait is 1/100 = 0.01s; jitter scales it per wait (the
        # tiny shortfall comes from tokens refilled between acquires).
        assert len(sleeps) == 2
        assert len(set(sleeps)) == len(sleeps)  # not metronome-exact
        for s in sleeps:
            assert 0.007 <= s <= 0.013  # nominal 0.01, bounded jitter
        assert sum(sleeps) / len(sleeps) == pytest.approx(0.01, abs=1e-3)

    # ── 4.x transport integration ──────────────────────────────────────

    def test_open_endpoint_fails_fast_with_zero_network(self, monkeypatch):
        wget_calls = []
        monkeypatch.setattr(
            "baidu_finance.transport.subprocess.check_output",
            lambda command, text=True: wget_calls.append(command),
        )
        session = _SuspectSession()
        t = _offline_transport(
            monkeypatch, session, rate=0, suspect_retries=0, circuit_threshold=3
        )
        try:
            for i in range(3):
                with pytest.raises(TransportError, match="suspected block"):
                    t.get_json(f"https://x/y?code={i}")
            assert session.calls == 3
            with pytest.raises(TransportError, match="cooling down"):
                t.get_json("https://x/y?code=99")
            assert session.calls == 3  # zero extra requests
            assert wget_calls == []
        finally:
            t.close()

    def test_suspect_path_never_invokes_wget(self, monkeypatch):
        wget_calls = []
        monkeypatch.setattr(
            "baidu_finance.transport.subprocess.check_output",
            lambda command, text=True: wget_calls.append(command),
        )
        session = _CannedSession(_SOFT_QUOTATION)
        t = _offline_transport(monkeypatch, session, rate=0, suspect_retries=2)
        try:
            with pytest.raises(TransportError, match="suspected block"):
                t.get_json("https://x/y?code=1")
            # 1 primary + 2 transport-level retries, no wget.
            assert session.calls == 3
            assert wget_calls == []
        finally:
            t.close()

    def test_hard_risk_opens_circuit_without_wget(self, monkeypatch):
        wget_calls = []
        monkeypatch.setattr(
            "baidu_finance.transport.subprocess.check_output",
            lambda command, text=True: wget_calls.append(command),
        )
        session = _CannedSession(_HARD_RISK, status=403)
        t = _offline_transport(monkeypatch, session, rate=0)
        try:
            with pytest.raises(TransportError, match="hit risk"):
                t.get_json("https://x/y?code=1")
            with pytest.raises(TransportError, match="cooling down"):
                t.get_json("https://x/y?code=2")
            assert session.calls == 1  # second call never reached the session
            assert wget_calls == []
        finally:
            t.close()

    def test_isolated_failure_then_success_keeps_rate(self, monkeypatch):
        monkeypatch.setattr(
            "baidu_finance.transport.subprocess.check_output",
            lambda command, text=True: '{"ok": 2}',
        )
        boom = _BoomSession()
        t = _offline_transport(monkeypatch, boom, rate=10, burst=10)
        try:
            t.get_json("https://x/y")  # network failure, wget rescues
            t.close()
            monkeypatch.setattr(t, "_get_session", lambda: _FakeSession())
            t.get_json("https://x/y")  # success
            assert t._bucket.rate == pytest.approx(10.0)
        finally:
            t.close()

    def test_custom_threshold_and_short_cooldown_flow(self, monkeypatch):
        monkeypatch.setattr(
            "baidu_finance.transport.subprocess.check_output",
            lambda command, text=True: '{"ok": 2}',
        )
        session = _SuspectSession()
        t = _offline_transport(
            monkeypatch,
            session,
            rate=0,
            suspect_retries=0,
            circuit_threshold=2,
            cooldown=0.0,
        )
        try:
            for i in range(2):
                with pytest.raises(TransportError, match="suspected block"):
                    t.get_json(f"https://x/y?code={i}")
            # cooldown=0 expires instantly: the next call is a half-open
            # probe and reaches the session again instead of failing fast.
            with pytest.raises(TransportError, match="suspected block"):
                t.get_json("https://x/y?code=9")
            assert session.calls == 3
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

    def test_legit_empty_range_keeps_data_key(self):
        """A weekend-only kline range must ship ``data: []``, not lose the key.

        This is the discriminator between a legitimately empty range and a
        soft-blocked quotation endpoint (design.md D1). The check only
        runs when the endpoint is observable: a health probe over a range
        with known trading days must return rows first. If the assertion
        ever fails on a healthy endpoint, the sniffer's structural rule
        must be demoted to a secondary signal.
        """
        t = RequestsTransport(rate=0)
        try:
            def kline(beg, end):
                return (
                    "https://finance.pae.baidu.com/vapi/v1/getquotation"
                    "?group=quotation_kline_ab&query=600519&code=600519"
                    "&market_type=ab&newFormat=1&is_kc=0&ktype=day"
                    f"&start_time={beg}+09:30&end_time={end}+09:30"
                    "&finClientType=pc&finClientType=pc"
                )

            try:
                healthy = t.get_json(kline("2026-08-20", "2026-08-28"))
                rows = ((healthy.get("Result") or {}).get("newMarketData") or {}).get(
                    "data"
                ) or []
            except TransportError:
                rows = []
            if not rows:
                pytest.skip(
                    "kline endpoint soft-blocked for this IP; "
                    "legit-empty shape not observable"
                )
            res = t.get_json(kline("2026-08-29", "2026-08-30"))
            market = ((res.get("Result") or {}).get("newMarketData")) or {}
            assert "data" in market
            assert market["data"] == []
        finally:
            t.close()
