"""Index data: K-lines and constituents for A-share market indices.

Baidu's index library is incomplete and, worse, its quotation backend
**silently falls back to the colliding Shenzhen stock when a bare 6-digit
index code has no index data**: ``000985`` (中证全指) yields the price history
of 大庆华科 (000985.SZ), byte-identical under ``isIndex=true`` and
``isStock=true``. The fallback is indistinguishable inside the quotation
payload itself, so every entry point here first verifies that the code exists
in Baidu's *index* constituents library (real indices always have members;
fallback stocks do not) and raises :class:`IndexNotFoundError` otherwise.
Verified codes are cached for 7 days.

Indices that Baidu simply does not carry (e.g. ``932000`` 中证2000,
``930903`` 中证A股) fail the same check and raise the same error.
"""

from __future__ import annotations

from datetime import datetime
from typing import Union

import pandas as pd

from .cache import Cache
from .enums import Period
from .models import CONSTITUENT_COLUMNS
from .parsing import parse_index_market_data
from .transport import Transport

_INDEX_TAG = "baidu:index"
_VERIFY_EXPIRE = 7 * 86400
_CONS_PAGE = 500
_CONS_MAX_PAGES = 20

# Validated against official constituent counts on 2026-08-28. Display-only:
# Baidu exposes no name for index objects, so codes outside this map get an
# empty name. Unlisted codes may still work if Baidu carries the index.
INDEX_NAMES = {
    "000300": "沪深300",
    "000852": "中证1000",
    "000905": "中证500",
    "000680": "科创综指",
    "000688": "科创50",
    "399006": "创业板指",
    "399673": "创业板50",
    "899050": "北证50",
}

# The selfselect endpoint takes numeric ktypes and carries no minute bars
# (ktype 4/7/8/9 return empty), so only calendar periods are supported.
_KTYPE = {Period.DAILY: "1", Period.WEEKLY: "2", Period.MONTHLY: "3"}

# Baidu ignores start_time on this endpoint (any non-empty window empties the
# response), so the full history is fetched and sliced client-side: ~2001
# daily bars back to mid-2018.
_KLINE_URL = (
    "https://finance.pae.baidu.com/selfselect/getstockquotation"
    "?all=1&isIndex=true&isBk=false&isBlock=false&isFutures=false&isStock=false"
    "&newFormat=1&group=quotation_index_kline&finClientType=pc"
    "&code={code}&market_type=ab&start_time=&ktype={ktype}"
)
_CONS_URL = (
    "https://finance.pae.baidu.com/sapi/v1/constituents?financeType=index"
    "&market=ab&code={code}&sortKey=&sortType=&style=tablelist"
    "&pn={pn}&rn={rn}&finClientType=pc"
)


class IndexNotFoundError(LookupError):
    """Raised when an index code is absent from Baidu's index library."""


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


class IndexAPI:
    """Index K-lines and constituents, keyed by public index code."""

    def __init__(self, transport: Transport, cache: Cache) -> None:
        self._transport = transport
        self._cache = cache

    def get_kline(
        self,
        code: str,
        period: Union[Period, str],
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Fetch OHLCV K-line for an index ``code``.

        ``start``/``end`` are inclusive calendar-date bounds applied
        client-side. Daily history reaches back to mid-2018 (~2001 bars).
        Minute periods are unsupported by the endpoint and raise
        :class:`ValueError`. Unknown indices raise
        :class:`IndexNotFoundError` *before* any quotation request, so a
        colliding stock's data can never be returned silently.
        """
        period = Period.parse(period)
        ktype = _KTYPE.get(period)
        if ktype is None:
            raise ValueError(
                f"Index K-lines support daily/weekly/monthly periods, got {period!r}"
            )
        self._ensure_index(code)

        res = self._transport.get_json(_KLINE_URL.format(code=code, ktype=ktype))
        result = res.get("Result") if isinstance(res, dict) else None
        return parse_index_market_data(
            result, code=code, name=INDEX_NAMES.get(code, ""), start=start, end=end
        )

    def get_constituents(self, code: str) -> pd.DataFrame:
        """Return ``[code, name]`` member stocks of an index, paginated."""
        self._ensure_index(code)

        rows = []
        offset = 0
        for _ in range(_CONS_MAX_PAGES):
            res = self._transport.get_json(
                _CONS_URL.format(code=code, pn=offset, rn=_CONS_PAGE)
            )
            result = _as_dict(res).get("Result")
            if offset == 0 and not isinstance(result, dict):
                raise IndexNotFoundError(
                    f"Index {code!r} not found in Baidu's index library"
                )
            # An exhausted page yields a bare []; a missing index yields None.
            lst = _as_dict(result).get("list")
            body = _as_list(_as_dict(lst).get("body"))
            for item in body:
                if isinstance(item, dict) and item.get("code"):
                    rows.append([item.get("code", ""), item.get("name", "")])
            if len(body) < _CONS_PAGE:
                break
            offset += _CONS_PAGE
        return pd.DataFrame(rows, columns=CONSTITUENT_COLUMNS)

    def _ensure_index(self, code: str) -> None:
        """Verify ``code`` exists in Baidu's index library, cache for 7 days."""
        if self._cache.get(code, tag=_INDEX_TAG):
            return
        if self._verify_index(code):
            return
        raise IndexNotFoundError(
            f"Index {code!r} not found in Baidu's index library; refusing to "
            "fetch because Baidu silently falls back to the colliding stock "
            "for such codes"
        )

    def _verify_index(self, code: str) -> bool:
        res = self._transport.get_json(_CONS_URL.format(code=code, pn=0, rn=1))
        result = _as_dict(res).get("Result")
        lst = _as_dict(result).get("list")
        body = _as_list(_as_dict(lst).get("body"))
        if len(body) > 0:
            self._cache.set(
                code, {"verified": True}, tag=_INDEX_TAG, expire=_VERIFY_EXPIRE
            )
            return True
        return False
