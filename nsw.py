#!/usr/bin/env python3
"""
New South Wales contract awards, from the buy.nsw register of notices.

NSW publishes 15,702 contract awards and 2,365 standing offers, plus 16,097
expired notices, and none of it reaches AusTender. It is richer than Victoria's
register — it names the contractor AND its ABN, which is what lets a supplier be
followed across jurisdictions: the same ABN that holds a Commonwealth contract
appears here against a NSW agency, or a council.

How the data gets here. There is no open-data copy — data.nsw carries council
registers and media releases, not this — but the platform publishes its own bulk
export at buy.nsw.gov.au/notices/notice-reports: public, no sign-in, filtered by
notice type, agency, category and publish date, with an Export CSV button. That
is the supported way to take the register in bulk, and `--fetch` asks for it the
way the button does, with the same fields and no token.

This was done by hand for a long time on the belief that the register answered
nothing but a browser. It answers an ordinary client perfectly well; what it
will not take is haste. Asking for the export about ten times in quick
succession, while working out what the form wanted, moved this machine from
answers to an empty 202 on every path, while a real browser on the same address
kept working — so the shield in front of it counts requests from a client rather
than judging where that client sits. Two requests a week, five seconds apart, is
a different thing entirely. robots.txt disallows only /search.

An export that comes back short, or not as a CSV at all, is refused before
anything is written: an empty 202 carries a success status, and a run that
trusted it would overwrite good evidence with nothing and report that all was
well.

Name each export for what it holds: can-*.csv for contract awards, son-*.csv for
standing offers. The export carries no notice-type column, so the filter used to
take it is the only record of what is in it — an unfiltered export mixes awards
and standing offers with no way to tell them apart afterwards, and is refused.

    python nsw.py --fetch          # take the exports, then read them
    python nsw.py --fetch --full   # ask for the whole history, not the window
    python nsw.py                  # read whatever is already in data/nsw/raw/

Exports may also be dropped into data/nsw/raw/ by hand. Overlapping date ranges
are fine, and where the same row appears twice the newest "last updated" wins.

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
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import build
import vic

DATA = os.environ.get("PROCLENS_DATA", "data")
SOURCE = "https://buy.nsw.gov.au/notices/notice-reports"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

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


def key_of(r):
    """A notice ID is not one contract. A standing offer names every supplier on
    it — the export's 2,461 standing-offer rows carry 577 notice IDs between
    them — and award notices do the same where several contractors share one
    notice. Keying on the ID alone silently collapsed those into one. The
    contractor and the start date are what separate them."""
    return "|".join([str(r.get("id") or ""), str(r.get("abn") or r.get("supplier") or ""),
                     str(r.get("start") or "")])

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


# One file per year, dictionary-coded, like every other store here: a 14MB JSON
# rewritten on every export would weigh on the history for no reason.
FIELDS = ["key", "id", "kind", "jurisdiction", "agency", "title", "supplier", "abn", "value",
          "category", "description", "scope", "published", "start", "end", "updated",
          "status", "piggyback", "opportunity_id", "url", "first_seen"]
DICT_FIELDS = ("kind", "jurisdiction", "agency", "category", "description", "status",
               "piggyback", "first_seen")


def encode(rows):
    dicts, lookup = {}, {}
    for f in DICT_FIELDS:
        vals, seen = [], {}
        for r in rows:
            v = r.get(f)
            if v not in seen:
                seen[v] = len(vals)
                vals.append(v)
        dicts[f], lookup[f] = vals, seen
    return dicts, [[lookup[f][r.get(f)] if f in DICT_FIELDS else r.get(f)
                    for f in FIELDS] for r in rows]


def load_store(outdir):
    store = {}
    idx = build.read_json(os.path.join(outdir, "index.json"), {})
    for sh in idx.get("shards", []):
        payload = build.read_json(os.path.join(outdir, sh["file"]), {})
        f, d = payload.get("fields", FIELDS), payload.get("dict", {})
        for row in payload.get("rows", []):
            r = {}
            for i, k in enumerate(f):
                v = row[i] if i < len(row) else None
                if k in d and isinstance(v, int):
                    v = d[k][v]
                r[k] = v
            if r.get("key"):
                store[r["key"]] = r
    return store


KINDS = {"can": "Contract award", "son": "Standing offer"}


def kind_of(path):
    """What an export holds, from the name it was saved under."""
    return KINDS.get(os.path.basename(path).split("-")[0].split(".")[0].lower())


def read_csv(path):
    kind = kind_of(path)
    if not kind:
        print(f"  ! {os.path.basename(path)}: cannot tell what notice type this holds — "
              f"name it can-*.csv or son-*.csv. Skipped.", file=sys.stderr)
        return
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
                "kind": kind,
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


# The export the register publishes for taking itself in bulk is the same form a
# person fills in at SOURCE, with the same button. It carries no token and needs
# no sign-in — a notice type, a date range, and export=1.
# How far back a routine run asks. The register keeps amending notices after it
# publishes them, so a window that only covered the days since the last run
# would take a notice once and never see it corrected. Thirteen months is long
# enough to catch an amendment and short enough to stay a small file.
WINDOW_DAYS = 400

# What a run may ask for. The shield in front of this register counts requests:
# taking the export about ten times in quick succession, while working out what
# the form wanted, moved this machine from answers to an empty 202 challenge on
# every path — including pages a browser on the same address still had. Two
# requests a week is a different thing entirely, and the pause below keeps even
# those from arriving together.
PAUSE = 5.0


def fetch_export(kind, date_from, date_to, timeout=300):
    """One export, as CSV text, or None if what came back was not one.

    What comes back when the shield objects is a 202 with an empty body rather
    than an error, so a run that trusted the status code would write an empty
    file over a good one and report success. Everything here is checked before
    anything is written: the type, the header the register's own export carries,
    and that there are rows under it."""
    body = urllib.parse.urlencode({
        "noticeType": kind, "agencyUUID": "", "category": "", "query": "",
        "dateFrom": date_from, "dateTo": date_to, "export": "1"}).encode()
    req = urllib.request.Request(SOURCE, data=body, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": SOURCE,
        "Accept": "text/csv,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, ctype = r.status, r.headers.get("Content-Type", "")
            text = r.read().decode("utf-8-sig", "replace")
    except Exception as e:
        print(f"  ! {kind}: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if status != 200 or "csv" not in ctype.lower():
        print(f"  ! {kind}: the register answered {status} {ctype or '(no type)'} rather "
              f"than a CSV — it is most likely challenging this client, which it does "
              f"with an empty 202. Nothing written.", file=sys.stderr)
        return None
    head = text.split("\n", 1)[0]
    if "Notice ID" not in head or "Contractor ABN" not in head:
        print(f"  ! {kind}: what came back is not a notice report. Nothing written.",
              file=sys.stderr)
        return None
    rows = sum(1 for _ in csv.DictReader(io.StringIO(text)))
    if rows < 1:
        print(f"  ! {kind}: the export is empty. Nothing written.", file=sys.stderr)
        return None
    return text, rows


def rows_in_raw(path):
    """How many rows an export already on disk holds, for comparison.

    Counted as CSV records rather than lines. A contract description runs to
    several lines inside its quotes — 24 of them in one export of 7,918 — so
    counting lines makes a file look larger than it is, and comparing a line
    count against a record count makes every fresh export look like a loss."""
    if not os.path.exists(path):
        return 0
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8-sig", errors="replace", newline="") as fh:
            if "Notice ID" not in (fh.readline() or ""):
                return 0
            fh.seek(0)
            return sum(1 for _ in csv.DictReader(fh))
    except OSError:
        return 0


def fetch_all(raw_dir, full=False, floor=0.8):
    """Take the exports the register publishes, into raw_dir.

    Awards are taken over a rolling window and standing offers in full, because
    there are only about 2,400 of them and the whole set is a smaller file than
    one year of awards. The historical exports already in raw_dir are left
    alone: they are the evidence for the years they cover, and nothing here
    rewrites them.

    An export holding markedly less than the one it would replace is refused.
    The register answering with a short file is how a silent truncation would
    look, and overwriting good evidence with it is the one thing this must not
    do."""
    today = date.today()
    start = "2000-01-01" if full else (today - timedelta(days=WINDOW_DAYS)).isoformat()
    end = (today + timedelta(days=1)).isoformat()
    wants = [("can", "can-full.csv" if full else "can-recent.csv", start),
             ("son", "son-all.csv", "2000-01-01")]

    took = 0
    for i, (kind, name, since) in enumerate(wants):
        if i:
            time.sleep(PAUSE)
        got = fetch_export(kind, since, end)
        if not got:
            continue
        text, rows = got
        path = os.path.join(raw_dir, name)
        had = rows_in_raw(path) or rows_in_raw(path + ".gz")
        if had and rows < had * floor:
            print(f"  ! {kind}: {rows:,} rows, against {had:,} in the export it would "
                  f"replace. Refusing to overwrite evidence with a short file.",
                  file=sys.stderr)
            continue
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        took += 1
        print(f"  {name}: {rows:,} rows, {since} to {end}"
              f"{f' (was {had:,})' if had else ''}", file=sys.stderr)
    return took


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
    ap.add_argument("--out", default=os.path.join(DATA, "nsw"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fetch", action="store_true",
                    help="take the exports from the register first")
    ap.add_argument("--full", action="store_true",
                    help="with --fetch, ask for the whole history rather than "
                         "the rolling window")
    args = ap.parse_args()

    if os.path.isfile(args.out):
        sys.exit(f"nsw: --out is {args.out}, which is a file. This store is a directory of "
                 f"year shards now; delete the old file or point --out at its directory.")
    os.makedirs(args.raw, exist_ok=True)
    if args.fetch:
        if fetch_all(args.raw, full=args.full) == 0:
            sys.exit("nsw: the register gave nothing this run. The store is unchanged, "
                     "which is the right outcome — but if this repeats, take an export "
                     f"by hand from {SOURCE} and put it in {args.raw}.")
    if not args.dry_run:
        keep_raw(args.raw)
    files = sorted(glob.glob(os.path.join(args.raw, "*.csv.gz")) +
                   glob.glob(os.path.join(args.raw, "*.csv")))
    if not files:
        sys.exit(f"nsw: no exports in {args.raw}. Take one from {SOURCE} — notice type "
                 f"'Contract award', a publish date range, then Export CSV — and put it there.")

    store = load_store(args.out)
    before, seen, superseded = len(store), 0, 0
    for path in files:
        n = 0
        for row in read_csv(path):
            n += 1
            seen += 1
            row["key"] = key_of(row)
            old = store.get(row["key"])
            # Exports overlap by design. The newer "last updated" wins, so a
            # re-pull of the same months corrects rather than duplicates.
            if old and (old.get("updated") or "") > (row.get("updated") or ""):
                superseded += 1
                continue
            row["first_seen"] = (old or {}).get("first_seen") or \
                datetime.now(timezone.utc).date().isoformat()
            store[row["key"]] = row
        print(f"  {os.path.basename(path)}: {n:,} notices", file=sys.stderr)

    rows = sorted(store.values(), key=lambda r: (r.get("published") or "", r.get("id") or ""), reverse=True)
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
                   "distinct_notice_ids": len({r.get("id") for r in rows}),
                   # How current the exports are. The platform cannot be polled,
                   # so this is the only thing that says whether the store has
                   # fallen behind the register.
                   "newest_published": max((r["published"] for r in rows if r.get("published")),
                                           default=None),
                   "rows_read": seen, "older_rows_ignored": superseded,
                   "with_an_abn": with_abn,
                   "by_kind": dict(Counter(r.get("kind") for r in rows).most_common()),
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
    os.makedirs(args.out, exist_ok=True)
    by_year = {}
    for r in rows:
        by_year.setdefault((r.get("published") or "unknown")[:4] or "unknown", []).append(r)
    shards = []
    for year in sorted(by_year):
        name = f"nsw-{year}.json"
        path = os.path.join(args.out, name)
        dicts, enc = encode(by_year[year])
        _, sha = build.write_json_if_changed(path, {"year": year, "fields": FIELDS,
                                                    "dict": dicts, "rows": enc})
        shards.append({"year": year, "file": name, "count": len(by_year[year]),
                       "bytes": os.path.getsize(path), "sha": sha})
    shards.sort(key=lambda x: x["year"], reverse=True)
    payload["shards"] = shards
    payload.pop("contracts", None)
    build.write_json_if_changed(os.path.join(args.out, "index.json"), payload)

    # A roll-up by ABN, so the site can show a supplier's NSW footprint beside
    # its Commonwealth one without fetching the whole register. An ABN is the
    # join: the same number holds contracts in both, which no name match can
    # tell you reliably.
    by = {}
    for r in rows:
        a = r.get("abn")
        if not a:
            continue
        e = by.setdefault(a, {"n": 0, "v": 0.0, "name": r.get("supplier") or "",
                              "agencies": set(), "kinds": set()})
        e["n"] += 1
        e["v"] += r["value"] if isinstance(r.get("value"), (int, float)) else 0
        if r.get("agency"):
            e["agencies"].add(r["agency"])
        if r.get("kind"):
            e["kinds"].add(r["kind"])
    build.write_json_if_changed(os.path.join(args.out, "suppliers.json"), {
        "generated": payload["generated"],
        "note": "NSW contract awards and standing offers rolled up by supplier ABN, so a "
                "supplier's NSW footprint can be shown beside its Commonwealth one. Keyed by "
                "ABN because a name match is not an identity.",
        "fields": ["contracts", "value_aud", "agencies", "name"],
        "suppliers": {a: [e["n"], round(e["v"], 2), len(e["agencies"]), e["name"]]
                      for a, e in sorted(by.items(), key=lambda kv: -kv[1]["v"])},
    })
    kinds = ", ".join(f"{n:,} {k.lower()}s" for k, n in
                      Counter(r.get("kind") for r in rows).most_common())
    summary = (f"**{len(rows):,} NSW notices** ({kinds}; {len(rows) - before:,} new), "
               f"{with_abn:,} with an ABN, ${payload['totals']['value_aud']:,.0f} in all, "
               f"from {len(files)} export(s).")
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(summary + "\n")


if __name__ == "__main__":
    main()
