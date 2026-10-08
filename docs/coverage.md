# What this archive is missing, and how to close it

Written 7 October 2026, after a Quantexa contract at the NDIA could not be found and the
reason turned out to be structural rather than particular. Every number here was measured
against the archive on that date, not estimated.

## The shape of the problem

AusTender is not the Commonwealth's contract record. It is the record of the entities bound
to report to it. The Department of Finance's own
[Australian Government Organisations Register](https://data.gov.au/data/dataset/australian-government-organisations-register)
lists **1,386 bodies**, of which:

| Class | Count | Reports to AusTender |
|---|---|---|
| Non-corporate Commonwealth entity | 104 | yes |
| **Corporate Commonwealth entity** | **74** | **no** |
| **Commonwealth company** | **17** | **no** |
| **Statutory advisory structure** | **26** | **no** |

Of those 117 non-reporting bodies, **41 appear in this archive and 76 do not appear at all**.
Not thinly — absent.

But they are not one group, and the difference decides what is worth doing. Finance's own
guidance, [RMG 403](https://www.finance.gov.au/publications/resource-management-guides/meeting-senate-order-entity-contracts-rmg-403),
sets the scope of the Senate Order: it is relevant to non-corporate entities "and officials
in corporate Commonwealth entities (CCEs), **excluding trading Public Non-Financial
Corporations (PNFCs)** as classified by the Australian Bureau of Statistics."

So of the 91 corporate entities and Commonwealth companies:

| | Count | Contract disclosure |
|---|---|---|
| Covered by the Senate Order | 80 | listings on their own websites — **49 of them absent here** |
| **Trading PNFCs, excluded** | **11** | **none, anywhere** |

The excluded eleven are the ones holding the most money, which is the opposite of convenient:

> ASC Pty Ltd · Australian Naval Infrastructure · Australian Postal Corporation ·
> Australian Rail Track Corporation · CEA Technologies · Hearing Australia ·
> NBN Co Limited · National Intermodal Corporation · Snowy Hydro Ltd · WSA Co Limited
> *(Airservices Australia is the one exception: it reports to AusTender voluntarily and is held here.)*

NBN Co's annual capital programme and Snowy 2.0's construction are not disclosed
contract-by-contract **anywhere**: not to AusTender, not under the Senate Order. This is a
policy boundary, not an engineering problem, and no amount of ingest work crosses it. The
only routes are Senate Estimates, ANAO audits and their annual reports — none of which give
a contract register.

### Where the obtainable work actually is

Of the 49 absent entities that the Order does cover, Finance rates **8 as Material** and 41
as Small:

> Australian Reinsurance Pool Corporation · Clean Energy Finance Corporation ·
> Coal Mining Industry (Long Service Leave Funding) Corporation · **CSIRO** ·
> Defence Housing Australia · Housing Australia · Indigenous Business Australia ·
> National Reconstruction Fund Corporation

CSIRO is the largest and the most surprising absence. These eight are the place to start.

### What makes this slower than it looks

`so13.py` reads spreadsheets, which is what the NDIA publishes. The others do not agree on a
format. Verified on 8 October 2026:

| Entity | Format |
|---|---|
| NDIA, National Library | `.xlsx` — works today |
| Defence Housing Australia | PDF, 28 pages |
| Clean Energy Finance Corporation | PDF, 49 pages |
| Indigenous Business Australia | page exists, no file links — HTML table or generated |

PDF table extraction is where silent corruption enters: `pypdf` returns a header split across
eleven lines, and a column misread is a wrong supplier against a real amount. If PDFs are
ingested it must be with per-row validation that refuses anything it cannot parse cleanly,
not best-effort text scraping. The same discipline the value guards use.

## What cannot be obtained, and should stop being chased

**Marketplace interiors.** What an agency drew down through the AWS, Azure, Google or
Salesforce marketplaces is private commercial data. There is no API, no disclosure
obligation and no FOI route that reaches it, because the Commonwealth does not hold it — the
marketplace operator and the reseller do. A contract bought through a marketplace is public;
what was inside it is not.

**OEM composition inside whole-of-government agreements.** Same reason. An FOI to the DTA
asks for something the DTA may not hold, and triggers third-party consultation under s.27 of
the FOI Act, which the vendor will use. A notice names whoever the agency paid, and AusTender
has no field for the product, so an OEM sold under a reseller's agreement appears only where
someone typed its name into a free-text description. IBM holds 3,782 contracts here and 29
name a product — 0.8% — while its two Whole of Government arrangements carry $3.4bn across
24 agencies.

**Contracts of trading government business enterprises.** See above: eleven of them, no
listing anywhere, by policy.

What *is* obtainable is the volume through each arrangement, and that is now held: every
contract cites its standing offer, and AusTender publishes the notices that name them. See
`son.py`. This answers the question most people reach for FOI to settle.

## The work, ranked by payoff over effort

### 1. Senate Order listings for the material entities — *five done, 8 October 2026*

**Done:** CSIRO (17,785 contracts, $24.9bn), the Clean Energy Finance Corporation (729,
$11.5bn), Defence Housing Australia (667, $635m), the Reserve Bank (1,179, $4.1bn) and the
National Library (391, $126m), beside the NDIA. **22,582 contracts worth $41.5bn** from
bodies that publish nothing to AusTender.

**Still to do:** Housing Australia, Indigenous Business Australia and Coal Mining Industry
LSL have not been attempted. The National Reconstruction Fund and the Australian Reinsurance
Pool were attempted and are blocked: both publish, and both build their index pages in the
browser, so a scheduled job cannot see the links. They are left as comments in `so13.py`
rather than removed.

Two things made this slower than "find 76 URLs":

- Entities put these pages where you would not look. Defence Housing Australia's is under
  procurement and consultancies; the CEFC's is under governance → compliance. Guessing cost
  more attempts than searching.
- The build was discarding what the ingest collected. `load_so13()` required a contract ID,
  and only the NDIA publishes one, so all 28,423 CSIRO rows were dropped in silence. An
  ingest that works and a build that drops it looks exactly like an ingest that does not
  work.

This is a smaller prize than it first appeared, because the entities holding the most money
are the excluded eleven. It is still the largest obtainable gain, and CSIRO alone is a
research procurement programme that currently contributes nothing.

Risk: three formats, not one, and pages move. The ingest must refuse a changed format loudly
rather than store nothing quietly — and must not best-effort its way through a PDF.

### 2. Earlier Senate Order periods for entities already held — *partly blocked*

The NDIA publishes only 2024–2026 on its page; older listings are gone. The Internet Archive
is the candidate route (it returned 503 when tried on 7 October). Worth one attempt per
entity, and worth nothing if the entity never published.

### 3. ANAO performance audits — *done 9 October 2026, refreshed by hand*

1,481 performance audits back to 1995, with report number, title, date and the audit's own
stated objective. **211 touch procurement, contracting or ICT** — Defence's infantry fighting
vehicles, the OneSKY contract, the DTA's implementation of procurement reforms.

The findings are deliberately not summarised. An audit's conclusions are argument, not data,
and summarising them automatically would put words in the Auditor-General's mouth. What the
index does is let a reader looking at an agency learn that its procurement has been examined,
and go and read it.

Not scheduled, and this is the third source in this archive that cannot be: anao.gov.au
answers a desk in under a second and never answers a GitHub runner, through retries and with
IPv6 ruled out. Their robots.txt permits `/pubs/` and `/work/`; the network does not. Audits
arrive at about thirty a year, so refreshing by hand costs nothing.

**A pattern worth naming.** Victoria, NSW and now the ANAO all serve an ordinary connection
and refuse or ignore a cloud one. Anything built here should be tested from a runner before
it is called automatic — three times in this archive a source has been described as
scheduled on the strength of a test from a desk, and three times that was wrong.

### 4. Transparency Portal annual reports — *spiked 9 October 2026, does not close the gap*

The API is real and open: `data.transparency.gov.au/api/datasets/simplified`, no sign-in, no
challenge. It ignores query parameters and returns everything — about 70MB, and it truncates
at a different point on every request, so it would need a resumable fetch.

What it holds, and what it does not:

- **Entity-level consultancy expenditure** per entity per year: number of contracts and total
  dollars, from the annual report.
- **Supplier-level consultancy shares** — name, ABN and expenditure, which would join to this
  archive on the ABN. **Non-corporate entities only: 367 records, none from a corporate
  entity or Commonwealth company.** So it does not reach the bodies this archive cannot see.
- Financial statements, employee counts and governance data for all body types — no contracts.
- Named individuals' salaries, which this archive has no reason to republish and will not.

So it is a cross-check on entities already held, not new coverage. Worth building only for
the integrity comparison, and that comparison is currently blocked by the next item.

### 4b. The consultancy flag has a hole in the middle — *found while spiking the above*

Comparing annual-report consultancy totals against this archive's consultancy-flagged spend
produced a 600-fold gap at Defence for 2022-23. That is not a finding about Defence; it is a
finding about this archive. The flag comes from AusTender's weekly export, which is retained
for eighteen months, and the historical extracts stop before 2020:

| Year published | Contracts carrying the flag |
|---|---|
| 2019 | 4.5% |
| 2020 | 2.2% |
| **2021** | **0.0%** |
| **2022** | **0.1%** |
| **2023** | **0.2%** |
| 2024 | 0.5% |
| 2025 | 3.5% |

Any question asked of this archive about consultancies between 2021 and 2024 will give an
answer that is wrong by two orders of magnitude, and nothing on the site currently says so.
That matters more than any new source: a visible gap is a caveat, an invisible one is a
false answer. Fixing it means finding a copy of the export's consultancy field for those
years, and it may not exist outside AusTender.

### 5. Senate Estimates Questions on Notice — *manual, high value per item*

Published on aph.gov.au. Senators ask precisely the questions this archive cannot answer
from registers — what a system cost, what was procured under an arrangement — and the
answers are public and citable. Not a bulk source; a targeted one.

### 6. Widen the vendor lexicon — *improves what is already held*

`data/vendors.json` holds 83 vendors and 163 aliases. Every alias added converts presumed
attributions into stated ones, and stated is the only tier worth putting in front of someone
who knows their own numbers. This is the cheapest accuracy work available and needs no new
source.

## A standing rule this episode produced

A gap found in one place is a class of gap, not an incident. The NDIA was missing because of
what it *is*, not because of anything particular to it, and the register showed 76 more of
the same kind. Before concluding that spend does not exist, check whether the entity was ever
required to report it.
