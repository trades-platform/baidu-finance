"""Sector data: industries (SW2), concepts, HK sectors, and US sectors.

All sectors are addressed by their public **name**. Each listing populates the
cache with ``name -> {real_code, market}`` so that subsequent constituent and
K-line calls can resolve the internal Baidu code without exposing it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable, Union

import pandas as pd

from .cache import Cache
from .enums import Period
from .models import (
    CONNECT_COLUMNS,
    CONSTITUENT_COLUMNS,
    SECTOR_COLUMNS,
    US_CONSTITUENT_COLUMNS,
    US_MEMBER_COLUMNS,
    US_QUOTE_COLUMNS,
)
from .parsing import parse_market_data
from .transport import Transport

_INDUSTRY_TAG = "baidu:industry"
_CONCEPT_TAG = "baidu:concept"
_HK_SECTOR_TAG = "baidu:hk_sector"
_US_SECTOR_TAG = "baidu:us_sector"
_BLOCK_EXPIRE = 7 * 86400

_BLOCKS_URL = (
    "https://finance.pae.baidu.com/vapi/v2/blocks"
    "?pn=0&rn={rn}&market=ab&typeCode={type_code}&finClientType=pc&finClientType=pc"
)
_HEATMAP_BLOCKS_URL = (
    "https://finance.pae.baidu.com/vapi/v2/blocks"
    "?style=heatmap&market={market}&typeCode={type_code}&sortKey=amount&sortType=desc"
    "&pn=0&rn={rn}&finClientType=pc&finClientType=pc"
)
_BLOCK_KLINE_URL = (
    "https://finance.pae.baidu.com/vapi/v1/getquotation?pointType=string"
    "&group=quotation_block_kline&query={code}&code={code}&market_type={market}"
    "&ktype={ktype}&start_time={beg}&end_time={end}&finClientType=pc&finClientType=pc"
)
_A_CONSTITUENT_URL = (
    "https://gushitong.baidu.com/opendata?resource_id=5352&group=block_stocks"
    "&finance_type=block&code={code}&finance_type=block&market={market}"
    "&marketType={market}&query={code}&pn=0&rn=300&pc_web=1"
)
_SAPI_CONSTITUENT_URL = (
    "https://finance.pae.baidu.com/sapi/v1/constituents?financeType=block"
    "&market={market}&code={code}&sortKey=&sortType=&style=tablelist"
    "&pn={pn}&rn={rn}&finClientType=pc"
)
_SAPI_PAGE = 500
_SAPI_MAX_PAGES = 20
_US_CONSTITUENT_WORKERS = 8


class SectorNotFoundError(LookupError):
    """Raised when a sector name cannot be resolved even after a refresh."""


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _result_dict(payload: object) -> dict:
    return _as_dict(_as_dict(payload).get("Result"))


def _list_body(payload: object) -> list:
    """``Result.list.body``, or ``Result.list`` when it is already a sequence."""
    listing = _result_dict(payload).get("list")
    if isinstance(listing, dict):
        return _as_list(listing.get("body"))
    return _as_list(listing)


def _walk(obj: object, *path: object):
    cur = obj
    for key in path:
        if isinstance(key, int):
            if not isinstance(cur, list) or key >= len(cur) or key < -len(cur):
                return None
            cur = cur[key]
            continue
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _num(value: object) -> float:
    if value is None or value == "":
        return float("nan")
    try:
        return float(str(value).replace("%", "").replace("+", "").replace(",", ""))
    except (TypeError, ValueError):
        return float("nan")


def _raw_num(item: dict, key: str) -> float:
    return _num(_as_dict(item.get("rawData")).get(key))


class SectorAPI:
    """Industry, concept, HK-sector, and US-sector listings, constituents, and K-line."""

    def __init__(self, transport: Transport, cache: Cache) -> None:
        self._transport = transport
        self._cache = cache

    # ── listings ─────────────────────────────────────────────────────────
    def list_industries(self) -> pd.DataFrame:
        """List SW2 industries as ``[name, ratio]`` and populate the cache."""
        return self._list_ab_blocks("HY", 500, _INDUSTRY_TAG)

    def list_concepts(self) -> pd.DataFrame:
        """List concepts as ``[name, ratio]`` and populate the cache."""
        return self._list_ab_blocks("GN", 5000, _CONCEPT_TAG)

    def list_hk_sectors(self) -> pd.DataFrame:
        """List HK sectors as ``[name, ratio]`` and populate the cache."""
        return self._list_heatmap_blocks("hk", "HSHY", 100, _HK_SECTOR_TAG)

    def list_us_sectors(self) -> pd.DataFrame:
        """List US sectors as ``[name, ratio]`` and populate the cache."""
        return self._list_heatmap_blocks("us", "HY", 500, _US_SECTOR_TAG)

    def us_sector_quotes(self) -> pd.DataFrame:
        """Latest US sector quotes as ``[name, last, change, ratio, volume, amount, market_value]``."""
        rows = []
        for block in self._iter_heatmap_blocks("us", "HY", 500, _US_SECTOR_TAG):
            rows.append([
                block.get("name", ""),
                _raw_num(block, "lastPx"),
                _raw_num(block, "pxChange"),
                _raw_num(block, "pxChangeRate"),
                _raw_num(block, "volume"),
                _raw_num(block, "amount"),
                _raw_num(block, "marketValue"),
            ])
        return pd.DataFrame(rows, columns=US_QUOTE_COLUMNS)

    def us_sector_quote(self, name: str) -> pd.DataFrame:
        """Latest quote for one US sector, same columns as ``us_sector_quotes``.

        Uses the heatmap list (one request) and returns the matching row.
        Unknown names raise :class:`SectorNotFoundError`. Open/high/low and
        other OHLC fields are on :meth:`us_sector_kline`, not this snapshot.
        """
        df = self.us_sector_quotes()
        hit = df.loc[df["name"].eq(name)].reset_index(drop=True)
        if hit.empty:
            raise SectorNotFoundError(f"Sector not found: {name!r}")
        return hit

    def _list_heatmap_blocks(
        self, market: str, type_code: str, rn: int, tag: str
    ) -> pd.DataFrame:
        rows = []
        for block in self._iter_heatmap_blocks(market, type_code, rn, tag):
            rows.append([block.get("name", ""), _num(block.get("pxChangeRate"))])
        return pd.DataFrame(rows, columns=SECTOR_COLUMNS)

    def _iter_heatmap_blocks(self, market: str, type_code: str, rn: int, tag: str):
        data = self._transport.get_json(
            _HEATMAP_BLOCKS_URL.format(market=market, type_code=type_code, rn=rn)
        )
        for block in _list_body(data):
            if not isinstance(block, dict):
                continue
            name = block.get("name", "")
            if not name:
                continue
            self._cache.set(
                name,
                {"real_code": block.get("code", ""), "market": block.get("market", market)},
                tag=tag,
                expire=_BLOCK_EXPIRE,
            )
            yield block

    def _list_ab_blocks(self, type_code: str, rn: int, tag: str) -> pd.DataFrame:
        data = self._transport.get_json(_BLOCKS_URL.format(rn=rn, type_code=type_code))
        rows = []
        for block in _as_list(_result_dict(data).get("blocks")):
            if not isinstance(block, dict):
                continue
            name = block.get("name") or ""
            if not name:
                continue
            ratio_obj = block.get("ratio")
            ratio_val = ratio_obj.get("value") if isinstance(ratio_obj, dict) else ratio_obj
            self._cache.set(
                name,
                {"real_code": block.get("code", ""), "market": block.get("market", "")},
                tag=tag,
                expire=_BLOCK_EXPIRE,
            )
            rows.append([name, _num(ratio_val)])
        return pd.DataFrame(rows, columns=SECTOR_COLUMNS)

    # ── resolution ───────────────────────────────────────────────────────
    def _resolve(self, name: str, tag: str, refresh: Callable[[], pd.DataFrame]) -> dict:
        item = self._cache.get(name, tag=tag)
        if item:
            return item
        refresh()
        item = self._cache.get(name, tag=tag)
        if item:
            return item
        raise SectorNotFoundError(f"Sector not found: {name!r}")

    # ── constituents ─────────────────────────────────────────────────────
    def industry_constituents(self, name: str) -> pd.DataFrame:
        """Return ``[code, name]`` member stocks of an industry."""
        info = self._resolve(name, _INDUSTRY_TAG, self.list_industries)
        return self._ab_constituents(info["real_code"], info["market"])

    def concept_constituents(self, name: str) -> pd.DataFrame:
        """Return ``[code, name]`` member stocks of a concept."""
        info = self._resolve(name, _CONCEPT_TAG, self.list_concepts)
        return self._ab_constituents(info["real_code"], info["market"])

    def hk_sector_constituents(self, name: str) -> pd.DataFrame:
        """Return ``[code, name]`` member stocks of an HK sector."""
        info = self._resolve(name, _HK_SECTOR_TAG, self.list_hk_sectors)
        return self._sapi_constituents(info["real_code"], info["market"])

    def us_sector_constituents(self, name: str) -> pd.DataFrame:
        """Return ``[code, name, market_value]`` member stocks of a US sector."""
        info = self._resolve(name, _US_SECTOR_TAG, self.list_us_sectors)
        rows = [
            [x.get("code", ""), x.get("name", ""), _raw_num(x, "marketValue")]
            for x in self._iter_sapi_members(info["real_code"], info["market"])
        ]
        return pd.DataFrame(rows, columns=US_CONSTITUENT_COLUMNS)

    def us_all_constituents(self) -> pd.DataFrame:
        """All US sector members as ``[code, name, sector, market_value]``.

        Lists every US sector, then fetches each sector's constituents
        concurrently. ``RequestsTransport`` gives each worker its own session.
        ``sector`` is the public sector name; ``market_value`` is USD.
        """
        sectors = self.list_us_sectors()
        jobs: list[tuple[str, str, str]] = []
        names = sectors["name"].tolist() if not sectors.empty else []
        for name in names:
            info = self._cache.get(name, tag=_US_SECTOR_TAG)
            if info and info.get("real_code"):
                jobs.append((name, info["real_code"], info["market"]))
        if not jobs:
            return pd.DataFrame(columns=US_MEMBER_COLUMNS)

        def _one(job: tuple[str, str, str]) -> pd.DataFrame:
            name, code, market = job
            rows = [
                [x.get("code", ""), x.get("name", ""), name, _raw_num(x, "marketValue")]
                for x in self._iter_sapi_members(code, market)
            ]
            if not rows:
                return pd.DataFrame(columns=US_MEMBER_COLUMNS)
            return pd.DataFrame(rows, columns=US_MEMBER_COLUMNS)

        workers = min(_US_CONSTITUENT_WORKERS, len(jobs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            frames = list(pool.map(_one, jobs))
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame(columns=US_MEMBER_COLUMNS)
        return pd.concat(frames, ignore_index=True)

    def _ab_constituents(self, code: str, market: str) -> pd.DataFrame:
        res = self._transport.get_json(_A_CONSTITUENT_URL.format(code=code, market=market))
        listing = _walk(
            res, "Result", 0, "DisplayData", "resultData", "tplData", "result", "list"
        )
        rows = []
        for item in _as_list(listing):
            if not isinstance(item, dict) or not item.get("code"):
                continue
            rows.append([item.get("code", ""), item.get("name", "")])
        return pd.DataFrame(rows, columns=CONSTITUENT_COLUMNS)

    def _sapi_constituents(self, code: str, market: str) -> pd.DataFrame:
        """Fetch all members as ``[code, name]``; ``pn`` is an offset, not a page."""
        rows = [
            [x.get("code", ""), x.get("name", "")]
            for x in self._iter_sapi_members(code, market)
        ]
        return pd.DataFrame(rows, columns=CONSTITUENT_COLUMNS)

    def _iter_sapi_members(self, code: str, market: str):
        """Yield raw constituent dicts, paginating by offset up to ``_SAPI_MAX_PAGES``."""
        offset = 0
        for _ in range(_SAPI_MAX_PAGES):
            res = self._transport.get_json(
                _SAPI_CONSTITUENT_URL.format(
                    code=code, market=market, pn=offset, rn=_SAPI_PAGE
                )
            )
            body = _list_body(res)
            for item in body:
                if isinstance(item, dict) and item.get("code"):
                    yield item
            if len(body) < _SAPI_PAGE:
                return
            offset += _SAPI_PAGE

    # ── kline ────────────────────────────────────────────────────────────
    def industry_kline(
        self, name: str, period: Union[Period, str], start: datetime, end: datetime
    ) -> pd.DataFrame:
        """OHLCV K-line for an industry, addressed by name."""
        info = self._resolve(name, _INDUSTRY_TAG, self.list_industries)
        return self._block_kline(name, info["real_code"], info["market"], period, start, end)

    def concept_kline(
        self, name: str, period: Union[Period, str], start: datetime, end: datetime
    ) -> pd.DataFrame:
        """OHLCV K-line for a concept, addressed by name."""
        info = self._resolve(name, _CONCEPT_TAG, self.list_concepts)
        return self._block_kline(name, info["real_code"], info["market"], period, start, end)

    def hk_sector_kline(
        self, name: str, period: Union[Period, str], start: datetime, end: datetime
    ) -> pd.DataFrame:
        """OHLCV K-line for an HK sector, addressed by name."""
        info = self._resolve(name, _HK_SECTOR_TAG, self.list_hk_sectors)
        return self._block_kline(name, info["real_code"], info["market"], period, start, end)

    def us_sector_kline(
        self, name: str, period: Union[Period, str], start: datetime, end: datetime
    ) -> pd.DataFrame:
        """OHLCV K-line for a US sector, addressed by name."""
        info = self._resolve(name, _US_SECTOR_TAG, self.list_us_sectors)
        return self._block_kline(name, info["real_code"], info["market"], period, start, end)

    def _block_kline(
        self,
        name: str,
        code: str,
        market: str,
        period: Union[Period, str],
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        period = Period.parse(period)
        beg = start.strftime("%Y-%m-%d+%H:%M")
        end_q = (end + timedelta(days=1)).strftime("%Y-%m-%d+%H:%M")
        url = _BLOCK_KLINE_URL.format(
            code=code, market=market, ktype=period.baidu_ktype, beg=beg, end=end_q
        )
        res = self._transport.get_json(url)
        result = res.get("Result") if isinstance(res, dict) else None
        return parse_market_data(result, code=code, name=name, start=start)

    # ── stock connect ────────────────────────────────────────────────────
    def hk_stock_connect(
        self, connect_type: str = "HGTGGT", page: int = 0, page_size: int = 5000
    ) -> pd.DataFrame:
        """Return HK Stock-Connect constituents.

        Columns: ``code, name, sector_code, sector_name``.
        """
        res = self._transport.get_json(
            _SAPI_CONSTITUENT_URL.format(
                code=connect_type, market="hk", pn=page, rn=page_size
            )
        )
        body = _list_body(res)
        rows = []
        for item in body:
            if not isinstance(item, dict) or not item.get("code"):
                continue
            block = item.get("block")
            sector_code = block.get("code", "") if isinstance(block, dict) else ""
            sector_name = block.get("name", "") if isinstance(block, dict) else ""
            rows.append([item.get("code", ""), item.get("name", ""), sector_code, sector_name])
        return pd.DataFrame(rows, columns=CONNECT_COLUMNS)
