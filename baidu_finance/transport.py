"""Transport layer: protocol and the default requests + wget implementation."""

from __future__ import annotations

import json
import random
import subprocess
import threading
import time
from typing import Callable, Optional, Protocol, Tuple, Union, runtime_checkable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://gushitong.baidu.com/",
    "Origin": "https://gushitong.baidu.com",
}

# Baidu's risk control randomly drops ~1/10 of rapid requests (TLS-level
# connection resets) and blocks sustained hammering. Requests are therefore
# metered by an adaptive token bucket: bursts up to ``burst`` fire at once
# (short parallel fan-outs keep their speed), sustained pulls are capped at
# ``rate`` requests/second, and the refill rate halves whenever the primary
# request fails — recovering as successes return — so throughput only
# degrades while Baidu actually pushes back.
_DEFAULT_RATE = 10.0
_DEFAULT_BURST = 10
_MIN_RATE = 0.5
_RECOVERY = 1.5

# Circuit breaker defaults. Baidu's risk-control blocks decay per endpoint
# on a minutes scale (observed: the info endpoint recovered in ~3 minutes,
# the quotation endpoint stayed blocked for 6+), so the cooldown starts at
# one minute and doubles on failed half-open probes up to a cap.
_DEFAULT_CIRCUIT_THRESHOLD = 3
_DEFAULT_COOLDOWN = 60.0
_MAX_COOLDOWN = 600.0
_DEFAULT_SUSPECT_RETRIES = 2

# Response classification against Baidu's risk control. Observed shapes:
# - soft block on info-style endpoints: HTTP 200, ``Result`` is null. This
#   is also what an unknown stock code returns, so a single hit is
#   ambiguous between "blocked" and "invalid code".
# - soft block on quotation endpoints: HTTP 200, ``newMarketData`` keeps
#   its ``headers``/``keys`` but loses the ``data`` key entirely. A
#   legitimately empty range is expected to still ship ``data: []``; this
#   discriminator could not be verified from a blocked (datacenter) IP and
#   is asserted by the live test, which skips while the endpoint is
#   blocked. If it is ever refuted, demote this structural rule to a
#   secondary signal — the cross-URL failure streak already gates the
#   breaker on its own.
# - hard block: HTTP 403 with ``{"Result": {"code": 403, "msg": "hit risk",
#   "isCaptchaEnabled": true}}``.
_NORMAL = "normal"
_SUSPECT = "suspect"
_HARD_RISK = "hard_risk"

JSON = Union[dict, list]


@runtime_checkable
class Transport(Protocol):
    """Contract for issuing GET requests that return parsed JSON."""

    def get_json(self, url: str) -> JSON:
        """Fetch ``url`` and return parsed JSON (dict or list)."""
        ...


def _classify_response(status: int, payload: object) -> str:
    """Classify a response against Baidu's risk-control signatures.

    Returns ``_NORMAL``, ``_SUSPECT`` (soft block / invalid-code shape) or
    ``_HARD_RISK`` (explicit 403 risk rejection). Payloads that are not
    Baidu envelopes (no ``Result`` key, non-dict bodies) are ``_NORMAL``:
    the sniffer must never reject shapes it does not know.
    """
    if not isinstance(payload, dict):
        return _NORMAL
    if status == 403:
        result = payload.get("Result")
        if isinstance(result, dict) and result.get("msg") == "hit risk":
            return _HARD_RISK
        return _NORMAL
    result = payload.get("Result")
    if "Result" in payload and result is None:
        return _SUSPECT
    if isinstance(result, dict):
        market = result.get("newMarketData")
        if isinstance(market, dict) and "data" not in market:
            return _SUSPECT
    return _NORMAL


def _endpoint_key(url: str) -> str:
    """Circuit granularity: host + path, query string excluded.

    Same-host endpoints decay independently (observed), so the path stays;
    query strings vary per call and would split the state.
    """
    parts = urlparse(url)
    return f"{parts.netloc}{parts.path}"


class _AdaptiveBucket:
    """Thread-safe token bucket whose refill rate adapts to outcomes.

    ``acquire`` consumes one token, sleeping only when the bucket is empty,
    so isolated interactive calls never wait while long parallel pulls are
    capped at the sustained ``rate``. Isolated failures do not touch the
    rate (Baidu randomly resets ~1/10 of rapid requests — noise); only a
    sustained failure confirmed by the circuit breaker calls ``penalize``,
    which halves the refill rate down to ``min_rate``. Successes scale it
    back toward ``rate`` via ``note``. Waits carry bounded random jitter so
    sustained pacing never looks metronome-exact.
    """

    def __init__(
        self,
        rate: float,
        burst: int,
        min_rate: float,
        rand: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.rate = rate
        self.base_rate = rate
        self.min_rate = min_rate
        self.burst = float(burst)
        self.tokens = float(burst)
        self._updated = time.monotonic()
        self._rand = rand
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self._updated) * self.rate)
        self._updated = now

    def acquire(self) -> None:
        # The lock is held across the sleep on purpose: pacing is global, so
        # concurrent callers queue behind the current slot instead of firing
        # a burst the moment it releases.
        with self._lock:
            self._refill()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            wait = (1.0 - self.tokens) / self.rate
            time.sleep(wait * self._rand(0.75, 1.25))
            self._refill()
            self.tokens = max(0.0, self.tokens - 1.0)

    def note(self, ok: bool) -> None:
        with self._lock:
            self._refill()
            if ok:
                self.rate = min(self.base_rate, self.rate * _RECOVERY)

    def penalize(self) -> None:
        with self._lock:
            self._refill()
            self.rate = max(self.min_rate, self.rate / 2)


class _Circuit:
    """Thread-safe per-endpoint circuit breaker.

    ``guard`` raises :class:`TransportError` while the endpoint is cooling
    down, so blocked endpoints see zero traffic; when the cooldown expires
    it admits a single half-open probe. ``record`` closes the breaker on
    success, doubles the cooldown on a failed probe (capped at
    ``max_cooldown``) and trips after ``threshold`` consecutive failures.

    Failures only count across *distinct* URLs: the same URL failing
    repeatedly (an unknown stock code also returns ``Result: null``) is a
    per-code problem and must not take the endpoint down. ``record``
    returns ``True`` when a sustained failure is confirmed — the signal
    that gates the bucket's rate penalty.
    """

    def __init__(
        self,
        threshold: int = _DEFAULT_CIRCUIT_THRESHOLD,
        cooldown: float = _DEFAULT_COOLDOWN,
        max_cooldown: float = _MAX_COOLDOWN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self.max_cooldown = max_cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._streak = 0
        self._last_fail_url: Optional[str] = None
        self._open_until = 0.0
        self._current_cooldown = cooldown
        self._half_open = False

    def guard(self) -> None:
        """Raise while cooling down; expire into a single half-open probe."""
        with self._lock:
            now = self._clock()
            if self._half_open:
                raise TransportError(
                    "endpoint probe in flight after cooldown"
                )
            if now < self._open_until:
                raise TransportError(
                    "endpoint is cooling down after sustained failures "
                    f"(retry in {self._open_until - now:.0f}s)"
                )
            if self._open_until > 0.0:
                self._half_open = True

    def record(self, url: str, ok: bool) -> bool:
        """Record an outcome; ``True`` means a sustained failure was confirmed."""
        with self._lock:
            if ok:
                was_probing = self._half_open
                self._half_open = False
                self._open_until = 0.0
                self._streak = 0
                self._last_fail_url = None
                if was_probing:
                    self._current_cooldown = self.cooldown
                return False
            if self._half_open:
                self._half_open = False
                self._current_cooldown = min(
                    self.max_cooldown, self._current_cooldown * 2
                )
                self._open_until = self._clock() + self._current_cooldown
                self._streak = 0
                self._last_fail_url = None
                return True
            if url == self._last_fail_url:
                return False
            self._last_fail_url = url
            self._streak += 1
            if self._streak >= self.threshold:
                self._streak = 0
                self._current_cooldown = self.cooldown
                self._open_until = self._clock() + self._current_cooldown
                return True
            return False

    def force_open(self) -> None:
        """Open immediately — the response said 'hit risk', no streak needed."""
        with self._lock:
            self._half_open = False
            self._current_cooldown = self.cooldown
            self._open_until = self._clock() + self._current_cooldown
            self._streak = 0
            self._last_fail_url = None


class RequestsTransport:
    """Default transport: a pooled ``requests`` session with a wget fallback.

    Each calling thread gets its own session (``requests.Session`` is not
    thread-safe). Sessions are created lazily and released by :meth:`close`.
    On a failed request the transport retries via a ``wget`` subprocess before
    giving up, mirroring the resilience of the original implementation.

    To stay under Baidu's risk control, requests pass through an adaptive
    token bucket shared by all threads: up to ``burst`` requests may fire
    back-to-back, sustained traffic is capped at ``rate`` per second, and
    the cap halves only on sustained failures confirmed by per-endpoint
    circuit breakers (isolated failures are noise), recovering as
    successes return. Responses are sniffed for risk-control shapes: a
    soft block (HTTP 200 with an emptied envelope) is retried briefly and
    then reported as a :class:`TransportError` without invoking wget; a
    hard block (403 "hit risk") opens the endpoint's circuit immediately.
    While an endpoint is cooling down its calls fail fast with zero
    network traffic. All requests carry full browser headers; connection
    errors and 429/5xx responses are retried with backoff inside the
    session, and the wget fallback (same headers) covers network-layer
    and non-risk HTTP failures.

    Args:
        timeout: ``(connect, read)`` timeout in seconds per request.
        retries: Session-level retries with exponential backoff.
        pool_connections / pool_maxsize: Pool sizing per session adapter.
        rate: Sustained request cap per second across all threads.
            Defaults to 10; ``0`` disables pacing entirely.
        burst: How many requests may fire back-to-back before pacing kicks in.
        circuit_threshold: Consecutive failures (across distinct URLs) that
            trip an endpoint's breaker.
        cooldown: Initial circuit cooldown in seconds; doubles on failed
            half-open probes up to ``max_cooldown``.
        suspect_retries: Transport-level retries for soft-block-shaped 200
            responses before giving up with a ``TransportError``.
        headers: Extra headers merged over the browser-like defaults.
    """

    def __init__(
        self,
        *,
        timeout: Tuple[float, float] = (2, 5),
        retries: int = 3,
        pool_connections: int = 10,
        pool_maxsize: int = 20,
        rate: float = _DEFAULT_RATE,
        burst: int = _DEFAULT_BURST,
        circuit_threshold: int = _DEFAULT_CIRCUIT_THRESHOLD,
        cooldown: float = _DEFAULT_COOLDOWN,
        max_cooldown: float = _MAX_COOLDOWN,
        suspect_retries: int = _DEFAULT_SUSPECT_RETRIES,
        headers: Optional[dict] = None,
    ) -> None:
        self._timeout = timeout
        self._retries = retries
        self._pool_connections = pool_connections
        self._pool_maxsize = pool_maxsize
        self._headers = dict(_DEFAULT_HEADERS)
        if headers:
            self._headers.update(headers)
        self._bucket = _AdaptiveBucket(rate, burst, _MIN_RATE) if rate > 0 else None
        self._circuit_threshold = circuit_threshold
        self._cooldown = cooldown
        self._max_cooldown = max_cooldown
        self._suspect_retries = suspect_retries
        self._circuits: dict[str, _Circuit] = {}
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._lock = threading.Lock()

    @property
    def _session(self) -> Optional[requests.Session]:
        """The calling thread's session, or ``None`` if it has not been used."""
        return getattr(self._local, "session", None)

    def _make_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(self._headers)
        adapter = HTTPAdapter(
            pool_connections=self._pool_connections,
            pool_maxsize=self._pool_maxsize,
            max_retries=Retry(
                total=self._retries,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
            ),
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _get_session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            with self._lock:
                live = session in self._sessions
            if not live:
                session = None
        if session is None:
            session = self._make_session()
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def _pace(self) -> None:
        if self._bucket is not None:
            self._bucket.acquire()

    def _note_outcome(self, ok: bool) -> None:
        if self._bucket is not None:
            self._bucket.note(ok)

    def _penalize(self) -> None:
        if self._bucket is not None:
            self._bucket.penalize()

    def _circuit_for(self, url: str) -> _Circuit:
        key = _endpoint_key(url)
        with self._lock:
            circuit = self._circuits.get(key)
            if circuit is None:
                circuit = _Circuit(
                    self._circuit_threshold, self._cooldown, self._max_cooldown
                )
                self._circuits[key] = circuit
            return circuit

    def get_json(self, url: str) -> JSON:
        circuit = self._circuit_for(url)
        circuit.guard()
        for _ in range(self._suspect_retries + 1):
            self._pace()
            try:
                resp = self._get_session().get(url, timeout=self._timeout)
                data = resp.json()
            except Exception:
                if circuit.record(url, ok=False):
                    self._penalize()
                return self._wget_fallback(url, circuit)
            verdict = _classify_response(resp.status_code, data)
            if verdict == _HARD_RISK:
                circuit.force_open()
                self._penalize()
                raise TransportError(
                    f"Blocked by Baidu risk control (403 hit risk) for {url!r}"
                )
            if verdict == _SUSPECT:
                # Cheap 200s: retry briefly, but never hand a soft block to
                # wget — it would fetch the same emptied envelope.
                if circuit.record(url, ok=False):
                    self._penalize()
                continue
            if resp.status_code >= 400:
                if circuit.record(url, ok=False):
                    self._penalize()
                return self._wget_fallback(url, circuit)
            circuit.record(url, ok=True)
            self._note_outcome(ok=True)
            return data
        raise TransportError(
            f"Empty risk-control responses for {url!r}: suspected block "
            f"or invalid code"
        )

    def _wget_fallback(self, url: str, circuit: _Circuit) -> JSON:
        self._pace()
        command = [
            "wget", "-q", "-O", "-",
            f"--timeout={max(1, round(self._timeout[1]))}",
            url,
        ]
        for key, value in self._headers.items():
            command += ["--header", f"{key}: {value}"]
        try:
            txt = subprocess.check_output(command, text=True)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"Failed to fetch {url!r}: {exc}") from exc
        circuit.record(url, ok=True)
        self._note_outcome(ok=True)
        return json.loads(txt)

    def close(self) -> None:
        """Release every thread's session."""
        with self._lock:
            sessions = self._sessions
            self._sessions = []
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass
        self._local.session = None


class TransportError(RuntimeError):
    """Raised when both the primary request and the wget fallback fail."""
