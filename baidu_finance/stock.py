"""Stock data: info resolution and K-line retrieval."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Union

import pandas as pd

from .cache import Cache
from .enums import Adjust, Period
from .models import StockInfo
from .parsing import parse_market_data
from .transport import Transport

_STOCK_TAG = "baidu:stock"
_INFO_URL = "https://finance.pae.baidu.com/vapi/v1/stockrelatedobjects?code={code}"
_QUOTATION_PREFIX = (
    "https://finance.pae.baidu.com/vapi/v1/getquotation"
    "?srcid=5353&all=1&pointType=string&"
)

_SEVEN_DAYS = 7 * 86400
_ONE_DAY = 86400


class StockAPI:
    """Stock info and K-line, keyed by public stock code."""

    def __init__(self, transport: Transport, cache: Cache) -> None:
        self._transport = transport
        self._cache = cache

    def get_info(self, code: str) -> StockInfo:
        """Resolve ``code`` to a :class:`StockInfo`, caching the result.

        Cached under ``baidu:stock`` keyed by the public code. Entries expire
        after 7 days, or 1 day when the name carries an ``XR``/``XD``/``DR``
        corporate-action marker.
        """
        cached = self._cache.get(code, tag=_STOCK_TAG)
        if cached:
            return StockInfo(code=code, name=cached["name"], market_id=cached["market_id"])

        res = self._transport.get_json(_INFO_URL.format(code=code))
        result = res["Result"]
        market_id = result["market"]
        name = result["stockName"]

        expire = _ONE_DAY if any(tag in name for tag in ("XR", "XD", "DR")) else _SEVEN_DAYS
        self._cache.set(
            code, {"name": name, "market_id": market_id}, tag=_STOCK_TAG, expire=expire
        )
        return StockInfo(code=code, name=name, market_id=market_id)

    def get_kline(
        self,
        code: str,
        period: Union[Period, str],
        start: datetime,
        end: datetime,
        adjust: Union[Adjust, str] = Adjust.QFQ,
    ) -> pd.DataFrame:
        """Fetch OHLCV K-line for ``code``.

        ``adjust`` is accepted for API parity but ignored by Baidu. K-line data
        is never cached.
        """
        period = Period.parse(period)
        Adjust.parse(adjust)  # validate only
        info = self.get_info(code)

        beg = start.strftime("%Y-%m-%d+%H:%M")
        end_q = (end + timedelta(days=1)).strftime("%Y-%m-%d+%H:%M")
        suffix = (
            f"group=quotation_kline_{info.market_id}&query={code}&code={code}"
            f"&market_type={info.market_id}&newFormat=1&is_kc=0"
            f"&ktype={period.baidu_ktype}&start_time={beg}&end_time={end_q}"
            f"&finClientType=pc&finClientType=pc"
        )
        res = self._transport.get_json(_QUOTATION_PREFIX + suffix)
        return parse_market_data(
            res.get("Result"), code=code, name=info.name, start=start
        )
