# Workflow recipes

Worked end-to-end workflows. Read the one that matches the job before starting it; each is
a composition of the five primitives below, so the last section is worth skimming even when
none of the named recipes fits exactly.

- [Literature review / field orientation](#literature-review--field-orientation)
- [Systematic review corpus (PRISMA-ready)](#systematic-review-corpus-prisma-ready)
- [Abstract screening against criteria](#abstract-screening-against-criteria)
- [Extract values from full text](#extract-values-from-full-text)
- [Verify citations in a draft](#verify-citations-in-a-draft)
- [Ground a manuscript's claims](#ground-a-manuscripts-claims)
- [Scheduled monitoring](#scheduled-monitoring)
- [Comparison table across interventions](#comparison-table-across-interventions)
- [Author, journal and field analysis](#author-journal-and-field-analysis)
- [The five primitives](#the-five-primitives)

---

## Literature review / field orientation

Produces a launching pad, not a finished review: what a generous colleague who knows the
field would tell you over coffee — the lay of the land, the key people, how the thinking
evolved, what to read first.

**Phase 1 — reconnaissance (1 search).** Run one broad unfiltered query. Confirm it
returned data before going further. Read the abstracts for the major themes, *the
terminology researchers actually use* (usually not the requester's phrasing), the
methodological distinctions that matter here, and angles the requester hasn't considered.
Note unusually high citation counts relative to age — those are your foundational papers.

**Phase 2 — pick a framework.** Evaluate PICO first; it applies far beyond clinical work.

| Framework          | Use when                                                     | Components                                          |
| ------------------ | ------------------------------------------------------------ | --------------------------------------------------- |
| **PICO** (primary) | Health, clinical, behavioural, educational, social science   | Population · Intervention · Comparison · Outcome    |
| **SPIDER**         | No clear intervention — lived experience, attitudes          | Sample · Phenomenon · Design · Evaluation · Research type |
| **Decomposition**  | The topic is a technology or system, not a population        | Mechanism · Applications · Limitations · Alternatives |

Many real questions span frameworks. Pick a primary for structure, say which components
borrow from elsewhere, and say in one sentence why you picked it — clarity, not orthodoxy.

**Phase 3 — confirm the budget before spending it.** Show the requester what phase 1 found,
a framework mapping table (one row per component, each proposing a sub-area), and let them
choose the depth:

| Depth                      | Searches | Allocation                                                              |
| -------------------------- | -------- | ----------------------------------------------------------------------- |
| Quick scan                 | 5        | 5 sub-area                                                              |
| **Standard** (recommended) | 10       | 5 sub-area · 2 review-article · 2 era-gated · 1 follow-up on top-cited  |
| Deep dive                  | 20       | 5 sub-area · 5 review · 4 era-gated · 3 follow-ups · 3 spare            |

Extra budget buys *deeper analysis*, not more of the same. Review-article searches
(`systematic review <topic>`, `meta-analysis <topic>`) are disproportionately valuable —
one good review pre-digests dozens of primary studies.

**Phase 4 — execute.** For a review of any size, group the plan by sub-area and use
`batch` rather than `fanout`: the per-query folders keep each result set beside the query
and filters that produced it, which is what you need when writing up the search strategy
weeks later, and `--resume` means an interruption costs no calls.

```bash
python consensus.py batch -f review-plan.txt -o searches --dry-run   # verify the tree
python consensus.py batch -f review-plan.txt -o searches --page-size 50
```

Each sub-area becomes a group, so its `corpus.json` is already deduplicated across that
sub-area's phrasings and its `index.md` lists the repeat-hit papers. Use `fanout` instead
when you only need one merged corpus to answer a question now:

```bash
python consensus.py fanout -f review-queries.txt \
  --page-size 50 -o corpus.json --audit audit.md --format json
```

Era-gated passes go as separate runs: the most important sub-area once with
`--year-max 2015`, once with `--year-min 2021`. Comparing them exposes terminology shifts
("gut flora" → "gut microbiome" — a researcher searching only modern terms misses the
foundations), conclusion shifts (a paradigm change or accumulating evidence), and
methodological evolution (how mature the evidence base is).

Track three things across *all* searches — this is what turns a pile of results into field
knowledge:

1. **Repeat-hit papers** — `found_by` length. A paper in 3 of 5 sub-area searches is
   foundational to the field, not to one sub-area. These are the must-reads.
2. **Recurring authors** — the same group across searches signals a dominant lab. Note the
   top 3–5; anyone entering the field needs to know the key voices.
3. **Citations per year** — `citation_count ÷ years since publication`. A 2023 paper with
   150 citations is a far stronger signal than a 2008 paper with 150.

**Phase 5 — produce the guide.**

1. **Topic overview** — one paragraph: the topic, the framework used, and a characterization
   of the evidence landscape ("robust on X, sparse on Y").
2. **Start here — priority reading order.** The most valuable section. 5–7 papers ordered as
   a newcomer should read them: best recent review first, then the foundational paper(s),
   then 2–3 at the frontier, ending with one that highlights a gap or controversy. Per paper
   say what it contributes *in the sequence* and what to attend to while reading it ("focus
   on Table 3, which compares effect sizes across all RCTs"). If it's just five good papers
   with no sequencing logic, the section has degraded into a list.
3. **How the field got here** — a 5–8 row timeline plus a note on terminology evolution.
4. **Sub-area guides** — per sub-area: what the research shows, 3–5 key papers, 6–10 search
   terms including historical ones, 2–3 ready-to-paste Boolean strings.
5. **Key research groups** — the 3–5 most frequent authors, sub-areas, a representative paper.
6. **Open questions and gaps** — methodological, population/context, conceptual. For each,
   say *why it matters*, not just that it's unstudied.
7. **Bibliography** — alphabetical, every entry with a clickable Consensus URL.
8. **Audit log** — from `--audit`.

Flag any sub-area returning fewer than 5 papers as possibly incomplete coverage rather than
a genuine gap, and say which it is.

---

## Systematic review corpus (PRISMA-ready)

The deliverable is a reproducible, deduplicated record set with the flow counts a review has
to report.

```bash
# One sweep per search string, at the plan's max page size
python consensus.py sweep "<string 1>" --page-size 300 --exclude-preprints \
  --format json -o records-1.json
```

Use `sweep`, not `search`: a review needs every record, not the first page. Then merge with
`fanout` semantics — or run the whole set through `fanout --pages 3` if per-query depth
matters more than exhaustiveness on any one.

Record, at each stage, the number you'll have to put in the flow diagram: records
identified per source and search string, duplicates removed (`results_pre_merge` minus
`unique_papers` from the fanout output), records screened, full-text assessed, included.
The `--audit` log gives you the first two directly.

Two honesty requirements the format demands: report the search strings **exactly as sent**,
including filters (the audit log does this for you), and report the date of the search —
the index refreshes weekly, so a rerun will not match. Where the 1,000-result per-query
ceiling truncated a string, say so; a truncated search is not an exhaustive one.

---

## Abstract screening against criteria

Apply explicit inclusion/exclusion criteria across a corpus, with a decision log a second
reviewer can audit.

1. Build the corpus with `fanout` or `sweep`, exported as CSV: `--format csv -o corpus.csv`.
2. Write the criteria down *before* screening, as a numbered list, each independently
   checkable from a title plus abstract.
3. Screen each record against each criterion, recording per record: the decision, the
   criterion that decided it, and the sentence from the abstract that justifies it.
4. Emit the log as a table keyed on `doi` so it joins back to the corpus.

Keep exclusions attributable to a single criterion — "excluded: C3 (not human)" is auditable
where "excluded: doesn't fit" is not. Where the abstract genuinely doesn't say, mark
*unclear* and escalate to full text rather than guessing; the rate of unclears is itself a
finding about the literature.

---

## Extract values from full text

Numbers, doses, formulas and parameters with their measurement conditions attached.

```bash
python consensus.py search "what dose of creatine improves working memory" \
  --full-text --open-access --page-size 50 --format md -o excerpts.md
```

Phrase the query as *the thing being extracted*, not the topic — excerpts are selected
against the query, so "what dose of X improves Y" returns dosing passages where "X" returns
general ones. `--open-access` trades coverage for excerpt availability, which is the right
trade when the passages are the point.

Extract into a table with one row per value and these columns: value, units, the conditions
it was measured under, the `doi`, and the excerpt it came from. A number without its
conditions is not reusable, and a number without its excerpt is not defensible. Where
`full_text_chunks` is absent and you fall back to the abstract, say so in the row — and for
anything high-stakes, follow `url` and read the paper. Excerpts are passages, not papers.

---

## Verify citations in a draft

Confirm every reference in a document exists, then that each says what the citing text
claims. These are two different checks and conflating them misses the more common failure.

1. Parse the reference list into title + first author + year.
2. For each, `search` the exact title, `--page-size 5`.
3. Classify: **found** (title matches closely), **mismatch** (a real paper exists but the
   metadata differs — wrong year, journal or authors), **not found** (nothing close).
4. For each found reference, compare the citing sentence against the paper's `takeaway`
   and, where available, its `--full-text` excerpts. Classify support as *supports*,
   *unrelated*, or *contradicts*.

"Not found" is a claim about Consensus's index, not proof of fabrication — books, theses,
standards, and very new preprints may legitimately be absent. Report the three classes
separately and say which is which; a hallucinated citation and an unindexed book look
identical in the search output but mean opposite things.

---

## Ground a manuscript's claims

The inverse of verification: start from a draft with no citations and find one for every
empirical claim, or flag it as unsupported before a reviewer does.

1. Extract every empirical claim — a statement that could be true or false about the world.
   Skip definitions, framing and the authors' own contributions.
2. For each, run one search phrased as the claim itself. Claims make good queries because
   retrieval is semantic.
3. Match each claim to the strongest supporting paper, using `takeaway` for the judgment and
   `--full-text` where the claim is quantitative.
4. Output: claim → citation → the evidence line, or claim → **unsupported**.

Resist the pull to attach the nearest-looking paper to an unsupported claim. "Unsupported"
is the finding the author needs; a weak citation buries it where a reviewer will find it.

---

## Scheduled monitoring

Sweep topics on a schedule, diff against the last run, report only what changed.

```bash
python consensus.py diff -f topics.txt --state seen.json \
  --year-min 2026 --month-min 8 --page-size 50 --format md -o new-this-week.md
```

`diff` stores the DOIs it has seen and reports only the new ones, updating the state file
unless you pass `--dry-run`. Window by `--year-min`/`--month-min` as well as diffing: the
window keeps the calls cheap, the diff keeps the report honest.

Route by relevance rather than dumping everything — a monitor nobody reads is a monitor that
has failed. A workable rule: report papers that change something (a new trial, a
contradicted finding, a new method) and summarize the rest as a count.

Record every run, including empty ones. An empty entry distinguishes "no new evidence this
week" from "the job broke silently three weeks ago", and only one of those needs attention.

For unattended runs, note that a 429 for the monthly allowance is terminal — the job should
surface it rather than retry until the next window.

---

## Comparison table across interventions

One search per intervention, same filters and same metrics across all of them, merged into
one table.

```bash
python consensus.py fanout \
  --query "intervention A effect on <outcome>" \
  --query "intervention B effect on <outcome>" \
  --query "intervention C effect on <outcome>" \
  --study-types "rct,meta-analysis" --human --year-min 2015 \
  --page-size 30 --format json -o comparisons.json --audit audit.md
```

The filters must be identical across interventions or the table compares your filters
rather than the interventions. Columns worth extracting: sample size (`sample_size`),
session/dose count, follow-up duration (`study_duration_days`), effect size, and study
design (`study_type`).

Where an intervention returns thin results, distinguish "less studied" from "studied under
different terminology" by re-running that row unfiltered before concluding anything — and
say in the table which cells rest on how many papers.

---

## Author, journal and field analysis

**An author's body of work.** Search their name plus their field, then their most-cited
paper's title to trace downstream impact. `institutions` and `countries_of_study` help
disambiguate common names — the API has no author-ID lookup, so name collisions are yours
to resolve, and you should say when you couldn't.

**Where to submit.** Search your paper's topic and abstract as the query, then read
`journal_name` and `sjr_best_quartile` across the top results. The journals actually
publishing work like yours, with their quartile, is a more realistic target list than a
reputation ranking.

**Terminology drift.** Run the same concept with `--year-max <old>` and `--year-min
<recent>` and compare the vocabulary in titles. Then search the *old* terms too — a
literature search using only current terminology silently misses the foundations.

---

## The five primitives

Every recipe above composes these. When none of the recipes fits, compose your own.

| Primitive                    | What it does                                     | How                                                                          |
| ---------------------------- | ------------------------------------------------ | ---------------------------------------------------------------------------- |
| **Fan-out and merge**        | One question → 5–30 searches → one clean corpus  | `fanout`; dedupes on `doi`, keeps `found_by`                                 |
| **Filter ladder**            | Trades recall for precision in controlled steps  | `ladder`; record the count at each rung                                      |
| **Exhaustive sweep**         | Every record, not the first page                 | `sweep`; walks `next_page` to the 1,000-result ceiling                       |
| **Excerpt-first extraction** | Numbers you can defend                           | `--full-text` with the query phrased as the thing extracted                  |
| **Diff against last run**    | Turns any search into a monitor                  | `diff`; windows by year/month, diffs on `doi`                                |

`batch` is fan-out with the results written out per query instead of merged — reach for it
whenever the corpus has to outlive the run, and for anything you may need to resume.

The filter ladder is the highest-leverage habit of the five. Most disappointing Consensus
results are an unfiltered query, and most empty ones are four filters applied at once. Move
one rung at a time and keep the counts — then you can say which constraint was binding
instead of guessing.
