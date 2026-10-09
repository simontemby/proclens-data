#!/usr/bin/env python3
"""
What agencies are funded to spend, which is the one thing here that looks forward.

Every other source in this archive is retrospective: a contract notice exists
only once money is committed. Portfolio Budget Statements are the other side —
what each agency has been given, by outcome and programme, with forward
estimates three years out. Put beside the contracts, they answer a question the
contracts cannot: money appropriated and not yet spent.

Published as CSV on data.gov.au, which also carries the historical contract
extracts, so this uses the same API and needs no scraping.

Two things it is not, stated here because the temptation to pretend otherwise is
obvious. It is **not an ICT split**: the statements record programmes, not
technology, and anyone selling a digital-spend figure derived from them has made
a judgement this archive has not. And a programme expense is **not a contract**;
the two must never be added, and nothing here does.

    python budget.py
"""
import argparse
import csv
import io
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

import requests

import build

DATA = os.environ.get("PROCLENS_DATA", "data")
CKAN = "https://data.gov.au/data/api/3/action/package_search"
UA = "Mozilla/5.0 (compatible; legal-tender-archive/1.0; procurement transparency research)"
# The file wanted is the programme-level one. Years before 2017 publish dozens of
# per-portfolio spreadsheets instead, which are a different shape and not read here.
WANTED = re.compile(r"program\s*expenses?\s*line\s*items", re.I)
YEAR = re.compile(r"(20\d\d)[-–](?:20)?(\d\d)")


def find(s):
    """Every budget year data.gov.au publishes as programme line items."""
    r = s.get(CKAN, params={"q": 'title:"Portfolio Budget Statements"', "rows": 50}, timeout=120)
    r.raise_for_status()
    out = {}
    for p in r.json().get("result", {}).get("results", []):
        if "Portfolio Budget" not in p.get("title", ""):
            continue
        m = YEAR.search(p["title"])
        if not m:
            continue
        year = f"{m.group(1)}-{m.group(2)}"
        for res in p.get("resources", []):
            if WANTED.search(res.get("name") or "") and (res.get("format") or "").lower().endswith("csv"):
                out[year] = {"url": res["url"], "name": res.get("name"), "dataset": p["title"]}
    return out


def money(v):
    """The statements are in thousands."""
    t = re.sub(r"[^\d.\-]", "", str(v or ""))
    try:
        return round(float(t) * 1000, 2) if t not in ("", "-", ".") else None
    except ValueError:
        return None


def read(s, url):
    """The header is not always the first line. 2022-23 and 2023-24 open with
    four empty rows, which read as a single nameless column and silently
    produced no agencies at all."""
    r = s.get(url, timeout=300)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    head = next((i for i, row in enumerate(rows[:25])
                 if any("portfolio" == (c or "").strip().lower() for c in row)
                 and sum(1 for c in row if (c or "").strip()) >= 5), None)
    if head is None:
        return []
    cols = [(c or "").strip() for c in rows[head]]
    return [dict(zip(cols, r)) for r in rows[head + 1:] if any((c or "").strip() for c in r)]


def main():
    ap = argparse.ArgumentParser(description="Capture Portfolio Budget Statement line items.")
    ap.add_argument("--out", default=os.path.join(DATA, "budget", "programs.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    s = requests.Session()
    s.headers["User-Agent"] = UA
    years = find(s)
    if not years:
        sys.exit("budget: no programme line items found on data.gov.au")
    print(f"{len(years)} budget years published as line items: "
          f"{', '.join(sorted(years))}", file=sys.stderr)

    agencies = defaultdict(lambda: defaultdict(lambda: {"programs": 0, "budget": 0.0,
                                                        "forward": {}, "portfolio": ""}))
    rows_read = 0
    for year in sorted(years):
        try:
            rows = read(s, years[year]["url"])
        except Exception as e:                        # noqa: BLE001
            print(f"  ! {year}: {str(e)[:90]}", file=sys.stderr)
            continue
        if not rows:
            continue
        cols = rows[0].keys()
        ag = next((c for c in cols if "agency" in c.lower() or "department" in c.lower()), None)
        pf = next((c for c in cols if c.lower().strip() == "portfolio"), None)
        if not ag:
            print(f"  ! {year}: no agency column", file=sys.stderr)
            continue
        # The budget year's own column, plus whatever forward years are given.
        ycols = sorted(c for c in cols if re.fullmatch(r"20\d\d-\d\d", c.strip()))
        rows_read += len(rows)
        for r in rows:
            name = (r.get(ag) or "").strip()
            if not name:
                continue
            e = agencies[name][year]
            e["programs"] += 1
            e["portfolio"] = (r.get(pf) or "").strip() if pf else ""
            for c in ycols:
                v = money(r.get(c))
                if v is None:
                    continue
                if c.strip() == year:
                    e["budget"] += v
                else:
                    e["forward"][c.strip()] = round(e["forward"].get(c.strip(), 0) + v, 2)
        print(f"  {year}: {len(rows):,} line items", file=sys.stderr)

    out = sorted(
        ({"agency": a,
          "portfolio": next((y["portfolio"] for y in sorted(yrs.values(), key=lambda x: -x["programs"])
                             if y["portfolio"]), ""),
          "years": {y: {"programs": v["programs"], "budget": round(v["budget"], 2),
                        "forward": v["forward"]} for y, v in sorted(yrs.items())}}
         for a, yrs in agencies.items()),
        key=lambda x: -max((y["budget"] for y in x["years"].values()), default=0))

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Portfolio Budget Statement programme expenses by agency and budget year, with "
                "forward estimates. These are appropriations, not contracts, and the two are "
                "never added. There is no technology split in them: the statements record "
                "programmes, and any digital or ICT figure drawn from them would be a "
                "judgement this archive has not made.",
        "source": CKAN,
        "years": sorted(years),
        "totals": {"agencies": len(out), "line_items_read": rows_read},
        "agencies": out,
    }
    if args.dry_run:
        import json
        print(json.dumps(payload["totals"], indent=1))
        for a in out[:6]:
            y = sorted(a["years"])[-1]
            print(f"  {a['agency'][:44]:<46} {y}  ${a['years'][y]['budget']/1e9:>8,.1f}bn "
                  f"({a['years'][y]['programs']} programmes)")
        return
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    build.write_json_if_changed(args.out, payload)
    summary = (f"**{len(out):,} agencies** across {len(years)} budget years, "
               f"{rows_read:,} programme line items.")
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(summary + "\n")


if __name__ == "__main__":
    main()
