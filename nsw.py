#!/usr/bin/env python3
"""
New South Wales contract awards, from the buy.nsw register of notices.

NSW publishes 15,702 contract awards and 2,365 standing offers, plus 16,097
expired notices, and none of it reaches AusTender. It is richer than Victoria's
register — it names the contractor AND its ABN, which is what lets a supplier be
followed across jurisdictions: the same ABN that holds a Commonwealth contract
appears here against a NSW agency, or a council.

How the data gets here, and why it is not automatic. The platform sits behind a
content delivery network that answers anything but a real browser with an empty
202, and there is no open-data copy: data.nsw carries council registers and
media releases, not this. The platform does publish its own bulk export —
buy.nsw.gov.au/notices/notice-reports, public, no sign-in, filtered by notice
type, agency, category and publish date, with an Export CSV button. That export
is the supported way to take the register in bulk, so that is the way this
archive takes it: a person clicks the button, the file lands in data/nsw/raw/,
and this reads whatever is there. Nothing here pretends to be a browser.

Drop any number of exports into data/nsw/raw/ — overlapping date ranges are
fine, notices are keyed by notice ID and the newest "last updated" wins — then:

    python nsw.py

Keep the CSVs. They are the evidence, and they are gzipped on the way in.
"""
import argparse
import csv
import glob
import gzip
import io
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

import build

DATA = os.environ.get("PROCLENS_DATA", "data")
SOURCE = "https://buy.nsw.gov.au/notices/notice-reports"

# The export's own headings, in its own words.
COLUMNS = {
    "Department/Agency": "agency",
    "Notice ID": "id",
    "Notice title": "title",
    "Category": "category",
    "Class type": "class_type",
    "Contract description": "description",
    "Project scope": "scope",
    "Related opportunity ID": "opportunity_id",
    "Related ETR CAN ID": "etr_can_id",
    "Publish date": "published",
    "Publish status": "status",
    "Contractor name": "supplier",
    "Contractor ABN": "abn",
    "Contract effective date": "start",
    "Contract end date": "end",
    "Contract value": "value",
    "Piggyback clause": "piggyback",
    "Last updated": "updated",
}

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def as_date(s):
    """"01-Sep-2026" -> "2026-09-01"."""
    m = re.match(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", str(s or "").strip())
    if not m:
        return None
    mon = MONTHS.get(m.group(2).lower())
    return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}" if mon else None


def money(s):
    v = re.sub(r"[^\d.\-]", "", str(s or ""))
    try:
        return round(float(v), 2) if v not in ("", "-", ".") else None
    except ValueError:
        return None


def abn(s):
    d = re.sub(r"\D", "", str(s or ""))
    return d if len(d) == 11 else None


def read_csv(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8-sig", errors="replace", newline="") as fh:
        head = fh.readline()
        if "Notice ID" not in head:
            print(f"  ! {os.path.basename(path)}: not a notice report, skipped", file=sys.stderr)
            return
        fh.seek(0)
        for raw in csv.DictReader(fh):
            row = {COLUMNS[k]: (v or "").strip() for k, v in raw.items()
                   if k in COLUMNS and v is not None}
            if not row.get("id"):
                continue
            yield {
                "id": row["id"],
                "jurisdiction": "New South Wales",
                "agency": row.get("agency") or None,
                "title": row.get("title") or None,
                "supplier": row.get("supplier") or None,
                "abn": abn(row.get("abn")),
                "value": money(row.get("value")),
                "category": row.get("category") or None,
                "description": row.get("description") or None,
                "scope": (row.get("scope") or None),
                "published": as_date(row.get("published")),
                "start": as_date(row.get("start")),
                "end": as_date(row.get("end")),
                "updated": as_date(row.get("updated")),
                "status": row.get("status") or None,
                "piggyback": row.get("piggyback") or None,
                "opportunity_id": row.get("opportunity_id") or None,
                # The register's own pages are keyed by a UUID the export does
                # not carry, so a direct link cannot be built from this data. A
                # search on the notice ID resolves to exactly one notice, which
                # is an honest link rather than a guessed one that 404s.
                "url": "https://buy.nsw.gov.au/notices/search?query=" + row["id"],
            }


def keep_raw(raw_dir):
    """Exports are the evidence; plain CSVs are gzipped where they sit."""
    for p in sorted(glob.glob(os.path.join(raw_dir, "*.csv"))):
        with open(p, "rb") as fh, gzip.open(p + ".gz", "wb") as out:
            out.write(fh.read())
        os.remove(p)
        print(f"  kept {os.path.basename(p)}.gz", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Ingest buy.nsw notice report exports.")
    ap.add_argument("--raw", default=os.path.join(DATA, "nsw", "raw"))
    ap.add_argument("--out", default=os.path.join(DATA, "nsw", "contracts.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.raw, exist_ok=True)
    if not args.dry_run:
        keep_raw(args.raw)
    files = sorted(glob.glob(os.path.join(args.raw, "*.csv.gz")) +
                   glob.glob(os.path.join(args.raw, "*.csv")))
    if not files:
        sys.exit(f"nsw: no exports in {args.raw}. Take one from {SOURCE} — notice type "
                 f"'Contract award', a publish date range, then Export CSV — and put it there.")

    existing = build.read_json(args.out, {})
    store = {r["id"]: r for r in existing.get("contracts", [])}
    before, seen, superseded = len(store), 0, 0
    for path in files:
        n = 0
        for row in read_csv(path):
            n += 1
            seen += 1
            old = store.get(row["id"])
            # Exports overlap by design. The newer "last updated" wins, so a
            # re-pull of the same months corrects rather than duplicates.
            if old and (old.get("updated") or "") > (row.get("updated") or ""):
                superseded += 1
                continue
            row["first_seen"] = (old or {}).get("first_seen") or \
                datetime.now(timezone.utc).date().isoformat()
            store[row["id"]] = row
        print(f"  {os.path.basename(path)}: {n:,} notices", file=sys.stderr)

    rows = sorted(store.values(), key=lambda r: (r.get("published") or "", r.get("id")), reverse=True)
    with_abn = sum(1 for r in rows if r.get("abn"))
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Contract award notices published by NSW government agencies and councils on the "
                "buy.nsw register. New South Wales is not the Commonwealth: these are counted "
                "separately and never added to a Commonwealth total. Taken from the register's own "
                "bulk CSV export, which a person runs; the platform serves it only to a browser and "
                "publishes no open-data copy.",
        "source": SOURCE,
        "totals": {"contracts": len(rows), "new_this_run": len(rows) - before,
                   "rows_read": seen, "older_rows_ignored": superseded,
                   "with_an_abn": with_abn,
                   "value_aud": round(sum(r["value"] for r in rows
                                          if isinstance(r.get("value"), (int, float))), 2),
                   "by_year": dict(sorted(Counter((r.get("published") or "?")[:4] for r in rows).items()))},
        "exports": [os.path.basename(f) for f in files],
        "contracts": rows,
    }
    if args.dry_run:
        import json
        print(json.dumps(payload["totals"], indent=1))
        return
    build.write_json_if_changed(args.out, payload)
    summary = (f"**{len(rows):,} NSW contract awards** ({len(rows) - before:,} new), "
               f"{with_abn:,} with an ABN, ${payload['totals']['value_aud']:,.0f} in all, "
               f"from {len(files)} export(s).")
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(summary + "\n")


if __name__ == "__main__":
    main()
