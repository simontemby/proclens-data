#!/usr/bin/env python3
"""
Victorian government contracts, from the state's own contract publishing system.

This archive is Commonwealth. Victoria publishes its own register at
tenders.vic.gov.au — 2,309 contracts, 306 current and 2,003 expired — and none
of it reaches AusTender. Everything here is labelled Victorian and counted
separately: a state contract is not a Commonwealth one and must never be added
to a Commonwealth total.

What this does NOT take, and why. The platform's robots.txt says:

    Disallow: /contract/view
    Disallow: /tender/view

and those detail pages are exactly where the supplier's name sits. So this
records what the permitted search pages give — contract number, title, status,
start and expiry dates, value — and the buyer, which is recoverable without
touching a disallowed page because the search accepts a buyerId and the form
lists all 577 of them. Each contract keeps the address of its own detail page so
a reader can open it; the archive just does not fetch it.

That leaves a register with no supplier names, which for an archive about who
sells to government is a real gap and is stated as one rather than glossed. The
way to close it is to ask Victoria for the register as data, not to ignore the
file where they asked not to be crawled.

    python vic.py            # update the store
    python vic.py --dry-run  # fetch and report, write nothing
"""
import argparse
import html
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import build

DATA = os.environ.get("PROCLENS_DATA", "data")
SITE = "https://www.tenders.vic.gov.au"
SEARCH = SITE + "/contract/search"
NOTE = ("Contracts published by Victorian government buyers on the state's contract publishing "
        "system. Victoria is not the Commonwealth: these are counted separately and never added "
        "to a Commonwealth total. Supplier names are NOT here. They appear only on the platform's "
        "contract detail pages, which its robots.txt asks crawlers not to fetch, so this archive "
        "does not fetch them; each contract carries the address of its own page instead.")
UA = ("Mozilla/5.0 (compatible; legal-tender-archive/1.0; "
      "+https://simontemby.github.io/proclens-data/)")
# One request every second and a half. At half a second the platform started
# refusing, the retry backoff absorbed it, and the run crawled: fifty buyers in
# the first minute and then nothing for six. This is a small state service, not
# a CDN, and the whole job still finishes inside twenty minutes.
PAUSE = 1.5

# Written as one file per year, dictionary-coded, like every other store here.
# A single 22MB JSON rewritten twice a week would add gigabytes to the history
# in a year; a year shard only changes when that year does.
FIELDS = ["key", "id", "code", "title", "status", "start", "expiry", "value",
          "buyer", "buyer_id", "url", "first_seen", "last_seen"]
DICT_FIELDS = ("status", "buyer", "buyer_id")


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
    """Every shard read back into one dict, keyed as the contracts are keyed."""
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


def session():
    """A polite client that survives the platform hanging up.

    The first full run died on a connection reset two thirds of the way through
    573 buyers and, having kept nothing, lost the lot. It now retries with
    backoff and checkpoints as it goes."""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    retry = Retry(total=3, connect=3, read=3, backoff_factor=2.0,
                  status_forcelist=(429, 500, 502, 503, 504),
                  respect_retry_after_header=True,
                  allowed_methods=frozenset(["GET"]))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=4))
    return s


def text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def buyers(s):
    """Every buyer the search form will filter on, which is how a contract gets
    an agency without opening a page robots.txt closes."""
    page = s.get(SEARCH, timeout=90).text
    m = re.search(r'<select[^>]*name="buyerId"[^>]*>(.*?)</select>', page, re.S)
    if not m:
        sys.exit("vic: no buyer list on the search form — the platform has changed")
    out = {}
    for value, label in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', m.group(1), re.S):
        name = text(label)
        if value.strip().isdigit() and name:
            out[value.strip()] = name
    return out


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def as_date(s):
    """"1 Mar 2027" -> "2027-03-01". Left as written if it is anything else."""
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", str(s or "").strip())
    if not m:
        return None
    mon = MONTHS.get(m.group(2).lower())
    return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}" if mon else None


# A ceiling no Victorian contract plausibly reaches. It exists because the first
# run produced one of $202 sextillion: the parser took the first cell holding a
# dollar sign, which was the title — "...in 2023-24 at a cost of $132,000
# (including GST and contingency), for the period 28 June 2024 to 31 January
# 2025" — and ran every digit in it together. That is the same mistake this
# archive documents agencies making; a guard is cheaper than trusting the parse.
VALUE_CEILING = 100e9


def money(s):
    """One currency amount, read as an amount rather than as loose digits."""
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", str(s or "").replace("\xa0", " "))
    if not m:
        return None
    try:
        v = round(float(m.group(1).replace(",", "")), 2)
    except ValueError:
        return None
    return v if 0 <= v < VALUE_CEILING else None


def rows_on(page_html):
    """The listing table: contract number, title, status, start, expiry, value,
    and the id of the detail page this archive links to but does not fetch."""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, re.S)[1:]:
        cells = [text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        cells = [c for c in cells if c]
        if len(cells) < 3:
            continue
        link = re.search(r'href="(/contract/view\?id=(\d+))"', tr)
        # Columns are read by position, not by hunting for a dollar sign or a
        # date: a title routinely contains both. Value is the last column; the
        # dates sit between the status and it, and expiry is absent on
        # contracts that have none.
        value = money(cells[-1]) if cells else None
        middle = cells[2:-1]
        status = next((c for c in middle if c in ("Current", "Expired")), None)
        dates = [d for d in (as_date(c) for c in middle) if d]
        out.append({
            "id": link.group(2) if link else None,
            "code": cells[0],
            "title": cells[1] if len(cells) > 1 else None,
            "status": status,
            "start": dates[0] if dates else None,
            "expiry": dates[1] if len(dates) > 1 else None,
            "value": value,
            "url": SITE + link.group(1) if link else None,
        })
    return out


def search(s, buyer_id=None, known=None, quiet=True):
    """Every contract the permitted search will list, for one buyer or for all.

    With `known`, stops as soon as a page holds nothing new. Results come newest
    first, so once a page is entirely contracts already held, the pages behind it
    are older still. That turns a weekly update from 80 minutes into about 15,
    and asks this small service for a fraction of what a full sweep does."""
    found, page = {}, 1
    while page <= 200:
        # The search runs only when it is given an order; without one the
        # platform returns the empty form, which read as "no contracts" rather
        # than as a malformed request.
        params = {"page": page, "contractStatus": "",
                  "orderBy": "awardedDate", "desc": "true"}
        if buyer_id:
            params["buyerId"] = buyer_id
        r = s.get(SEARCH, params=params, timeout=90)
        r.raise_for_status()
        rows = rows_on(r.text)
        if not rows:
            break
        fresh = 0
        for row in rows:
            key = row["id"] or f"{row['code']}|{row['title']}"
            if known is not None and key in known:
                continue
            fresh += 1
            found[key] = row
        if known is not None and not fresh:
            break                       # this page, and everything older, is held
        if len(rows) < 25:
            break
        page += 1
        time.sleep(PAUSE)
    return found


def collect(s, checkpoint=None, done=None, rows=None, known=None):
    all_rows = dict(rows or {})
    done = set(done or ())
    if not all_rows and known is None:
        all_rows = search(s)
    print(f"{len(all_rows):,} contracts in the open listing", file=sys.stderr, flush=True)
    names = buyers(s)
    print(f"{len(names):,} buyers to ask", file=sys.stderr, flush=True)
    failed = []
    for i, (bid, name) in enumerate(sorted(names.items()), 1):
        if bid in done:
            continue
        try:
            got = search(s, bid, known=known)
        except Exception as e:                        # noqa: BLE001
            # One buyer failing is not a reason to lose the other 572.
            failed.append((bid, name, str(e)[:80]))
            time.sleep(PAUSE * 4)
            continue
        for key, row in got.items():
            row = all_rows.setdefault(key, row)
            row["buyer"] = name
            row["buyer_id"] = bid
        done.add(bid)
        if i % 25 == 0:
            attributed = sum(1 for r in all_rows.values() if r.get("buyer"))
            print(f"  {i}/{len(names)} buyers · {len(all_rows):,} contracts · "
                  f"{attributed:,} with an agency", file=sys.stderr, flush=True)
            if checkpoint:
                checkpoint(all_rows, done)
        time.sleep(PAUSE)
    for bid, name, why in list(failed):
        try:
            got = search(s, bid, known=known)
        except Exception:                             # noqa: BLE001
            continue
        for key, row in got.items():
            row = all_rows.setdefault(key, row)
            row["buyer"] = name
            row["buyer_id"] = bid
        failed = [f for f in failed if f[0] != bid]
        time.sleep(PAUSE)
    if failed:
        print(f"  ! {len(failed)} buyers could not be read: "
              + ", ".join(n for _, n, _ in failed[:5]), file=sys.stderr)
    return all_rows


def main():
    ap = argparse.ArgumentParser(description="Capture the Victorian contract register.")
    ap.add_argument("--out", default=os.path.join(DATA, "vic"))
    ap.add_argument("--full", action="store_true",
                    help="sweep every page of every buyer, not just what is new. "
                         "A contract already held can change — its value amended, its "
                         "status moved to expired — and only a full sweep sees that.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    today = datetime.now(timezone.utc).date().isoformat()
    s = session()
    part = os.path.join(args.out, "collecting.partial")

    def checkpoint(rows, done):
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(part, "w") as fh:
            json.dump({"contracts": list(rows.values()), "buyers_done": sorted(done)}, fh)

    # A stopped run resumes where it left off rather than asking the platform
    # for everything again.
    prior = build.read_json(part, {}) if not args.dry_run else {}
    resume = {r.get("id") or f"{r.get('code')}|{r.get('title')}": r
              for r in prior.get("contracts", [])}
    if resume:
        print(f"resuming with {len(resume):,} contracts and "
              f"{len(prior.get('buyers_done', [])):,} buyers already read", file=sys.stderr)
    held = load_store(args.out)
    known = None if (args.full or not held) else set(held)
    if known:
        print(f"incremental: {len(known):,} contracts already held; stopping at the "
              f"first page of each buyer that holds nothing new", file=sys.stderr)
    rows = collect(s, checkpoint=None if args.dry_run else checkpoint,
                   done=prior.get("buyers_done"), rows=resume, known=known)
    if not rows and not held:
        sys.exit("vic: nothing returned and nothing held — refusing to write an empty store")
    if not rows:
        print("nothing new on the platform", file=sys.stderr)

    store = dict(held)
    new = 0
    for key, row in rows.items():
        row = dict(row, key=key, jurisdiction="Victoria")
        old = store.get(key)
        row["first_seen"] = (old or {}).get("first_seen") or today
        row["last_seen"] = today
        if not old:
            new += 1
        store[key] = row

    out = sorted(store.values(), key=lambda r: (r.get("start") or "", r.get("code") or ""), reverse=True)
    attributed = sum(1 for r in out if r.get("buyer"))
    vals = [r["value"] for r in out if isinstance(r.get("value"), (int, float))]
    totals = {"contracts": len(out), "new_this_run": new,
              "with_an_agency": attributed,
              "without_an_agency": len(out) - attributed,
              "value_aud": round(sum(vals), 2),
              "by_status": dict(Counter(r.get("status") for r in out).most_common()),
              "by_year": dict(sorted(Counter((r.get("start") or "?")[:4] for r in out).items()))}
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": NOTE,
        "source": SEARCH,
        "fields": FIELDS,
        "totals": totals,
    }
    if args.dry_run:
        print(json.dumps(payload["totals"], indent=1))
        return
    os.makedirs(args.out, exist_ok=True)
    by_year = {}
    for r in out:
        by_year.setdefault((r.get("start") or "unknown")[:4] or "unknown", []).append(r)
    shards = []
    for year in sorted(by_year):
        name = f"vic-{year}.json"
        path = os.path.join(args.out, name)
        dicts, enc = encode(by_year[year])
        _, sha = build.write_json_if_changed(path, {"year": year, "fields": FIELDS,
                                                    "dict": dicts, "rows": enc})
        shards.append({"year": year, "file": name, "count": len(by_year[year]),
                       "bytes": os.path.getsize(path), "sha": sha})
    shards.sort(key=lambda x: x["year"], reverse=True)
    payload["shards"] = shards
    build.write_json_if_changed(os.path.join(args.out, "index.json"), payload)
    if os.path.exists(part):
        os.remove(part)
    summary = (f"**{len(out):,} Victorian contracts** ({new:,} new this run), "
               f"{attributed:,} with an agency. No supplier names: the platform's robots.txt "
               f"closes the pages that carry them.")
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(summary + "\n")
    print(f"done in {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
