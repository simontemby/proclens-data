#!/usr/bin/env python3
"""
Opportunities on BuyICT, which never reach AusTender.

AusTender publishes approaches to market: open tenders, expressions of interest,
the notices any supplier can answer. It does not publish what happens inside a
panel. BuyICT — the Digital Transformation Agency's ICT buying platform — is
where Commonwealth agencies actually put their digital and ICT work, as requests
for quote to panel sellers and requests for information. None of it appears in
AusTender's current-notice feed, so none of it was in this archive: 9,780
opportunities since 2022, most of them invited-sellers-only.

The contract that comes out the other end is reported to AusTender as a contract
notice, so the money is eventually visible. The demand is not. A supplier
watching AusTender alone sees the award and never sees the request.

There is no open data for this. The DTA published Digital Marketplace extracts
once; nothing for BuyICT appears on data.gov.au today, and the platform has no
documented API. What it has is a ServiceNow Service Portal whose two public
widgets answer unauthenticated requests: a filter widget that hands out the
query scaffolding, and a list widget that returns the records. This asks them
the same questions a browser does, in the same order, and keeps every answer.

    page  ->  g_ck token + cookies
    filter widget  ->  the filters object (marketplaces, statuses, agencies)
    list widget  ->  records, paged by startLoc

Nothing here is scraped out of HTML: the records arrive as JSON with the field
names the platform uses. The store is append-only, like the approaches to market
in atm.py — a closed opportunity vanishes from the platform's default view, and
an archive that forgot it would be no archive.

Records are written in exactly the shape atm.py writes, and are read by the same
shard loader, matched by the same watchlist and carried in the same Atom feed. A
BuyICT request for quote is not an approach to market and is labelled as its own
source throughout; what it shares with one is that a supplier wants to know
about it before the award, not after.

    python buyict.py                 # update the store
    python buyict.py --since 2025-01 # only look at recent pages
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import requests

import atm
import build

DATA = os.environ.get("PROCLENS_DATA", "data")
PORTAL = "https://www.buyict.gov.au"
PAGE = PORTAL + "/public?id=opportunities"
RECT = PORTAL + "/api/now/sp/rectangle/{}?id=opportunities"
# The two widget instances on the opportunities page. They are part of the
# page's configuration rather than its data, so they change only if the DTA
# rebuilds the page — at which point this refuses to run rather than quietly
# storing nothing.
FILTER_WIDGET = "f3c3ac0a8738d950f973a8e50cbb35c2"
LIST_WIDGET = "bc93ec0a8738d950f973a8e50cbb3598"
PAGE_SIZE = 200
UA = "Mozilla/5.0 (compatible; legal-tender-archive/1.0; +https://simontemby.github.io/proclens-data/)"

# What the platform calls a thing, and what it is.
PROC_TYPE = {"u_lh_procurement": "ICT labour hire",
             "u_pcs_procurement": "Professional and consulting services",
             "u_rfi_procurement": "Request for information"}


def session():
    """A portal session: cookies, and the token every widget call must carry."""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    html = s.get(PAGE, timeout=90).text
    m = re.search(r"g_ck = '([^']+)'", html)
    if not m:
        sys.exit("buyict: no session token on the opportunities page — the portal has changed")
    s.headers["X-UserToken"] = m.group(1)
    return s


def widget(s, wid, body):
    r = s.post(RECT.format(wid), json=body, timeout=180)
    r.raise_for_status()
    out = r.json().get("result") or {}
    if out.get("invalid_token"):
        sys.exit("buyict: the portal rejected the session token")
    return out.get("data") or {}


def scaffolding(s):
    """The filter widget builds the query the list widget runs. Without it the
    list widget answers every request with no records and no error, which is why
    this asks for it rather than posting an empty filter."""
    data = widget(s, FILTER_WIDGET, {})
    filters = data.get("filters")
    if not filters or "opp_status" not in filters:
        sys.exit("buyict: the filter widget returned nothing usable — the portal has changed")
    # Live only, by default. Everything the platform will admit to, here.
    for c in filters["opp_status"]["choices"]:
        c["selected"] = True
    return filters


def fetch(s, filters, since=None, quiet=False):
    """Every opportunity the platform will return, newest first.

    Paged on startLoc, all the way to the total the platform reports — never
    stopping at an empty page. The platform pages over its own rows and only
    then drops the ones a visitor may not see, so pages thin out and some come
    back empty long before the end: 187 records, then 174, then 123, then one.
    Around 6,900 of the 9,780 rows it counts are public. Both numbers are
    returned, so the store can say what it is missing instead of implying it
    holds everything."""
    boot = widget(s, LIST_WIDGET, {})
    found, reported, start = {}, None, 0
    while True:
        body = dict(boot, filters=filters, getPageData=True,
                    startLoc=start, endLoc=start + PAGE_SIZE - 1,
                    page_size=str(PAGE_SIZE), currentPage=start // PAGE_SIZE + 1,
                    searchAll_RFQ=True,
                    sortOrder={"field": "u_published_date", "direction": "desc"})
        d = widget(s, LIST_WIDGET, body)
        items = d.get("pageItems") or []
        reported = d.get("totalItems") or reported
        for r in items:
            if r.get("sys_id"):
                found[r["sys_id"]] = r
        if not quiet:
            print(f"  {start + len(items):>6,} of {reported or '?'} · {len(found):,} distinct",
                  file=sys.stderr, flush=True)
        if since and items and all(published(r) and published(r) < since for r in items):
            break
        start += PAGE_SIZE
        if not reported or start >= int(reported) or start > 100 * PAGE_SIZE:
            break
        time.sleep(0.3)
    return found, reported


def published(r):
    """The platform writes dates as DD-MM-YYYY HH:MM:SS, Canberra time."""
    d = str(r.get("u_published_date") or "")
    m = re.match(r"(\d{2})-(\d{2})-(\d{4})", d)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def closes(r):
    d = str(r.get("u_close_date") or r.get("close_date") or "")
    m = re.match(r"(\d{2})-(\d{2})-(\d{4})", d)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return d[:10] if re.match(r"\d{4}-\d{2}-\d{2}", d) else None


def row(r):
    """One opportunity, in the same fields atm.py writes, so the front end, the
    watchlist and the feed need no special case. `kind` carries the source."""
    parts = [PROC_TYPE.get(r.get("proc_type"), r.get("proc_type")),
             r.get("panel"), r.get("category")]
    extra = [("Open to", r.get("u_open_to")), ("Working arrangement", r.get("u_delivery_method")),
             ("Specialisation", r.get("invited_specialisation")),
             ("Categories", ", ".join(c for c in (r.get("offering_categories") or []) if c))]
    desc = " · ".join(f"{k}: {v}" for k, v in extra if str(v or "").strip())
    return {
        "guid": r.get("sys_id"),
        "kind": "BuyICT",
        "atm_id": r.get("number"),
        "title": (r.get("short_description") or "").strip(),
        "agency": (r.get("u_buyer_name") or "").strip(),
        "cat": None,
        "cat_title": (r.get("category") or "").strip() or None,
        "atm_type": PROC_TYPE.get(r.get("proc_type"), r.get("proc_type")),
        "location": (r.get("u_delivery_state") or "").strip() or None,
        "panel": (r.get("panel") or "").strip() or None,
        "multi_agency": None,
        "multi_stage": None,
        "publish": published(r),
        "close": closes(r),
        "desc": desc or None,
        "detail": PORTAL + "/public" + r["url"] if r.get("url") else None,
        # The platform states this itself, per record. atm.py has to infer it
        # from a notice falling out of AusTender's current-ATM feed, because
        # that feed carries only what is open; this listing carries both.
        "open": 0 if r.get("closed") else 1,
    }


def merge(store, fetched, today):
    """Append-only, and never rewrites a first_seen.

    Unlike the AusTender feed, this listing includes opportunities that have
    already closed, so closing is read from the record rather than inferred from
    its disappearance. A record that does leave the listing keeps whatever it
    last said."""
    changed = Counter()
    for sys_id, raw in fetched.items():
        new = row(raw)
        old = store.get(sys_id)
        if not old:
            new["first_seen"] = today
            if not new["open"]:
                new["closed"] = new.get("close") or today
            store[sys_id] = new
            changed["new"] += 1
            continue
        new["first_seen"] = old.get("first_seen") or today
        new["closed"] = old.get("closed") or (new.get("close") or today if not new["open"] else None)
        if {k: v for k, v in old.items() if k != "first_seen"} != \
           {k: v for k, v in new.items() if k != "first_seen"}:
            changed["updated"] += 1
        store[sys_id] = new
    return changed


def main():
    ap = argparse.ArgumentParser(description="Capture BuyICT opportunities.")
    ap.add_argument("--out", default=os.path.join(DATA, "buyict"))
    ap.add_argument("--since", help="stop paging once a page is entirely older than this (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    args = ap.parse_args()

    t0 = time.time()
    today = datetime.now(timezone.utc).date().isoformat()
    s = session()
    filters = scaffolding(s)
    fetched, reported = fetch(s, filters, since=args.since)
    if not fetched:
        sys.exit("buyict: the platform returned no opportunities at all — refusing to "
                 "rewrite the store on nothing")
    print(f"{len(fetched):,} distinct opportunities ({reported} reported by the platform) "
          f"in {time.time() - t0:.0f}s", file=sys.stderr)

    store = atm.load_store(args.out)
    before = len(store)
    changed = merge(store, fetched, today)

    # Shards by publish year, in the shape atm.py writes, read by the same loader.
    by_year = {}
    for r in store.values():
        by_year.setdefault((r.get("publish") or r.get("first_seen") or "")[:4] or "unknown", []).append(r)
    shards = []
    for year in sorted(by_year):
        rows = sorted(by_year[year], key=lambda r: r.get("publish") or "", reverse=True)
        name = f"buyict-{year}.json"
        path = os.path.join(args.out, name)
        dicts, enc = atm.encode(rows)
        _, sha = atm.write_json_if_changed(path, {"year": year, "fields": atm.FIELDS,
                                                  "dict": dicts, "rows": enc})
        shards.append({"year": year, "file": name, "count": len(rows),
                       "bytes": os.path.getsize(path), "sha": sha})
    shards.sort(key=lambda x: x["year"], reverse=True)

    open_now = sum(1 for r in store.values() if r.get("open"))
    hidden = (int(reported) - len(fetched)) if reported else None
    atm.write_json_if_changed(os.path.join(args.out, "index.json"), {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Opportunities published on BuyICT, the Digital Transformation Agency's ICT buying "
                "platform: panel requests for quote and requests for information. None of this is "
                "published to AusTender, and none of it is an approach to market — most is open only "
                "to invited sellers. Kept permanently, because a closed opportunity leaves the "
                "platform's listing. The platform counts more rows than it will show a visitor who "
                "is not signed in; the difference is recorded below rather than hidden.",
        "source": PAGE,
        "fields": atm.FIELDS,
        "shards": shards,
        "totals": {"notices": len(store), "open": open_now,
                   "new_this_run": changed["new"], "updated_this_run": changed["updated"],
                   "seen_today": len(fetched), "reported_by_platform": reported,
                   "not_shown_to_the_public": hidden},
        "capture_start": min((r.get("first_seen") or "" for r in store.values()), default=today)[:10],
    })

    summary = [f"**{len(store):,} BuyICT opportunities** ({before:,} before this run): "
               f"{changed['new']:,} new, {changed['updated']:,} updated, {open_now:,} open now.",
               "", f"The platform reports {reported} rows and shows {len(fetched):,} to the public; "
               f"the other {hidden:,} are not visible without a seller account." if hidden else "",
               "", "| Kind | Opportunities |", "|---|---|"]
    for k, n in Counter(r.get("atm_type") for r in store.values()).most_common():
        summary.append(f"| {k} | {n:,} |")
    text = "\n".join(x for x in summary if x is not None)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text + "\n")


if __name__ == "__main__":
    main()
