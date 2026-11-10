# Investment Intelligence OS

A Python CLI that collects primary-source investment information and delivers source-linked Markdown briefs. It monitors corporate filings and institutional disclosures, academic research, US macro releases, and AI, technology and crypto announcements. Collection runs without a model account. The service has no brokerage integration.

## Run the four-module demo

From this directory, use Python 3.11 or newer. The local service targets macOS and Linux. Windows users can use Compose or WSL.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[test,cloud]'
python -m investment_intelligence demo
python -m pytest -q
```

The initial installation downloads dependencies. Once installed, the demo runs offline, ignores live credentials, uses a fixed clock and writes a separate database. Open **output/demo/corporate.md**, **research.md**, **macro.md** or **news.md** there. The demo replays an NVIDIA-style amendment, a two-quarter 13F, a CPI revision, a revised paper and an Ethereum postponement. All demonstration source payloads are synthetic.

```sh
DATABASE_URL=sqlite:///output/demo/demo.sqlite3 python -m investment_intelligence search "CPI"
DATABASE_URL=sqlite:///output/demo/demo.sqlite3 python -m investment_intelligence reports
DATABASE_URL=sqlite:///output/demo/demo.sqlite3 python -m investment_intelligence reports demo-macro
```

## Run live

```sh
cp .env.example .env
python -m investment_intelligence doctor
python -m investment_intelligence run news_scan --source nvidia_news --backfill
python -m investment_intelligence run calendar_sync
python -m investment_intelligence run sec_poll
python -m investment_intelligence run paper_weekly
python -m investment_intelligence run crypto_scan
python -m investment_intelligence run premarket_brief
python -m investment_intelligence reports
```

Public sources start working as soon as the host can reach them. Set **SEC_USER_AGENT** before running SEC jobs. For a quick one-source report, run `python -m investment_intelligence run premarket_brief --source nvidia_news`. The source filter restricts collection. The brief also includes qualifying records already in the database. Ordinary scan jobs archive events, and immediately deliver material updates. Regular items appear in the next brief. With SMTP unconfigured, the CLI reports email state `disabled`.

`run JOB --dry-run` fetches and parses against the current archive in a rollback-only transaction, skips the model and delivery, and reports projected changes. `--backfill` applies the adapter's bounded lookback. Repeat it to page through capped sources. A manual job ignores the clock. `dispatch` runs only due work. `serve` checks once per minute.

```sh
python -m investment_intelligence run sec_poll --dry-run
python -m investment_intelligence run event_check
python -m investment_intelligence dispatch
python -m investment_intelligence serve
```

## Configure a deployment

The CLI loads the working directory's .env first. Exported environment variables take precedence. The public examples contain identifiers and monitoring tiers only. Copy config/watchlist.example.yaml to config/watchlist.private.yaml and set WATCHLIST_FILE to use your own list. Use CONFIG_DIR for a replacement source/config directory. Source IDs are durable identities. Keep them stable across edits.

| Setting | Use |
|---|---|
| DATABASE_URL | Default sqlite:///data/intelligence.sqlite3. For cloud use postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=require and install the cloud extra. Percent-encode reserved password characters. |
| OUTPUT_DIR, TIMEZONE | Default output directory is output. Editorial schedule, reports and budget windows default to America/New_York. Use an IANA zone. Market hours remain in New York time. |
| FETCH_LIMIT | Bounded per-adapter collection. Default 100. The arXiv calendar window resumes across runs. SEC distributes work across issuer streams. Source-specific details are in the connector guides. |
| SEC_USER_AGENT | Real operator or organization name and monitored contact email, separated by a space. SEC metadata is keyless and subject to fair-access limits. |
| OPENALEX_API_KEY | Enables authenticated OpenAlex requests and the associated quota. arXiv collection survives enrichment failures. |
| BLS_API_KEY, FRED_API_KEY | Optional BLS registration for higher quotas. A FRED key switches market observations to the official series API. Without it the connector uses public CSV. |
| LLM_BASE_URL, LLM_API_KEY, LLM_MODEL | Optional OpenAI-compatible chat/completions endpoint with JSON output. Supply a pinned model or set LLM_MODEL_VERSION to its deployed revision. A loopback HTTP endpoint is supported. Other endpoints require HTTPS. |
| LLM_INPUT_USD_PER_M, LLM_OUTPUT_USD_PER_M | Supply the model's applicable USD prices per million input and output tokens. Missing or invalid prices defer paid analysis. For a separate news model, set LLM_NEWS_MODEL, its two LLM_NEWS_* price fields, and optionally LLM_NEWS_MODEL_VERSION. |
| LLM_DAILY_USD, LLM_MONTHLY_USD, LLM_MAX_CALLS | Defaults are $0.50 per day, $10 per month and five calls per analysis job. An atomic ledger reserves a conservative token allowance before a request and reconciles reported usage. An ambiguous timeout retains the reservation. |
| SMTP_HOST, SMTP_PORT, SMTP_SECURITY | Optional authenticated or trusted-relay email. Use starttls, typically port 587, or ssl, typically 465. |
| SMTP_FROM, SMTP_TO, SMTP_USER, SMTP_PASSWORD | Set sender and recipient(s). Supply login credentials if the relay requires them. |

Budgets depend on configured prices and the provider honoring its output cap. Use the highest billed rates for the selected model and features. Local accounting cannot see charges incurred by another application. While a budget is exhausted, new evidence is persisted and important items carry a factual summary with analysis pending. A later eligible job resumes the queue. Paid summaries accept only allowed evidence links and validated fields. They still require the reader's judgment.

## Job schedule

| Job | Default time | Result |
|---|---|---|
| calendar_sync | Daily 07:30 | Known dates and changes, BLS observations/revisions and market reference data |
| sec_poll | Weekdays 06:00–22:00 ET, 15-minute slots | Material filings, new or amended periodic filings, latest quarter-pair 13F comparison |
| news_scan | Every 2 hours | Publisher announcements and source-linked alerts |
| crypto_scan | Every 4 hours, including weekends | BTC, ETH and SOL project news and release status |
| event_check | Registered events starting five minutes after their check time | Polls for matching release evidence. Stops after six attempts or three hours, marks overdue events delayed and retries recent cases in daily recovery. |
| paper_weekly | Monday 08:00, with catch-up | Prior local Monday through Sunday. Keeps all qualifying papers, ranks five for the brief, and continues capped scans before final delivery. |
| premarket_brief | XNYS session days 09:10 ET | New validated observations since the prior brief and today's registered events |
| after_close | XNYS session days 16:40 ET | Filing refresh and after-close brief. The requested fixed clock also applies on early-close days. |
| source_health | Daily 06:05 | Source diagnostics, recovery/backfill and a health report |

The scheduler persists completed slots, uses the exchange calendar for holidays, and handles DST. A delayed dispatcher processes the latest eligible periodic slot. It does not simulate all missed polls. Cursor overlap and bounded backfill recover what the origin still retains. A newly processed old event is identifiable by its distinct event and first-seen dates. Filing age, source access, free quota and feed retention can limit historical coverage.

## Local Docker Compose

Use Docker with Compose 2.24 or newer. From this directory:

```sh
docker compose up --build -d
docker compose logs -f intelligence
docker compose exec intelligence python -m investment_intelligence doctor
docker compose exec intelligence python -m investment_intelligence demo
docker compose cp intelligence:/srv/intelligence/output/. ./output
```

The service runs as a non-root user. Named volumes hold data and output, and config is mounted read-only. Set the container's WATCHLIST_FILE to a path it can read, such as /srv/intelligence/config/watchlist.private.yaml. Use `docker compose down` to stop the service. Removing the data volume erases its archive. For a consistent SQLite backup use SQLite's backup command/API or stop the service before copying its database and accompanying files.

## GitHub template deployment

1. Put this project in a GitHub template repository under your control. Choose **Use this template → Create a new repository**. Its default branch must contain .github/workflows/pipeline.yml. Use a private repository for private watchlists and reports.
2. Create a persistent PostgreSQL database, including a managed PostgreSQL or Supabase session/direct connection. The application role needs table/index creation and reads/writes in its schema. Use TLS and a session-capable connection for PostgreSQL advisory locks.
3. Under **Settings → Secrets and variables → Actions**, add DATABASE_URL and SEC_USER_AGENT. Add the optional API and mail secrets from the table as needed. WATCHLIST can hold your private YAML as a secret. Set nonsecret model names, prices, budgets, TIMEZONE and limits as Actions variables.
4. Open **Actions**, enable workflows, select **Intelligence pipeline**, and choose **Run workflow**. Start with calendar_sync, then run dispatch or the desired collection job. Inspect its coverage and connector warnings. An unconfigured or unavailable source can yield partial coverage while other sources succeed.
5. The workflow wakes the dispatcher every five minutes and serializes runs. PostgreSQL stores evidence, cursors, budgets, reports and mail claims across runners. Markdown artifacts have seven-day retention. Set UPLOAD_REPORTS=false to disable artifact upload. A private WATCHLIST disables uploads by default unless UPLOAD_REPORTS=true. The test workflow creates an independent disposable PostgreSQL service.

Actions schedules run from the default branch. Queue load can delay or drop a wake-up. Public repositories can have scheduled workflows disabled after 60 days without activity. If reports stop, inspect the last run, repository Actions settings, secret availability and database connectivity. Re-enable **Intelligence pipeline** in the Actions tab, then run dispatch manually. [GitHub schedule syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule) · [GitHub scheduled event restrictions](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## Read, audit and recover

- `doctor` displays each configured source's last observed success, status and errors. An unseen source has no recorded probe yet. Run its collection job to probe it. Source health and report history also live in PostgreSQL on hosted deployments.
- `search "phrase"` uses database full-text search over event titles and factual summaries. Use `history 'EVENT_KEY'` to see observation revisions, facts, evidence URLs and timestamps.
- `reports` lists durable IDs and mail states. `reports REPORT_ID` prints an archived snapshot even when its original runner has disappeared. Use shell redirection to restore the Markdown file.
- Failed fetches keep their progress available for retry. Re-run a job, optionally with --source and --backfill. The daily recovery job rechecks unavailable or stale sources. A changed site can require a parser update.
- SMTP marks a claim before sending. A crash during handoff or a delivery error can leave sending or uncertain. Check the relay log and recipient first. To deliberately retry after confirming non-delivery, set that report's mail_state to ready in reports, then run a job. Misconfigured sender/recipient details leave the email ready for retry after correction. Snapshots created with email disabled stay disabled.
- Spend rows contain job, model, reservation, reconciled charge and token counts. Monetary amounts are integer millionths of a US dollar. Unreconciled requests retain their estimate to avoid a second spend against the same allowance.
- The reusable skill is at skills/investment-intelligence/SKILL.md. To install in Codex, copy that folder into your skills directory, for example ~/.codex/skills/. It guides archive queries, manual runs and evidence interpretation.

## Tests and coverage

```sh
python -m pytest -q
TEST_DATABASE_URL='postgresql://USER:PASSWORD@HOST/TEST_DB' python -m pytest -q
```

The delivery verification run passed all 75 tests, including PostgreSQL 16 parity. The offline demo produced 26 events and 33 document versions with zero warnings. A clean installation of the built wheel also ran the demo outside the source checkout. [Packaging verification](docs/DEPLOYMENT_VERIFICATION.md)

Use a disposable test database where the role can create and drop its own uniquely named test schema. Without TEST_DATABASE_URL, pytest skips the PostgreSQL parity test. The parity test compares four complete pipelines across both engines. The remaining tests exercise parser contracts, financial and macro revisions, identity and protocol transitions, retries, DST, exchange holidays, budgets, SMTP and offline operation.

The source audit distinguishes live HTTP, archived provider payloads, fixture coverage, access failures and unimplemented scope. [Source status](docs/SOURCE_STATUS.md) · [Implementation decisions](docs/IMPLEMENTATION_NOTES.md) · [Corporate](docs/CORPORATE.md) · [Macro](docs/MACRO.md) · [Research and news](docs/RESEARCH_NEWS.md)

```text
config/                          public watchlist, source bindings and module policies
src/investment_intelligence/     CLI, storage, scheduler, model budgets and delivery
  collectors/                    corporate, macro, research and news adapters
  fixtures/                      packaged synthetic wire responses
tests/                           repeatable acceptance and regression tests
skills/investment-intelligence/   reusable query and operation skill
.github/workflows/                scheduled production dispatcher and test workflow
docker-compose.yml, Dockerfile   local service and persistent volumes
docs/                            operating details and verification evidence
```
