# Source and verification status

On November 11, 2026, live checks parsed real records for all 18 entries in `config/sources.yaml`. The checks used current feeds, historical backfill for infrequent sources, and a previously announced financial release to exercise the event-triggered path. Source retention and publication schedules affect the number of records returned.

| Sources | Verified route and coverage |
|---|---|
| SEC filings and 13F | With an operator contact configured, the normal EDGAR path retrieved a 10-Q and parsed its company facts. The 13F adapter retrieved information tables and compared consecutive filings. |
| Earnings calendar and results | The issuer's IR calendar supplied a future earnings event. Matching a past release to its same-day SEC filing verified the results path. Date-only appointments retain their published precision. |
| OpenAI, NVIDIA, Google AI and Anthropic | The seven-day scan parsed announcements from each publisher's own feed or listing. |
| Bitcoin Core, Ethereum Foundation and Agave | The Bitcoin Core feed yielded records in the 90-day backfill window. Ethereum and Agave returned official protocol notices and client releases. Client release tags alone do not establish network activation. |
| arXiv and OpenAlex | The resumable arXiv OAI-PMH path completed a whole weekly window. OpenAlex enriched exact work matches; papers also remain available with their original arXiv evidence. [Research collection](RESEARCH_NEWS.md) |
| BLS calendar and actuals | Direct BLS calendar requests returned HTTP 403 on the verification host. New York Fed dates and the U.S. advance release metadata distributed by the IMF supplied usable schedules. Dates without corroborated reference months stay provisional. The BLS API returned CPI, employment and PPI observations independently. [Macro collection](MACRO.md) |
| BEA releases, BEA and Fed calendars | The BEA schedule and Fed meeting calendar returned upcoming events. The BEA current-releases page also led to published GDP and personal income/outlays bulletins on a fresh database. |
| Treasury yields and VIX | The FRED CSV route timed out on the verification host. The Federal Reserve H.15 release and Cboe history supplied the corresponding daily observations and source links. |
| Delivery and deployment | The private Actions-to-inbox path was exercised with a GitHub notification email. SMTP and optional paid model integrations are covered by controlled contract tests. SQLite and PostgreSQL parity run in CI. |

## Boundaries of the MVP

The four modules have runnable source-to-report pipelines. Several broader watchlist items are supported through configurable identities or context tags. The following source families and analyses remain incomplete.

- **Corporate** — Standard US-GAAP companyfacts and SEC document metadata cover the financial slice. Company-specific segment KPIs, complete debt definitions, narrative guidance and call/Q&A comparisons need issuer-specific extraction and authorized transcripts. Form 4, S-1 and Schedule 13D/G discovery is available for configured issuer streams. A filing made by a different reporting party may require another CIK. The example tracks private-company announcements through official news. ARK daily ETF baskets and automated issuer-wide IR calendar scraping are outside the current adapters.
- **Research** — Weekly discovery is topic-based on arXiv. OpenAlex metadata and validated identities support author/affiliation matches. Independent institution-wide publication harvesting, Semantic Scholar recommendations, Hugging Face rankings and citation/benchmark trend histories remain outside V1. Clinical or investment implications of research are not established by a keyword match.
- **Macro** — Local first-seen vintages start with the first collection. An unavailable official timestamp remains empty. No historic ALFRED reconstruction, commercial consensus, FOMC language diff, VIX futures term structure or DXY level feed. No surprise is inferred from previous values.
- **News and crypto** — Official-source headlines, metadata and explicit supported upgrade language drive events. The system does not monitor every EIP or Solana proposal, exchange flow, token unlock, CFTC release, ETF decision, third-party interview, GitHub trend or social rumor listed in the broader scope. The stock indices and benchmark funds in the watchlist provide context and asset links, without a quote feed.
- **Delivery and history** — SMTP offers no universal exactly-once receipt protocol. An ambiguous handoff needs operator review. A reporting snapshot is fixed at its cutoff. Late revisions are archived and appear in later reporting. No public report or external repository is created by the software during local setup.

Connector-specific source links, supported fields and parser policies are available in [Corporate](CORPORATE.md), [US macro](MACRO.md) and [Research and news](RESEARCH_NEWS.md).
