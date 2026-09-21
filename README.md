# consensus-api

A [Claude Code](https://claude.com/claude-code) skill for the
[Consensus](https://consensus.app) REST API — search 220 million peer-reviewed papers from
an agent, a script, or the command line.

Built against the API rather than the Consensus MCP connector, which means pagination,
`clinical_guideline` and semantic scores, unattended scheduled runs, and an audit log that
makes coverage reportable.

## Install

```bash
git clone git@github.com:Capta1n-n9m0/consensus-api-skill.git ~/.claude/skills/consensus-api
```

Create a key at [consensus.app/api-mcp](https://consensus.app/api-mcp/) → **Keys and
clients** → **New key** (shown once), then put it in the environment:

```bash
export CONSENSUS_API_KEY=ak_...            # bash
setx CONSENSUS_API_KEY "ak_..."            # Windows, persistent
```

Or set it for Claude Code alone in `~/.claude/settings.json`:

```json
{ "env": { "CONSENSUS_API_KEY": "ak_..." } }
```

Python 3.9+, standard library only — no dependencies to install.

## Use

Claude picks the skill up automatically when a task involves research papers, citations,
literature reviews or published evidence. The CLI also stands on its own:

```bash
python consensus.py plan                      # what this key is allowed to do
python consensus.py search "does creatine improve working memory" --full-text
python consensus.py ladder "resistance training older adults" --study-types rct --human
python consensus.py sweep "GLP-1 agonists weight loss" --page-size 300 -o corpus.json
python consensus.py fanout -f queries.txt --year-min 2020 -o corpus.json --audit audit.md
python consensus.py diff -f topics.txt --state seen.json --year-min 2026 --month-min 8
```

| Command  | Purpose                                                        |
| -------- | -------------------------------------------------------------- |
| `plan`   | Detect the key's page-size cap for one billed call             |
| `search` | One query → table, markdown citations, JSON or CSV             |
| `ladder` | Re-run adding one filter at a time, with counts per rung       |
| `sweep`  | Paginate one query to exhaustion                               |
| `fanout` | Many queries, paced and merged, deduplicated on DOI            |
| `diff`   | Report only papers absent from a stored state file             |

All commands share the same filters (`--year-min`, `--study-types`, `--human`, `--sjr-max`,
`--open-access`, `--domain`, `--medical-mode`, …) and the same `--format` choices.
`--help` on any command lists them.

## What the client handles for you

- **Pacing** — one call every 3 seconds by default, well back from the 1 req/sec limit,
  since the quota is per account and shared with MCP. `--min-interval` overrides it.
- **The two 429s** — "too fast" is retried; "monthly allowance used up" is not, because
  retrying that one only wastes time.
- **Retries** — 3s wait, one retry, then a loud failure. Three consecutive failures abort
  the run rather than silently producing thin coverage.
- **Ceilings** — stops at the 1,000-result per-query limit and reports when a page filled
  up, since only returned papers are citable.
- **Audit logs** — searches sent, papers returned, calls billed, and which searches were
  capped, as a markdown table.

## Layout

```
SKILL.md                      instructions Claude loads
consensus.py                  the CLI (stdlib only)
references/api-reference.md   every parameter, response field, error and plan limit
references/recipes.md         worked workflows: reviews, screening, extraction, monitoring
examples/queries.txt          a fan-out query file to copy
```

## License

MIT. Not affiliated with Consensus; see their
[documentation](https://docs.consensus.app) and
[terms](https://consensus.app) for the service itself.
