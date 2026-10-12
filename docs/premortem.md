# Pre-mortem, and how others solve this

Written 12 October 2026. The exercise: assume it is late 2027 and this archive is no longer
trusted or no longer used. Work backwards to why.

## How it most plausibly fails

### 1. It goes stale without saying so

The likeliest death, and it is quiet. Three sources need a person: NSW needs a click,
Victoria is a snapshot, the Auditor-General index needs a run from a machine the site will
answer. Each will be refreshed enthusiastically for a month and then not.

The site already warns on one of them — NSW says so past 45 days — and labels Victoria a
snapshot on every row. **The Commonwealth corpus has no such warning at all.** If
`refresh.yml` silently stops, nothing on the page says the archive stopped moving. A reader
in 2027 would see 2026 data presented exactly as today's.

*Mitigation, not yet built:* a banner derived from the newest contract in the corpus, not
from a hand-set date. Cheap, and it removes the worst failure.

### 2. A scheduled run breaks and the only signal is an email

Nine workflows write to this archive. A failure sends an email to one person. The pattern
this month was three sources that worked from a desk and failed in CI, each found only
because someone looked. There is no aggregate view of "did everything that should have run,
run".

### 3. One wrong number destroys the premise

This archive's distinctive claim is that it tells you what it cannot tell you. That is worth
a great deal and is fragile in a specific way: it only takes one confident falsehood for the
calibration to look like decoration.

It has already happened twice, briefly. **$914 million was attributed to Amazon Web Services
on the strength of three contracts that said so**, published and live, until someone asked
how. The consultancy filter answered confidently and wrongly for four years. Both were
caught by a person noticing an implausible number, not by anything in the system.

*This is the central risk and the system has no defence against it.*

### 4. There are no tests

6,414 lines of Python, 1.3 million contracts, a merge across six sources with precedence
rules, dictionary encoding, an inverted index that must match a tokenizer in another
language, and **not one automated check**. Every defect this month — the concatenated
values, the AWS attribution, 28,423 CSIRO rows dropped in silence, two budget years reading
as empty, the agency join that gave the Auditor-General 338 audits of itself — was caught by
eyeballing output.

That worked because someone was looking hard. It is not a property of the system.

### 5. The repository outgrows itself

`.git` is 433 MB and grows with every data commit, because git keeps every version of every
shard forever. Clones get slower, Actions checkouts get slower. Nothing breaks; it just
gets heavier until someone has to do surgery on history.

### 6. One maintainer

No second person could pick this up quickly. The knowledge that matters — why Victoria is a
snapshot, why nothing is presumed about IBM, why the Senate Order floor is $100,000 — is in
commit messages and two documents. That is better than nothing and worse than a handover.

## How other people build this

| | Approach | What it buys | What it costs |
|---|---|---|---|
| USAspending, OpenSpending | Postgres behind an API | arbitrary queries, server-side aggregation | servers, ops, money |
| Datasette | SQLite published as a queryable site | SQL for readers, tiny ops | a host, or WASM limits |
| Intermedium | private database plus analysts | context and judgement no file has | subscription, unverifiable |
| **This archive** | **static JSON shards, computed in the browser** | **$0, auditable in git, forkable, no ops** | **no query engine** |

The static choice has been right. It costs nothing, every number is traceable to a commit,
and anyone can fork the whole thing. But it has one consequence that shapes everything else:
**there is no query engine, so every question must be anticipated and precomputed.**

That is why the agency roll-up, the analysis series and the supplier index all exist as
separate precomputed files, and why the analysis view cannot be filtered — the answer to any
question nobody precomputed is "download the archive".

### The alternative worth knowing about

**DuckDB-WASM reading Parquet over HTTP range requests** solves exactly this problem and did
not exist in usable form when this archive's shape was chosen. The data stays static files
on the same free hosting; the browser runs real SQL against them, fetching only the row
groups a query touches. SQLite with `sql.js-httpvfs` does the same more cheaply.

What that would replace: the month-shard loader, the dictionary encoding, the hand-rolled
inverted index and the tokenizer that must stay byte-identical between `build.py` and
`index.html` — several hundred lines of bespoke code whose only job is to avoid needing a
query engine. What it would add: arbitrary aggregation, a filterable analysis view, and a
reader who can ask their own question.

It is not a small change and the current approach is not broken. But if this archive grows
another three sources, the precompute-everything strategy is what will bend first.

## What I would do, in order

1. **A staleness banner driven by the data.** Removes the quietest failure. An hour.
2. **An assertion layer** — `check.py`, run after every build, failing loudly on things that
   must be true: no value above a ceiling without the contradiction flag, flag coverage not
   collapsing year on year, no source losing more than a few per cent of its rows between
   runs, the tokenizer agreeing with itself. Every defect this month would have been caught
   by three or four such assertions. **This is the highest-value thing missing.**
3. **A workflow health page** — when each source last ran and last changed, read from the
   stores themselves.
4. **Evaluate DuckDB-WASM** before adding more precomputed files, not after.

Nothing above needs money. The first two need an afternoon and would have caught every
mistake made this month.
