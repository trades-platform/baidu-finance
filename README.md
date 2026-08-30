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
- **Indices** are addressed by their public index code: `000300`, `899050`.
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
quotes = client.us_sector_quotes()                  # [name, last, change, ratio, volume, amount, market_value]
quote = client.us_sector_quote("半导体")              # one row, same columns as quotes

# indices (by code)
idx_kline = client.index_kline("000300", Period.DAILY, start, end)
idx_members = client.index_constituents("000852")    # [code, name]

client.close()
```

### Index coverage and the silent-fallback guard

Baidu's index library is incomplete, and for a bare 6-digit code with no index
data its quotation backend **silently returns the colliding Shenzhen stock**
(`000985` 中证全指 yields 大庆华科's ¥16 price history; `isIndex=true` does not
help). Since the fallback is undetectable in the payload, every index call
first verifies the code against Baidu's *index constituents* library — real
indices always have members — and raises `IndexNotFoundError` otherwise.
Verification is cached for 7 days. Unknown-to-Baidu indices such as `932000`
(中证2000) raise the same error.

Validated indices (constituent counts match the official ones):
`000300` 沪深300, `000852` 中证1000, `000905` 中证500, `000680` 科创综指,
`000688` 科创50, `399006` 创业板指, `399673` 创业板50, `899050` 北证50.
Other codes Baidu carries also work; `INDEX_NAMES` provides display names for
the validated set. Daily K-lines reach back to mid-2018 (~2001 bars); minute
periods are unsupported by the endpoint.

### Persistent cache

```python
from baidu_finance import Client
from baidu_finance.cache import DiskCache      # requires baidu-finance[disk]

client = Client(cache=DiskCache("~/.baidu_finance_cache"))
```

### Rate limiting & risk control

Baidu's risk control has four observable states, and the transport answers
each one:

| State | Looks like | Transport behavior |
|---|---|---|
| Normal | 200 + data | Returns the payload |
| Random reset | TLS handshake drop (~1/10 of rapid requests) | Retried in-session; treated as noise — the rate is *not* halved for isolated failures |
| Soft block | 200 + emptied envelope (`Result` null / `data` key missing) | Retried briefly, then `TransportError` — never handed to wget; counts toward the breaker |
| Hard block | 403 + `{"msg": "hit risk", "isCaptchaEnabled": true}` | `TransportError` immediately; the endpoint's circuit opens at once |

`RequestsTransport` meters requests with an **adaptive token bucket**: up
to `burst` (10) requests may fire back-to-back — isolated calls and short
parallel fan-outs never wait — while sustained pulls are capped at `rate`
(10/s) with bounded random jitter (never metronome-exact). The refill
rate halves (floor 0.5/s) only on a **sustained** failure confirmed by the
per-endpoint circuit breaker, recovering as successes return.

Each endpoint (host + path) carries its own **circuit breaker**: after 3
consecutive failures across distinct URLs it opens, and calls during the
cooldown fail fast with `TransportError` and **zero network traffic**.
Blocks decay on a minutes scale per endpoint, so the cooldown starts at
60s, doubles on failed half-open probes, and caps at 600s; a successful
probe closes the breaker. The same URL failing repeatedly (e.g. an
unknown stock code, which also returns `Result: null`) does **not** trip
the breaker — only cross-URL streaks do. All requests carry full browser
headers; connection errors and 429/5xx are retried with backoff, and the
wget fallback (same headers) covers network-layer and non-risk HTTP
failures only.

Note: with this change an unknown/invalid stock code now surfaces as
`TransportError` ("suspected block or invalid code") instead of a
`TypeError` deep in parsing.

```python
from baidu_finance import Client
from baidu_finance.transport import RequestsTransport

client = Client(transport=RequestsTransport(rate=5, burst=5))  # gentler
client = Client(transport=RequestsTransport(rate=0))           # unmetered
# Breaker tuning (defaults shown):
client = Client(transport=RequestsTransport(
    circuit_threshold=3, cooldown=60.0, max_cooldown=600.0, suspect_retries=2))
```

## Tests

Tests run against **live** Baidu endpoints (no mocks) and are skip-gated:

```bash
BAIDU_LIVE=1 pytest          # run live tests
pytest                       # skipped by default
```
