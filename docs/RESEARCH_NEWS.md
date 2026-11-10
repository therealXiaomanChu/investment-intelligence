# Research and publisher sources

## Weekly research

`paper_weekly` sets a Monday-to-Monday window in `TIMEZONE`. It queries arXiv's Atom API with the topic terms in `config/research.yaml`. The first pass uses a submission-date filter for new work. A second pass sorts by update time to find revised papers that were originally submitted earlier. The revision's own publication time must be inside the window. Each paper retains its original submission time, stable arXiv ID, versioned evidence URL, authors, technical tags and an abstract excerpt. Full paper text is outside the collector.

OpenAlex enrichment submits OR groups of exact preprint and publisher DOIs. A matched work contributes its OpenAlex URL, author IDs, ORCIDs when present, and institution IDs. Display names alone never join people. Optional `OPENALEX_API_KEY` supplies the operator's access tier. If a lookup fails, the arXiv observation remains available. A successful lookup can yield no work for a recent preprint. The institution and author ID lists in the configuration are ranking preferences within collected papers. The MVP does not crawl every publication from a tracked institution or resolve a list of people from their names.

If Atom times out or returns an unusable feed, the collector can harvest the same reporting week through arXiv's official OAI-PMH service. Set `provider: oai` in `config/research.yaml` to use that route directly. It scans daily metadata changes, then filters on each paper's latest **article version timestamp**, preserving its first submission time, exact version and original author byline. An administrative metadata update can happen long after a paper was published. The scan covers announcement delays up to seven days after the cutoff or the current day, whichever comes first. Later changes beyond that range can require a broader harvest. OAI-PMH pagination resumes from the saved day and position, including when a short-lived continuation token expires. The `MEV` crypto tag is case-sensitive so a physics paper's energy unit `MeV` cannot trigger it. [arXiv OAI-PMH formats and datestamps](https://info.arxiv.org/help/oa/index.html)

The weekly priority score starts at 0.52. Each matched topic adds 0.07. Each of up to five distinct matched terms adds 0.025. A configured OpenAlex identity match adds 0.10. The score is capped at 0.96. Topic-to-asset mappings explain which watchlist interests led to inclusion. Evidence of commercial adoption or stock performance requires separate reporting.

`FETCH_LIMIT` caps new or changed papers per run. `max_pages_per_run` also bounds HTTP work. A capped run saves the window, query stage, page offset and fingerprints. Rerun the same job when it reports incomplete coverage. Pagination overlaps several entries because arXiv's index can change between requests. `--backfill` expands the reporting history to four weeks by default and uses its own scan mode. A paper updated after a historical cutoff might only be available from arXiv in its newest version. The local archive preserves the versions actually observed.

[arXiv API query and date semantics](https://info.arxiv.org/help/api/user-manual.html) · [OpenAlex API access](https://help.openalex.org/api/) · [OpenAlex authentication](https://help.openalex.org/api/authentication/)

## News and protocol monitoring

| Source | Job | What is captured |
| --- | --- | --- |
| OpenAI, NVIDIA, Google AI | news_scan | Official RSS headlines, canonical links, publication times and announcement changes |
| Anthropic | news_scan | Dated announcements in the public news listing. Day-only evidence keeps the publication instant empty |
| Bitcoin Core | crypto_scan | Maintainer release and security notices |
| Ethereum Foundation | crypto_scan, calendar_sync, event_check | Official announcements. A named upgrade and network in an unambiguous headline can share an event across updates |
| Anza Agave | crypto_scan | Official Solana client releases, draft exclusion and pre-release flags |

Named Ethereum upgrades use the fork and network as their event identity. The collector can distinguish mainnet, Hoodi and Sepolia announcements and recognize explicit postponements. It registers a precise activation check only when the publisher supplies a compatible explicit UTC time. A month-only estimate, an unspecified date and a multi-network roadmap cannot establish a precise activation instant. Software publication alone cannot establish validator adoption or mainnet activation.

`config/news.yaml` controls publishers, asset mappings and initial/backfill lookbacks (7/90 days). Polls compare each item's ID and content fingerprint, including revisions without a changed publication timestamp. Repeated long headlines with the same meaningful words on the same day can share an event across publishers. Short or unrelated headlines retain their own identities. This heuristic can miss paraphrases. A source's retained feed or listing bounds recovery. The Agave connector reads up to 100 recent releases. The Solana roadmap returned HTTP 429 during verification. Agave is the supported SOL feed. Restricted sources fail with a health diagnostic. Publisher feeds are used for metadata and evidence links; there is no full-article scraper, authenticated transcript service, rumour feed, or inferred IPO schedule.

[OpenAI RSS](https://openai.com/news/rss.xml) · [NVIDIA RSS](https://blogs.nvidia.com/feed/) · [Google AI RSS](https://blog.google/technology/ai/rss/) · [Anthropic News](https://www.anthropic.com/news) · [Bitcoin Core RSS](https://bitcoincore.org/en/rss.xml) · [Ethereum Foundation RSS](https://blog.ethereum.org/feed.xml) · [Agave releases](https://github.com/anza-xyz/agave/releases)

## Watchlist identity evidence

The default nine author IDs were checked against authorships of the following works in November 2026. OpenAlex can merge new versions of its catalog. Audit these links when changing the watchlist. A printed name without an OpenAlex ID cannot trigger an identity bonus.

| Person | OpenAlex author | Evidence on a named work |
| --- | --- | --- |
| Tri Dao | A5091734792 | [FlashAttention](https://api.openalex.org/works/W7133227460) |
| Song Han | A5070926896 | [Deep Compression](https://api.openalex.org/works/W2119144962) |
| Tim Dettmers | A5067514564 | [QLoRA](https://api.openalex.org/works/W7133233472) |
| Jeff Dean | A5011889606 | [PaLM, Google affiliation](https://api.openalex.org/works/W4224308101) |
| Noam Shazeer | A5021878400 | [Attention Is All You Need, Google affiliation](https://api.openalex.org/works/W2626778328) |
| Ilya Sutskever | A5006446297 | [Sequence to Sequence Learning with Neural Networks](https://api.openalex.org/works/W2130942839) |
| Sergey Levine | A5026322200 | [End-to-End Training of Deep Visuomotor Policies](https://api.openalex.org/works/W2964161785) |
| Chelsea Finn | A5005431772 | [Model-Agnostic Meta-Learning](https://api.openalex.org/works/W2604763608) |
| Percy Liang | A5025255782 | [Holistic Evaluation of Language Models](https://api.openalex.org/works/W4320003957) |

| Scope organization | Default filter | Identity evidence or limitation |
| --- | --- | --- |
| OpenAI | I4210161460 | [OpenAlex institution](https://api.openalex.org/institutions/I4210161460) · [ROR](https://ror.org/05wx9n238) |
| Anthropic | I4387930290 | [OpenAlex institution](https://api.openalex.org/institutions/I4387930290) · [ROR](https://ror.org/056y0v115) |
| Google DeepMind | I4210090411 | UK entity, which includes the DeepMind alias. Papers affiliated with Google alone do not acquire a DeepMind attribution. [OpenAlex institution](https://api.openalex.org/institutions/I4210090411) · [ROR](https://ror.org/00971b260) |
| MIT CSAIL | I4405258931 | [OpenAlex lab record](https://api.openalex.org/institutions/I4405258931) · [ROR](https://ror.org/01e85r662) |
| Meta FAIR | Unresolved | The Facebook AI Research search yielded no verified lab-level OpenAlex ID. Meta-wide affiliations are too broad. |
| Stanford HAI | Unresolved | The full institute-name search yielded no verified lab-level ID. Stanford University alone does not identify HAI. |
| Berkeley BAIR | Unresolved | The lab-name search yielded no verified lab-level ID. UC Berkeley affiliations apply to many departments. |
| NVIDIA Research | Unresolved | The lab-name search yielded no verified lab-level ID. NVIDIA company disclosures are still monitored independently. |

A preprint can merge with its conference publication in OpenAlex. The collector accepts an exact requested DOI or explicit matching DOI/arXiv identifier among the work locations. It never infers identity from title similarity or author-name similarity.

## Live smoke check

On November 11, 2026 (Shanghai), the deployment host retrieved real weekly records from both the arXiv search API and OAI-PMH, and joined work metadata from OpenAlex without a key. For example, *MAC-v2: Long-horizon model-based value expansion from pixels* was submitted on November 4 at 23:51:02 UTC. Its abstract matched the configured “world model” term, and the exact arXiv DOI matched an OpenAlex authorship identifying Sergey Levine as A5026322200. [arXiv v1](https://arxiv.org/abs/2611.07245v1) · [OpenAlex W7225419626](https://openalex.org/W7225419626)

The production `paper_weekly` command ran on November 8, 2026 (Asia/Shanghai) with the research configuration narrowed to **speculative decoding**. It archived 24 real arXiv papers for October 26 through November 1 in New York. Each paper matched an OpenAlex work by exact identifier. The archived Markdown had five ranked entries. The full multi-topic query also collected papers in a separate capped smoke run.

A live `premarket_brief --source nvidia_news` archived eight announcements and wrote Markdown with email disabled. Separate HTTP and parser checks used genuine responses from OpenAI, NVIDIA, Google AI, Anthropic, Bitcoin Core, Ethereum Foundation and the Agave GitHub release API. Two Ethereum publisher notices linked to one postponed Hoodi event. The checked-in offline fixtures contain synthetic examples and can run independently of these live responses.
