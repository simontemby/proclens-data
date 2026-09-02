#!/usr/bin/env python3
"""
Legal Tender — AusTender archive builder.

Builds and maintains a permanent, growing archive of Commonwealth contract
notices, sharded by publication month.

    python refresh.py --backfill-from 2021-09-01   # one-time deep build
    python refresh.py --since-days 21              # weekly top-up

WHAT THIS CAN AND CANNOT KNOW — read before trusting a figure.

The AusTender OCDS API exposes only the CURRENT state of each contract. There
is no amendment-history endpoint (findByOCID, /record and /releases are not
routes). Each contracting process appears at most once per query window, so a
single pull can never reveal what a contract was worth before it was amended.

Consequently:
  * A backfill gives five years of COVERAGE, at today's values.
  * Original values are only knowable for contracts first seen AFTER this
    archive started. That is what value_first and first_seen record.
  * value_growth therefore only fires on growth this archive actually observed.

The archive never deletes. A contract that disappears from AusTender keeps its
last observed state and stops advancing last_seen, which is how withdrawal
becomes visible instead of silent.

Two date axes, used for different jobs:
  contractPublished     — stable window membership, works back to at least 2021.
                          Used for backfill.
  contractLastModified  — catches amendments, but returns "no records" for
                          windows older than roughly two years. Used weekly.
"""
import argparse, json, os, re, sys, time
from datetime import date, datetime, timedelta, timezone
from collections import Counter, defaultdict

import requests

BASE = os.environ.get("AUSTENDER_BASE", "https://api.tenders.gov.au/ocds")
TOKEN = os.environ.get("AUSTENDER_TOKEN")
UA = os.environ.get("PROCLENS_UA", "LegalTender/1.0 (procurement transparency research)")
DATA_DIR = os.environ.get("PROCLENS_DATA", "data")
WINDOW_DAYS = 7
PAUSE = 0.6
TIMEOUT = 60
RETRIES = 4

# id and url are omitted deliberately: both are derivable from cn, and at roughly
# 64 bytes a row they would add ~17% to an archive already near 100 MB.
FIELDS = ["ocid", "cn", "title", "buyer", "supplier", "abn", "value",
          "value_first", "cur", "pub", "signed", "start", "end", "method", "cat",
          "amendments", "first_seen", "last_seen", "flags"]

# Fields whose change between observations is itself the finding.
WATCHED = ("value", "supplier", "abn", "end", "start", "method", "title", "buyer")

PUBLISH_DEADLINE_DAYS = 42
THRESHOLDS = [10_000, 80_000, 400_000, 7_500_000]
THRESHOLD_BAND = 0.05
VALUE_GROWTH_PCT = 50.0
LONG_TERM_DAYS = 5 * 365
BACKDATE_DAYS = 30
LIMITED_TOKENS = ("limited", "direct", "sole", "single", "select", "restricted")

# Supplier names that carry a second entity inside them: agents, trustees and
# trading names. These are a principal reason spend hides under another vendor.
AGENT_RE = re.compile(
    r"\b(a/c|acting as|on behalf of|as agent|as agen|t/a|t/as|trading as|atf|"
    r"as trustee|the trustee for)\b", re.I)

# Platforms, marketplaces and volume resellers. A contract with one of these
# names the CHANNEL, not the capability bought through it: software acquired via
# a cloud marketplace or a reseller agreement leaves no notice naming the actual
# vendor. Flagging them does not imply anything improper — it marks the records
# where a question about "who really supplied this" cannot be answered from
# AusTender alone, and where an FOI for drawdowns and order forms is the next step.
PLATFORM_RE = re.compile(
    r"\b(amazon web services|\baws\b|microsoft|azure|google cloud|\bgcp\b|snowflake|"
    r"databricks|salesforce|servicenow|oracle|\bsap\b|vmware|palantir|"
    r"data ?#? ?3|datacom|dxc|kyndryl|insight enterprises|softwareone|softwareone|"
    r"crayon|rhipe|dicker data|ingram micro|synnex|cdw|shi |sos recruitment|"
    r"telstra purple|kinetic it|atturra|versent|deloitte|accenture|kpmg|"
    r"pricewaterhousecoopers|\bpwc\b|ernst & young|\bey\b|mckinsey|boston consulting)\b",
    re.I)


# ---------------------------------------------------------------- http

def get(path, params=None):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    url = path if path.startswith("http") else f"{BASE}{path}"
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException:
            if attempt == RETRIES - 1:
                raise
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt * 2)
            continue
        if r.status_code in (401, 403):
            sys.exit(f"AusTender returned {r.status_code}. Set AUSTENDER_TOKEN and retry.")
        # An empty window is reported as 400 errorCode 100, not as an empty 200.
        # Treating it as fatal kills a run on any quiet week, and on every window
        # older than the contractLastModified horizon.
        if r.status_code == 400:
            try:
                body = r.json()
            except ValueError:
                body = {}
            if body.get("errorCode") in (100, "100"):
                return {"releases": []}
            sys.exit(f"AusTender rejected {url}: {body or r.text[:200]}")
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"gave up on {url}")


def api_ts(d):
    """Boundaries must be ISO 8601 UTC to the second; a bare date returns 400."""
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def windows(start, end, days=WINDOW_DAYS):
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=days), end)
        yield cur, nxt
        cur = nxt


def releases(since, until, axis="contractPublished", quiet=False):
    """Yield OCDS releases in a period, following cursor pagination."""
    for a, b in windows(since, until):
        path = f"/findByDates/{axis}/{api_ts(a)}/{api_ts(b)}"
        page, guard, n = path, 0, 0
        while page and guard < 400:
            data = get(page)
            batch = data.get("releases", [])
            n += len(batch)
            for pkg in batch:
                yield pkg
            nxt = (data.get("links") or {}).get("next")
            page = nxt if nxt and nxt != page else None
            guard += 1
            if page:
                time.sleep(PAUSE)
        if not quiet:
            print(f"  {a} → {b}  {n}", file=sys.stderr)


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
    return str(v)[:10] if v else None


def as_money(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def party_index(rel):
    return {p.get("id"): p for p in (rel.get("parties") or []) if isinstance(p, dict)}


def abn_of(party):
    """ABN is an additionalIdentifiers entry with scheme AU-ABN."""
    for ident in (party or {}).get("additionalIdentifiers") or []:
        if str(ident.get("scheme") or "").upper() == "AU-ABN":
            return str(ident.get("id") or "").replace(" ", "")
    return ""


def buyer_of(rel):
    """parties[] is unordered and parties[0] is often the supplier."""
    for p in rel.get("parties") or []:
        roles = [str(x).lower() for x in (p.get("roles") or [])]
        if "procuringentity" in roles or "buyer" in roles:
            return str(p.get("name") or "").strip()
    return str(dig(rel, "buyer", "name") or "").strip()


def to_row(rel):
    """Map one OCDS release to a bundle row."""
    ocid = rel.get("ocid")
    award = dig(rel, "awards", 0, default={}) or {}
    contract = dig(rel, "contracts", 0, default={}) or {}
    tender = rel.get("tender") or {}
    supplier = dig(award, "suppliers", 0, default={}) or {}
    supplier_party = party_index(rel).get(supplier.get("id")) or {}

    cn = contract.get("id") or award.get("id") or rel.get("id") or ocid
    value = as_money(dig(contract, "value", "amount"))
    if value is None:
        value = as_money(dig(award, "value", "amount"))
    period = contract.get("period") or award.get("contractPeriod") or {}

    return {
        "ocid": ocid,
        "cn": cn,
        # contracts[].title is the agency's internal PO reference ("4600094809");
        # contracts[].description carries the actual subject.
        "title": (contract.get("description") or contract.get("title")
                  or tender.get("title") or "").strip(),
        "buyer": buyer_of(rel),
        "supplier": (supplier.get("name") or "").strip(),
        "abn": abn_of(supplier_party),
        "value": value,
        # Currency is carried so mixed-currency rows are never silently summed.
        "cur": (dig(contract, "value", "currency") or "AUD").upper(),
        "pub": as_date(rel.get("date")),
        "signed": as_date(contract.get("dateSigned")),
        "start": as_date(period.get("startDate")),
        "end": as_date(period.get("endDate")),
        "method": (tender.get("procurementMethodDetails")
                   or tender.get("procurementMethod") or "").strip(),
        # Items hang off contracts[]; the classification is a UNSPSC code.
        "cat": str(dig(contract, "items", 0, "classification", "id")
                   or tender.get("mainProcurementCategory") or "").strip(),
    }


# ---------------------------------------------------------------- flags

def flag(r):
    f = []
    pub, start = r.get("pub"), r.get("start")
    signed = r.get("signed") or start
    if pub and signed:
        try:
            if (date.fromisoformat(pub) - date.fromisoformat(signed)).days > PUBLISH_DEADLINE_DAYS:
                f.append("late_publish")
        except ValueError:
            pass
    v, vf = r.get("value"), r.get("value_first")
    # Only ever fires on growth this archive observed; pre-archive amendments
    # are not recoverable from the API.
    if v and vf and vf > 0 and (v / vf - 1) * 100 > VALUE_GROWTH_PCT:
        f.append("value_growth")
    if v:
        for t in THRESHOLDS:
            if t * (1 - THRESHOLD_BAND) <= v < t:
                f.append("threshold_hugging")
                break
    if r.get("signed") and start:
        try:
            if (date.fromisoformat(r["signed"]) - date.fromisoformat(start)).days > BACKDATE_DAYS:
                f.append("backdated")
        except ValueError:
            pass
    if any(t in (r.get("method") or "").lower() for t in LIMITED_TOKENS):
        f.append("limited_tender")
    if start and r.get("end"):
        try:
            if (date.fromisoformat(r["end"]) - date.fromisoformat(start)).days > LONG_TERM_DAYS:
                f.append("long_term")
        except ValueError:
            pass
    if AGENT_RE.search(r.get("supplier") or ""):
        f.append("agent_or_trustee")
    if PLATFORM_RE.search(r.get("supplier") or ""):
        f.append("platform_or_reseller")
    return ",".join(f)


# ---------------------------------------------------------------- store

def shard_key(r):
    """Shard by month.

    Year shards would be ~20 MB and rewritten every week, so git would carry a
    gigabyte a year. A month shard is ~1.7 MB and, once its month has passed,
    usually does not change at all — so it is written once and never again.
    Month granularity also lets the front end fetch exactly the date range asked
    for instead of a whole year.
    """
    p = r.get("pub") or r.get("signed") or r.get("start") or ""
    return p[:7] if len(p) >= 7 and p[:4].isdigit() else "unknown"


def load_base(datadir):
    """Read the immutable month shards: each row as first archived."""
    base = {}
    if not os.path.isdir(datadir):
        return base
    for name in sorted(os.listdir(datadir)):
        if not (name.startswith("contracts-") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(datadir, name)) as fh:
                b = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ! {name} unreadable ({e}); skipped", file=sys.stderr)
            continue
        for r in decode_shard(b):
            key = r.get("ocid") or r.get("cn")
            if key:
                base[key] = r
    return base


def load_updates(datadir):
    """Rows whose current state has diverged from the base shard."""
    u = read_json(os.path.join(datadir, "updates.json"), {})
    fields = u.get("fields", FIELDS)
    out = {}
    for row in u.get("rows", []):
        r = dict(zip(fields, row))
        key = r.get("ocid") or r.get("cn")
        if key:
            out[key] = r
    return out


def load_store(datadir):
    """Current truth = immutable base, overlaid with accumulated updates.

    Splitting the two is what keeps this affordable. A week's amendments touch
    contracts published across dozens of past months; rewriting each of those
    shards would push ~95 MB of new objects into git every week. Instead the
    month shard is written once and never touched again, and everything that
    changed afterwards lives in one small overlay file.

    It is also the more honest structure: the shard is what AusTender said when
    we first saw the contract, and the overlay is what changed since.
    """
    base = load_base(datadir)
    store = dict(base)
    store.update(load_updates(datadir))
    return store, base


def merge(store, rows, today):
    """Fold newly observed rows into the archive. Never deletes.

    Identity is the OCID, the contracting process, so an amendment arriving in a
    later pull updates the original row rather than creating a duplicate.
    """
    changes, added, updated = [], 0, 0
    for r in rows:
        key = r.get("ocid") or r.get("cn")
        if not key:
            continue
        old = store.get(key)
        if old is None:
            r["first_seen"] = today
            r["last_seen"] = today
            r["value_first"] = r.get("value")
            r["amendments"] = 0
            store[key] = r
            added += 1
            continue
        # Carry archive-only provenance forward.
        r["first_seen"] = old.get("first_seen") or today
        r["last_seen"] = today
        r["value_first"] = old.get("value_first", old.get("value"))
        r["amendments"] = old.get("amendments") or 0
        diffs = [f for f in WATCHED
                 if old.get(f) != r.get(f) and old.get(f) not in (None, "")]
        if diffs:
            if "value" in diffs:
                r["amendments"] = (old.get("amendments") or 0) + 1
            for f in diffs:
                changes.append({"ocid": key, "cn": r.get("cn"), "field": f,
                                "from": old.get(f), "to": r.get(f), "seen": today})
            updated += 1
        store[key] = r
    return changes, added, updated


def build_suppliers(store):
    """Roll spend up by ABN, not by name.

    The same entity is published under several spellings — one ABN carrying
    three names is common — so a name-keyed total undercounts it.
    """
    by_abn = defaultdict(lambda: {"names": Counter(), "n": 0, "total": 0.0,
                                  "agent": False, "buyers": set()})
    for r in store.values():
        abn = (r.get("abn") or "").strip()
        if not abn:
            continue
        s = by_abn[abn]
        nm = (r.get("supplier") or "").strip()
        if nm:
            s["names"][nm] += 1
        s["n"] += 1
        if r.get("cur", "AUD") == "AUD" and r.get("value"):
            s["total"] += r["value"]
        if AGENT_RE.search(nm):
            s["agent"] = True
        if r.get("buyer"):
            s["buyers"].add(r["buyer"])
    out = []
    for abn, s in by_abn.items():
        names = [n for n, _ in s["names"].most_common()]
        out.append({"abn": abn, "canonical": names[0] if names else "",
                    "names": names, "n": s["n"], "total": round(s["total"], 2),
                    "agent": s["agent"], "buyers": len(s["buyers"])})
    out.sort(key=lambda x: x["total"], reverse=True)
    return out


def save_store(store, base, datadir, changes, meta):
    os.makedirs(datadir, exist_ok=True)
    for r in store.values():
        r["flags"] = flag(r)

    # Rows absent from the base are new to the archive and must be written into
    # their month shard. Everything else stays where it is.
    new_by_month = defaultdict(list)
    for key, r in store.items():
        if key not in base:
            new_by_month[shard_key(r)].append(r)

    existing = defaultdict(list)
    for key, r in base.items():
        existing[shard_key(r)].append(r)

    index_shards, rewritten = [], 0
    for month in sorted(set(existing) | set(new_by_month)):
        rows = existing.get(month, []) + new_by_month.get(month, [])
        rows.sort(key=lambda r: r.get("pub") or "", reverse=True)
        name = f"contracts-{month}.json"
        path = os.path.join(datadir, name)
        payload = encode_shard(month, rows)
        if write_json_if_changed(path, payload):
            rewritten += 1
        total = sum(r["value"] for r in rows
                    if r.get("value") and r.get("cur", "AUD") == "AUD")
        index_shards.append({"month": month, "file": name, "count": len(rows),
                             "total": round(total, 2), "bytes": os.path.getsize(path)})

    # The overlay: every row that now differs from its base version.
    overlay = [r for key, r in store.items()
               if key in base and any(r.get(f) != base[key].get(f) for f in FIELDS)]
    overlay.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    write_json_if_changed(os.path.join(datadir, "updates.json"),
                          {"generated": meta["generated"], "fields": FIELDS,
                           "rows": [[r.get(f) for f in FIELDS] for r in overlay]})
    print(f"  {rewritten} shards written, {len(overlay)} rows in overlay",
          file=sys.stderr)

    suppliers = build_suppliers(store)
    write_json_if_changed(os.path.join(datadir, "suppliers.json"),
                          {"generated": meta["generated"], "suppliers": suppliers[:5000]})

    prior = read_json(os.path.join(datadir, "changes.json"), {}).get("changes", [])
    allch = (prior + changes)[-20000:]
    write_json_if_changed(os.path.join(datadir, "changes.json"),
                          {"generated": meta["generated"], "changes": allch})

    flagged = Counter()
    for r in store.values():
        for f in (r.get("flags") or "").split(","):
            if f:
                flagged[f] += 1

    index = dict(meta)
    index.update({
        "fields": FIELDS,
        "shards": index_shards,
        "updates_file": "updates.json",
        "updates_rows": len(overlay),
        "totals": {
            "contracts": len(store),
            "value_aud": round(sum(r["value"] for r in store.values()
                                   if r.get("value") and r.get("cur", "AUD") == "AUD"), 2),
            "suppliers_by_abn": len(suppliers),
            "changes": len(allch),
        },
        "flag_counts": dict(flagged),
    })
    write_json(os.path.join(datadir, "index.json"), index)
    return index


# Fields with few distinct values are stored once in a dictionary and referenced
# by integer. method has 3 distinct values, buyer ~200, cur 1 — yet each was
# repeated in full on every row, and together they were a third of the payload.
DICT_FIELDS = ("buyer", "supplier", "method", "flags", "cat", "cur")


def encode_shard(month, rows):
    dicts, lookup = {}, {}
    for f in DICT_FIELDS:
        vals, seen = [], {}
        for r in rows:
            v = r.get(f)
            if v not in seen:
                seen[v] = len(vals)
                vals.append(v)
        dicts[f] = vals
        lookup[f] = seen
    out = []
    for r in rows:
        out.append([lookup[f][r.get(f)] if f in lookup else r.get(f) for f in FIELDS])
    return {"month": month, "fields": FIELDS, "dict": dicts, "rows": out}


def decode_shard(b):
    """Inverse of encode_shard, for reading the archive back in."""
    fields = b.get("fields", FIELDS)
    dicts = b.get("dict") or {}
    out = []
    for row in b.get("rows", []):
        r = dict(zip(fields, row))
        for f, vals in dicts.items():
            i = r.get(f)
            if isinstance(i, int) and 0 <= i < len(vals):
                r[f] = vals[i]
        out.append(r)
    return out


def write_json_if_changed(path, obj):
    """Write only when the serialised form differs. Returns True if written."""
    blob = json.dumps(obj, separators=(",", ":"))
    try:
        with open(path) as fh:
            if fh.read() == blob:
                return False
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(blob)
    os.replace(tmp, path)
    return True


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default if default is not None else {}


# ---------------------------------------------------------------- embed

SLOT = "<!--PROCLENS_BUNDLE_SLOT-->"


def embed(template, bundle, dest):
    with open(template) as fh:
        html = fh.read()
    if SLOT not in html:
        print(f"{template} has no {SLOT} marker; skipping embed.", file=sys.stderr)
        return
    payload = json.dumps(bundle, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace(SLOT, f"<script>window.__PROCLENS_BUNDLE__={payload};</script>")
    with open(dest, "w") as fh:
        fh.write(html)
    print(f"Wrote {dest}: {os.path.getsize(dest)/1e6:.1f} MB self-contained",
          file=sys.stderr)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Build the Legal Tender archive.")
    ap.add_argument("--backfill-from", metavar="YYYY-MM-DD",
                    help="deep build from this date using contractPublished")
    ap.add_argument("--backfill-to", metavar="YYYY-MM-DD",
                    help="stop the deep build here, so it can run in resumable chunks")
    ap.add_argument("--resume", action="store_true",
                    help="continue a chunked backfill from the checkpoint in index.json")
    ap.add_argument("--since-days", type=int,
                    help="top-up the last N days using contractLastModified")
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--chunk-days", type=int, default=180,
                    help="how much one --resume step fetches before saving")
    ap.add_argument("--axis", choices=["contractPublished", "contractLastModified"],
                    help="override the date axis")
    ap.add_argument("--inspect", action="store_true",
                    help="print field paths present in live responses and exit")
    ap.add_argument("--embed", metavar="TEMPLATE",
                    help="also write a self-contained HTML build")
    args = ap.parse_args()

    today = date.today()
    today_s = today.isoformat()

    if args.inspect:
        seen, n = Counter(), 0
        for rel in releases(today - timedelta(days=7), today, quiet=True):
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
        return

    target = None
    if args.resume:
        # The archive is its own checkpoint. A chunked backfill records how far
        # it reached, so a timeout costs one chunk rather than the whole run.
        ck = read_json(os.path.join(args.data_dir, "index.json"), {}).get("backfill")
        if not ck or not ck.get("next"):
            print("Nothing to resume; backfill is complete.", file=sys.stderr)
            return
        since = date.fromisoformat(ck["next"])
        target = date.fromisoformat(ck["target"]) if ck.get("target") else today
        axis = "contractPublished"
    elif args.backfill_from:
        since = date.fromisoformat(args.backfill_from)
        target = date.fromisoformat(args.backfill_to) if args.backfill_to else today
        axis = args.axis or "contractPublished"
    elif args.since_days:
        since = today - timedelta(days=args.since_days)
        axis = args.axis or "contractLastModified"
    else:
        since = today - timedelta(days=21)
        axis = args.axis or "contractLastModified"

    store, base = load_store(args.data_dir)
    existing = read_json(os.path.join(args.data_dir, "index.json"), {})
    archive_start = existing.get("archive_start") or today_s
    print(f"Archive holds {len(store)} contracts.", file=sys.stderr)

    until = today
    if target is not None:
        # Chunk every backfill, not just --resume. A single three-hour fetch that
        # commits only at the end loses everything to one failed push; a chunk
        # costs at most --chunk-days of refetching.
        until = min(target, since + timedelta(days=args.chunk_days))

    rows, seen_ocids = [], set()
    for rel in releases(since, until, axis=axis):
        r = to_row(rel)
        key = r.get("ocid") or r.get("cn")
        # Within one run the API returns each process once, but guard anyway so
        # a duplicate cannot inflate the amendment count.
        if key in seen_ocids:
            continue
        seen_ocids.add(key)
        rows.append(r)

    print(f"Fetched {len(rows)} releases {since} → {until} on {axis}.", file=sys.stderr)
    changes, added, updated = merge(store, rows, today_s)

    coverage_from = existing.get("coverage", {}).get("from")
    if not coverage_from or since.isoformat() < coverage_from:
        coverage_from = since.isoformat()

    meta = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "austender-ocds",
        "archive_start": archive_start,
        "coverage": {"from": coverage_from, "to": today_s},
        "last_run": {"axis": axis, "since": since.isoformat(),
                     "until": until.isoformat(), "fetched": len(rows),
                     "added": added, "updated": updated},
        "caveats": [
            "Values are committed at award, not amounts actually paid.",
            "The AusTender API exposes only current state; contract values before "
            f"{archive_start} are not recoverable, so observed growth begins there.",
            "Panel and standing-offer relationships are not published in this feed.",
            "Subcontractors are not published; they require a written request to the agency.",
        ],
    }
    prev_ck = read_json(os.path.join(args.data_dir, "index.json"), {}).get("backfill") or {}
    if target is not None:
        meta["backfill"] = {
            "start": prev_ck.get("start") or since.isoformat(),
            "target": target.isoformat(),
            "next": until.isoformat() if until < target else None,
            "complete": until >= target,
        }
    elif prev_ck:
        meta["backfill"] = prev_ck

    index = save_store(store, base, args.data_dir, changes, meta)

    t = index["totals"]
    print(f"Archive: {t['contracts']} contracts, {t['suppliers_by_abn']} suppliers by ABN, "
          f"${t['value_aud']:,.0f} committed. +{added} new, {updated} updated, "
          f"{len(changes)} field changes this run.", file=sys.stderr)

    if args.embed:
        bundle = {"index": index, "fields": FIELDS,
                  "rows": [[r.get(f) for f in FIELDS] for r in store.values()],
                  "suppliers": build_suppliers(store)[:5000]}
        embed(args.embed, bundle, "bundle.html")


if __name__ == "__main__":
    main()
