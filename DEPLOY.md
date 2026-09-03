# Deploying Legal Tender

One repository does everything: holds the code, runs the ingests on GitHub's machines,
stores the data, and serves the site. Nothing runs on your computer, and there is no
server, database or hosting bill.

```
  GitHub Actions                    data/ in the repo                GitHub Pages
  ──────────────                    ─────────────────                ────────────
  refresh.py    weekly          →   contracts-YYYY-MM.json       →   index.html
  atm.py        3x daily            atm/atm-YYYY.json                fetches only the
  senate.py     weekly              senate/contracts.json            shards a query
  so13.py       monthly             so13/*.json                      actually needs
  historical.py monthly             historical/contracts-YYYY.json
```

## Setup

1. **Pages.** Settings → Pages → Source: *Deploy from a branch* → `main` / root. The
   site is live at `https://USER.github.io/REPO/` a minute later.
2. **Actions write permission.** Settings → Actions → General → Workflow permissions →
   *Read and write*. Without it every workflow builds correctly and then fails to push.
3. Nothing else. The page looks for `data/` beside itself — same origin, no CORS, no
   URL to configure.

The repo must be public for Pages to serve it on a free account. That is correct for a
transparency archive and wrong for anything else.

## Verify the mapper before trusting the data

```bash
pip install requests openpyxl
python refresh.py --inspect
```

This samples live releases and prints every field path present, with counts. Reconcile
it against `to_row()`. This is not a formality: the mapper was originally written from
documentation rather than observed responses, and four fields were wrong — the buyer
column showed the supplier, and the title showed an internal purchase-order reference.

## The data layout

Contracts are sharded by month, historical extracts and notices by year. A date filter
fetches only the shards it needs instead of pulling 163 MB to answer a question about
one quarter. Each shard's entry in `index.json` carries a **content digest**, which the
front end stamps into the URL: a changed shard is a different URL, an unchanged one
still serves from cache. That digest replaced a count-and-byte-size stamp, which could
not tell two versions apart when a value was corrected to another figure of the same
length.

Month shards are immutable — what AusTender said when the archive first saw each
contract. `updates.json` carries every row that has changed since and is overlaid by
ocid, so the table shows current truth without rewriting history.

## Backfill

The archive covers five years. A full build is long enough to outlast a runner, so it
is chunked and resumable:

```bash
python refresh.py --backfill-from 2021-01-01 --backfill-to 2026-01-01 --chunk-days 180
python refresh.py --resume            # continues from the checkpoint in index.json
```

`backfill-resume.yml` runs that every 30 minutes until the checkpoint is complete. Each
chunk commits on its own, so an interrupted run loses one chunk rather than everything.
Note that Actions uses the workflow file from the commit that *triggered* the run: a fix
pushed after a run starts does not apply to it.

## Concurrency

Every workflow that writes `data/` refuses to start while another writer is in flight,
and refuses to commit a file containing `<<<<<<<`. Both guards exist because two
unguarded writers once overlapped, wrote conflict markers into five files on `main`, and
took the site down. `refresh.py` is the priority writer; the others stand down for it.

## Size

About 634,000 distinct contracts, 163 MB on disk and ~44 MB gzipped across the shards. That is well
inside Pages' soft limits, but the front end still does not load it all by default: it
opens on the most recent three months and fetches the rest in the background only where
the browser reports a connection and a device that can take it. Everyone else gets a
button.

## What this deliberately does not do

- **No live sync.** Agencies have 42 days to publish a contract notice, so a weekly
  refresh loses nothing real.
- **No email.** Alerts are published as an Atom feed; a static host cannot send mail.
- **No amounts paid.** Every value here is committed at award. Reconciling against what
  was actually paid is the next layer and the most valuable one.
- **No subcontractors.** Only obtainable by written request to the agency contact named
  on each notice.

## Reading the flags

`late_publish`, `value_growth`, `threshold_hugging`, `backdated`, `limited_tender`,
`long_term`, `agent_or_trustee`, `platform_or_reseller`, `source_disagreement`.

Every one has innocent explanations — emergency procurement is legitimately
limited-tender, long IT contracts are normal, thresholds change, and two arms of the
Commonwealth can report different figures for defensible reasons. Treat them as a queue
for review, not as findings. Thresholds are set at the top of `refresh.py`; confirm the
current figures before relying on them.
