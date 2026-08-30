"""Transport layer: protocol and the default requests + wget implementation."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Optional, Protocol, Tuple, Union, runtime_checkable

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

JSON = Union[dict, list]


@runtime_checkable
class Transport(Protocol):
    """Contract for issuing GET requests that return parsed JSON."""

    def get_json(self, url: str) -> JSON:
        """Fetch ``url`` and return parsed JSON (dict or list)."""
        ...


class _AdaptiveBucket:
    """Thread-safe token bucket whose refill rate adapts to outcomes.

    ``acquire`` consumes one token, sleeping only when the bucket is empty,
    so isolated interactive calls never wait while long parallel pulls are
    capped at the sustained ``rate``. Failures halve the refill rate (down
    to ``min_rate``); successes scale it back toward ``rate``.
    """

    def __init__(self, rate: float, burst: int, min_rate: float) -> None:
        self.rate = rate
        self.base_rate = rate
        self.min_rate = min_rate
        self.burst = float(burst)
        self.tokens = float(burst)
        self._updated = time.monotonic()
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
            time.sleep((1.0 - self.tokens) / self.rate)
            self._refill()
            self.tokens = max(0.0, self.tokens - 1.0)

    def note(self, ok: bool) -> None:
        with self._lock:
            self._refill()
            if ok:
                self.rate = min(self.base_rate, self.rate * _RECOVERY)
            else:
                self.rate = max(self.min_rate, self.rate / 2)


class RequestsTransport:
    """Default transport: a pooled ``requests`` session with a wget fallback.

    Each calling thread gets its own session (``requests.Session`` is not
    thread-safe). Sessions are created lazily and released by :meth:`close`.
    On a failed request the transport retries via a ``wget`` subprocess before
    giving up, mirroring the resilience of the original implementation.

    To stay under Baidu's risk control, requests pass through an adaptive
    token bucket shared by all threads: up to ``burst`` requests may fire
    back-to-back, sustained traffic is capped at ``rate`` per second, and the
    cap halves after any failed primary request, recovering as successes
    return. All requests carry full browser headers; connection errors and
    429/5xx responses are retried with backoff inside the session, and the
    wget fallback carries the same headers.

    Args:
        timeout: ``(connect, read)`` timeout in seconds per request.
        retries: Session-level retries with exponential backoff.
        pool_connections / pool_maxsize: Pool sizing per session adapter.
        rate: Sustained request cap per second across all threads.
            Defaults to 10; ``0`` disables pacing entirely.
        burst: How many requests may fire back-to-back before pacing kicks in.
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

    def get_json(self, url: str) -> JSON:
        self._pace()
        try:
            resp = self._get_session().get(url, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            self._note_outcome(ok=False)
            return self._wget_fallback(url)
        self._note_outcome(ok=True)
        return data

    def _wget_fallback(self, url: str) -> JSON:
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
            return json.loads(txt)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"Failed to fetch {url!r}: {exc}") from exc

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
