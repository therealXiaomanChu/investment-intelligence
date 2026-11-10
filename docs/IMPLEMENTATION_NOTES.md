# Implementation decisions

| Specification overlap | Implemented behavior |
|---|---|
| Daily fetches in the technical plan, higher-frequency polling in the decisions | SEC runs on weekdays every 15 minutes from 06:00 through 22:00 New York time. News runs every two hours, and crypto every four hours, including weekends. |
| Cloud PostgreSQL and local deployment | Both engines use the same observation, event, fact, signal and report logic. SQLite uses FTS5 and PostgreSQL uses a GIN text-search index. The Actions runner requires an external PostgreSQL database. |
| Delivery channel defaults | Each snapshot is archived in the database and rendered as Markdown. SMTP delivery uses explicit configuration. A missing model key yields a factual brief. |
| Paid consensus and direction | Public releases supply facts. Macro observations carry nullable consensus, provider and as-of metadata. The MVP has no licensed consensus provider. Factual signals leave direction and confidence empty. |
| Named sources and accessible protocols | SEC provides the structured financial slice. NVIDIA IR supplies an automated earnings calendar, and operators can register additional official dates. BTC, ETH and SOL use Bitcoin Core, the Ethereum Foundation and Agave. Optional and unsupported coverage is inventoried in SOURCE_STATUS.md. |
| Multiple symbols, one issuer | Alphabet shares one CIK across two share classes, with distinct asset IDs. SpaceX has a pinned CIK and listing date. An earlier issuer's SPCX filings cannot match the SpaceX example. |
| Official clocks and customizable time zones | Store timestamps in UTC, preserve release time semantics at the connector, and use XNYS sessions for US equity briefs. TIMEZONE selects editorial schedule and budget boundaries. SEC and market briefs use New York time. |
| Event-aware Actions scheduling | A fixed five-minute dispatcher looks up durable slots and due events. On a late run it processes the latest applicable slot, with a catch-up window for weekly research. |
| Source text and cost | Parsers and Decimal arithmetic extract numbers. The model receives bounded untrusted evidence and has no tools. Only schema-validated results with known evidence URLs update signals. |

## Persistent state

The initial SQL migration is idempotent. Each source commits its successful cursor and observations in the same transaction. Source errors are isolated. SEC substreams maintain separate progress. A single run lock coordinates local processes or PostgreSQL sessions. The migration table records schema version 1.

Evidence versions retain source identity, canonical link, observation and publication times, normalized payload, and content hash. Join records associate evidence with an event. Facts retain the metric, value, unit, period, observed vintage and evidence ID. Events expose the current interpretation. To reconstruct what the installation knew at a historical cutoff, read the immutable evidence and filter by first_seen_at. Historical source data ingested today only became known to the installation today. Raw licensed articles and full release archives are not redistributed.

High-priority alert intents are committed alongside collected events. Report IDs and the SMTP claim are durable. Reports can be retrieved again after a runner's filesystem is destroyed. After an ambiguous SMTP failure, check the server logs using the deterministic Message-ID before manually resending. See the operator guide in the README.

## Components and licenses

The application and synthetic fixtures use the repository's MIT license. External dependencies retain their upstream licenses. Runtime components include SQLAlchemy (MIT), Requests (Apache-2.0), Beautiful Soup (MIT), defusedxml (PSF), PyYAML (MIT), python-dateutil (Apache-2.0/BSD), exchange_calendars (Apache-2.0), python-dotenv (BSD) and optional psycopg (LGPL). The package declares dependency ranges in pyproject.toml. Installation fetches upstream packages. No third-party collector source was copied. Publisher terms still apply to fetched material, including Cboe series distributed through FRED.
