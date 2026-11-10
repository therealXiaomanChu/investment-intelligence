# US macro releases

Run `python -m investment_intelligence run calendar_sync` to register official dates and obtain the latest BLS observations. Run `python -m investment_intelligence run event_check` to check due events. The dispatcher's release checks start five minutes after the stored time. `premarket_brief` renders the stored evidence in Markdown. The offline demo clock and its source payloads are synthetic.

| Source | What is captured | Constraints |
|---|---|---|
| [BLS iCalendar](https://www.bls.gov/schedule/news_release/bls.ics) | CPI, PPI and Employment Situation release dates, rescheduling, cancellation and reference month | Requires the official reference month in the entry. Some network locations receive HTTP 403. The separate BLS data API continues to run. |
| [BLS public data API](https://www.bls.gov/developers/api_signature_v2.htm) | CPI-U headline/core, total nonfarm payrolls, unemployment rate, private average hourly earnings and final-demand PPI | `BLS_API_KEY` is optional for this small batch. BLS applies daily quotas. The API can lag a release. |
| [BEA schedule](https://www.bea.gov/news/schedule) and [current releases](https://www.bea.gov/news/current-releases) | PCE dates and GDP dates by estimate stage. A matching published release links to a public page with monthly PCE/core PCE or quarterly real GDP measures. | Measures are read from labeled tables. A changed table or missing measure yields an availability warning. The rolling release index may omit older vintages. |
| [Federal Reserve FOMC calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) | Regular meeting end dates, separate statement, SEP and minutes events linked to their meeting-specific publications | The 14:00 ET check time is estimated where the calendar provides only a day. Minutes checks use the published three-week policy or an explicit released date. Special calls and notation votes have no assumed release time. |
| [DGS2](https://fred.stlouisfed.org/series/DGS2), [DGS10](https://fred.stlouisfed.org/series/DGS10) and [VIXCLS](https://fred.stlouisfed.org/series/VIXCLS) | Daily H.15 Treasury yields, the 10-year minus 2-year spread in basis points, and Cboe's VIX close via public FRED CSV exports or the FRED observations API when `FRED_API_KEY` is configured | The latest market date can lag. The CSV origin can time out. A FRED key uses the official JSON API directly. The Board of Governors supplies the yield observations. Cboe supplies VIX, which has its own redistribution terms. |

## Reading the numbers

- Each BLS observation carries the official series ID, unit and reference month. CPI-U and PPI are seasonally adjusted index levels. Derived monthly changes use the two exact adjacent monthly levels and decimal arithmetic. CPI month-over-month values are rounded to three decimal places. A missing month (including the BLS `-` marker) produces an unavailable comparison.
- `actual` is the current reported level. `previous` is the prior month's earliest level observed by this installation. `revised_previous` gives the current prior-month level if it changed. `revision` measures change from this installation's first recorded actual for the same period. Observed vintages accumulate as the collector sees new snapshots.
- BLS supplies observation periods without per-row publication timestamps. Daily bootstrap can create a release record when the calendar is inaccessible. Such a record carries a month-end observation date, an unknown release time and no schedule. An observation for an earlier month cannot satisfy a due release check.
- Each BEA GDP estimate has its own release identity. The source table can contain multiple estimates for the same quarter. Real GDP is reported at a seasonally adjusted annual rate. Monthly PCE release values are percent changes from the preceding month. The relevant official release pages provide the evidence.
- A `null` consensus, provider and as-of timestamp are the default. No surprise, beat or miss is calculated. Stored local vintages begin on first observation. Historical ALFRED point-in-time import, a licensed consensus feed, VIX futures term structure, DXY levels and full FOMC text comparison are outside the implemented adapters.

## Configuration and verification

`config/macro.yaml` defines BLS series and windows. `calendar_sync --backfill` widens calendar and BLS reference-month discovery. An ordinary run rechecks recent BLS releases for revisions. FRED uses an overlap for recent corrections. Calendar times use the source's New York time zone across DST and are persisted in UTC.

The macro integration suite exercises calendar registration, a stale post-release response, exact-period publication, a changed prior month, an amended actual and replay through SQLite. It also covers GDP estimate identity, PCE table extraction, FOMC evidence matching, FRED spread arithmetic, changed source schemas and keyless BLS bootstrap.

```sh
python -m pytest -q tests/test_macro.py
```
