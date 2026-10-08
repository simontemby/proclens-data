# Taking the site further, for about nothing

Written 9 October 2026. Every number here was measured, not estimated.

## 1. The attribution pattern — done

The NDIA buys $235.7m of Salesforce platform and files it as "Platform as a Service (PaaS -
Cloud)". That is not an NDIA habit. AusTender has no field for the product, so a diligent
officer and a careless one file the same words, and whatever runs on that platform is
invisible to anyone reading the record.

Generalised: a description made entirely of transaction vocabulary — provision, service,
software, platform, labour, hire, licence — names nothing that was bought. The test is the
one `graph.py` applies to supplier names: strip what is common to everything and see what
survives. "Microsoft 365 E5 licences" survives; "Platform as a Service (PaaS - Cloud)" does
not.

**160,265 contracts, 12.1% of the archive, $113.1bn.** Defence $54.2bn, Services Australia
$10.7bn. The commonest are "Labour Hire" (18,182), "ICT Contractor Services" (7,316),
"Software" (3,521). Flagged `product_not_named`, live.

This is the honest ceiling on vendor attribution. No ingest closes it, because the fact was
never written down.

## 2. What runs itself, and what cannot

| Source | Scheduled | Why not |
|---|---|---|
| Commonwealth contracts, export, Senate, historical | yes | — |
| Approaches to market, BuyICT | yes | — |
| Standing offer arrangements | yes | — |
| Supplier graph | yes | — |
| **NSW** | no | AWS WAF challenge; export is a person's click |
| **Victoria** | no | Cloudflare 403 to datacentres; parked as a snapshot |
| **ANAO audits** | no | connection dropped from runners; ~30 audits a year |
| **Senate Order entities** | yes | CSIRO, CEFC, DHA, RBA, NLA, NDIA |

Three sources answer a desk and not a runner. That is a property of those sites, not a bug
to fix, and the rule for anything added later is: **test from a runner before calling it
automatic.** It has been got wrong three times here.

What would make the three automatic is a self-hosted runner on a residential connection —
one machine, no cost beyond leaving it on. That is the only change that converts all of them
at once, and it is worth doing before adding more manual sources.

## 3. Cloudflare R2 — worth it, but not for speed

Measured today: **548 MB served across 2,813 files**, against GitHub Pages' **1 GB hard
limit**. 55% used, and every entity added moves it up. `.git` is 433 MB and grows with every
data commit.

R2's free tier: 10 GB storage, 1 million writes and 10 million reads a month, **egress
free**. This archive's access pattern is 5–20 small files per page load, so 10 million reads
is somewhere between 500,000 and 2 million page loads a month. Data commits touch a few
hundred objects a week against a million. It stays free with a wide margin.

**It will not make the site faster.** Pages is behind a CDN and so is R2; for small JSON
shards the time is round-trips, not origin. Anyone promising a speed-up from this move is
guessing.

What it does buy, in order of value:

1. **Headroom.** 10 GB against 1 GB. The historical extracts (208 MB), the weekly exports
   (28 MB) and the Senate snapshots (14 MB) are currently excluded from the site because of
   the Pages limit. On R2 they could be published, which would make every source this
   archive uses downloadable by a reader — a real gain in auditability.
2. **No rebuild churn.** Every data commit triggers a Pages build over 2,813 files, and
   Pages allows ten builds an hour. Several workflows committing on the same evening already
   queue behind each other.
3. **A smaller repository.** Git keeps every version of every shard forever.

**Recommended shape:** HTML, CSS and JS stay on Pages — versioned, free, simple. `data/`
mirrors to R2 on each workflow run. **Git stays the source of record**; R2 is a serving
copy. Do not move data out of git: the audit trail of what this archive said and when is
most of why it can be believed.

Cost: $0. Effort: a `wrangler` step in each workflow, a bucket with CORS, and one constant
in `index.html`.

## 4. Budget papers — a new section, and the first forward-looking data here

Everything in this archive is retrospective: a contract notice exists only once money is
committed. Portfolio Budget Statements are the opposite — what each agency is funded to do
next — and they are published as **CSV on data.gov.au, 2015-16 to current**, through the
same API `historical.py` already uses. Automatable, no scraping.

What they give: agency and program-level expenses and forward estimates. What they do not
give: an ICT split. Intermedium classifies that by hand and sells the result. This archive
should not guess at it, and should say so.

The honest use is context rather than analysis: on an agency's page, what it is funded to
spend beside what it has contracted. A reader can see a program funded in the budget and no
contracts against it yet — which is the question "what is coming" answered from public data.

## 5. An analytics page inside these constraints

There is no server. Everything is either precomputed at build time or computed in the
browser, and that constraint has produced the archive's better decisions so far.

- **Precompute in `build.py`** into the index, as the totals already are. An aggregate that
  costs a reader a 50 MB download is the wrong aggregate.
- **No charting library.** Inline SVG from precomputed series. The page already draws value
  bars this way; a library would cost more than everything else on the page.
- **A separate route**, not the landing page. The landing stays one screen.
- **Lead with what the archive knows that others do not**: the $113.1bn that names no
  product, the $266.8bn contradicted across sources, the 2,761 ABNs in two jurisdictions,
  the arrangements by volume, the years a flag cannot speak for.

The analytics worth publishing are the ones about the record's reliability. Spend charts are
available from a dozen places; calibration is not.

## 6. What a reader needs that they do not have

- **Agency pages.** An agency's contracts, its arrangements, the ANAO audits of it, its
  budget. Everything needed for this is now collected and none of it is joined up.
- **Flag grouping.** Fourteen flags in one undifferentiated row. They are three different
  things — facts (confidential, consultancy), review signals (late publish, value growth)
  and coverage warnings (product not named, value contradicted) — and should be grouped.
- **A coverage page.** State plainly what the archive cannot see: the eleven trading GBEs,
  the $100,000 Senate Order floor, the consultancy hole from 2021 to 2024, the 12.1% that
  name no product. Nobody else publishes this, and it is the most defensible thing here.
- **A stable link per contract.** There is no permalink to a single contract today.

## 7. Order, and cost

| | Effort | Cost |
|---|---|---|
| Flag grouping and a coverage page | small | $0 |
| Agency pages joining contracts, arrangements, audits | medium | $0 |
| R2 mirror and publishing the excluded sources | small | $0 |
| Budget papers ingest and an agency budget line | medium | $0 |
| Analytics route | medium | $0 |
| Self-hosted runner for NSW, Victoria and ANAO | small | $0 beyond power |

All of it is $0. The limit is attention, not money.
