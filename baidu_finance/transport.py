"""Transport layer: protocol and the default requests + wget implementation."""

from __future__ import annotations

import json
import subprocess
from typing import Optional, Protocol, Tuple, Union, runtime_checkable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
    )
}

JSON = Union[dict, list]


@runtime_checkable
class Transport(Protocol):
    """Contract for issuing GET requests that return parsed JSON."""

    def get_json(self, url: str) -> JSON:
        """Fetch ``url`` and return parsed JSON (dict or list)."""
        ...


class RequestsTransport:
    """Default transport: a pooled ``requests`` session with a wget fallback.

    The session is created lazily on first use and released by :meth:`close`.
    On a failed request the transport retries via a ``wget`` subprocess before
    giving up, mirroring the resilience of the original implementation.
    """

    def __init__(
        self,
        *,
        timeout: Tuple[float, float] = (2, 5),
        retries: int = 3,
        pool_connections: int = 10,
        pool_maxsize: int = 20,
    ) -> None:
        self._timeout = timeout
        self._retries = retries
        self._pool_connections = pool_connections
        self._pool_maxsize = pool_maxsize
        self._session: Optional[requests.Session] = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            session = requests.Session()
            session.headers.update(_DEFAULT_HEADERS)
            adapter = HTTPAdapter(
                pool_connections=self._pool_connections,
                pool_maxsize=self._pool_maxsize,
                max_retries=Retry(
                    total=self._retries,
                    backoff_factor=0.3,
                    status_forcelist=(500, 502, 503, 504),
                ),
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self._session = session
        return self._session

    def get_json(self, url: str) -> JSON:
        try:
            resp = self._get_session().get(url, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return self._wget_fallback(url)

    def _wget_fallback(self, url: str) -> JSON:
        try:
            txt = subprocess.check_output(
                f'wget -q -O - --timeout {self._timeout[1]} "{url}"',
                shell=True,
                text=True,
            )
            return json.loads(txt)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"Failed to fetch {url!r}: {exc}") from exc

    def close(self) -> None:
        """Release the underlying session."""
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None


class TransportError(RuntimeError):
    """Raised when both the primary request and the wget fallback fail."""
