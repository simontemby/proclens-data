#!/usr/bin/env python3
"""
The arrangements contracts are bought under.

Every contract notice cites the standing offer it was placed against, and this
archive has held those SON IDs all along — 3,816 of them, carrying $325.7bn. It
held them as bare numbers, so the arrangements were invisible: SON3490955 means
nothing until you learn it is the Software and ERP Marketplace Panel, through
which 101 agencies have spent $5.1bn.

AusTender publishes the notices themselves, and will hand over all 5,342 of them
as a spreadsheet from its own search page — no sign-in, no challenge, one
request. That is the whole source. It names 98% of the arrangements this archive
sees cited, covering effectively all of the money.

This matters because it is the honest answer to a question people reach for
freedom-of-information requests to settle: how much has been bought under a
whole-of-government arrangement. The volumes were already public and already
here; only the labels were missing.

What it still does not tell you is what was inside each purchase. A contract
placed against a software marketplace panel says what it cost and who was paid,
not whose product it was. That question belongs to vendors.py, which answers it
only where a contract's own words allow.

    python son.py
"""
import argparse
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from xml.etree import ElementTree as ET

import requests

import build

DATA = os.environ.get("PROCLENS_DATA", "data")
# AusTender's own "Download Results" link, with no filters: every standing offer
# notice it holds. Found by running the search a reader would run.
EXPORT = ("https://www.tenders.gov.au/Search/SonAdvancedSearchDownload"
          "?SearchFrom=SonSearch&Type=Son&AgencyStatus=-1&KeywordTypeSearch=AllWord"
          "&DateType=Publish%20Date")
UA = ("Mozilla/5.0 (compatible; legal-tender-archive/1.0; "
      "+https://simontemby.github.io/proclens-data/)")
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
EPOCH = datetime(1899, 12, 30)      # the spreadsheet's own zero


def cells(xlsx):
    """Rows out of the workbook, without a spreadsheet library."""
    z = zipfile.ZipFile(BytesIO(xlsx))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        shared = ["".join(t.text or "" for t in si.iter(NS + "t"))
                  for si in ET.fromstring(z.read("xl/sharedStrings.xml"))]
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    for row in sheet.iter(NS + "row"):
        out = []
        for c in row.iter(NS + "c"):
            v = c.find(NS + "v")
            if v is None or v.text is None:
                out.append("")
            elif c.get("t") == "s":
                out.append(shared[int(v.text)])
            else:
                out.append(v.text)
        yield out


def as_date(v):
    """The export writes dates as spreadsheet serials, some with a time."""
    s = str(v or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", s):
        return s[:10]
    try:
        return (EPOCH + timedelta(days=float(s))).date().isoformat()
    except ValueError:
        return None


def fetch(url=EXPORT):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=300)
    r.raise_for_status()
    if not r.content[:2] == b"PK":
        sys.exit(f"son: AusTender returned {r.headers.get('content-type')} "
                 f"({len(r.content):,} bytes), not a spreadsheet")
    return r.content


def parse(xlsx):
    """The sheet opens with a summary of the search criteria; the records are
    the block under the row that names the columns."""
    rows = [r for r in cells(xlsx) if len(r) >= 6]
    head = next((i for i, r in enumerate(rows) if r and r[0].strip() == "SON ID"), None)
    if head is None:
        sys.exit("son: no column header in the export — AusTender has changed it")
    cols = [c.strip() for c in rows[head]]
    out = {}
    for r in rows[head + 1:]:
        rec = dict(zip(cols, r))
        sid = (rec.get("SON ID") or "").strip()
        if not sid:
            continue
        out[sid] = {
            "son": sid,
            "title": (rec.get("Title") or "").strip() or None,
            "agency": (rec.get("Agency") or "").strip() or None,
            "category": (rec.get("Primary Category") or "").strip() or None,
            "published": as_date(rec.get("Publish Date")),
            "start": as_date(rec.get("Contract Start Date")),
            "end": as_date(rec.get("Contract End Date")),
            "atm": (rec.get("ATM ID") or "").strip() or None,
        }
    return out


def main():
    ap = argparse.ArgumentParser(description="Capture AusTender standing offer notices.")
    ap.add_argument("--out", default=os.path.join(DATA, "son", "arrangements.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    recs = parse(fetch())
    if len(recs) < 1000:
        sys.exit(f"son: only {len(recs)} notices came back; refusing to replace the store "
                 f"on what looks like a truncated export")
    print(f"{len(recs):,} standing offer notices", file=sys.stderr)

    existing = build.read_json(args.out, {})
    store = {r["son"]: r for r in existing.get("arrangements", [])}
    today = datetime.now(timezone.utc).date().isoformat()
    new = 0
    for sid, r in recs.items():
        old = store.get(sid)
        r["first_seen"] = (old or {}).get("first_seen") or today
        if not old:
            new += 1
        store[sid] = r
    rows = sorted(store.values(), key=lambda r: (r.get("published") or "", r["son"]), reverse=True)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Standing offer notices published to AusTender: the arrangements contracts are "
                "bought under. Taken from AusTender's own bulk export of its standing offer "
                "search. A notice says what the arrangement is, not what was bought through it.",
        "source": EXPORT,
        "totals": {"arrangements": len(rows), "new_this_run": new},
        "arrangements": rows,
    }
    if args.dry_run:
        import json
        print(json.dumps(payload["totals"], indent=1))
        for r in rows[:5]:
            print(f"  {r['son']:<12} {str(r['title'])[:56]}")
        return
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    build.write_json_if_changed(args.out, payload)
    summary = f"**{len(rows):,} standing offer notices** ({new:,} new this run)."
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(summary + "\n")


if __name__ == "__main__":
    main()
