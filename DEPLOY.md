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
   site is live at `https://USER.github.io/REPO/` a minute later. Any static host works
   — the page addresses its data relatively. The only absolute address is the one the
   Atom feed publishes for itself; set the `PROCLENS_SITE` repository variable to change
   it. Note that GitHub Pages is public even when served from a private repo, so if the
   site itself must be access-controlled it needs a host that can sit behind auth.
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

Each ingest script keeps its own store — `data/contracts-*.json` for the API,
`data/export`, `data/historical`, `data/senate`, `data/so13` — and `build.py` merges them
into `data/corpus/`, which is all the page reads:

- `list/YYYY-MM.json` — what the table, filters, totals and CSV need
- `detail/YYYY-MM.json` — everything else, fetched when a contract is opened
- `terms/*.json` — word to months, so a search fetches only months that can match
- `updates.json` — records changed since their month file was written
- `index.json` — months with content digests, totals, agencies, and the build report

Month files are written once; later changes go to `updates.json` so a week of amendments
across every year does not rewrite a hundred files in git. `build.py --compact` folds
them back, and happens automatically past 40,000 changed records. Every file's content
digest is stamped into its URL, so a changed file is a new URL and an unchanged one
serves from cache.

The source stores are excluded from the published site by `_config.yml`. Publishing them
put the site at 975 MB, against GitHub Pages' 1 GB limit, while serving nothing a reader
could reach.

## Backfill

A full rebuild from the API is chunked and resumable, and fetches several date windows at
once — the API has answered 18 concurrent requests without refusing one:

```bash
python refresh.py --backfill-from 2013-12-31 --backfill-to 2026-09-15 --chunk-days 365 --workers 16
python refresh.py --resume --workers 16      # continues from the checkpoint in index.json
```

Start a day early: the API's date windows are UTC, and a contract published on the morning
of 1 January in Canberra falls in 31 December's window. Sixteen workers rebuild 2014 to
2026 in about two hours.

## Concurrency

Every workflow that writes `data/` refuses to start while another writer is in flight,
and refuses to commit a file containing `<<<<<<<`. Both guards exist because two
unguarded writers once overlapped, wrote conflict markers into five files on `main`, and
took the site down. `refresh.py` is the priority writer; the others stand down for it.

## Size

About 1.3 million contracts. A full load is about 45 MB gzipped, but nothing requires it: searches fetch only matching months. That is well
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
