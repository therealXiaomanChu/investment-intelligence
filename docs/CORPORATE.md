# Corporate and institutional sources

## Filing collection

`sec_poll` reads SEC submissions by CIK. For companies in the public watchlist, the collector validates the returned issuer CIK and ticker list. To resolve another listed company from the SEC ticker file, provide its exact `sec_name` and `symbol`. A CIK is preferable for stable identity. SpaceX uses CIK 0001181412 and an effective ticker date of June 12, 2026. Both constraints apply even if the user supplies a different ticker history. The issuer identifies the first trading day in its [second-quarter 2026 release](https://www.sec.gov/Archives/edgar/data/1181412/000162828026052515/earningsreleaseq22608042.htm).

Set `SEC_USER_AGENT` to your organization or name followed by your monitored contact email. The client shares a rate limit across SEC subdomains, caps response size and backs off on overload. Source configuration and rate policy are based on the [SEC application APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) and [fair-access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data).

The first poll considers 90 days of filings per issuer. Later polls use an accession ledger, retain failed accessions for retry and extend discovery across missed polling intervals. `--backfill` includes one year by default, paginates through the filings history and re-parses files to detect corrections. The bounds are configurable in `config/corporate.yaml`. Accepted times, document hashes and accession numbers are retained. A 10-Q/A or 10-K/A is separate evidence for the same issuer/report-period event. A distinct 8-K keeps its accession identity even if other 8-Ks have the same date.

Periodic filings use accession-matched US-GAAP companyfacts. Revenue, diluted EPS, gross profit, operating income, operating cash flow, capex, cash and noncurrent long-term debt use explicit tags and units. Fiscal period start/end travels with each amount. A disclosure can report both a discrete quarter and year-to-date values. Code computes margins and free cash flow only for matching periods, and revenue growth only when a unique, comparable prior-year value occurs in that filing. Unmatched fields are recorded as unavailable. Standardized companyfacts do not cover every segment or guidance figure. A newly filed report may appear before companyfacts have updated. Use `sec_poll --backfill` to refresh it.

8-K parsing records the reported item numbers. S-1, registered ownership Form 4 and Schedule 13D/G filings are discovered when they appear under a configured CIK and form. These forms retain the evidence URL and hash. Insider transaction reconstruction, free conference-call transcripts, automatic issuer discovery for private-company IPOs and exhaustive segment-specific metrics are outside this implementation.

## Institutional comparisons

The default six managers are configured by SEC CIK in `config/corporate.yaml`. A private watchlist may override `managers` with a list of `{name, cik}` objects. The collector uses the SGML submission's INFORMATION TABLE XML, skips unchanged comparisons, and checks that report dates are adjacent quarters. Restatements replace a quarter's earlier table. Additive amendments are applied to its prior holdings. An unrecognized amendment type produces an unavailable status. Historical backfill processes additional quarter pairs from the SEC recent submissions list.

Quantities aggregate by CUSIP, Put/Call/non-option and share/principal unit. Values determine relative weight within each report. The position metadata retains prior and current quantity and weight, change and action. Each event retains the evidence URLs for **both** quarters. The reported value of an option may represent underlying shares. 13F weight provides neither a live allocation nor an estimate of net economic exposure. ARK daily ETF baskets are a separate disclosure series and have no role in this comparison.

## Earnings appointments

Daily calendar sync reads NVIDIA's [official IR event feed](https://investor.nvidia.com/rss/event.aspx). The feed may publish only a date. The system archives this precision and schedules a results check at 18:00 New York time on the announced date. `scheduled_at` stays empty if the issuer has not announced a time. The source can change the date on the same event GUID. Company-published transcripts are not assumed available.

To register another issuer's confirmed appointment, add an entry to the private watchlist:

```yaml
# Replace every field with values from an official announcement.
earnings:
  - id: your-stable-issuer-event-id
    cik: '0001045810'
    title: Your verified issuer event title
    scheduled_at: '2026-11-02T09:00:00-05:00'
    url: 'https://example.org/replace-with-official-announcement'
```

The dispatcher checks for a same-day SEC 8-K item 2.02 under the same issuer, within its bounded retry policy. An unrelated filing or an earlier quarter cannot complete the event. Issuers that publish only on an IR page, delay filing, or release results on a different date may exhaust the check window. The regular SEC pipeline can still ingest their later filing.

## Verification

- The synthetic test replays an NVIDIA-style 10-Q, its amendment and an 8-K into SQLite. It verifies fact vintages, idempotence, FTS results and the original and revised revenues. Its two-quarter manager fixture tests distinct stock/put changes and additive amendments.
- A live run retrieved NVIDIA's RSS, parsed the FY27 Q3 financial-results date of November 17, 2026, and wrote and searched the event in SQLite.
- The production 13F parser was also run against the SEC browser's archived INFORMATION TABLE sections for Situational Awareness. The two real reports contained 29 and 42 normalized position keys. Their comparison produced 46 changes, and the Q1 total agreed with the filing cover's reported 13,676,657,577. [2025 Q4](https://www.sec.gov/Archives/edgar/data/2045724/000204572426000002/0002045724-26-000002.txt) · [2026 Q1](https://www.sec.gov/Archives/edgar/data/2045724/000204572426000008/0002045724-26-000008.txt)
- The complete EDGAR network poll requires the operator's declared contact. No contact was configured during build verification. Corporate demo values, accessions and filing URLs are synthetic.
