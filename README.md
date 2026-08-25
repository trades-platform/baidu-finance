# baidu-finance

Baidu market-data SDK: stock info, K-line, and sector (industry / concept / HK / US)
data via Baidu's finance endpoints.

## Install

```bash
pip install baidu-finance            # core (pandas + requests)
pip install baidu-finance[disk]      # + persistent diskcache-rs cache
```

## Public-handle contract

- **Stocks** are addressed by their standard public code: `600519`, `00700`.
- **Industries / concepts / HK / US sectors** are addressed by their public **name**:
  `白酒`, `人工智能`, `恒生科技`, `半导体`.

Baidu's internal routing ids (`market_id`, block `real_code`/`market`) are never
exposed — they are resolved and stored in the cache only.

## Usage

```python
from datetime import datetime, timedelta
from baidu_finance import Client, Period, Adjust

client = Client()                                   # in-memory cache by default

# stock
info = client.get_info("600519")                    # StockInfo(code, name, market_id)
end = datetime.now(); start = end - timedelta(days=30)
df = client.get_kline("000001", Period.DAILY, start, end, Adjust.QFQ)

# sectors (by name)
industries = client.list_industries()               # [name, ratio]
members = client.industry_constituents("白酒")        # [code, name]
kline = client.industry_kline("白酒", Period.DAILY, start, end)
us = client.list_us_sectors()                       # [name, ratio]
us_members = client.us_sector_constituents("半导体")  # [code, name, market_value]
us_all = client.us_all_constituents()               # [code, name, sector, market_value]

client.close()
```

### Persistent cache

```python
from baidu_finance import Client
from baidu_finance.cache import DiskCache      # requires baidu-finance[disk]

client = Client(cache=DiskCache("~/.baidu_finance_cache"))
```

## Tests

Tests run against **live** Baidu endpoints (no mocks) and are skip-gated:

```bash
BAIDU_LIVE=1 pytest          # run live tests
pytest                       # skipped by default
```
