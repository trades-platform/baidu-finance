"""Pure parsing helpers — no I/O, no side effects.

Converts Baidu's ``marketData`` payload (a ``;``/``,``-delimited string) into a
standard OHLCV DataFrame indexed by a ``DatetimeIndex``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from .models import KLINE_COLUMNS


def empty_kline() -> pd.DataFrame:
    """Return an empty OHLCV DataFrame with the standard columns."""
    df = pd.DataFrame(columns=KLINE_COLUMNS)
    df.index = pd.DatetimeIndex([], name="datetime")
    return df


def parse_market_data(
    result: Optional[dict],
    *,
    code: str = "",
    name: str = "",
    start: Optional[datetime] = None,
) -> pd.DataFrame:
    """Parse a Baidu quotation ``Result`` block into an OHLCV DataFrame.

    Args:
        result: The ``Result`` object from a Baidu quotation response, or a
            falsy value when there is no data.
        code: Public code stored in ``df.attrs['code']``.
        name: Display name stored in ``df.attrs['name']``.
        start: Optional lower bound; rows before it are dropped.

    Returns:
        DataFrame indexed by ``DatetimeIndex`` with columns
        ``open, high, low, close, volume``.
    """
    if not result:
        return empty_kline()

    market_data = result.get("newMarketData", {}).get("marketData", "")
    if not market_data:
        return empty_kline()

    rows = [item.split(",")[1:7] for item in market_data.split(";") if item]
    # Baidu's field order within marketData is: datetime, open, close, volume,
    # high, low.
    df = pd.DataFrame(
        data=rows,
        columns=["datetime", "open", "close", "volume", "high", "low"],
    )
    df = df.replace("--", 0)
    df["datetime"] = pd.to_datetime(df["datetime"])
    numeric = ["open", "close", "high", "low", "volume"]
    df[numeric] = df[numeric].astype(float)

    if start is not None:
        df = df[df["datetime"] >= start]

    df = df[["datetime", *KLINE_COLUMNS]].set_index("datetime")
    df.attrs["code"] = code
    df.attrs["name"] = name
    return df
