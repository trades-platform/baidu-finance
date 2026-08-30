"""baidu-finance — Baidu market data SDK."""

from __future__ import annotations

__version__ = "0.1.0"

from .client import Client
from .enums import Adjust, Period
from .index import INDEX_NAMES, IndexNotFoundError
from .models import StockInfo
from .sector import SectorNotFoundError

__all__ = [
    "Client", "Period", "Adjust", "StockInfo", "SectorNotFoundError",
    "IndexNotFoundError", "INDEX_NAMES", "__version__",
]
