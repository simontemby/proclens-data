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
import argparse, hashlib, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
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
          "amendments", "amended", "trail", "first_seen", "last_seen", "flags"]

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

# Licensing resellers and distributors: suppliers whose name tells you the
# CHANNEL, not whose product was bought. A contract with Data#3 for "software
# licences" leaves no record of the vendor behind it.
#
# The rule used to include the vendors themselves — Palantir, Microsoft, Oracle,
# SAP, Snowflake, Databricks, AWS — and the big consultancies. That flagged every
# Palantir contract as a reseller sale, which is backwards: a contract with
# Palantir is the one kind where the vendor is not in question. Vendors selling
# their own products, and integrators delivering services, are no longer flagged.
# Flagging still implies nothing improper; it marks records where "who really
# supplied this" cannot be answered from the notice alone.
PLATFORM_RE = re.compile(
    r"\b(data ?#? ?3|dicker data|crayon|softwareone|software one|insight enterprises|"
    r"rhipe|ingram micro|td synnex|synnex|westcon|arrow ecs|exclusive networks|"
    r"marketplace)\b",
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


def fetch_window(a, b, axis):
    """Every release in one window, following cursor pagination to the end."""
    out, page, guard = [], f"/findByDates/{axis}/{api_ts(a)}/{api_ts(b)}", 0
    while page and guard < 400:
        data = get(page)
        out.extend(data.get("releases", []))
        nxt = (data.get("links") or {}).get("next")
        page = nxt if nxt and nxt != page else None
        guard += 1
        if page:
            time.sleep(PAUSE)
    if guard >= 400:
        # A window that hits the page guard has been truncated. Saying nothing
        # would record it as covered when it is not.
        raise RuntimeError(f"{a} → {b}: pagination guard hit; window is incomplete")
    return out


def releases(since, until, axis="contractPublished", quiet=False, workers=1):
    """Yield OCDS releases in a period, window by window, in date order.

    Windows are independent, so a deep backfill can fetch several at once. Each
    worker still paginates its own window one page at a time with the usual
    pause, so the load on the API grows with the worker count and no faster. At
    one worker, seven years is several hours of waiting on a single socket.
    """
    wins = list(windows(since, until))
    if workers <= 1:
        results = (fetch_window(a, b, axis) for a, b in wins)
    else:
        pool = ThreadPoolExecutor(max_workers=workers)
        results = pool.map(lambda w: fetch_window(w[0], w[1], axis), wins)
    for (a, b), batch in zip(wins, results):
        if not quiet:
            print(f"  {a} → {b}  {len(batch)}", file=sys.stderr, flush=True)
        for pkg in batch:
            yield pkg


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


# Canberra keeps Sydney time. Needed on every date the API publishes, not just
# timestamps that look like times.
AU_TZ = ZoneInfo("Australia/Sydney")


def as_date(v):
    """The calendar date in Canberra, which is the date AusTender means.

    The API writes dates as UTC instants, and records a day as local midnight: a
    contract ending 30 June 2021 arrives as 2021-06-29T14:00:00Z. Cutting the
    first ten characters therefore put every start and end date a day early, and
    any notice published in the Australian evening or early morning on the day
    before. One contract's award date appeared both ways across its releases,
    2016-06-14T04:27:25Z and 2016-06-13T14:00:00Z, which are the same day in
    Canberra and two different days in UTC.
    """
    if not v:
        return None
    s = str(v)
    if "T" in s:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.astimezone(AU_TZ).date().isoformat()
        except ValueError:
            pass
    return s[:10]


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
        # The release date is when THIS release was published, which for an
        # amendment is the amendment date. awards[].date carries the contract's
        # original publication on every release, including a contract whose
        # original release is missing and only amendments survive.
        "pub": as_date(award.get("date") or rel.get("date")),
        "signed": as_date(contract.get("dateSigned")),
        "start": as_date(period.get("startDate")),
        "end": as_date(period.get("endDate")),
        "method": (tender.get("procurementMethodDetails")
                   or tender.get("procurementMethod") or "").strip(),
        # Items hang off contracts[]; the classification is a UNSPSC code.
        "cat": str(dig(contract, "items", 0, "classification", "id")
                   or tender.get("mainProcurementCategory") or "").strip(),
    }


AMEND_NO = re.compile(r"-A(\d+)$", re.I)


def release_order(rel):
    """Amendment number first, publication time second.

    Timestamps alone cannot order a contract's history: AusTender republished
    some contracts' entire amendment series in one batch, so CN3350299 carries
    seventy amendments stamped with the same second, returned in no particular
    order. The amendment id (CN3350299-A21) is the real sequence. Taking the
    last release the API happened to list recorded that contract at $2.7bn from
    its ninth amendment rather than its latest.
    """
    n = 0
    for c in rel.get("contracts") or []:
        for a in c.get("amendments") or []:
            m = AMEND_NO.search(str(a.get("id") or ""))
            if m:
                n = max(n, int(m.group(1)))
    return (n, rel.get("date") or "")


def collapse(rels):
    """Fold every release of each contracting process into one row.

    The API does not return one release per contract. It returns the original
    publication and then one release per amendment, each dated when it was
    published — and it returns all of them together, whichever date axis or
    endpoint asked. Two mistakes followed from treating each release as a row.

    Keeping the first release seen stored the ORIGINAL value of every amended
    contract: in a three-week sample from July 2020, 1,324 of 5,406 contracts
    carried a superseded figure. And letting a later release replace an earlier
    one overwrote the publication date with the amendment date, moving 367
    contracts in the live archive into the month, and often the year, they were
    amended rather than published.

    So the row is assembled from both ends of the history: the publication date
    and the first value from the earliest release, everything describing the
    contract as it stands now from the latest.
    """
    by = defaultdict(list)
    for rel in rels:
        key = rel.get("ocid") or rel.get("id")
        if key:
            by[key].append(rel)
    rows = []
    for key, group in by.items():
        # The same release can arrive twice across overlapping windows.
        uniq = {}
        for rel in group:
            uniq[rel.get("id") or json.dumps(rel, sort_keys=True)] = rel
        ordered = sorted(uniq.values(), key=release_order)
        first, last = to_row(ordered[0]), to_row(ordered[-1])
        row = dict(last)
        row["pub"] = min(p for p in (first.get("pub"), last.get("pub")) if p) \
            if (first.get("pub") or last.get("pub")) else None
        row["value_first"] = first.get("value")
        row["amendments"] = len(ordered) - 1
        row["amended"] = as_date(ordered[-1].get("date")) if len(ordered) > 1 else None
        # The dated value of every release, so the contract's value as it stood on
        # any given day is recoverable. A point-in-time source such as a Senate
        # Order snapshot can only be fairly compared with the value on its date.
        row["trail"] = ([[as_date(x.get("date")), to_row(x).get("value")] for x in ordered]
                        if len(ordered) > 1 else None)
        rows.append(row)
    return rows


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
    # value_first is the value at original publication, taken from the API's
    # release history, so this measures growth over the contract's whole life.
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
            if r.get("value_first") is None:
                r["value_first"] = r.get("value")
            r["amendments"] = r.get("amendments") or 0
            store[key] = r
            added += 1
            continue
        # Carry archive-only provenance forward.
        r["first_seen"] = old.get("first_seen") or today
        r["last_seen"] = today
        # A contract is published once. A later fetch can only ever be looking at
        # the same publication, so the earlier date stands.
        if old.get("pub") and (not r.get("pub") or old["pub"] < r["pub"]):
            r["pub"] = old["pub"]
        # The API's own release history is the authority on the original value
        # and the number of amendments; the archive's observation is a fallback.
        if r.get("value_first") is None:
            r["value_first"] = old.get("value_first", old.get("value"))
        r["amendments"] = max(r.get("amendments") or 0, old.get("amendments") or 0)
        r["amended"] = r.get("amended") or old.get("amended")
        if len(old.get("trail") or []) > len(r.get("trail") or []):
            r["trail"] = old["trail"]
        diffs = [f for f in WATCHED
                 if old.get(f) != r.get(f) and old.get(f) not in (None, "")]
        if diffs:
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
        written, sha = write_json_if_changed(path, payload)
        if written:
            rewritten += 1
        total = sum(r["value"] for r in rows
                    if r.get("value") and r.get("cur", "AUD") == "AUD")
        index_shards.append({"month": month, "file": name, "count": len(rows),
                             "total": round(total, 2), "bytes": os.path.getsize(path),
                             "sha": sha})

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
    """Write only when the serialised form differs.

    Returns (written, sha) where sha is a short digest of the content that was
    left on disk. Callers stamp that digest into index.json so a browser can
    tell one version of a shard from another; a count-and-size stamp cannot,
    because a value edit that keeps the same number of digits changes neither.
    """
    blob = json.dumps(obj, separators=(",", ":"))
    sha = hashlib.sha256(blob.encode()).hexdigest()[:12]
    try:
        with open(path) as fh:
            if fh.read() == blob:
                return False, sha
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(blob)
    os.replace(tmp, path)
    return True, sha
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
    ap.add_argument("--workers", type=int, default=1,
                    help="date windows fetched concurrently (deep backfills only)")
    ap.add_argument("--chunk-days", type=int, default=180,
                    help="how much one --resume step fetches before saving")
    ap.add_argument("--axis", choices=["contractPublished", "contractLastModified"],
                    help="override the date axis")
    ap.add_argument("--inspect", action="store_true",
                    help="print field paths present in live responses and exit")
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

    fetched = list(releases(since, until, axis=axis, workers=max(1, args.workers)))
    rows = collapse(fetched)

    print(f"Fetched {len(fetched)} releases for {len(rows)} contracts "
          f"{since} → {until} on {axis}.", file=sys.stderr)
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
            "Each contract's first value and amendment count come from AusTender's "
            "own release history; the reason for an amendment is not in the API.",
            "Panel and standing-offer relationships are not published in this feed.",
            "Subcontractors are not published; they require a written request to the agency.",
        ],
    }
    prev_ck = read_json(os.path.join(args.data_dir, "index.json"), {}).get("backfill") or {}
    if target is not None:
        same_run = prev_ck.get("target") == target.isoformat() and not prev_ck.get("complete")
        meta["backfill"] = {
            "start": (prev_ck.get("start") if same_run else None) or since.isoformat(),
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



if __name__ == "__main__":
    main()
