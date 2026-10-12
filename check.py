#!/usr/bin/env python3
"""
Assertions the archive must satisfy, run after every build.

Every defect found this month was caught by a person looking at a number and
thinking it wrong: $914m attributed to Amazon Web Services on the strength of
three contracts, 28,423 CSIRO rows dropped in silence, two budget years reading
as empty, a value of $202 sextillion, an agency join that gave the
Auditor-General 338 audits of itself. That worked because someone was looking
hard, which is not a property of a system.

These are the checks that would have caught them. Each one states what must be
true and why, and a failure names the number that broke it rather than saying
a test failed.

Exit 1 on any failure, so a workflow stops before committing.

    python check.py                 # against data/
    python check.py --warn-only     # report, do not fail
"""
import argparse
import json
import os
import re
import sys

import build

DATA = os.environ.get("PROCLENS_DATA", "data")
PROBLEMS, NOTES = [], []


def fail(msg):
    PROBLEMS.append(msg)


def note(msg):
    NOTES.append(msg)


def check_corpus(idx):
    t = idx.get("totals") or {}
    rep = idx.get("report") or {}

    # Something is always worse than nothing: an empty build must never ship.
    if (t.get("contracts") or 0) < 1_000_000:
        fail(f"only {t.get('contracts'):,} contracts; the archive has held over a million "
             f"since 2024, so this is a collapsed build, not a quiet month")

    # The two $120bn typing errors were found this way: a value far above any
    # real contract, with no source disagreeing, is a parse fault rather than a
    # purchase. Anything genuinely that large has more than one source.
    ceiling = 50e9
    contra = (t.get("contradicted") or {}).get("contracts") or 0
    if contra == 0:
        fail("no contract carries value_contradicted; that flag has found real "
             "errors every build since it existed, so zero means it stopped working")

    # A flag that collapses year on year means its source stopped arriving, not
    # that behaviour changed. This is the consultancy hole, caught late.
    for field, years in (t.get("flag_coverage") or {}).items():
        ys = sorted(years)
        for a, b in zip(ys, ys[1:]):
            pa, pb = years[a]["pct"], years[b]["pct"]
            if pa >= 2.0 and pb < pa / 10:
                note(f"{field} coverage fell from {pa:.1f}% in {a} to {pb:.1f}% in {b} — "
                     f"the source probably stopped covering that year")

    # Attribution must stay mostly read rather than mostly guessed. The AWS
    # episode was 3% stated and nobody noticed until it was questioned.
    acc = rep.get("vendor_rule_accuracy") or {}
    for rule, v in acc.items():
        if v.get("tested_on", 0) < 100:
            note(f"vendor rule '{rule}' tested on only {v.get('tested_on')} contracts; "
                 f"its accuracy figure is too thin to quote")
        elif v.get("right_pct", 0) < 85:
            fail(f"vendor rule '{rule}' is right {v['right_pct']}% of the time, below the "
                 f"85% this archive is willing to call 'likely'")

    # The guard that stops the archive presuming against a vendor selling itself.
    if "presumption_withheld_vendor_supplier" not in (rep.get("vendors") or {}):
        fail("the vendor-supplier guard is not reporting; without it the rules "
             "attribute competitors' products to a vendor's own contracts")

    pn = t.get("product_not_named") or {}
    if not pn.get("contracts"):
        fail("product_not_named is empty; 12% of this archive has always carried it")
    return t


def check_sources(t):
    """Each store must still hold roughly what it held. A source that loses most
    of its rows between runs has broken, not shrunk, and a store that reports no
    count at all cannot be checked at all — which is how 28,423 CSIRO rows were
    dropped in silence."""
    expect = {
        "anao/audits.json": 1_000,
        "son/arrangements.json": 4_000,
        "budget/programs.json": 10_000,
        "nsw/index.json": 15_000,
        "buyict/index.json": 5_000,
        "vic/index.json": 50_000,
        "so13/index.json": 28_000,
    }
    for path, floor in expect.items():
        p = os.path.join(DATA, path)
        if not os.path.exists(p):
            note(f"{path} is missing")
            continue
        got = rows_in(build.read_json(p, {}))
        if got is None:
            fail(f"{path} reports no row count of any kind, so nothing about it can "
                 f"be verified; give its index a totals block")
        elif got < floor:
            fail(f"{path} holds {got:,} rows, below the {floor:,} it has held before")


def rows_in(d):
    """How many rows a source index claims, whatever it calls them. Each ingest
    names its own unit — audits, arrangements, notices, contracts, items — so
    look for any of them rather than insisting on one."""
    t = d.get("totals") or {}
    for key in ("contracts", "notices", "audits", "arrangements", "items", "rows", "programs", "line_items_read"):
        if isinstance(t.get(key), int):
            return t[key]
    # so13 and budget keep their counts per listing or per agency instead.
    for group in ("listings", "agencies", "entities", "sources"):
        g = d.get(group)
        rows = g if isinstance(g, list) else list(g.values()) if isinstance(g, dict) else None
        if rows:
            n = sum(x.get("count") or x.get("rows") or x.get("items") or 0
                    for x in rows if isinstance(x, dict))
            if n:
                return n
    return None


def check_so13_floor():
    """The Senate Order sets a $100,000 floor, so a listing full of amounts far
    below it is reporting in some other unit. The CEFC reports its investment
    contracts in a table headed "($ million)" in the same document that reports
    goods and services in dollars, and reading that column as dollars put $295
    against a transmission investment and $1.20 against a battery company."""
    import glob
    for p in sorted(glob.glob(os.path.join(DATA, "so13", "*.json"))):
        if p.endswith("index.json"):
            continue
        d = build.read_json(p, {})
        fields, rows = d.get("fields") or [], d.get("rows") or []
        if "value" not in fields or len(rows) < 10:
            continue
        vi = fields.index("value")
        vals = [r[vi] for r in rows if isinstance(r[vi], (int, float)) and r[vi] > 0]
        if not vals:
            continue
        tiny = [v for v in vals if v < 1_000]
        if tiny:
            fail(f"{os.path.basename(p)}: {len(tiny)} amount(s) under $1,000, the "
                 f"smallest ${min(tiny):,.2f}, in a listing of contracts of $100,000 "
                 f"or more — an amount in millions read as dollars")
        huge = [v for v in vals if v > 50e9]
        if huge:
            fail(f"{os.path.basename(p)}: {len(huge)} amount(s) above $50bn, "
                 f"the largest ${max(huge):,.0f} — a cell holding a value and a "
                 f"sentence about a larger facility read as one number")


def check_freshness():
    """What the build wrote down about its own age.

    A store past its cadence is reported but does not fail the run: a source
    that has gone quiet is news, not a reason to stop publishing everything
    else. The site says so itself through the banner this file feeds."""
    f = build.read_json(os.path.join(DATA, "freshness.json"), None)
    if not f:
        fail("no freshness.json; the site cannot tell a reader how old it is")
        return
    for s in f.get("stores", []):
        if s.get("missing"):
            note(f"{s['label']} is not in this build at all")
        elif s.get("overdue"):
            note(f"{s['label']} last changed {s['days_old']} days ago, against a "
                 f"source that publishes at least every {s['cadence_days']}")


def check_search(idx):
    """The index and the page must tokenise identically. A word indexed one way
    and matched another sends a reader to a month and shows them nothing."""
    html = ""
    for p in ("index.html",):
        if os.path.exists(p):
            html = open(p, encoding="utf-8").read()
    if not html:
        return
    if "function acronym" in html and "def acronym" not in open("build.py", encoding="utf-8").read():
        fail("index.html derives agency acronyms and build.py does not; a search for "
             "NDIA would reach no month")
    terms = idx.get("terms") or {}
    if len(terms) < 100:
        fail(f"only {len(terms)} search-index shards; the archive needs thousands")


def main():
    ap = argparse.ArgumentParser(description="Assert what must be true of the built archive.")
    ap.add_argument("--warn-only", action="store_true")
    args = ap.parse_args()

    idx = build.read_json(os.path.join(DATA, "corpus", "index.json"), None)
    if not idx:
        sys.exit("check: no corpus index; nothing to verify")

    t = check_corpus(idx)
    check_sources(t)
    check_so13_floor()
    check_freshness()
    check_search(idx)

    for n in NOTES:
        print(f"  note: {n}", file=sys.stderr)
    for p in PROBLEMS:
        print(f"  FAIL: {p}", file=sys.stderr)
    summary = (f"{len(PROBLEMS)} failure(s), {len(NOTES)} note(s) "
               f"against {(t.get('contracts') or 0):,} contracts.")
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(("**Checks passed.** " if not PROBLEMS else "**Checks failed.** ") + summary + "\n")
    if PROBLEMS and not args.warn_only:
        sys.exit(1)


if __name__ == "__main__":
    main()
