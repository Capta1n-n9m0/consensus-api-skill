#!/usr/bin/env python3
"""Consensus REST API client for agent workflows.

One endpoint (GET https://api.consensus.app/v1/search), stdlib only, built so the
things that go wrong at scale -- the 1 req/sec rate limit, the 1,000-result ceiling,
silent filter emptiness, uncounted searches -- are handled once here instead of
being rediscovered in every workflow.

Auth: CONSENSUS_API_KEY in the environment (or --api-key).
Run `python consensus.py --help` for commands.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.consensus.app/v1/search"
RESULT_CEILING = 1000  # (page + 1) * page_size may not exceed this, on any plan

# Pacing: every self-serve plan allows one request per second, and going faster buys
# 429s rather than speed, so the client paces itself rather than trusting the caller.
# The default sits well back from the limit at one call every 3 seconds: the quota is
# per account and shared with MCP, so anything else signed in on the same account is
# spending from the same budget, and a fan-out that crowds the ceiling collides with it.
# A 20-query fan-out costs a minute either way -- retries would cost more.
MIN_INTERVAL = 3.0
RETRY_WAIT = 3.0
MAX_CONSECUTIVE_FAILURES = 3

STUDY_TYPES = [
    "bench experiment", "case report", "case study", "case-control study",
    "cohort study", "commentary or perspective", "cross-sectional study",
    "field study", "historical or archival analysis", "interview study",
    "literature review", "longitudinal / panel data study", "meta-analysis",
    "mixed methods study", "non-randomized experimental study", "non-rct in vitro",
    "other", "rct", "systematic review",
    "theoretical, modeling, or simulation study", "non-rct experimental",
    "non-rct observational study", "animal",
]

DOMAINS = [
    "med", "bio", "cs", "chem", "psych", "phys", "mat", "eng", "env", "bus",
    "econ", "math", "poli", "agri", "edu", "soc", "geol", "geog", "hist", "art",
    "philo", "law", "ling",
]

# Query params that are pure filters, in the order the API reference lists them.
FILTER_PARAMS = [
    "year_min", "year_max", "month_min", "month_max", "study_types", "human",
    "controlled", "sample_size_min", "sjr_min", "sjr_max", "citation_min",
    "duration_min", "duration_max", "exclude_preprints", "open_access",
    "publisher_name", "domain", "country", "journal_name", "clinical_guideline",
    "medical_mode",
]

BOOL_PARAMS = {
    "human", "controlled", "exclude_preprints", "open_access",
    "clinical_guideline", "medical_mode", "include_full_text_chunks",
    "include_semantic_score",
}

INT_PARAMS = {
    "year_min", "year_max", "month_min", "month_max", "sample_size_min",
    "sjr_min", "sjr_max", "citation_min", "duration_min", "duration_max",
    "page", "page_size",
}

ALL_PARAMS = set(FILTER_PARAMS) | INT_PARAMS | BOOL_PARAMS

# Names Windows refuses as a path component, whatever the extension. A query or
# group called "Nul" would otherwise produce a folder that silently isn't one.
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", "clock$"} | {
    "%s%d" % (stem, i) for stem in ("com", "lpt") for i in range(1, 10)
}


class ConsensusError(RuntimeError):
    """A request failed in a way retrying will not fix."""


class _Retryable(RuntimeError):
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _classify(code, body, retry_after):
    """Turn an HTTP status into the right kind of failure.

    The two 429s mean opposite things: one says slow down, and retrying works; the
    other says the month's allowance is gone, and retrying only wastes time.
    """
    try:
        wait = float(retry_after) if retry_after else None
    except (TypeError, ValueError):
        wait = None

    if code == 401:
        return ConsensusError(
            "401 -- key missing, invalid or revoked. Check CONSENSUS_API_KEY."
        )
    if code == 402:
        return ConsensusError(
            "402 -- billing past due. Resolve payment; calls resume after."
        )
    if code == 403:
        return ConsensusError(
            "403 feature_not_allowed -- pagination (page>0) and full-text excerpts "
            "need a paid plan. Drop the option or upgrade."
        )
    if code == 429:
        if "included" in body.lower() or "allowance" in body.lower():
            return ConsensusError(
                "429 -- monthly call allowance used up. Retrying will not help; "
                "enable additional usage at https://consensus.app/api-mcp/ "
                "or wait for the 1st."
            )
        return _Retryable("429 -- too fast (limit is 1 request/sec)", wait or RETRY_WAIT)
    if code == 422:
        return ConsensusError("422 -- invalid parameter: " + body)
    if code >= 500:
        return _Retryable(str(code) + " -- server error", wait)
    return ConsensusError(str(code) + " -- " + body)


class Client:
    """Paced, self-auditing wrapper around GET /v1/search.

    Every call lands in `self.audit`, because the three numbers that decide how much
    weight a result set deserves -- searches sent, papers returned, papers cited --
    are only cheap to produce if you record them as you go.
    """

    def __init__(self, api_key, min_interval=MIN_INTERVAL, verbose=True):
        if not api_key:
            raise ConsensusError(
                "No API key. Set CONSENSUS_API_KEY or pass --api-key. Create one at "
                "https://consensus.app/api-mcp/ under 'Keys and clients'."
            )
        self.api_key = api_key
        self.min_interval = min_interval
        self.verbose = verbose
        self._last_call = 0.0
        self.audit = []
        self.page_size_cap = None

    # -- plumbing ---------------------------------------------------------

    def _wait_turn(self):
        gap = time.monotonic() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)

    def _log(self, msg):
        if self.verbose:
            print(msg, file=sys.stderr)

    def _request(self, params):
        url = BASE_URL + "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(
            url, headers={"x-api-key": self.api_key, "Accept": "application/json"}
        )
        self._wait_turn()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                self._last_call = time.monotonic()
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self._last_call = time.monotonic()
            body = exc.read().decode("utf-8", "replace")[:400]
            raise _classify(exc.code, body, exc.headers.get("retry-after")) from None
        except urllib.error.URLError as exc:
            self._last_call = time.monotonic()
            raise _Retryable("network error: " + str(exc.reason)) from None

    def search(self, query, **filters):
        """One search. Retries once on a transient failure, then gives up loudly.

        Silently skipping a failed search is how a thin result set gets reported as
        a complete one, so a failure that survives the retry raises.
        """
        params = {"query": query}
        for key, value in filters.items():
            if value is None or value == [] or value == "":
                continue
            if isinstance(value, bool):
                params[key] = "true" if value else "false"
            elif isinstance(value, (list, tuple)):
                # study_types is a real array parameter, so it must go out as a
                # repeated key (?study_types=rct&study_types=meta-analysis) and
                # urlencode(doseq=True) does that from a list. Comma-joining it
                # instead -- which the published docs example appears to show --
                # is rejected with 422 on more than one value.
                params[key] = list(value)
            else:
                params[key] = value

        attempt = 0
        while True:
            attempt += 1
            try:
                body = self._request(params)
                break
            except _Retryable as exc:
                if attempt >= 2:
                    self._record(query, filters, None, "failed: " + str(exc))
                    raise ConsensusError(str(exc) + " (after 1 retry)") from None
                wait = exc.retry_after or RETRY_WAIT
                self._log("  ! %s -- retrying in %.0fs" % (exc, wait))
                time.sleep(wait)
            except ConsensusError as exc:
                self._record(query, filters, None, "failed: " + str(exc))
                raise

        results = body.get("results", [])
        page_size = body.get("page_size", len(results))
        if self.page_size_cap is None:
            self.page_size_cap = page_size
        status = "ok" if results else "ok, no results"
        if attempt > 1:
            status += " (retry succeeded)"
        self._record(query, filters, len(results), status, body)
        return body

    def _record(self, query, filters, returned, status, body=None):
        entry = {
            "n": len(self.audit) + 1,
            "at": _now(),
            "query": query,
            "filters": {k: v for k, v in filters.items()
                        if v is not None and v != [] and v != ""},
            "returned": returned,
            "status": status,
        }
        if body is not None:
            entry["page"] = body.get("page", 0)
            entry["page_size"] = body.get("page_size")
            entry["more_available"] = not body.get("is_end", True)
            entry["calls_billed"] = max(1, math.ceil((returned or 0) / 100))
        self.audit.append(entry)

    # -- workflows --------------------------------------------------------

    def sweep(self, query, max_results=None, **filters):
        """Walk pages until the backend says stop or the per-query ceiling hits.

        Coverage past 1,000 results comes from more queries, not more pages -- the
        ceiling is per query on every plan, Enterprise included.
        """
        page = filters.pop("page", 0) or 0
        out = []
        while True:
            body = self.search(query, page=page, **filters)
            out.extend(body.get("results", []))
            if max_results and len(out) >= max_results:
                return out[:max_results]
            if body.get("is_end", True) or body.get("next_page") is None:
                return out
            page = body["next_page"]
            page_size = body.get("page_size", 20)
            if (page + 1) * page_size > RESULT_CEILING:
                self._log(
                    "  . stopping at the %d-result per-query ceiling; narrow the "
                    "query or add filters for wider coverage" % RESULT_CEILING
                )
                return out

    def fanout(self, queries, per_query_pages=1, **filters):
        """Run many queries sequentially and merge them into one deduplicated corpus.

        The merge keeps which queries found each paper: a paper surfaced by four of
        five phrasings is a different kind of hit than one found by a single narrow
        query, and that signal is free if you record it during the merge.
        """
        corpus = {}
        pre_merge = 0
        consecutive_failures = 0

        for i, query in enumerate(queries, 1):
            self._log("[%d/%d] %s" % (i, len(queries), query))
            try:
                if per_query_pages > 1:
                    page_size = filters.get("page_size") or 20
                    results = self.sweep(
                        query, max_results=per_query_pages * page_size, **filters
                    )
                else:
                    results = self.search(query, **filters).get("results", [])
                consecutive_failures = 0
            except ConsensusError as exc:
                consecutive_failures += 1
                self._log("  ! " + str(exc))
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise ConsensusError(
                        "%d consecutive failures -- stopping. Partial coverage "
                        "reported as complete is worse than no result."
                        % MAX_CONSECUTIVE_FAILURES
                    ) from None
                continue

            pre_merge += len(results)
            for paper in results:
                key = (paper.get("doi") or paper.get("url")
                       or paper.get("title", "")).lower()
                if key in corpus:
                    corpus[key]["found_by"].append(query)
                else:
                    record = dict(paper)
                    record["found_by"] = [query]
                    corpus[key] = record
            self._log("  -> %d results, %d unique so far" % (len(results), len(corpus)))

        papers = sorted(
            corpus.values(),
            key=lambda p: (-len(p["found_by"]), -(p.get("citation_count") or 0)),
        )
        return {
            "queries_sent": len(queries),
            "results_pre_merge": pre_merge,
            "unique_papers": len(papers),
            "page_size_cap": self.page_size_cap,
            "papers": papers,
            "audit": self.audit,
        }


# -- batch plans ----------------------------------------------------------

# Matches `key = value` runs in a @filters directive, allowing spaces inside the
# value -- study types like "systematic review" would break a naive split.
_FILTER_DIRECTIVE = re.compile(r"([A-Za-z_]\w*)\s*=\s*(.*?)(?=\s+[A-Za-z_]\w*\s*=|$)")

MAX_PATH_HINT = 240  # Windows MAX_PATH is 260 unless long paths are enabled


def safe_name(text, limit=50):
    """Turn arbitrary query text into one portable path component.

    Truncation happens at a word boundary so the folder still reads as words, and
    the Windows-hostile cases -- illegal characters, trailing dots, reserved device
    names -- are handled here rather than surfacing as a confusing IO error later.
    """
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = "".join(ch for ch in text if ord(ch) >= 32)
    if len(text) > limit:
        head = text[:limit].rsplit(" ", 1)[0]
        text = (head or text[:limit]).strip()
    text = text.rstrip(". ")
    if not text:
        return "unnamed"
    if text.lower() in WINDOWS_RESERVED:
        return text + "_"
    return text


def _coerce_filter(key, value, where):
    if key not in ALL_PARAMS:
        sys.exit("%s: unknown filter '%s'\nvalid: %s"
                 % (where, key, ", ".join(sorted(ALL_PARAMS))))
    value = value.strip()
    if key == "study_types":
        types = [s.strip() for s in value.split(",") if s.strip()]
        unknown = [s for s in types if s not in STUDY_TYPES]
        if unknown:
            sys.exit("%s: unknown study type(s) %s\nvalid: %s"
                     % (where, unknown, ", ".join(STUDY_TYPES)))
        return types
    if key in BOOL_PARAMS:
        if value.lower() in ("true", "yes", "1", ""):
            return True
        if value.lower() in ("false", "no", "0"):
            return False
        sys.exit("%s: %s expects true or false, got '%s'" % (where, key, value))
    if key in INT_PARAMS:
        try:
            return int(value)
        except ValueError:
            sys.exit("%s: %s expects a number, got '%s'" % (where, key, value))
    return value


def _parse_directive(text, where):
    out = {}
    for key, value in _FILTER_DIRECTIVE.findall(text):
        out[key] = _coerce_filter(key, value, where)
    if not out and text.strip():
        sys.exit("%s: could not read '@filters %s'; expected key=value pairs"
                 % (where, text.strip()))
    return out


def _parse_plan_text(raw, path):
    """Groups as `[Section]` headers, one query per line.

        # comments and blank lines are ignored
        @filters year_min=2020 human=true      <- applies to what follows

        [Detection methods]
        deepfake video detection generalization :: generalization
        face forgery frequency artifacts

    A `::` gives the query a short folder name; without one the folder is the
    truncated query. A `@filters` line before any group applies to every group;
    inside a group it applies to that group and overrides the global one.
    """
    groups, current, global_filters = [], None, {}

    for lineno, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        where = "%s:%d" % (os.path.basename(path), lineno)

        if line.startswith("[") and line.endswith("]"):
            current = {"name": line[1:-1].strip() or "unnamed",
                       "filters": {}, "queries": []}
            groups.append(current)
            continue

        if line.lower().startswith("@filters"):
            parsed = _parse_directive(line[len("@filters"):], where)
            if current is None:
                global_filters.update(parsed)
            else:
                current["filters"].update(parsed)
            continue

        query, _, short = line.partition("::")
        query = query.strip()
        if not query:
            sys.exit("%s: a query is missing before '::'" % where)
        if current is None:
            current = {"name": "ungrouped", "filters": {}, "queries": []}
            groups.append(current)
        current["queries"].append(
            {"query": query, "name": short.strip() or None, "filters": {}})

    for group in groups:
        merged = dict(global_filters)
        merged.update(group["filters"])
        group["filters"] = merged
    return groups


def _parse_plan_json(data, path):
    """Same shape as the text format, for generated plans.

        {"filters": {...},
         "groups": [{"name": "...", "filters": {...},
                     "queries": ["text", {"query": "...", "name": "...",
                                          "filters": {...}}]}]}
    """
    where = os.path.basename(path)
    global_filters = {k: _coerce_filter(k, str(v), where)
                      for k, v in (data.get("filters") or {}).items()}
    groups = []
    for gi, raw_group in enumerate(data.get("groups") or [], 1):
        filters = dict(global_filters)
        filters.update({k: _coerce_filter(k, str(v), where)
                        for k, v in (raw_group.get("filters") or {}).items()})
        queries = []
        for raw_query in raw_group.get("queries") or []:
            if isinstance(raw_query, str):
                queries.append({"query": raw_query, "name": None, "filters": {}})
                continue
            queries.append({
                "query": raw_query["query"],
                "name": raw_query.get("name"),
                "filters": {k: _coerce_filter(k, str(v), where)
                            for k, v in (raw_query.get("filters") or {}).items()},
            })
        groups.append({"name": raw_group.get("name") or "group %d" % gi,
                       "filters": filters, "queries": queries})
    return groups


def parse_plan(path):
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    if path.lower().endswith(".json") or raw.lstrip().startswith("{"):
        return _parse_plan_json(json.loads(raw), path)
    return _parse_plan_text(raw, path)


# -- rendering ------------------------------------------------------------

def _authors(paper, limit=3):
    names = paper.get("authors") or []
    if not names:
        return "?"
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + " et al."


def _link(paper):
    if paper.get("url"):
        return paper["url"]
    if paper.get("doi"):
        return "https://doi.org/" + paper["doi"]
    return ""


def render_table(papers):
    if not papers:
        return "(no results)"
    lines = []
    for i, p in enumerate(papers, 1):
        flags = []
        if p.get("is_preprint"):
            flags.append("preprint")
        if p.get("study_type"):
            flags.append(p["study_type"])
        if p.get("sample_size"):
            flags.append("n=%s" % p["sample_size"])
        if p.get("found_by"):
            flags.append("hits=%d" % len(p["found_by"]))
        quartile = "Q%s" % p["sjr_best_quartile"] if p.get("sjr_best_quartile") else "-"
        meta = " | ".join(x for x in [
            p.get("journal_name") or "?",
            str(p.get("publish_year") or "?"),
            quartile,
            "%s cites" % p.get("citation_count", 0),
            ", ".join(flags),
        ] if x)
        lines.append("%3d. %s" % (i, p.get("title", "(untitled)")))
        lines.append("     " + _authors(p))
        lines.append("     " + meta)
        if p.get("takeaway"):
            lines.append("     > " + p["takeaway"])
        for chunk in (p.get("full_text_chunks") or []):
            section, _, passage = chunk.partition(" | ")
            lines.append("     [%s] %s" % (
                section.replace("Section: ", "", 1), passage[:220]))
        lines.append("     " + _link(p))
        lines.append("")
    return "\n".join(lines)


def render_markdown(papers):
    """Numbered citations, each with a live link -- no link means not citable."""
    out = []
    for i, p in enumerate(papers, 1):
        out.append("[%d] [%s](%s) (%s, %s, %s, %s citations)" % (
            i, p.get("title", "(untitled)"), _link(p), _authors(p),
            p.get("publish_year", "n.d."), p.get("journal_name", "?"),
            p.get("citation_count", 0)))
        if p.get("takeaway"):
            out.append("    - " + p["takeaway"])
        for chunk in (p.get("full_text_chunks") or []):
            section, _, passage = chunk.partition(" | ")
            out.append("    - *%s*: %s" % (
                section.replace("Section: ", "", 1), passage))
    return "\n".join(out)


CSV_FIELDS = [
    "title", "authors", "publish_year", "journal_name", "publisher_name",
    "sjr_best_quartile", "citation_count", "influential_citation_count",
    "study_type", "population_type", "sample_size", "study_duration_days",
    "is_preprint", "doi", "url", "takeaway", "found_by",
]


def render_csv(papers):
    buf = []

    class _Sink:
        def write(self, s):
            buf.append(s)

    writer = csv.DictWriter(_Sink(), fieldnames=CSV_FIELDS,
                            extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for p in papers:
        row = {k: p.get(k) for k in CSV_FIELDS}
        row["authors"] = "; ".join(p.get("authors") or [])
        row["found_by"] = "; ".join(p.get("found_by") or [])
        writer.writerow(row)
    return "".join(buf)


def render_audit(audit, totals=None):
    rows = ["| # | Query | Filters | Returned | Status |",
            "| - | ----- | ------- | -------- | ------ |"]
    for a in audit:
        filters = ", ".join("`%s=%s`" % (k, v) for k, v in a["filters"].items()) or "none"
        rows.append("| %s | %s | %s | %s | %s |" % (
            a["n"], a["query"], filters,
            a["returned"] if a["returned"] is not None else "-", a["status"]))

    billed = sum(a.get("calls_billed", 1) for a in audit)
    failed = sum(1 for a in audit if a["status"].startswith("failed"))
    capped = [a["n"] for a in audit if a.get("more_available")]

    out = ["## Audit log", ""] + rows + [""]
    out.append("- Searches sent: **%d** (%d failed after retry)" % (len(audit), failed))
    out.append("- Papers returned: **%d**"
               % sum(a["returned"] or 0 for a in audit))
    for k, v in (totals or {}).items():
        out.append("- %s: **%s**" % (k, v))
    out.append("- Calls billed (1 per 100 papers, min 1 per request): **~%d**" % billed)
    if capped:
        out.append(
            "- Searches where more results existed than were returned: %s. Only the "
            "returned papers are citable -- a thin section here may be a page-size "
            "ceiling rather than a real gap in the literature." % capped)
    return "\n".join(out)


# -- CLI ------------------------------------------------------------------

def add_filter_args(p):
    g = p.add_argument_group("filters (start unfiltered, then add one rung at a time)")
    g.add_argument("--year-min", type=int)
    g.add_argument("--year-max", type=int)
    g.add_argument("--month-min", type=int, help="requires --year-min")
    g.add_argument("--month-max", type=int, help="requires --year-max")
    g.add_argument("--study-types",
                   help="comma-separated; see --list-study-types")
    g.add_argument("--human", action="store_true", default=None)
    g.add_argument("--controlled", action="store_true", default=None)
    g.add_argument("--sample-size-min", type=int)
    g.add_argument("--sjr-min", type=int, choices=[1, 2, 3, 4])
    g.add_argument("--sjr-max", type=int, choices=[1, 2, 3, 4],
                   help="1 is the best quartile; --sjr-max 1 keeps only Q1 journals")
    g.add_argument("--citation-min", type=int)
    g.add_argument("--duration-min", type=int, help="study duration, days")
    g.add_argument("--duration-max", type=int, help="study duration, days")
    g.add_argument("--exclude-preprints", action="store_true", default=None)
    g.add_argument("--open-access", action="store_true", default=None,
                   help="pairs with --full-text: excerpts need an open-access PDF")
    g.add_argument("--publisher-name")
    g.add_argument("--domain",
                   help="academic field codes, not web domains: " + ", ".join(DOMAINS))
    g.add_argument("--country", help="ISO 3166-1 alpha-2 codes, e.g. us,gb")
    g.add_argument("--journal-name",
                   help="boosts matching journals; does not exclude others")
    g.add_argument("--clinical-guideline", action="store_true", default=None)
    g.add_argument("--medical-mode", action="store_true", default=None)

    o = p.add_argument_group("request options")
    o.add_argument("--page", type=int, default=None,
                   help="0-indexed; anything past page 0 needs a paid plan")
    o.add_argument("--page-size", type=int, default=None,
                   help="default 20, clamped to your plan cap (check the echoed value)")
    o.add_argument("--full-text", dest="include_full_text_chunks",
                   action="store_true", default=None,
                   help="query-relevant excerpts; paid plans, open-access papers. "
                        "The budget is 60 per request whatever the page size, so keep "
                        "--page-size near 20 when the passages are the point")
    o.add_argument("--semantic-score", dest="include_semantic_score",
                   action="store_true", default=None,
                   help="relevance score, top 50 results only")


def collect_filters(args):
    out = {}
    names = FILTER_PARAMS + ["page", "page_size", "include_full_text_chunks",
                             "include_semantic_score"]
    for name in names:
        value = getattr(args, name, None)
        if value is None:
            continue
        if name == "study_types" and isinstance(value, str):
            value = [s.strip() for s in value.split(",") if s.strip()]
            unknown = [s for s in value if s not in STUDY_TYPES]
            if unknown:
                sys.exit("unknown study type(s) %s\nvalid values: %s"
                         % (unknown, ", ".join(STUDY_TYPES)))
        out[name] = value
    return out


def emit(papers, fmt, out_path):
    renderers = {
        "table": render_table,
        "md": render_markdown,
        "csv": render_csv,
        "json": lambda ps: json.dumps(ps, indent=2, ensure_ascii=False),
    }
    text = renderers[fmt](papers)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("wrote %d papers to %s" % (len(papers), out_path), file=sys.stderr)
    else:
        print(text)


def cmd_search(client, args):
    body = client.search(args.query, **collect_filters(args))
    papers = body.get("results", [])
    emit(papers, args.format, args.out)
    more = "" if body.get("is_end", True) else "  (more pages available)"
    print("\n%d returned | page %s | page_size %s%s" % (
        len(papers), body.get("page", 0), body.get("page_size"), more),
        file=sys.stderr)


def cmd_sweep(client, args):
    papers = client.sweep(args.query, max_results=args.max, **collect_filters(args))
    emit(papers, args.format, args.out)
    print("\n" + render_audit(client.audit), file=sys.stderr)


def cmd_fanout(client, args):
    queries = _read_queries(args)
    result = client.fanout(queries, per_query_pages=args.pages,
                           **collect_filters(args))
    emit(result["papers"], args.format, args.out)
    audit_md = render_audit(result["audit"], {
        "Unique papers after dedup": result["unique_papers"],
        "Results before dedup": result["results_pre_merge"],
    })
    if args.audit:
        with open(args.audit, "w", encoding="utf-8") as fh:
            fh.write(audit_md + "\n")
        print("wrote audit log to " + args.audit, file=sys.stderr)
    print("\n" + audit_md, file=sys.stderr)


def _filters_md(filters):
    return ", ".join("`%s=%s`" % (k, v) for k, v in sorted(filters.items())) or "none"


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _dedup(papers):
    """Merge a group's pages into one corpus, keeping which queries found what."""
    merged = {}
    for paper, query in papers:
        key = (paper.get("doi") or paper.get("url") or paper.get("title", "")).lower()
        if key in merged:
            if query not in merged[key]["found_by"]:
                merged[key]["found_by"].append(query)
        else:
            record = dict(paper)
            record["found_by"] = [query]
            merged[key] = record
    return sorted(merged.values(),
                  key=lambda p: (-len(p["found_by"]), -(p.get("citation_count") or 0)))


def cmd_batch(client, args):
    """Run a grouped plan of queries into a browsable folder tree.

    One folder per query keeps each result set next to the exact query and filters
    that produced it, which is what makes a review reproducible weeks later -- the
    index and audit files alone can't tell you that, because they summarise.
    """
    groups = parse_plan(args.plan)
    if not groups or not any(g["queries"] for g in groups):
        sys.exit("no queries found in " + args.plan)

    base_filters = collect_filters(args)
    formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    unknown = [f for f in formats if f not in ("md", "json", "csv", "table")]
    if unknown:
        sys.exit("unknown --formats %s; choose from md, json, csv, table" % unknown)

    # Lay the whole tree out before spending a call, so --dry-run can show it and a
    # path that is too long for Windows surfaces now rather than 20 searches in.
    jobs, long_paths = [], []
    for gi, group in enumerate(groups, 1):
        gdir = os.path.join(args.out, "%d.%s" % (gi, safe_name(group["name"],
                                                              args.name_length)))
        for qi, query in enumerate(group["queries"], 1):
            label = query["name"] or query["query"]
            leaf = os.path.join(gdir, "Q%d.%d-%s" % (
                gi, qi, safe_name(label, args.name_length)))
            filters = dict(base_filters)
            filters.update(group["filters"])
            filters.update(query["filters"])
            jobs.append({"gi": gi, "qi": qi, "group": group, "gdir": gdir,
                         "leaf": leaf, "query": query["query"], "filters": filters})
            if len(os.path.abspath(leaf)) > MAX_PATH_HINT:
                long_paths.append(leaf)

    if long_paths:
        print("warning: %d path(s) exceed %d characters and may fail on Windows "
              "without long paths enabled. Shorten --name-length or --out, or give "
              "queries short names with `::`." % (len(long_paths), MAX_PATH_HINT),
              file=sys.stderr)

    minutes = len(jobs) * client.min_interval / 60.0
    if args.dry_run:
        print("%s/" % args.out)
        for gi, group in enumerate(groups, 1):
            gjobs = [j for j in jobs if j["gi"] == gi]
            print("  %s/   (%d quer%s, filters: %s)" % (
                os.path.basename(gjobs[0]["gdir"]), len(gjobs),
                "y" if len(gjobs) == 1 else "ies",
                _filters_md(group["filters"]) if group["filters"] else "none"))
            for job in gjobs:
                print("    %s/" % os.path.basename(job["leaf"]))
                print("      %s" % job["query"])
        print("\n%d queries in %d groups | ~%d billed calls minimum | "
              "~%.1f min at %.1fs spacing" % (
                  len(jobs), len(groups), len(jobs), minutes, client.min_interval))
        print("nothing was sent (--dry-run)")
        return

    client._log("%d queries in %d groups, ~%.1f min at %.1fs spacing -> %s"
                % (len(jobs), len(groups), minutes, client.min_interval, args.out))

    consecutive_failures = 0
    for job in jobs:
        tag = "Q%d.%d" % (job["gi"], job["qi"])
        if args.resume and os.path.exists(os.path.join(job["leaf"], "meta.json")):
            # Reload what the earlier run stored, so the group corpus and the index
            # files this run rewrites still describe the whole plan. Skipping the
            # call but dropping the records would quietly report the resumed
            # queries as having found nothing.
            job["skipped"] = True
            stored = os.path.join(job["leaf"], "results.json")
            try:
                with open(stored, encoding="utf-8") as fh:
                    job["papers"] = json.load(fh)
                with open(os.path.join(job["leaf"], "meta.json"), encoding="utf-8") as fh:
                    job["more"] = json.load(fh).get("more_available", False)
                client._log("[%s] skipped, reusing %d stored results (--resume)"
                            % (tag, len(job["papers"])))
            except (OSError, ValueError):
                job["papers"] = []
                job["more"] = False
                job["incomplete"] = True
                client._log("[%s] skipped, but results.json is missing or unreadable "
                            "-- this group's corpus will undercount. Rerun without "
                            "--resume, or with --formats including json." % tag)
            continue

        client._log("[%s] %s" % (tag, job["query"]))
        try:
            if args.pages > 1:
                page_size = job["filters"].get("page_size") or 20
                papers = client.sweep(job["query"], max_results=args.pages * page_size,
                                      **job["filters"])
                body = {"page_size": page_size, "is_end": True}
            else:
                body = client.search(job["query"], **job["filters"])
                papers = body.get("results", [])
            consecutive_failures = 0
        except ConsensusError as exc:
            consecutive_failures += 1
            client._log("  ! " + str(exc))
            job["error"] = str(exc)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise ConsensusError(
                    "%d consecutive failures -- stopping with %d of %d queries done. "
                    "Rerun with --resume to pick up where this left off."
                    % (MAX_CONSECUTIVE_FAILURES,
                       sum(1 for j in jobs if "papers" in j), len(jobs))) from None
            continue

        job["papers"] = papers
        job["more"] = not body.get("is_end", True)
        os.makedirs(job["leaf"], exist_ok=True)

        header = "\n".join([
            "# %s" % job["query"],
            "",
            "- Group: **%d. %s**" % (job["gi"], job["group"]["name"]),
            "- Filters as sent: %s" % _filters_md(job["filters"]),
            "- Retrieved: %s" % _now(),
            "- Papers returned: **%d**%s" % (
                len(papers),
                "  (page filled -- more exist, and only these are citable)"
                if job["more"] else ""),
            "", "---", "",
        ])
        if "md" in formats:
            _write(os.path.join(job["leaf"], "results.md"),
                   header + (render_markdown(papers) or "_no results_") + "\n")
        if "json" in formats:
            _write(os.path.join(job["leaf"], "results.json"),
                   json.dumps(papers, indent=2, ensure_ascii=False) + "\n")
        if "csv" in formats:
            _write(os.path.join(job["leaf"], "results.csv"), render_csv(papers))
        if "table" in formats:
            _write(os.path.join(job["leaf"], "results.txt"), render_table(papers))
        _write(os.path.join(job["leaf"], "meta.json"), json.dumps({
            "query": job["query"],
            "group": {"number": job["gi"], "name": job["group"]["name"]},
            "query_number": job["qi"],
            "filters_as_sent": job["filters"],
            "retrieved_at": _now(),
            "returned": len(papers),
            "more_available": job["more"],
        }, indent=2, ensure_ascii=False, default=str) + "\n")
        client._log("  -> %d results%s" % (len(papers), " (more exist)" if job["more"] else ""))

    # Per-group index, with the within-group dedup a fan-out would have given you.
    group_rows = []
    for gi, group in enumerate(groups, 1):
        gjobs = [j for j in jobs if j["gi"] == gi]
        if not gjobs:
            continue
        gdir = gjobs[0]["gdir"]
        os.makedirs(gdir, exist_ok=True)
        flat = [(p, j["query"]) for j in gjobs for p in j.get("papers") or []]
        corpus = _dedup(flat)
        _write(os.path.join(gdir, "corpus.json"),
               json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")

        lines = ["# %d. %s" % (gi, group["name"]), "",
                 "Group filters: %s" % _filters_md(group["filters"]), "",
                 "| Q | Query | Returned | Folder |",
                 "| - | ----- | -------- | ------ |"]
        for job in gjobs:
            if job.get("error"):
                state = "failed"
            elif job.get("incomplete"):
                state = "reused, records unreadable"
            else:
                state = str(len(job.get("papers") or []))
                if job.get("skipped"):
                    state += " (reused)"
            lines += ["| Q%d.%d | %s | %s | `%s` |" % (
                gi, job["qi"], job["query"], state, os.path.basename(job["leaf"]))]
        lines += ["",
                  "- Results before dedup: **%d**" % len(flat),
                  "- Unique papers in this group: **%d**" % len(corpus),
                  "- Full deduplicated corpus: `corpus.json` "
                  "(each paper carries `found_by`)"]
        repeats = [p for p in corpus if len(p["found_by"]) > 1]
        if repeats:
            lines += ["",
                      "Papers found by more than one query in this group -- central to "
                      "the group rather than to one phrasing:", ""]
            for p in repeats[:10]:
                lines.append("- (%d hits) [%s](%s)" % (
                    len(p["found_by"]), p.get("title", "(untitled)"), _link(p)))
        _write(os.path.join(gdir, "index.md"), "\n".join(lines) + "\n")
        group_rows.append((gi, group, gjobs, len(flat), len(corpus),
                           os.path.basename(gdir)))

    done = [j for j in jobs if "papers" in j and not j.get("skipped")]
    root_lines = ["# Search plan results", "",
                  "- Plan: `%s`" % args.plan,
                  "- Run: %s" % _now(),
                  "- Base filters from the command line: %s" % _filters_md(base_filters),
                  "- Pacing: one call every %.1fs" % client.min_interval, "",
                  "| # | Group | Queries | Returned | Unique | Folder |",
                  "| - | ----- | ------- | -------- | ------ | ------ |"]
    for gi, group, gjobs, flat_n, uniq, folder in group_rows:
        root_lines.append("| %d | %s | %d | %d | %d | `%s` |" % (
            gi, group["name"], len(gjobs), flat_n, uniq, folder))
    root_lines += ["",
                   "- Queries in plan: **%d**" % len(jobs),
                   "- Queries searched this run: **%d**" % len(done),
                   "- Reused from a previous run (--resume): **%d**"
                   % sum(1 for j in jobs if j.get("skipped")),
                   "- Failed after retry: **%d**" % sum(1 for j in jobs if j.get("error")),
                   "- Papers returned across all groups: **%d**"
                   % sum(len(j.get("papers") or []) for j in jobs),
                   "",
                   "Dedup is per group, in each group's `corpus.json`. A paper can "
                   "legitimately appear in several groups.", ""]
    os.makedirs(args.out, exist_ok=True)
    _write(os.path.join(args.out, "index.md"), "\n".join(root_lines))
    _write(os.path.join(args.out, "audit.md"), render_audit(client.audit, {
        "Queries in plan": len(jobs),
        "Groups": len(group_rows),
    }) + "\n")

    print("\n".join(root_lines[-8:]))
    print("\nwritten to %s (index.md, audit.md, one folder per group and query)"
          % args.out)


def cmd_ladder(client, args):
    """Gauge how each filter narrows the pool, one rung at a time.

    Most disappointing result sets are an unfiltered query, and most empty ones are
    four filters applied at once; the ladder tells you which rung was binding.

    The API returns a page, not a total, so a rung is only informative while the page
    is bigger than the result set. 100 is the useful default: it resolves counts up
    to 100 and still bills as a single call per rung.
    """
    base = collect_filters(args)
    base.pop("page", None)
    page_size = base.pop("page_size", None) or 100

    rungs = [("unfiltered", {})]
    for name in FILTER_PARAMS:
        if name in base:
            rungs.append(("+ %s=%s" % (name, base[name]), {name: base[name]}))

    cumulative = {}
    report = []
    for label, step in rungs:
        cumulative.update(step)
        body = client.search(args.query, page_size=page_size, **cumulative)
        n = len(body.get("results", []))
        more = "+" if not body.get("is_end", True) else ""
        report.append((label, "%d%s" % (n, more)))
        client._log("  %-42s %d%s" % (label, n, more))

    print("| Rung | Returned |")
    print("| ---- | -------- |")
    for label, n in report:
        print("| %s | %s |" % (label, n))
    print("\n`+` means the page filled up and more results exist, so the true count "
          "is only visible once a rung drops below --page-size (default 100). A rung "
          "that drops you to near-zero is the binding constraint -- relax that one "
          "first, and say which one it was rather than reporting 'little evidence "
          "exists'.")


def cmd_diff(client, args):
    """Report only papers not seen in the stored state -- the monitoring primitive."""
    seen = set()
    if os.path.exists(args.state):
        with open(args.state, encoding="utf-8") as fh:
            seen = set(json.load(fh).get("seen", []))

    result = client.fanout(_read_queries(args), **collect_filters(args))
    fresh = [p for p in result["papers"]
             if (p.get("doi") or p.get("url", "")).lower() not in seen]
    emit(fresh, args.format, args.out)
    print("\n%d new of %d unique (%d previously seen)" % (
        len(fresh), result["unique_papers"], len(seen)), file=sys.stderr)

    if not args.dry_run:
        seen |= {(p.get("doi") or p.get("url", "")).lower() for p in result["papers"]}
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump({"updated": _now(), "seen": sorted(seen)}, fh, indent=1)
        print("state updated: " + args.state, file=sys.stderr)


def cmd_plan(client, args):
    """Detect the plan's page_size cap for one billed call.

    A filter that can match nothing (year_min far in the future) returns zero papers,
    so the request bills as a single call, while the echoed page_size still reports
    the ceiling your key is entitled to.
    """
    body = client.search("plan capability probe", page_size=1000, year_min=2300)
    cap = body.get("page_size")
    plans = {20: "Free", 300: "Pro or Teams", 750: "Deep", 1000: "Enterprise"}
    print("page_size cap : %s  (%s)" % (cap, plans.get(cap, "unrecognised")))
    print("result ceiling: %d per query, on every plan" % RESULT_CEILING)
    print("max pages     : %s at that page size"
          % (RESULT_CEILING // cap if cap else "?"))
    print("pagination past page 0 and full-text excerpts require a paid plan")


def _read_queries(args):
    queries = list(args.query or [])
    if args.queries_file:
        with open(args.queries_file, encoding="utf-8") as fh:
            queries += [ln.strip() for ln in fh
                        if ln.strip() and not ln.lstrip().startswith("#")]
    if not queries:
        sys.exit("give at least one --query or a --queries-file")
    return queries


EPILOG = """examples:
  consensus.py plan
  consensus.py search "what dose of creatine improves working memory" --full-text
  consensus.py ladder "resistance training older adults" --study-types rct --human
  consensus.py sweep "GLP-1 agonists weight loss" --page-size 100 --max 400 -o corpus.json
  consensus.py fanout -f queries.txt --human --year-min 2020 -o corpus.json --audit audit.md
  consensus.py batch -f plan.txt -o searches --dry-run
  consensus.py batch -f plan.txt -o searches --page-size 50 --full-text
  consensus.py diff -f topics.txt --state seen.json --year-min 2026 --month-min 8
"""

PLAN_FORMAT = """plan file format (text):

  # comments and blank lines are ignored
  @filters year_min=2020 human=true          <- applies to every group below

  [Detection methods]
  deepfake video detection generalization across datasets
  face forgery detection frequency artifacts :: frequency artifacts

  [Calibration]
  @filters study_types=rct,meta-analysis     <- this group only, overrides global
  uncertainty calibration under domain shift

`::` gives a query a short folder name; without one the folder is the truncated
query. A JSON plan works too: {"filters": {...}, "groups": [{"name": ...,
"filters": {...}, "queries": ["text", {"query": ..., "name": ..., "filters": {...}}]}]}

resulting tree:

  searches/
    index.md                 groups, counts, totals
    audit.md                 every call, with filters as sent
    1.Detection methods/
      index.md               this group's queries and repeat-hit papers
      corpus.json            deduplicated across the group, each paper has found_by
      Q1.1-deepfake video detection generalization across/
        results.md  results.json  meta.json
      Q1.2-frequency artifacts/
        results.md  results.json  meta.json
    2.Calibration/
      ...
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog="consensus.py",
        description="Search 220M peer-reviewed papers over the Consensus REST API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    p.add_argument("--api-key", default=os.environ.get("CONSENSUS_API_KEY", ""))
    p.add_argument("--min-interval", type=float, default=MIN_INTERVAL,
                   help="seconds between calls (default 3.0; the API limit is 1/sec, "
                        "so this leaves headroom for other clients on the same account)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="suppress progress output on stderr")
    p.add_argument("--list-study-types", action="store_true",
                   help="print the valid --study-types values and exit")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("search", help="one search")
    sp.add_argument("query")
    sp.add_argument("--format", choices=["table", "md", "json", "csv"], default="table")
    sp.add_argument("-o", "--out")
    add_filter_args(sp)
    sp.set_defaults(func=cmd_search)

    sw = sub.add_parser("sweep", help="paginate one query to exhaustion")
    sw.add_argument("query")
    sw.add_argument("--max", type=int, help="stop after this many papers")
    sw.add_argument("--format", choices=["table", "md", "json", "csv"], default="json")
    sw.add_argument("-o", "--out")
    add_filter_args(sw)
    sw.set_defaults(func=cmd_sweep)

    fo = sub.add_parser("fanout", help="many queries into one deduplicated corpus")
    fo.add_argument("--query", action="append", help="repeatable")
    fo.add_argument("-f", "--queries-file",
                    help="one query per line; blank lines and # comments ignored")
    fo.add_argument("--pages", type=int, default=1, help="pages per query (default 1)")
    fo.add_argument("--format", choices=["table", "md", "json", "csv"], default="json")
    fo.add_argument("-o", "--out")
    fo.add_argument("--audit", help="write the audit log here as markdown")
    add_filter_args(fo)
    fo.set_defaults(func=cmd_fanout)

    ba = sub.add_parser(
        "batch", help="run a grouped query plan into a folder tree",
        description="Run a plan of queries, grouped by topic, into one folder per "
                    "query under --out, paced at the default interval.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=PLAN_FORMAT)
    ba.add_argument("-f", "--plan", required=True,
                    help="plan file: [Group] sections with one query per line, or JSON")
    ba.add_argument("-o", "--out", required=True,
                    help="root folder; groups are created inside it")
    ba.add_argument("--formats", default="md,json",
                    help="per-query result files: md, json, csv, table (default md,json)")
    ba.add_argument("--pages", type=int, default=1, help="pages per query (default 1)")
    ba.add_argument("--name-length", type=int, default=50,
                    help="truncate folder names to this many characters (default 50)")
    ba.add_argument("--resume", action="store_true",
                    help="skip queries whose folder already holds results")
    ba.add_argument("--dry-run", action="store_true",
                    help="print the folder tree and cost estimate without searching")
    add_filter_args(ba)
    ba.set_defaults(func=cmd_batch)

    la = sub.add_parser("ladder", help="show how each filter narrows the pool")
    la.add_argument("query")
    add_filter_args(la)
    la.set_defaults(func=cmd_ladder)

    di = sub.add_parser("diff", help="report only papers not seen in a previous run")
    di.add_argument("--query", action="append")
    di.add_argument("-f", "--queries-file")
    di.add_argument("--state", required=True, help="JSON file of already-seen DOIs")
    di.add_argument("--dry-run", action="store_true",
                    help="report new papers without updating the state file")
    di.add_argument("--format", choices=["table", "md", "json", "csv"], default="table")
    di.add_argument("-o", "--out")
    add_filter_args(di)
    di.set_defaults(func=cmd_diff)

    pl = sub.add_parser("plan", help="detect this key's plan limits (1 billed call)")
    pl.set_defaults(func=cmd_plan)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_study_types:
        print("\n".join(STUDY_TYPES))
        return 0
    if not args.command:
        parser.print_help()
        return 2
    try:
        client = Client(args.api_key, min_interval=args.min_interval,
                        verbose=not args.quiet)
        args.func(client, args)
    except ConsensusError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
