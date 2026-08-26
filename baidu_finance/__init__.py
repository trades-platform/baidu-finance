"""baidu-finance — Baidu market data SDK."""

from __future__ import annotations

__version__ = "0.1.0"

from .client import Client
from .enums import Adjust, Period
from .models import StockInfo
from .sector import SectorNotFoundError

__all__ = [
    "Client", "Period", "Adjust", "StockInfo", "SectorNotFoundError", "__version__",
]
