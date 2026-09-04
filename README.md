# Legal Tender

A public archive of Australian Commonwealth contract notices, with the sources that
disagree with them kept alongside rather than reconciled away.

Live at **https://simontemby.github.io/proclens-data/**

Nothing runs on a server. GitHub Actions build the data, GitHub Pages serves it, and
the whole front end is one `index.html` that fetches static JSON shards.

## What is in here

| Source | Script | What only it has |
|---|---|---|
| AusTender OCDS API | `refresh.py` | The current state of every contract notice, weekly |
| Senate Order snapshots | `senate.py` | Value at each reporting period, and confidentiality provisions |
| Senate Order 13 listings | `so13.py` | Entities that do not report to AusTender at all — the NDIA among them |
| data.gov.au extracts, 1999–2020 | `historical.py` | Standing-offer ids, approach-to-market ids, panel flags, amendment lineage |
| AusTender current-notice feed | `atm.py` | Approaches to market — what is about to be bought |

The first four are merged into one corpus, keyed on the CN id, and searched together.
Approaches to market are deliberately kept in their own tab: an approach and an award
are different kinds of fact, and folding a notice carrying no value into a table of
committed spend invites exactly the reading the data does not support.

## Where the numbers disagree

Contracts of $100,000 and above appear in both the OCDS API and the Senate Order
snapshots, and for about 29,000 of them the two publications report different values.
Those are flagged `source_disagreement` and **both figures are shown**. They are not
reconciled, averaged, or silently resolved in favour of one source, because there is no
basis on which to choose: about 63% look like an API frozen at the original value, and
the rest do not fit that explanation at all.

Every flag in this archive is a queue for review, never a finding.

## Approaches to market

`atm.py` polls AusTender's current-notice feed three times a day. That feed is a
window, not a history: it publishes 75 open notices and keeps no archive, so a notice
that opens and closes between two polls is gone for good. The store here is therefore
permanent and append-only — the feed is treated as a sighting, never as the truth about
what exists. The archive starts the day it began watching and cannot recover anything
earlier.

Each notice is enriched once from its own page, which publishes three fields the OCDS
API does not expose anywhere: **panel arrangement**, **multi-agency access** and
**multi-stage**.

### Alerts

`data/watchlist.json` holds the watches. Each fires only for notices first seen after
the watch existed, so adding one does not replay the archive into the feed. Matches are
written to `data/atm/alerts.json` and to an Atom feed at `data/atm/alerts.xml`, which
any reader can subscribe to — a feed is the one alerting mechanism a static host can
actually deliver.

Terms match on word boundaries with an optional trailing "s", so `participant` catches
"participants" but `ndia` does not fire inside "Indian Ocean Territories". Curly
apostrophes and en dashes are flattened first, so a term typed with a straight
apostrophe still matches "Analyst's Notebook" as the page actually writes it.

Watches fire forward only. To replay an edited watchlist against everything already
captured:

```bash
python atm.py --out data/atm --rematch --no-detail
```

Every term in the watchlist was measured against the 306,542 contract records in this
archive before being included. The ones that did not survive are recorded in the file
under `rejected_terms`, with the reason — `intelligence` matched 2,459 records that were
almost all the ACIC's own name, `kg` is kilograms, `ml` is millilitres, and `cypher` is a
door lock. Do not re-add one without re-testing it.

## Schedules

| Workflow | When | Notes |
|---|---|---|
| `refresh.yml` | Sundays 18:00 UTC | The main writer; everything else stands down for it |
| `atm.yml` | 02:15, 10:15, 18:15 UTC | Notices and alerts |
| `senate.yml` | Tuesdays 21:00 UTC | AusTender keeps only three periods before deleting them |
| `so13.yml` | 1st monthly | Published twice yearly; checking monthly is cheap |
| `historical.yml` | 8th monthly | Content-hashed, so a repeat run is a no-op |
| `backfill-resume.yml` | every 30 min | Resumes a chunked backfill from its checkpoint |

Every writer refuses to start while another is in flight, and refuses to commit a file
containing conflict markers. Both defences exist because two unguarded writers once
overlapped, put conflict markers into `main`, and took the site down.

## Running it locally

```bash
pip install requests openpyxl
python refresh.py --data-dir data          # AusTender, incremental
python atm.py --out data/atm               # notices and alerts
python -m http.server 8765                 # then open http://localhost:8765
```

`refresh.py --inspect` samples live releases and prints every field path present, with
counts. Use it before trusting the mapper: four of its field mappings were wrong at
first, and the buyer column was showing the supplier.

## Deployment

See [DEPLOY.md](DEPLOY.md).
