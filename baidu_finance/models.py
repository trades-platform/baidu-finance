"""Data models and DataFrame schema constants."""

from __future__ import annotations

from dataclasses import dataclass

# Canonical column orders (part of the public contract).
KLINE_COLUMNS = ["open", "high", "low", "close", "volume"]
SECTOR_COLUMNS = ["name", "ratio"]
CONSTITUENT_COLUMNS = ["code", "name"]
CONNECT_COLUMNS = ["code", "name", "sector_code", "sector_name"]
US_CONSTITUENT_COLUMNS = ["code", "name", "market_value"]
US_MEMBER_COLUMNS = ["code", "name", "sector", "market_value"]
US_QUOTE_COLUMNS = [
    "name", "last", "change", "ratio", "volume", "amount", "market_value",
]


@dataclass(frozen=True)
class StockInfo:
    """Resolved stock identity.

    Attributes:
        code: Public stock code (e.g. ``"600519"``).
        name: Human-readable name (e.g. ``"贵州茅台"``).
        market_id: Baidu-internal market id used to build request URIs.
    """

    code: str
    name: str
    market_id: str
