# Investment Intelligence OS

A Python CLI that collects primary-source investment information and delivers source-linked Markdown briefs. It monitors corporate filings and institutional disclosures, academic research, US macro releases, and AI, technology and crypto announcements. Collection runs without a model account. The service has no brokerage integration.

[中文部署指南：GitHub、自己的服务器、邮件与 AI 工具](docs/QUICKSTART.zh-CN.md)

For scheduled alerts, configure a sender and choose one deployment below. GitHub Actions can run while your own devices are offline. A Mac or Linux server can run the same collector with Docker Compose. The collector, archive and email delivery work with an ordinary shell. Connecting an AI assistant is optional.

Each installation owns its recipients, watchlist, archive and delivery history. Set your email and provider credentials in a local `.env` or in the Actions secrets of a repository you control. The shared source contains a blank [.env.example](.env.example); cloning or contributing to the project never subscribes you to another installation's email.

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
umask 077
cp -n .env.example .env
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

`run JOB --dry-run` fetches and parses against the current archive in a rollback-only transaction, skips the model and delivery, and reports projected changes. `--backfill` applies the adapter's bounded lookback. Repeat it to page through capped sources. A manual job ignores the clock. `dispatch` runs only due work. `serve` checks once per minute. The job schedule below determines how often each source is polled.

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
| DATABASE_URL | Local default sqlite:///data/intelligence.sqlite3. GitHub Actions supports an archived SQLite snapshot or external PostgreSQL. For PostgreSQL use postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=require and install the cloud extra. Percent-encode reserved password characters. |
| OUTPUT_DIR, TIMEZONE | Default output directory is output. Editorial schedule, reports and budget windows default to America/New_York. Use an IANA zone. Market hours remain in New York time. |
| FETCH_LIMIT | Bounded per-adapter collection. Default 100. The arXiv calendar window resumes across runs. SEC distributes work across issuer streams. Source-specific details are in the connector guides. |
| SEC_USER_AGENT | Real operator or organization name and monitored contact email, separated by a space. SEC metadata is keyless and subject to fair-access limits. |
| OPENALEX_API_KEY | Enables authenticated OpenAlex requests and the associated quota. arXiv collection survives enrichment failures. |
| BLS_API_KEY, FRED_API_KEY | Optional BLS registration increases data-query quotas. BLS calendar collection also checks official release pages if the calendar feed fails. A FRED key selects the FRED series API, otherwise the collector reads public CSV. When FRED is unreachable, it can collect Treasury yields from the Federal Reserve H.15 table and daily VIX closes from Cboe. |
| LLM_BASE_URL, LLM_API_KEY, LLM_MODEL | Optional model, provider API key and base URL. Supports Chat Completions with JSON output or Anthropic's native Messages endpoint. Set LLM_MODEL_VERSION to the deployed revision if the model ID is an alias. Use HTTPS for remote services or HTTP on loopback. |
| LLM_INPUT_USD_PER_M, LLM_OUTPUT_USD_PER_M | Supply the model's applicable USD prices per million input and output tokens. Missing or invalid prices defer paid analysis. For a separate news model, set LLM_NEWS_MODEL, its two LLM_NEWS_* price fields, and optionally LLM_NEWS_MODEL_VERSION. |
| LLM_DAILY_USD, LLM_MONTHLY_USD, LLM_MAX_CALLS | Defaults are $0.50 per day, $10 per month and five calls per analysis job. An atomic ledger reserves a conservative token allowance before a request and reconciles reported usage. An ambiguous timeout retains the reservation. |
| SMTP_HOST, SMTP_PORT, SMTP_SECURITY | Optional authenticated or trusted-relay email. Use starttls, typically port 587, or ssl, typically 465. |
| SMTP_FROM, SMTP_TO, SMTP_USER, SMTP_PASSWORD | Set sender and recipient(s). Supply login credentials if the relay requires them. |
| GMAIL_ADDRESS, GMAIL_APP_PASSWORD | Two-field Gmail setup for sending to yourself. Set SMTP_TO for a different recipient. Enable 2-Step Verification and generate an app password for this deployment. Enter it in your local .env or your own repository's Actions secrets. Set SMTP_HOST to use the independent SMTP configuration instead. |
| EMAIL_SCOPE | `alerts` emails qualifying material events. `all` also sends scheduled reports. Both keep reports in the archive. Actions and the .env example select `alerts`; an existing local deployment with no EMAIL_SCOPE keeps `all`. |

Budgets depend on configured prices and the provider honoring its output cap. Use the highest billed rates for the selected model and features. Local accounting cannot see charges incurred by another application. While a budget is exhausted, new evidence is persisted and important items carry a factual summary with analysis pending. A later eligible job resumes the queue. Paid summaries accept only allowed evidence links and validated fields. They still require the reader's judgment.

## Use your own AI tool

To read a brief, give a chat assistant the Markdown produced by `investment-intelligence reports REPORT_ID`. To query the live archive from a shell-enabled agent, give it the reusable [skill](skills/investment-intelligence/SKILL.md) and the path to your deployment. It can run `reports`, `search` and `history` with the same CLI as a human operator. Codex discovers a project copy under `.agents/skills/investment-intelligence/`, and Claude Code under `.claude/skills/investment-intelligence/`. Other hosts, including agents backed by Grok or DeepSeek, can read the same SKILL.md directly. [Codex skills](https://developers.openai.com/codex/skills) · [Claude Code skills](https://code.claude.com/docs/en/skills)

To add **unattended** commentary to the collector, configure one API credential issued by your chosen provider, an available model ID, both per-million token prices and a budget. The collector sends selected source excerpts, evidence links and associated asset identifiers to that endpoint. Leave `LLM_API_KEY` empty for factual-only alerts. An assistant's interactive login is separate from the collector's API credential.

| Analysis provider | LLM_BASE_URL | Interface |
|---|---|---|
| OpenAI or a compatible deployment | `https://api.openai.com/v1` or your own base URL | Chat Completions, JSON object mode, bounded output, usage counts |
| xAI (Grok) | `https://api.x.ai/v1` | [Chat Completions](https://docs.x.ai/developers/rest-api-reference/inference/chat-completions) |
| DeepSeek | `https://api.deepseek.com` | [JSON output](https://api-docs.deepseek.com/guides/json_mode/), with thinking disabled for short summaries |
| Anthropic (Claude) | `https://api.anthropic.com/v1` | [Messages API](https://platform.claude.com/docs/en/api/messages) with a workspace-scoped API key |

Choose a model that accepts the parameters for its endpoint. Custom Chat Completions services should accept `messages`, `max_tokens` and `response_format={"type":"json_object"}`. The optional news model uses the same endpoint and credential.

## Job schedule

| Job | Default time | Result |
|---|---|---|
| calendar_sync | Daily 07:30 | Known dates and changes, BLS observations/revisions and market reference data |
| sec_poll | Weekdays 06:00–22:00 ET, 15-minute slots | Material filings, new or amended periodic filings, latest quarter-pair 13F comparison |
| news_scan | Every 15 minutes | Publisher announcements and source-linked alerts |
| crypto_scan | Every 15 minutes, including weekends | BTC, ETH and SOL project news and release status |
| event_check | Registered events starting five minutes after their check time | Polls for matching release evidence. Stops after six attempts or three hours, marks overdue events delayed and retries recent cases in daily recovery. |
| paper_weekly | Monday 08:00, with catch-up | Prior local Monday through Sunday. Keeps all qualifying papers, ranks five for the brief, and continues capped scans before final delivery. |
| premarket_brief | XNYS session days 09:10 ET | New validated observations since the prior brief and today's registered events |
| after_close | XNYS session days 16:40 ET | Filing refresh and after-close brief. The requested fixed clock also applies on early-close days. |
| source_health | Daily 06:05 | Source diagnostics, recovery/backfill and a health report |

The scheduler persists completed slots, uses the exchange calendar for holidays, and handles DST. A delayed dispatcher processes the latest eligible periodic slot. It does not simulate all missed polls. Cursor overlap and bounded backfill recover what the origin still retains. A newly processed old event is identifiable by its distinct event and first-seen dates. Filing age, source access, free quota and feed retention can limit historical coverage.

## Local Docker Compose

On an always-on Mac or Linux server, install Docker with Compose 2.24 or newer. Run these commands in the checkout. Start with a private .env populated from .env.example. For Gmail alerts, fill in GMAIL_ADDRESS and GMAIL_APP_PASSWORD. For another SMTP service, enter its host, port, TLS mode, sender, recipients and relay credentials. Use a sender your relay authorizes. Any mailbox that accepts email can be the recipient. The test sends one real email. Check its arrival before starting the service.

```sh
docker compose build
docker compose run --rm intelligence python -m investment_intelligence test-email
docker compose up --build -d
docker compose logs -f intelligence
docker compose exec intelligence python -m investment_intelligence doctor
docker compose exec intelligence python -m investment_intelligence demo
docker compose cp intelligence:/srv/intelligence/output/. ./output
```

The service runs as a non-root user. Named volumes hold data and output, and config is mounted read-only. Set the container's WATCHLIST_FILE to a path it can read, such as /srv/intelligence/config/watchlist.private.yaml. Use `docker compose down` to stop the service. Removing the data volume erases its archive. For a consistent SQLite backup use SQLite's backup command/API or stop the service before copying its database and accompanying files.

## GitHub Actions deployment

A private repository owned by your personal account can deliver material updates through **GitHub notification email**. Enable email for participating and @mentions in [your GitHub notification settings](https://github.com/settings/notifications), confirm your preferred inbox, then set the repository **variable** `EMAIL_TRANSPORT=github`. Enable Issues on the private repository. Each alert creates a private issue mentioning the owner; GitHub sends the notification. People with access to that repository can see its issues, and watchers may receive their own notifications. Run `test-email` and check the actual inbox. This option uses the workflow's built-in token and needs no Gmail app password.

At a 15-minute cadence, GitHub can schedule 96 runs per day. Private hosted runner usage counts toward the owner's plan; compare the expected monthly use with your included quota and budget. [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

1. Fork the repository, use **Use this template** if available, or push a copy to your own repository. Keep .github/workflows/pipeline.yml on the default branch and enable Actions. Under **Settings → Secrets and variables → Actions**, configure your own repository's secrets. A private repository is the simplest option for a personal archive.
2. For email alerts add `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD`, or the appropriate `SMTP_*` settings above. SMTP_PORT, SMTP_SECURITY and EMAIL_SCOPE belong under **Variables**. Add `SEC_USER_AGENT` for SEC filings. Put optional private watchlist YAML in a `WATCHLIST` secret. If enabling automatic model analysis, add the provider key under **Secrets** and the endpoint URL, model ID, prices, and budget under **Variables**.
3. Choose how to persist the archive. The default stores a SQLite snapshot on the `intelligence-state` branch. Allow the workflow token to write repository contents and permit updates to that branch. For a public repository, create the `STATE_ENCRYPTION_KEY` secret with a new Fernet key. A private repository can use the same encryption. Keep a private backup of the key for restoring the archive. Generate one locally with `python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'`. As an alternative, set a `DATABASE_URL` secret for a persistent PostgreSQL instance with TLS, a session-capable connection, and table/index creation and read/write privileges.
4. In **Actions → Intelligence pipeline → Run workflow**, select the default branch and `test-email`. Check the recipient mailbox. For the first SQLite run, choose `dispatch` with `initialize_state` selected. Subsequent runs restore the snapshot automatically. PostgreSQL users can run `dispatch` directly.
5. After the manual run succeeds, set the repository variable `ENABLE_SCHEDULE=true` in your own deployment under **Settings → Secrets and variables → Actions → Variables**. This opts your copy into a dispatch every 15 minutes. The workflow serializes runs. Jobs have their own cadences below the scheduler level. Check **Actions** for actual start times and inspect the job output for connector warnings. Set `UPLOAD_REPORTS=false` to keep Markdown artifacts off. Encrypting a database snapshot only covers that snapshot. Treat any report artifacts you enable as readable by everyone who can access them.

Actions schedules run from the default branch. Queue load can delay or drop a wake-up. Public repositories can have scheduled workflows disabled after 60 days without activity. If reports stop, inspect the last run, repository Actions settings, secret availability and database connectivity. Re-enable **Intelligence pipeline** in the Actions tab, then run dispatch manually. [GitHub schedule syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule) · [GitHub scheduled event restrictions](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## Read, audit and recover

- `doctor` displays each configured source's last observed success, status and errors. An unseen source has no recorded probe yet. Run its collection job to probe it. Source health and report history also live in PostgreSQL on hosted deployments.
- `search "phrase"` uses database full-text search over event titles and factual summaries. Use `history 'EVENT_KEY'` to see observation revisions, facts, evidence URLs and timestamps.
- `reports` lists durable IDs and mail states. `reports REPORT_ID` prints an archived snapshot even when its original runner has disappeared. Use shell redirection to restore the Markdown file.
- Failed fetches keep their progress available for retry. Re-run a job, optionally with --source and --backfill. The daily recovery job rechecks unavailable or stale sources. A changed site can require a parser update.
- SMTP marks a claim before sending. A crash during handoff or a delivery error can leave sending or uncertain. Check the relay log and recipient first. To deliberately retry after confirming non-delivery, set that report's mail_state to ready in reports, then run a job. Misconfigured sender/recipient details leave the email ready for retry after correction. Snapshots created with email disabled stay disabled.
- Spend rows contain job, model, reservation, reconciled charge and token counts. Monetary amounts are integer millionths of a US dollar. Unreconciled requests retain their estimate to avoid a second spend against the same allowance.

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
