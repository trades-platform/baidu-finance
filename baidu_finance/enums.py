"""Type definitions for baidu-finance: Period and Adjust enums."""

from __future__ import annotations

from enum import Enum
from typing import Union


class Period(str, Enum):
    """K-line frequency. Inherits ``str`` for direct comparison with strings."""

    MIN_1 = "1m"
    MIN_5 = "5m"
    MIN_15 = "15m"
    MIN_30 = "30m"
    MIN_60 = "60m"
    MIN_120 = "120m"
    DAILY = "1d"
    WEEKLY = "1w"
    MONTHLY = "1M"

    @classmethod
    def parse(cls, value: Union["Period", str]) -> "Period":
        """Parse a ``Period`` enum or string/alias into a ``Period``."""
        if isinstance(value, cls):
            return value
        for member in cls:
            if member.value == value:
                return member
        aliases = {
            "1": cls.MIN_1, "1min": cls.MIN_1,
            "5": cls.MIN_5, "5min": cls.MIN_5,
            "15": cls.MIN_15, "15min": cls.MIN_15,
            "30": cls.MIN_30, "30min": cls.MIN_30,
            "60": cls.MIN_60, "60min": cls.MIN_60, "1h": cls.MIN_60,
            "120": cls.MIN_120, "120min": cls.MIN_120, "2h": cls.MIN_120,
            "daily": cls.DAILY, "day": cls.DAILY, "d": cls.DAILY,
            "weekly": cls.WEEKLY, "week": cls.WEEKLY, "w": cls.WEEKLY,
            "monthly": cls.MONTHLY, "month": cls.MONTHLY, "M": cls.MONTHLY,
        }
        if value in aliases:
            return aliases[value]
        raise ValueError(f"Unknown period: {value!r}")

    @property
    def baidu_ktype(self) -> str:
        """The ``ktype`` token Baidu's quotation endpoint expects."""
        mapping = {
            Period.DAILY: "day",
            Period.WEEKLY: "week",
            Period.MONTHLY: "month",
            Period.MIN_1: "min1",
            Period.MIN_5: "min5",
            Period.MIN_15: "min15",
            Period.MIN_30: "min30",
            Period.MIN_60: "min60",
            Period.MIN_120: "min120",
        }
        return mapping[self]


class Adjust(str, Enum):
    """Price adjustment type. Baidu ignores this; kept for API parity."""

    NONE = "none"
    QFQ = "qfq"
    HFQ = "hfq"

    @classmethod
    def parse(cls, value: Union["Adjust", str]) -> "Adjust":
        """Parse an ``Adjust`` enum or string into an ``Adjust``."""
        if isinstance(value, cls):
            return value
        for member in cls:
            if member.value == value:
                return member
        aliases = {"forward": cls.QFQ, "backward": cls.HFQ}
        if value in aliases:
            return aliases[value]
        raise ValueError(f"Unknown adjust: {value!r}")
