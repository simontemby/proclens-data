# Legal Tender

A public archive of Australian Commonwealth contract notices, with the sources that
disagree with them kept alongside rather than reconciled away.

Live at **https://simontemby.github.io/proclens-data/**

The front end addresses its data relatively, so it runs unchanged on any static host.
The one place the address is baked in is the Atom feed, which has to state its own
absolute URL: set the `PROCLENS_SITE` repository variable when the site moves.

Nothing runs on a server. GitHub Actions build the data, GitHub Pages serves it, and
the whole front end is one `index.html` that fetches static JSON.

## Coverage

**1,297,943 contracts, each appearing once**, however many publications carry it.
Coverage is complete from January 2014. Counted by publication year against Love Me
Tender, an independent archive of the same AusTender data, as a benchmark rather than a
ground truth:

| | 2014 | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026* |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Legal Tender | 63,977 | 71,702 | 68,876 | 62,830 | 63,172 | 65,552 | 64,431 | 70,624 | 66,440 | 58,778 | 62,522 | 57,978 | 40,443 |
| Love Me Tender | 64,635 | 70,022 | 67,772 | 64,934 | 65,079 | 64,769 | 64,753 | 71,262 | 66,324 | 58,274 | 62,414 | 57,683 | 39,243 |

\*to mid-September. 817,325 against 817,164 in total. Where Legal Tender is higher the
difference is real contracts: in May 2026 the Department of Veterans' Affairs published
517 separate counselling-services contracts in one day, which Love Me Tender does not
count. 2017 and 2018 are about 3% lower and not yet explained. Contracts published
before 2014 come from the Department of Finance extracts and are not complete.

## What is in here

| Source | Script | What only it has |
|---|---|---|
| AusTender OCDS API | `refresh.py` | Every contract from 2014, its current value, and its full release history: first value and each amendment's date and value |
| AusTender weekly export | `export.py` | Why each amendment was made, confidentiality and consultancy with reasons, the ATM and standing offer, supplier location, agency branch |
| data.gov.au extracts, 2007–2020 | `historical.py` | Contracts the API no longer serves, and the same enrichment fields for their years |
| Senate Order snapshots | `senate.py` | What each contract was reported as worth at the end of each reporting period |
| Senate Order 13 listings | `so13.py` | Entities that do not report to AusTender at all — the NDIA among them |
| AusTender current-notice feed | `atm.py` | Approaches to market — what is about to be bought |

`build.py` merges the first five into one corpus keyed on the CN ID, with a build report
in `data/corpus/index.json` counting every collision and every contract only one source
holds. The weekly export and the notice feed are windows, not archives — AusTender
deletes export files after eighteen months and keeps no history of notices — so both
stores are permanent from first capture.

## Search

A search does not download the archive. `build.py` writes an index mapping every word to
the months that contain it; the page looks a query's words up there and fetches only
those months. Searching a supplier who appears in one month costs one small file.

Every word must match a whole word in the contract's description, agency, supplier, ABN
or CN ID, except the last, which matches as a prefix while it is being typed: `quant`
finds Quantexa, and `graph database` does not also mean "graphic database". A query in
quotes must appear as an exact phrase.

## Resellers and the vendor behind them

When software is bought through a reseller or integrator, the notice names the channel,
not whose product it was. `vendors.py` identifies those sales and, where the evidence
allows, the vendor.

A supplier counts as an intermediary when its software contracts name three or more
other vendors, when it is a known licensing channel, or when a published partnership
says so. **126 suppliers and 47,594 contracts** qualify. For each, the vendor is:

- **stated** — the description names it (4,110 contracts);
- **likely** — a published announcement names the supplier, vendor and buyer, or other
  contracts between the same parties consistently name one vendor (2,513);
- otherwise left blank (40,971). Nothing is guessed below that.

The pattern rules are tested on every build by hiding the vendor on contracts that name
one and asking the rule to recover it. At the current thresholds the agency-and-supplier
rule is right **91.6%** of the time (1,385 contracts) and the supplier rule **95.6%**
(298). Loosening the first to two contracts and 60% dropped it to 81.7%. Each presumption
on the site carries its rule's measured accuracy.

`data/vendors.json` holds the vendor lexicon — every alias tested against 157,697 software
contracts, with rejected ones and why (`sas` is also the Special Air Service) — and the
partnerships, each quoted from a page that was read. A vendor selling its own product is
never flagged: a Palantir contract for Palantir is the case where the vendor is not in
question.

## Senate Order snapshots against the API

A snapshot lists a contract as it stood at the end of its reporting period, so it is
compared with the value the API's own release history gives for that date, never with
today's value. Of the contracts where the two still differ:

- **14,836** carry a value in the snapshot that AusTender published only more than 42
  days after the period ended — a median of 175 days. The agency reported the amendment
  to the Senate before publishing it; the Commonwealth Procurement Rules allow 42 days.
  Flagged `late_amendment`.
- **1,063** carry a value that never appears anywhere in the API's history. Both figures
  are shown and neither is treated as correct. Flagged `source_disagreement`.

Every flag in this archive is a queue for review, never a finding.

## Corrections

An earlier version of this archive, and of this README, was wrong in ways that affected
published figures:

- It kept the first release the API returned for each contract, which is the original.
  **61,504 of 308,675 contracts (19.9%) showed a superseded value.**
- A later release's date replaced the publication date, moving 477 contracts to the
  month, often the year, they were amended.
- Amendments published in the same second were ordered as the API listed them; 2,450
  contracts showed an earlier amendment's value. They are now ordered by amendment number.
- Dates were read in UTC. AusTender records a day as Canberra midnight, so every start
  and end date and about a quarter of publication dates were a day early.
- The historical extracts' literal `NULL` was stored as a standing-offer and
  approach-to-market id for most contracts.
- It reported 29,105 contracts where the API and Senate Order snapshots disagree. Almost
  all of that was the first bug above and late publication of amendments; see the
  previous section for the corrected figures.
- Senate Order 13 contracts were collected but never shown.

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
| `export.yml` | Mondays 06:00 UTC | Sunday's contract notice export, and any earlier weeks still listed |
| `atm.yml` | 02:15, 10:15, 18:15 UTC | Notices and alerts |
| `senate.yml` | Tuesdays 21:00 UTC | AusTender keeps only three periods before deleting them |
| `so13.yml` | 1st monthly | Published twice yearly; checking monthly is cheap |
| `historical.yml` | 8th monthly | Content-hashed, so a repeat run is a no-op |
| `backfill-resume.yml` | manual only | Resumes a chunked backfill; schedule off since the backfill completed |

Every workflow that changes a source rebuilds the corpus before committing, so the site
never shows a source the corpus has not absorbed. Every writer refuses to start while another is in flight, and refuses to commit a file
containing conflict markers. Both defences exist because two unguarded writers once
overlapped, put conflict markers into `main`, and took the site down.

## Running it locally

```bash
pip install requests openpyxl
python refresh.py --data-dir data          # AusTender, incremental
python export.py --out data/export         # weekly contract notice exports
python build.py                            # merge everything into data/corpus
python atm.py --out data/atm               # notices and alerts
python -m http.server 8765                 # then open http://localhost:8765
```

`refresh.py --inspect` samples live releases and prints every field path present, with
counts. Use it before trusting the mapper: four of its field mappings were wrong at
first, and the buyer column was showing the supplier.

## Deployment

See [DEPLOY.md](DEPLOY.md).
