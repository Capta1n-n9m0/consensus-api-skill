# Consensus API reference

Everything the endpoint accepts and returns, the plan limits that shape every workflow,
and the behaviours worth knowing that the published docs don't spell out.

- [Endpoint and auth](#endpoint-and-auth)
- [Query parameters](#query-parameters)
- [Response schema](#response-schema)
- [Errors](#errors)
- [Plans, quotas and billing](#plans-quotas-and-billing)
- [Observed behaviour](#observed-behaviour)
- [How search works underneath](#how-search-works-underneath)
- [API vs MCP](#api-vs-mcp)

## Endpoint and auth

```
GET https://api.consensus.app/v1/search
x-api-key: YOUR_API_KEY
```

Base URL `https://api.consensus.app`. Enterprise keys may use `Authorization: Bearer
YOUR_API_KEY` instead. `GET /v1/quick_search` is the legacy endpoint — deprecated, removed
2027-02-07; use `/v1/search`.

Create and revoke keys at [consensus.app/api-mcp](https://consensus.app/api-mcp/) under
**Keys and clients**. The full key is shown once, at creation.

## Query parameters

`query` is the only required parameter. Everything else narrows or annotates.

### Time

| Parameter   | Type | Notes                                            |
| ----------- | ---- | ------------------------------------------------ |
| `year_min`  | int  | Exclude papers before this year                  |
| `year_max`  | int  | Exclude papers after this year                   |
| `month_min` | 1–12 | Within `year_min`; **requires** `year_min`       |
| `month_max` | 1–12 | Within `year_max`; **requires** `year_max`       |

Era-gating is the cheapest way to read a field's trajectory: run the same query once with
`--year-max 2015` and once with `--year-min 2021` and compare. Terminology shifts ("gut
flora" → "gut microbiome"), conclusion shifts, and methodological evolution
(observational → RCT → meta-analysis) all show up in that one comparison.

### Study design and population

| Parameter          | Type     | Notes                                                        |
| ------------------ | -------- | ------------------------------------------------------------ |
| `study_types`      | list     | Only these designs. Exact strings — see the enum below        |
| `human`            | bool     | Human studies only                                           |
| `controlled`       | bool     | Controlled studies only                                      |
| `sample_size_min`  | int ≥1   | Exclude smaller studies                                      |
| `duration_min`     | int      | Minimum study duration, **days**                             |
| `duration_max`     | int      | Maximum study duration, **days**                             |
| `clinical_guideline` | bool   | Papers classified as clinical guidelines. Not exposed on MCP |
| `medical_mode`     | bool     | Top medical journals and guidelines, ~8M documents           |

`study_types` values, exactly as the API spells them:

```
bench experiment · case report · case study · case-control study · cohort study
commentary or perspective · cross-sectional study · field study
historical or archival analysis · interview study · literature review
longitudinal / panel data study · meta-analysis · mixed methods study
non-randomized experimental study · non-rct in vitro · other · rct · systematic review
theoretical, modeling, or simulation study · non-rct experimental
non-rct observational study · animal
```

`consensus.py --list-study-types` prints these, and rejects unknown values before
spending a call on a request that would quietly match nothing.

### Quality and provenance

| Parameter           | Type   | Notes                                                              |
| ------------------- | ------ | ------------------------------------------------------------------ |
| `sjr_min`           | 1–4    | Exclude journals in **better** quartiles                           |
| `sjr_max`           | 1–4    | Exclude journals in **lesser** quartiles. `sjr_max=1` keeps only Q1 |
| `citation_min`      | int    | Exclude papers with fewer citations                                |
| `exclude_preprints` | bool   | Peer-reviewed only                                                 |
| `open_access`       | bool   | Open-access papers only. Pair with full-text excerpts              |
| `publisher_name`    | string | Comma-separated publisher display names                            |
| `journal_name`      | string | **Boosts** matching journals — does not exclude others             |

1 is the best quartile, so `sjr_min` and `sjr_max` read backwards at first glance:
`sjr_max=1` is the strict one. `citation_min` quietly biases toward older work, since
citations accumulate with age — on recent literature prefer sorting by citations per year
after the fact.

### Scope

| Parameter | Type   | Notes                                                      |
| --------- | ------ | ---------------------------------------------------------- |
| `domain`  | string | Comma-separated **academic field codes**, not web domains  |
| `country` | string | ISO 3166-1 alpha-2 codes (`us`, `gb`), countries of study  |

Field codes: `med bio cs chem psych phys mat eng env bus econ math poli agri edu soc geol
geog hist art philo law ling`.

Unknown country codes are ignored; if none resolve, the filter silently isn't applied —
so a typo here produces a wider result set, not an error.

### Output control

| Parameter                  | Type | Notes                                                             |
| -------------------------- | ---- | ----------------------------------------------------------------- |
| `page`                     | 0–49 | Zero-indexed. Past page 0 requires a paid plan                     |
| `page_size`                | int  | Default 20, **clamped** to the plan cap rather than rejected      |
| `include_full_text_chunks` | bool | Query-relevant excerpts. Paid plans only                          |
| `include_semantic_score`   | bool | Relevance score, **top 50 results only**. Not exposed on MCP      |

`(page + 1) × page_size` may not exceed 1,000 on any plan, Enterprise included. Because
`page_size` is clamped silently, always read the `page_size` echoed back rather than
assuming the value you sent.

## Response schema

```json
{
  "results": [ { … } ],
  "page": 0,
  "page_size": 20,
  "is_end": false,
  "next_page": 1
}
```

Per result — always present: `title`, `authors[]`, `abstract`, `doi`, `journal_name`,
`volume`, `pages`, `publish_year`, `url`, `citation_count`.

Present when known: `takeaway` (the paper's conclusion in one line — the single most useful
field for triage), `study_type`, `publisher_name`, `sjr_best_quartile` (1 = top),
`sample_size`, `study_count` (studies synthesized, for meta-analyses), `population_type`
(`human` / `animal` / `other`), `is_preprint`, `countries_of_study[]`,
`study_duration_days`, `influential_citation_count` (from Semantic Scholar),
`institutions[]`, `publish_date` (`YYYY-MM-DD`), `semantic_score`, `full_text_chunks[]`.

`full_text_chunks` entries are formatted `Section: <heading> | <passage>` — split on the
first `|`. Up to 3 per paper and 60 per request, chosen against your query across the whole
page, so on a large page some eligible papers carry fewer or none. Excerpts require an
open-access PDF; Consensus also reads paywalled full text from publisher partners, but that
text powers the app rather than being returned through the API. Retracted papers never
carry excerpts and are never used in analyses.

## Errors

| Status                             | Meaning                                       | What to do                                              |
| ---------------------------------- | --------------------------------------------- | ------------------------------------------------------- |
| `401`                              | Missing, invalid or revoked key               | Check `x-api-key`, or create a new key                  |
| `402`                              | Billing past due                              | Resolve payment; calls resume after                     |
| `403` `feature_not_allowed`        | Pagination or excerpts on a Free plan         | Drop the option or upgrade                              |
| `422`                              | Invalid parameter                             | Read `detail[].loc` for the offending field             |
| `429` "used all included searches" | Monthly allowance exhausted                   | **Retrying does not help.** Enable additional usage     |
| `429` "Too many requests"          | Faster than 1 req/sec                         | Wait for `retry-after`, then retry                      |

The two 429s are the ones that matter: one is transient and one is terminal, and a retry
loop that can't tell them apart will burn minutes achieving nothing. `consensus.py`
distinguishes them by response body.

Retry policy worth keeping: on failure wait 3 seconds and retry once; after 3 consecutive
failures across any combination of calls, stop and surface the problem rather than quietly
producing a thin result. `consensus.py` implements exactly this.

## Plans, quotas and billing

|                                | Free | Pro   | Teams | Deep   | Enterprise |
| ------------------------------ | ---- | ----- | ----- | ------ | ---------- |
| Included calls per month       | 30   | 500   | 500   | 2,000  | Custom     |
| Max with additional usage      | 30   | 1,000 | 1,000 | 10,000 | Custom     |
| Max `page_size`                | 20   | 300   | 300   | 750    | 1,000      |
| Pagination past page 0         | No   | Yes   | Yes   | Yes    | Yes        |
| Full-text excerpts             | No   | Yes   | Yes   | Yes    | Yes        |
| Requests per second            | 1    | 1     | 1     | 1      | Custom     |

- **One call per 100 papers returned**, rounded up, minimum one call per request. A request
  returning 20 papers is 1 call; 150 papers is 2. So a `page_size` of 100 costs the same as
  20 and returns five times as much — raising page size is usually free breadth.
- **API and MCP draw on the same monthly pool.**
- **Usage resets on the 1st.** Track it in the Usage tab of the API & MCP Dashboard.
- Additional usage is **$0.05 per call** past the included amount, off by default, and
  Free plans stop at 30.

Every plan can use every search filter — quality comes from filters, which cost nothing.

## Observed behaviour

Verified against the live endpoint, and not all of it is in the published docs:

- **Absent, not null.** Optional fields are omitted from the JSON entirely rather than
  returned as `null`. Read every optional field with `.get()`; `paper["sample_size"]` will
  raise on most papers. `next_page` is likewise absent when `is_end` is true.
- **`page_size` is echoed after clamping**, which makes it a free plan probe: a request
  with a filter that can match nothing (`year_min=2300`) returns zero papers — so it bills
  as a single call — while still reporting your true ceiling. That is what
  `consensus.py plan` does.
- **`is_end: false` alongside a not-quite-full page** happens; treat `is_end` as the
  authority on whether to request another page, not the returned count.
- **The response carries no total-found count.** You cannot ask "how many papers exist on
  X" — only "did this page fill up". This is why the filter ladder measures with a large
  `--page-size` (default 100): below the page size the count is real, at or above it you
  only know there are more.
- **Consensus `url`s carry a `?utm_source=publicapi` suffix.** Keep them as returned;
  they resolve fine and stripping them gains nothing.
- **Cloudflare blocks plain `curl` on `docs.consensus.app`** (not on the API itself). Pass
  a browser `User-Agent` if you need to re-fetch the documentation.

## How search works underneath

Worth knowing because it explains what the filters can and can't do:

1. **Hybrid retrieval** across all 220M papers — semantic embeddings for intent plus BM25
   for exact terms, matched against titles, abstracts and full text where available.
   Typos are rewritten, MeSH synonyms expanded, Boolean operators supported.
2. **Quality re-rank** of the top 1,500, blending relevance with recency, citation count
   and journal reputation.
3. **Precision re-rank** of the top 20 with a larger model.

The index draws on Semantic Scholar, OpenAlex, Consensus's own crawl of the scholarly web,
and publisher partnerships, refreshed weekly. Because ranking already blends in quality
signals, `citation_min` and `sjr_max` are for *excluding* work you don't want to see, not
for surfacing the best — the ranking has largely done that.

## API vs MCP

|                          | REST API                                | MCP server                          |
| ------------------------ | --------------------------------------- | ----------------------------------- |
| Auth                     | One `x-api-key` for the integration     | OAuth per user, per-user limits     |
| Best for                 | Pipelines, sweeps, anything scheduled   | Interactive work, no code           |
| `clinical_guideline`     | Available                               | Not exposed                         |
| `include_semantic_score` | Available                               | Not exposed                         |
| Requests per second      | 1 (self-serve plans)                    | 3 on paid plans                     |
| Max papers per request   | 20 Free / 300 Pro / 750 Deep / 1,000 Ent | 10 Free / 3 signed-out             |

## Sources

Fetched from `docs.consensus.app` (see `/llms.txt` for the full index) plus the OpenAPI
spec at `https://docs.consensus.app/openapi.json`, and verified against the live endpoint.
Key pages: `/api-get-started`, `/api-reference/query-for-relevant-papers`,
`/api-plans-and-access`, `/api-full-text`, `/use-cases/best-practices`,
`/get-started/how-search-works`.
