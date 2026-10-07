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
Not thinly — absent. Among them:

- **WSA Co Limited** — builds Western Sydney Airport
- **Australian Naval Infrastructure Pty Ltd**
- **National Reconstruction Fund Corporation** — a $15 billion fund
- **CEA Technologies Pty Limited** — defence radar
- Indigenous Business Australia, the Indigenous Land and Sea Corporation, and every Land Council

The NDIA is in this class and is the one we partly hold. What we hold of it shows the shape
of the whole gap:

- **0** records from AusTender. It does not report there and never has.
- **1,450** records, all from Senate Order 13 listings, covering **two periods only** — 2025
  and 2025-26. Anything bought before 2025 is absent.
- The smallest record is **exactly $100,000**, which is the Senate Order floor. Nothing below
  it is ever listed, by design.
- **Zero** contracts touching analytics, fraud or entity resolution — for an agency running a
  fraud taskforce. That is a coverage artefact, not a finding.

## What cannot be obtained, and should stop being chased

**Marketplace interiors.** What an agency drew down through the AWS, Azure, Google or
Salesforce marketplaces is private commercial data. There is no API, no disclosure
obligation and no FOI route that reaches it, because the Commonwealth does not hold it — the
marketplace operator and the reseller do. A contract bought through a marketplace is public;
what was inside it is not.

**OEM composition inside whole-of-government agreements.** Same reason. An FOI to the DTA
asks for something the DTA may not hold, and triggers third-party consultation under s.27 of
the FOI Act, which the vendor will use.

What *is* obtainable is the volume through each arrangement, and that is now held: every
contract cites its standing offer, and AusTender publishes the notices that name them. See
`son.py`. This answers the question most people reach for FOI to settle.

## The work, ranked by payoff over effort

### 1. Senate Order 13 for every corporate entity — *verified feasible, biggest gap*

`so13.py` already does this for the NDIA and its parser is generic; it has one entry in
`SOURCES`. Each of the 76 absent entities must publish, twice yearly, every contract of
$100,000 or more, as a spreadsheet on its own website. The work is finding 76 URLs, not
writing code.

Expected: the single largest increase in coverage available, into entities that currently
contribute nothing. Unknown until collected, but WSA Co and Australian Naval Infrastructure
alone build multi-billion-dollar assets.

Risk: each entity publishes on its own page in its own format, and pages move. The ingest
must refuse a changed format loudly rather than store nothing quietly.

### 2. Earlier Senate Order periods for entities already held — *partly blocked*

The NDIA publishes only 2024–2026 on its page; older listings are gone. The Internet Archive
is the candidate route (it returned 503 when tried on 7 October). Worth one attempt per
entity, and worth nothing if the entity never published.

### 3. ANAO performance audits — *needs a spike*

The Auditor-General audits ICT procurement repeatedly, and the reports carry figures and
sometimes vendor breakdowns that appear in no register — including spend inside arrangements.
anao.gov.au has no feed and no obvious index endpoint; the report listing needs discovering
the way BuyICT's did. Value is corroboration and the occasional number that exists nowhere
else, not bulk.

### 4. Transparency Portal annual reports — *needs a spike*

transparency.gov.au carries every Commonwealth annual report. Corporate entities disclose
consultancy expenditure and significant contracts there, which is a second route into the
same 76 entities. The site is a JavaScript application with no visible API; its data
endpoints need discovering.

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
