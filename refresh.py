#!/usr/bin/env python3
"""
ProcLens batch refresh.

Pulls Commonwealth contract notices from the AusTender OCDS API, normalises them,
computes integrity flags, diffs against the previous run to catch retrospective
edits, and writes a compact bundle the ProcLens front end can fetch.

    python refresh.py --months 24 --out bundle.json

Designed to run unattended on a schedule (cron, systemd timer, GitHub Actions),
commit bundle.json to a public repo, and let the front end read it over HTTPS.

IMPORTANT — these field paths are written from the published API documentation,
not from observed responses. Run `--inspect` first and reconcile before trusting
any output.
"""
import argparse, json, os, sys, time, hashlib
from datetime import date, datetime, timedelta, timezone
from collections import Counter

import requests

BASE = os.environ.get("AUSTENDER_BASE", "https://api.tenders.gov.au/ocds")
TOKEN = os.environ.get("AUSTENDER_TOKEN")
UA = os.environ.get("PROCLENS_UA", "ProcLens/0.2 (procurement transparency research)")
WINDOW_DAYS = 7          # the API is documented and used with roughly weekly windows
PAUSE = 1.0              # polite delay between requests
TIMEOUT = 60
RETRIES = 4

FIELDS = ["id","jur","ocid","cn","title","buyer","supplier","abn","value","value_orig",
          "pub","signed","start","end","method","cat","amendments","flags","url"]

# Commonwealth Procurement Rules reporting window for contract notices.
PUBLISH_DEADLINE_DAYS = 42
# Values sitting just below a threshold are worth surfacing; repeated near-misses
# can indicate contract splitting. Confirm current figures before relying on these.
THRESHOLDS = [10_000, 80_000, 400_000, 7_500_000]
THRESHOLD_BAND = 0.05
VALUE_GROWTH_PCT = 50.0
LONG_TERM_DAYS = 5 * 365
BACKDATE_DAYS = 30
LIMITED_TOKENS = ("limited", "direct", "sole", "single", "select", "restricted")


# ---------------------------------------------------------------- http

def get(path, params=None):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    url = path if path.startswith("http") else f"{BASE}{path}"
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            if attempt == RETRIES - 1:
                raise
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt * 2)
            continue
        if r.status_code in (401, 403):
            sys.exit(f"AusTender returned {r.status_code}. Set AUSTENDER_TOKEN and retry.")
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"gave up on {url}")


def api_ts(d):
    """AusTender rejects plain dates.

    Boundaries must be ISO 8601 UTC to the second: YYYY-MM-DDTHH:MM:SSZ.
    A bare YYYY-MM-DD returns 400 errorCode 102, which is not retryable.
    """
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def windows(start, end, days=WINDOW_DAYS):
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=days), end)
        yield cur, nxt
        cur = nxt


def releases(since, until):
    """Yield OCDS releases modified in the period.

    Sync on lastModified rather than published: that window also catches
    amendments and after-the-fact corrections, which published-date misses.
    """
    for a, b in windows(since, until):
        path = f"/findByDates/contractLastModified/{api_ts(a)}/{api_ts(b)}"
        page, guard = path, 0
        while page and guard < 400:
            data = get(page)
            for pkg in data.get("releases", []):
                yield pkg
            nxt = (data.get("links") or {}).get("next")
            page = nxt if nxt and nxt != page else None
            guard += 1
            time.sleep(PAUSE)
        print(f"  {a} → {b}", file=sys.stderr)


# ---------------------------------------------------------------- mapping

def dig(o, *path, default=None):
    for p in path:
        if isinstance(o, list):
            o = o[p] if isinstance(p, int) and len(o) > p else default
        elif isinstance(o, dict):
            o = o.get(p, default)
        else:
            return default
        if o is None:
            return default
    return o


def as_date(v):
    if not v:
        return None
    return str(v)[:10]


def as_money(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def party_index(rel):
    """id -> party. Supplier ABN and buyer name live in parties[], not on the award."""
    return {p.get("id"): p for p in (rel.get("parties") or []) if isinstance(p, dict)}


def abn_of(party):
    """ABN is an additionalIdentifiers entry with scheme AU-ABN, not identifier.id."""
    for ident in (party or {}).get("additionalIdentifiers") or []:
        if str(ident.get("scheme") or "").upper() == "AU-ABN":
            return str(ident.get("id") or "").replace(" ", "")
    return ""


def buyer_of(rel):
    """parties[] is unordered and parties[0] is often the supplier, so pick by role."""
    for p in rel.get("parties") or []:
        roles = [str(x).lower() for x in (p.get("roles") or [])]
        if "procuringentity" in roles or "buyer" in roles:
            return str(p.get("name") or "").strip()
    return str(dig(rel, "buyer", "name") or "").strip()


def to_row(rel):
    """Map one OCDS release to the bundle row order."""
    ocid = rel.get("ocid")
    award = dig(rel, "awards", 0, default={}) or {}
    contract = dig(rel, "contracts", 0, default={}) or {}
    tender = rel.get("tender") or {}
    supplier = dig(award, "suppliers", 0, default={}) or {}
    parties = party_index(rel)
    supplier_party = parties.get(supplier.get("id")) or {}

    cn = contract.get("id") or award.get("id") or rel.get("id") or ocid
    value = as_money(dig(contract, "value", "amount")) or as_money(dig(award, "value", "amount"))
    # AusTender publishes amendments as separate releases; the original value is
    # carried on the parent. Reconciled in roll_up() below, not here.
    period = contract.get("period") or award.get("contractPeriod") or {}

    return {
        "id": f"austender:{cn}",
        "jur": "CTH",
        "cn": cn,
        "ocid": ocid,
        # contracts[].title is the agency's internal PO reference ("4600094809");
        # contracts[].description carries the actual subject. Prefer the subject.
        "title": (contract.get("description") or contract.get("title")
                  or award.get("title") or tender.get("title") or "").strip(),
        "buyer": buyer_of(rel),
        "supplier": (supplier.get("name") or "").strip(),
        "abn": abn_of(supplier_party),
        "value": value,
        "value_orig": value,
        "pub": as_date(rel.get("date")),
        "start": as_date(period.get("startDate")),
        "end": as_date(period.get("endDate")),
        "signed": as_date(contract.get("dateSigned")),
        "method": (tender.get("procurementMethodDetails") or tender.get("procurementMethod") or "").strip(),
        # Items hang off contracts[], not awards[], and the classification carries a
        # UNSPSC code with no description field.
        "cat": str(dig(contract, "items", 0, "classification", "id")
                   or tender.get("mainProcurementCategory") or "").strip(),
        "url": f"https://www.tenders.gov.au/Cn/Show/{cn}",
    }


def roll_up(rows):
    """Collapse a contracting process to one current row, keeping the original value.

    Never aggregate on parent value alone — amendments carry a large share of
    total committed value. The OCID is the process key.
    """
    by_ocid = {}
    for r in rows:
        key = r.get("ocid") or r["id"]
        by_ocid.setdefault(key, []).append(r)
    out = []
    for key, group in by_ocid.items():
        group.sort(key=lambda r: (r.get("pub") or "", r.get("cn") or ""))
        first, last = group[0], group[-1]
        row = dict(last)
        row["value_orig"] = first.get("value")
        row["amendments"] = len(group) - 1
        out.append(row)
    return out


# ---------------------------------------------------------------- flags

def flag(r):
    f = []
    pub, start, end = r.get("pub"), r.get("start"), r.get("signed") or r.get("start")
    if pub and end:
        lag = (date.fromisoformat(pub) - date.fromisoformat(end)).days
        if lag > PUBLISH_DEADLINE_DAYS:
            f.append("late_publish")
    v, vo = r.get("value"), r.get("value_orig")
    if v and vo and vo > 0 and (v / vo - 1) * 100 > VALUE_GROWTH_PCT:
        f.append("value_growth")
    if v:
        for t in THRESHOLDS:
            if t * (1 - THRESHOLD_BAND) <= v < t:
                f.append("threshold_hugging")
                break
    if r.get("signed") and start and \
       (date.fromisoformat(r["signed"]) - date.fromisoformat(start)).days > BACKDATE_DAYS:
        f.append("backdated")
    if any(t in (r.get("method") or "").lower() for t in LIMITED_TOKENS):
        f.append("limited_tender")
    if start and r.get("end"):
        try:
            if (date.fromisoformat(r["end"]) - date.fromisoformat(start)).days > LONG_TERM_DAYS:
                f.append("long_term")
        except ValueError:
            pass
    return ",".join(f)


# ---------------------------------------------------------------- diffing

WATCHED = ("value", "supplier", "abn", "end", "method", "title")

def diff(previous, rows):
    """Detect retrospective edits.

    AusTender records mutate silently after publication. A weekly overwrite
    destroys the evidence; comparing successive snapshots preserves it, and the
    edit is itself the finding.
    """
    if not previous:
        return []
    idx = {f: i for i, f in enumerate(previous.get("fields", FIELDS))}
    old = {r[idx["id"]]: r for r in previous.get("rows", [])}
    changes = []
    for r in rows:
        o = old.get(r["id"])
        if not o:
            continue
        for field in WATCHED:
            if field not in idx:
                continue
            before, after = o[idx[field]], r.get(field)
            if before != after and before not in (None, ""):
                changes.append({"id": r["id"], "cn": r.get("cn"), "field": field,
                                "from": before, "to": after,
                                "seen": datetime.now(timezone.utc).date().isoformat()})
    return changes


# ---------------------------------------------------------------- main

SLOT = "<!--PROCLENS_BUNDLE_SLOT-->"

def embed(template, bundle, out_json):
    """Write a self-contained HTML build with the data inlined.

    Removes the network dependency entirely: the result opens from a file, a
    static host, or a pasted Claude artifact with no fetch and no CORS.
    """
    with open(template) as fh:
        html = fh.read()
    if SLOT not in html:
        print(f"{template} has no {SLOT} marker; skipping embed.", file=sys.stderr)
        return
    payload = json.dumps(bundle, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace(SLOT, f"<script>window.__PROCLENS_BUNDLE__={payload}</script>")
    dest = os.path.splitext(out_json)[0] + ".html"
    with open(dest, "w") as fh:
        fh.write(html)
    print(f"Wrote {dest}: {os.path.getsize(dest)/1e6:.1f} MB self-contained",
          file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Refresh the ProcLens bundle.")
    ap.add_argument("--months", type=int, default=24,
                    help="rolling window to keep in the bundle (default 24)")
    ap.add_argument("--out", default="bundle.json")
    ap.add_argument("--max-rows", type=int, default=60000,
                    help="cap on bundle size; the front end holds it all in memory")
    ap.add_argument("--inspect", action="store_true",
                    help="print the fields actually present in live responses and exit")
    ap.add_argument("--embed", metavar="TEMPLATE",
                    help="also write a self-contained HTML build with the data inlined, "
                         "using TEMPLATE (proclens.html) as the shell")
    args = ap.parse_args()

    until = date.today()
    since = until - timedelta(days=args.months * 31)

    if args.inspect:
        seen, n = Counter(), 0
        for rel in releases(until - timedelta(days=7), until):
            def walk(o, prefix=""):
                if isinstance(o, dict):
                    for k, v in o.items():
                        seen[f"{prefix}{k}"] += 1
                        walk(v, f"{prefix}{k}.")
                elif isinstance(o, list) and o:
                    walk(o[0], f"{prefix}0.")
            walk(rel)
            n += 1
            if n >= 50:
                break
        print(f"# {n} releases sampled\n")
        for k, c in seen.most_common():
            print(f"{c:5d}  {k}")
        print("\nReconcile these against to_row() before trusting any output.", file=sys.stderr)
        return

    print(f"Fetching {since} → {until}", file=sys.stderr)
    raw = [to_row(rel) for rel in releases(since, until)]
    print(f"{len(raw)} releases", file=sys.stderr)

    rows = roll_up(raw)
    for r in rows:
        r["flags"] = flag(r)
    rows.sort(key=lambda r: r.get("pub") or "", reverse=True)
    rows = rows[:args.max_rows]

    previous = None
    if os.path.exists(args.out):
        try:
            with open(args.out) as fh:
                previous = json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Previous bundle unreadable ({e}); skipping diff.", file=sys.stderr)

    changes = diff(previous, rows)
    if previous:
        changes = (previous.get("changes") or [])[-5000:] + changes

    bundle = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "austender",
        "note": f"Commonwealth contract notices, {since} to {until}. "
                "Values are committed at award, not amounts paid.",
        "window": {"from": since.isoformat(), "to": until.isoformat()},
        "fields": FIELDS,
        "rows": [[r.get(f) for f in FIELDS] for r in rows],
        "changes": changes,
    }

    tmp = args.out + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(bundle, fh, separators=(",", ":"))
    os.replace(tmp, args.out)

    if args.embed:
        embed(args.embed, bundle, args.out)

    size = os.path.getsize(args.out) / 1e6
    flagged = sum(1 for r in rows if r["flags"])
    print(f"Wrote {args.out}: {len(rows)} contracts, {flagged} flagged, "
          f"{len(changes)} recorded edits, {size:.1f} MB", file=sys.stderr)
    if size > 12:
        print("Bundle is large. Narrow --months or --max-rows; the browser loads it whole.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
