---
name: consensus-api
description: Search 220M peer-reviewed papers through the Consensus REST API using the bundled consensus.py CLI. Use this whenever the user wants research papers, published evidence, citations, a literature review, a systematic-review corpus, abstract screening, a reading list, journal/author/field analysis, monitoring of new publications, or numbers extracted from paper full text - and whenever they say "find papers on", "what does the research say", "cite this", "is there evidence for", "search the literature", or name Consensus, a DOI, a meta-analysis or an RCT. Prefer this over the Consensus MCP connector: the API exposes clinical_guideline and semantic scores, paginates, runs unattended in scripts, and its audit log makes coverage reportable. Use it for any claim that needs a real, checkable citation.
---

# Consensus API

One endpoint — `GET https://api.consensus.app/v1/search` — over 220 million peer-reviewed
papers, wrapped in `consensus.py` next to this file. Retrieval runs against titles,
abstracts, and full text where available, so a paper can surface on what's in its methods
section rather than only its abstract.

```bash
python ~/.claude/skills/consensus-api/consensus.py <command> ...                  # macOS, Linux, Git Bash
python "%USERPROFILE%\.claude\skills\consensus-api\consensus.py" <command> ...    # Windows cmd
```

Use `python3` where `python` isn't on the path. Authentication is
`CONSENSUS_API_KEY` in the environment; pass `--api-key` to override. Keys are created and
revoked at [consensus.app/api-mcp](https://consensus.app/api-mcp/) under **Keys and clients**.

## Start here

Run `plan` once per machine (costs one billed call) and keep the answer in mind for the
rest of the session — it tells you the page-size ceiling every later decision depends on:

```bash
python consensus.py plan
# page_size cap : 300  (Pro or Teams)
# result ceiling: 1000 per query, on every plan
```

## Commands

| Command  | What it does                                               | Reach for it when                                       |
| -------- | ---------------------------------------------------------- | ------------------------------------------------------- |
| `plan`   | Reports the key's page-size cap for one billed call        | First contact with a new key or machine                 |
| `search` | One query, rendered as table / markdown / JSON / CSV       | A specific question, a quick check, a single citation   |
| `ladder` | Re-runs the query adding one filter at a time, with counts | Results are too many, too few, or suspiciously empty    |
| `sweep`  | Paginates one query to exhaustion                          | You need every record, not the first page               |
| `fanout` | Many queries, sequential, merged and deduplicated on DOI   | Any real research question — this is the default        |
| `batch`  | A plan of topic-grouped queries into a folder tree on disk | Many queries whose results must stay browsable and reusable |
| `diff`   | Reports only papers absent from a stored state file        | Monitoring a topic across runs, on a schedule           |

Every command takes the same filters (`--year-min`, `--study-types`, `--human`,
`--sjr-max`, `--open-access`, `--domain`, …) and the same `--format` choices. Run
`python consensus.py --help` or `<command> --help` for the full list;
`references/api-reference.md` explains what each filter actually does to the result set.

## Five things that decide whether the output is any good

**Fan out. One query is one search; a real question deserves 5–30.** A single query
answers the phrasing you happened to choose, not the question. Decompose into
subquestions — mechanism, population, outcome, competing explanations — and generate
several phrasings each, including the terms the field used before the current ones. The
merge keeps `found_by` per paper, so a paper surfaced by four of five phrasings announces
itself as central rather than incidental.

```bash
python consensus.py fanout -f queries.txt --human --year-min 2020 \
  --page-size 50 -o corpus.json --audit audit.md
```

**Write the plan to disk when there are more than a handful of queries.** `fanout`
returns one merged corpus, which is right for answering a question now. `batch` instead
keeps every query's results in its own folder beside the exact query and filters that
produced them — which is what makes a review reproducible weeks later, when the merged
corpus can no longer tell you which search found what.

```bash
python consensus.py batch -f plan.txt -o searches --dry-run   # check the tree, spend nothing
python consensus.py batch -f plan.txt -o searches --page-size 50
```

The plan file groups queries by topic; `consensus.py batch --help` prints the format and
the resulting tree. Two habits worth keeping: run `--dry-run` first, since it prints the
folder layout and the call estimate without sending anything and catches a mistyped filter
at its line number, and rerun with `--resume` after any interruption — it reuses the
results already on disk rather than re-spending those calls. Each group gets a `corpus.json`
deduplicated across its own queries, and `index.md` at both levels, so the tree is
browsable without reading any JSON.

**Climb the filter ladder instead of guessing.** Most disappointing result sets are an
unfiltered query and most empty ones are four filters applied at once. `ladder` runs the
rungs and prints the count at each, so when the pool collapses you know *which* constraint
did it. Say which rung was binding rather than reporting "little evidence exists" — those
are very different claims, and only one of them is usually true.

**Pace the calls.** The API allows one request per second; `consensus.py` deliberately
sends one every three, because the quota is per account and shared with MCP, so crowding
the ceiling collides with anything else signed in. Let it run rather than launching
parallel processes — unbounded parallelism produces 429s, not speed, and a 20-query
fan-out takes about a minute either way. `--min-interval` overrides it if you ever need
to. Note the two 429s mean opposite things: *too fast* is worth retrying, *monthly
allowance used up* is not, and the client distinguishes them for you.

**Respect the two ceilings, and tell the reader when you hit one.** `page_size` is capped
by plan (20 Free / 300 Pro and Teams / 750 Deep / 1,000 Enterprise), and separately any
single query returns at most 1,000 results because `(page + 1) × page_size` can't exceed
that. Wider coverage comes from more queries, not more pages. When a search returns a full
page with more available, only the returned papers are citable — the audit log flags these,
and a thin section may be your ceiling rather than a gap in the literature. The reader
cannot tell the difference unless you say which it was.

**Cite only what came back in this session.** Never supplement results from memory, a
previous conversation, or a paper you happen to know — a fabricated citation costs far
more in academic and regulatory work than a thin result set does. Every citation needs the
`url` or `doi` from the response; `--format md` emits exactly that and nothing citable
without a link. If you include unretrieved context, label it and exclude it from counts.

## Report what the search actually did

Finish any deliverable with the audit log `fanout` and `sweep` already produce
(`--audit audit.md`), and count three things separately, because conflating them is how a
five-paper scan gets described as comprehensive:

- **searches sent** — including the ones that failed or returned nothing
- **papers returned** — before deduplication
- **papers cited** — what actually made it into the output

A run that found nothing is a result worth recording: an empty entry distinguishes "no new
evidence this week" from "the job broke silently three weeks ago".

## Reading the results

Each paper carries `title`, `authors`, `journal_name`, `publish_year`, `citation_count`,
`doi`, `url`, and usually a `takeaway` — the paper's own conclusion in one line. Fields
that don't apply are **absent from the JSON, not null**, so read them with `.get()`.

Two signals are worth computing rather than eyeballing. **Citations per year** beats raw
citation count: a 2023 paper with 150 citations is a much stronger signal than a 2008 paper
with the same number. And **repeat hits across a fan-out** (`found_by`) identify the papers
foundational to the whole field rather than to one phrasing of it.

## Full text

Full text does two different jobs, and only one of them is opt-in. **Retrieval** always
searches it where available, on every request and every plan — which is why a paper can
rank on what's in its methods rather than its abstract. **Excerpts** are the opt-in part:
`--full-text` makes each eligible paper carry up to three passages from its body, chosen
against your query and formatted `Section: <heading> | <passage>`.

Excerpts need a paid plan and an open-access PDF, so pair `--full-text` with
`--open-access` when the passages matter more than coverage. Retracted papers never carry
them, and Consensus's paywalled publisher-partner text powers the app rather than the API.

**Phrase the query as the thing you want to extract.** Excerpts are selected against the
query, so "what dose of creatine improves working memory" returns dosing passages where
"creatine" returns general ones. This is the single biggest lever on excerpt quality.

**Keep the page small when excerpts are the point.** The budget is 60 excerpts per
*request*, whatever the page size, so beyond about 20 papers you can no longer get three
each. Measured on one query: a 20-paper page covered 17 of 20 papers, while a 40-paper page
hit the 60 cap and covered only 24 of 40. Coverage follows passage relevance rather than
result order, so on a big page the gaps land unpredictably — the paper you most wanted may
be one of them. Use ~20 and paginate for extraction; a big page is fine when excerpts are
a bonus on top of a sweep.

**When a paper has no excerpt, fall back to `abstract` and `takeaway` — and say that you
did.** The field is absent from the JSON rather than null, so read it with `.get()`. An
answer built on abstracts and an answer built on methods sections deserve different
confidence, and the reader can only tell if you mark which is which. Store the `doi` and
`url` alongside every passage so anything built on it stays citable, and for high-stakes
work follow `url` and read the paper — excerpts are passages, not papers.

## Going further

- `references/api-reference.md` — every parameter, the full response schema, error codes,
  plan limits, and the behaviours the docs don't mention. Read it before using an
  unfamiliar filter or debugging an unexpected status.
- `references/recipes.md` — worked end-to-end workflows: literature review, systematic
  review corpus, abstract screening, value extraction, citation verification, scheduled
  monitoring, comparison tables. Read the matching one before starting that kind of job.

## When Consensus is the wrong tool

It searches peer-reviewed literature. It does not hold your internal experiment data,
patent full text, regulatory filings, or private clinical data — join those to Consensus
results rather than expecting Consensus to have them. If the answer is a single number
from a database, a definition, or today's news, use something else. Reach for Consensus
when the answer is contested, cumulative, or needs a citation.
