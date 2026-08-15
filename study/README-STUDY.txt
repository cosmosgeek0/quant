PURPOSE | study/tape_pull.py downloads public historical spot tapes only; it has no order, account, credential, or signed-call path.
COHORT | candidates are TRADING USDT spot symbols with an exact TRADING USD-M PERPETUAL; rank is the Decimal sum of spot quote volume in the newest requested complete UTC day.
LAYOUT | study/data/spot/daily/klines/<SYMBOL>/1m/<SYMBOL>-1m-YYYY-MM-DD.zip
CONTENTS | each retained ZIP contains the matching Binance Vision 1m kline CSV and passes member-name, size, and CRC checks.
DEFAULT RUN | python3 study/tape_pull.py
DEFAULTS | --cohort 100 --days 3 --dest study/data/
ONE RETAINED SYMBOL | python3 study/tape_pull.py --cohort 1 --days 1 --dest study/data/
RERUN | repeat the same command; intact destination ZIPs print SKIPPED and are not downloaded again.
DATES | --days selects the preceding completed UTC calendar days; the current partial UTC day is never requested.
PARALLELISM | metadata reads are sequential; archive work uses at most four concurrent public GETs and backs off on HTTP 429.
RANK COST | exact ranking may read one newest-day archive for every common candidate; only selected cohort archives are retained under --dest.
FAILURE | corrupt existing or downloaded ZIPs, malformed metadata, unknown quote volume, and required missing archives fail closed with a nonzero exit.
NETWORK | GET only: api.binance.com/api/v3/exchangeInfo, fapi.binance.com/fapi/v1/exchangeInfo, and data.binance.vision daily spot kline archives.
