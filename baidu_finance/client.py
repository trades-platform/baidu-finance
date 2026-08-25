"""The public ``Client`` facade composing stock and sector APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Union

import pandas as pd

from .cache import Cache, MemoryCache
from .enums import Adjust, Period
from .models import StockInfo
from .sector import SectorAPI
from .stock import StockAPI
from .transport import RequestsTransport, Transport


class Client:
    """Baidu market-data client.

    Stocks are addressed by public code; industries, concepts, HK sectors,
    and US sectors are addressed by public name. Baidu internal ids never
    appear in returns.

    Args:
        transport: Transport implementation. Defaults to :class:`RequestsTransport`.
        cache: Cache implementation. Defaults to :class:`MemoryCache`.
    """

    def __init__(
        self,
        *,
        transport: Optional[Transport] = None,
        cache: Optional[Cache] = None,
    ) -> None:
        self._transport = transport if transport is not None else RequestsTransport()
        self._cache = cache if cache is not None else MemoryCache()
        self._stock = StockAPI(self._transport, self._cache)
        self._sector = SectorAPI(self._transport, self._cache)

    # ── stock ────────────────────────────────────────────────────────────
    def get_info(self, code: str) -> StockInfo:
        """Resolve a stock code to a :class:`StockInfo`."""
        return self._stock.get_info(code)

    def get_kline(
        self,
        code: str,
        period: Union[Period, str] = Period.DAILY,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        adjust: Union[Adjust, str] = Adjust.QFQ,
    ) -> pd.DataFrame:
        """Fetch OHLCV K-line for a stock code."""
        return self._stock.get_kline(code, period, start, end, adjust)

    # ── industries ───────────────────────────────────────────────────────
    def list_industries(self) -> pd.DataFrame:
        """List SW2 industries as ``[name, ratio]``."""
        return self._sector.list_industries()

    def industry_constituents(self, name: str) -> pd.DataFrame:
        """Member stocks of an industry as ``[code, name]``."""
        return self._sector.industry_constituents(name)

    def industry_kline(
        self, name: str, period: Union[Period, str], start: datetime, end: datetime
    ) -> pd.DataFrame:
        """OHLCV K-line for an industry, by name."""
        return self._sector.industry_kline(name, period, start, end)

    # ── concepts ─────────────────────────────────────────────────────────
    def list_concepts(self) -> pd.DataFrame:
        """List concepts as ``[name, ratio]``."""
        return self._sector.list_concepts()

    def concept_constituents(self, name: str) -> pd.DataFrame:
        """Member stocks of a concept as ``[code, name]``."""
        return self._sector.concept_constituents(name)

    def concept_kline(
        self, name: str, period: Union[Period, str], start: datetime, end: datetime
    ) -> pd.DataFrame:
        """OHLCV K-line for a concept, by name."""
        return self._sector.concept_kline(name, period, start, end)

    # ── hong kong ────────────────────────────────────────────────────────
    def list_hk_sectors(self) -> pd.DataFrame:
        """List HK sectors as ``[name, ratio]``."""
        return self._sector.list_hk_sectors()

    def hk_sector_constituents(self, name: str) -> pd.DataFrame:
        """Member stocks of an HK sector as ``[code, name]``."""
        return self._sector.hk_sector_constituents(name)

    def hk_sector_kline(
        self, name: str, period: Union[Period, str], start: datetime, end: datetime
    ) -> pd.DataFrame:
        """OHLCV K-line for an HK sector, by name."""
        return self._sector.hk_sector_kline(name, period, start, end)

    def hk_stock_connect(
        self, connect_type: str = "HGTGGT", page: int = 0, page_size: int = 5000
    ) -> pd.DataFrame:
        """HK Stock-Connect constituents as ``[code, name, sector_code, sector_name]``."""
        return self._sector.hk_stock_connect(connect_type, page, page_size)

    # ── united states ────────────────────────────────────────────────────
    def list_us_sectors(self) -> pd.DataFrame:
        """List US sectors as ``[name, ratio]``."""
        return self._sector.list_us_sectors()

    def us_sector_constituents(self, name: str) -> pd.DataFrame:
        """Member stocks of a US sector as ``[code, name, market_value]``."""
        return self._sector.us_sector_constituents(name)

    def us_sector_kline(
        self, name: str, period: Union[Period, str], start: datetime, end: datetime
    ) -> pd.DataFrame:
        """OHLCV K-line for a US sector, by name."""
        return self._sector.us_sector_kline(name, period, start, end)

    def us_all_constituents(self) -> pd.DataFrame:
        """All US sector members as ``[code, name, sector, market_value]``."""
        return self._sector.us_all_constituents()

    # ── lifecycle ────────────────────────────────────────────────────────
    def close(self) -> None:
        """Release transport resources."""
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()
