#!/usr/bin/env python3
"""
AusTender weekly Contract Notice Export — the fields the OCDS API leaves out.

Every Sunday AusTender publishes a spreadsheet of every contract notice and
amendment published in the previous week. It carries 32 columns, and a third of
them exist nowhere in the OCDS API:

  * why each amendment was made ("Contract novated to a new supplier"),
  * the approach-to-market and standing-offer the contract came from,
  * whether the contract or its outputs are confidential, and why,
  * whether it is a consultancy, and why,
  * the supplier's city, postcode and country, and the agency's branch and
    division.

AusTender keeps these files for eighteen months and then deletes them. The
data.gov.au dataset that looks like their archive holds eight files, all from
early 2013. So, like the approach-to-market feed, this is a window, and the store
it builds is permanent: a file captured is never lost, and one that ages out
before it is captured is gone.

This is an ENRICHMENT store. The contract list itself comes from the OCDS API,
which is complete back to 2014 and already matches independent counts. Nothing
here creates a contract; it only adds what the API does not publish.

Identity. A parent notice has an id like CN4222228 and no parent. An amendment
has an id like CN3770520-A11 and names CN3770520 as its parent. Everything is
keyed on the parent, and each amendment on its own id, so the same amendment
appearing in two weekly files, or a file captured twice, cannot count twice.

    python export.py --out data/export
"""
import argparse, hashlib, io, json, os, re, sys, time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests

try:
    import openpyxl
except ImportError:
    sys.exit("export.py needs openpyxl: pip install openpyxl")

SITE = "https://www.tenders.gov.au"
LIST = f"{SITE}/Reports/CnWeeklyExportList"
UA = os.environ.get("PROCLENS_BROWSER_UA",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 120
PAUSE = 1.0

# Column heading -> store field. Matched exactly after whitespace is collapsed;
# a heading that is renamed upstream should fail loudly, not map to the wrong
# field, so unknown headings are reported rather than guessed at.
COLUMNS = {
    "Agency": "agency",
    "Parent CN ID": "parent",
    "CN ID": "cn_id",
    "Publish Date": "pub",
    "Amendment Publish Date": "amended",
    "Status": "status",
    "Start Date": "start",
    "End Date": "end",
    "Value": "value",
    "Description": "title",
    "Agency Ref. ID": "agency_ref",
    "Category": "category",
    "Procurement Method": "method",
    "ATM ID": "atm",
    "SON ID": "son",
    "Confidentiality - Contract": "conf_contract",
    "Confidentiality - Contract Reason(s)": "conf_contract_reason",
    "Confidentiality - Outputs": "conf_outputs",
    "Confidentiality - Outputs Reason(s)": "conf_outputs_reason",
    "Consultancy": "consultancy",
    "Consultancy Reason(s)": "consultancy_reason",
    "Amendment Reason": "amend_reason",
    "Supplier Name": "supplier",
    "Supplier City": "supplier_city",
    "Supplier Postcode": "supplier_postcode",
    "Supplier Country": "supplier_country",
    "Supplier ABNExempt": "abn_exempt",
    "Supplier ABN": "abn",
    "Agency Branch": "agency_branch",
    "Agency Divison": "agency_division",   # sic, as AusTender spells it
    "Agency Postcode": "agency_postcode",
}

# Stored per contract. The amendment trail is kept separately.
FIELDS = ["cn", "pub", "agency", "agency_branch", "agency_division", "agency_postcode",
          "agency_ref", "title", "category", "method", "value", "start", "end",
          "atm", "son", "conf_contract", "conf_contract_reason", "conf_outputs",
          "conf_outputs_reason", "consultancy", "consultancy_reason", "supplier",
          "abn", "abn_exempt", "supplier_city", "supplier_postcode", "supplier_country",
          "status", "amendments", "first_file", "last_file"]
DICT_FIELDS = ("agency", "agency_branch", "agency_division", "agency_postcode",
               "category", "method", "conf_contract", "conf_outputs", "consultancy",
               "consultancy_reason", "conf_contract_reason", "conf_outputs_reason",
               "supplier_city", "supplier_country", "status", "abn_exempt",
               "first_file", "last_file")

AMEND_RE = re.compile(r"^(CN\d+)-A(\d+)$", re.I)
NULLISH = {"", "null", "none", "n/a", "na", "-"}


def clean(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (int, float)):
        return v
    s = re.sub(r"\s+", " ", str(v)).strip()
    return None if s.lower() in NULLISH else s


def iso(v, with_time=False):
    if isinstance(v, datetime):
        return v.isoformat(timespec="minutes") if with_time else v.date().isoformat()
    if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
        return v[:16] if with_time else v[:10]
    return None


def money(v):
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    try:
        return round(float(re.sub(r"[^0-9.\-]", "", str(v))), 2)
    except (TypeError, ValueError):
        return None


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default if default is not None else {}


def write_json_if_changed(path, obj):
    blob = json.dumps(obj, separators=(",", ":"), default=str)
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


# ---------------------------------------------------------------- fetching

def session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": LIST})
    return s


def discover(s):
    """The list page names each file by the week it covers."""
    html = s.get(LIST, timeout=TIMEOUT).text
    out = []
    for guid, label in re.findall(
            r'href="/Reports/CnWeeklyExportDownload/([0-9a-f-]{20,})"[^>]*>\s*([^<]+?)\s*</a>', html):
        m = re.match(r"(\d{1,2}-[A-Za-z]{3}-\d{2}) to (\d{1,2}-[A-Za-z]{3}-\d{2})", label)
        if not m:
            continue
        week = [datetime.strptime(x, "%d-%b-%y").date().isoformat() for x in m.groups()]
        out.append({"guid": guid, "from": week[0], "to": week[1]})
    if not out:
        raise RuntimeError("the export list parsed to zero files; the page layout may have changed")
    return out


# ---------------------------------------------------------------- parsing

def parse(blob):
    wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    it = wb.active.iter_rows(values_only=True)
    header = None
    for r in it:
        if r and sum(1 for c in r if c) > 5:
            header = [re.sub(r"\s+", " ", str(c)).strip() if c else "" for c in r]
            break
    if not header:
        raise ValueError("no header row")
    unknown = [h for h in header if h and h not in COLUMNS]
    missing = [h for h in COLUMNS if h not in header]
    if missing:
        raise ValueError(f"expected columns missing: {missing}")
    idx = {COLUMNS[h]: i for i, h in enumerate(header) if h in COLUMNS}
    rows = []
    for r in it:
        if not r or not any(r):
            continue
        rows.append({k: clean(r[i]) if i < len(r) else None for k, i in idx.items()})
    return rows, unknown


# ---------------------------------------------------------------- store

def load_store(outdir):
    store = {}
    idx = read_json(os.path.join(outdir, "index.json"), {})
    for sh in idx.get("shards", []):
        p = read_json(os.path.join(outdir, sh["file"]), {})
        f, d = p.get("fields", FIELDS), p.get("dict", {})
        for row in p.get("rows", []):
            o = {}
            for i, k in enumerate(f):
                v = row[i] if i < len(row) else None
                if k in d and isinstance(v, int):
                    v = d[k][v]
                o[k] = v
            o["amendments"] = {a[0]: a[1:] for a in (o.get("amendments") or [])}
            store[o["cn"]] = o
    return store, idx


def fold(store, rows, week_to):
    """Merge one week's rows. Returns (new contracts, updated, new amendments)."""
    new_c = upd = new_a = 0
    # Within a file, parents before amendments, and amendments in order, so the
    # latest state is the one that lands last.
    def order(r):
        m = AMEND_RE.match(str(r.get("cn_id") or ""))
        return (1, int(m.group(2))) if m else (0, 0)
    for r in sorted(rows, key=order):
        cid = str(r.get("cn_id") or "").strip().upper()
        if not cid:
            continue
        m = AMEND_RE.match(cid)
        base = (str(r.get("parent") or "").strip().upper() or (m.group(1) if m else cid))
        if m and m.group(1) != base:
            # An amendment whose id and parent disagree is not something to
            # guess about. Keyed on the stated parent, and counted.
            print(f"  ! {cid} names parent {base}", file=sys.stderr)
        rec = store.get(base)
        if rec is None:
            rec = {"cn": base, "amendments": {}, "first_file": week_to}
            store[base] = rec
            new_c += 1
        else:
            upd += 1
        if m:
            key = f"A{int(m.group(2))}"
            if key not in rec["amendments"]:
                new_a += 1
            rec["amendments"][key] = [iso(r.get("amended"), with_time=True),
                                      money(r.get("value")), r.get("amend_reason")]
        # Descriptive fields: the latest row wins, but a blank never erases a
        # value an earlier row supplied.
        is_later = (week_to >= (rec.get("last_file") or "")) or not rec.get("last_file")
        for k in ("agency", "agency_branch", "agency_division", "agency_postcode",
                  "agency_ref", "title", "category", "method", "start", "end", "atm",
                  "son", "conf_contract", "conf_contract_reason", "conf_outputs",
                  "conf_outputs_reason", "consultancy", "consultancy_reason",
                  "supplier", "abn", "abn_exempt", "supplier_city",
                  "supplier_postcode", "supplier_country", "status"):
            v = r.get(k)
            if k in ("start", "end"):
                v = iso(v)
            if v is None or v == "":
                continue
            if is_later or rec.get(k) in (None, ""):
                rec[k] = v
        if r.get("pub"):
            p = iso(r["pub"])
            rec["pub"] = min(p, rec["pub"]) if rec.get("pub") else p
        # The contract's current value is the parent's value until an amendment
        # restates it; then the highest-numbered amendment's value stands.
        if rec["amendments"]:
            last = max(rec["amendments"], key=lambda a: int(a[1:]))
            rec["value"] = rec["amendments"][last][1]
        elif r.get("value") is not None and not m:
            rec["value"] = money(r.get("value"))
        rec["last_file"] = max(week_to, rec.get("last_file") or week_to)
    return new_c, upd, new_a


def save(store, outdir, sources, generated):
    by_year = defaultdict(list)
    for rec in store.values():
        by_year[(rec.get("pub") or "unknown")[:4]].append(rec)
    shards = []
    for year in sorted(by_year):
        rows = sorted(by_year[year], key=lambda r: r["cn"])
        dicts, lookup = {}, {}
        for f in DICT_FIELDS:
            vals, seen = [], {}
            for r in rows:
                v = r.get(f)
                if v not in seen:
                    seen[v] = len(vals)
                    vals.append(v)
            dicts[f], lookup[f] = vals, seen
        enc = []
        for r in rows:
            row = []
            for f in FIELDS:
                if f == "amendments":
                    row.append([[k] + v for k, v in sorted(r["amendments"].items(),
                                                         key=lambda kv: int(kv[0][1:]))])
                elif f in DICT_FIELDS:
                    row.append(lookup[f][r.get(f)])
                else:
                    row.append(r.get(f))
            enc.append(row)
        name = f"export-{year}.json"
        _, sha = write_json_if_changed(os.path.join(outdir, name),
                                       {"year": year, "fields": FIELDS, "dict": dicts, "rows": enc})
        shards.append({"year": year, "file": name, "count": len(rows),
                       "bytes": os.path.getsize(os.path.join(outdir, name)), "sha": sha})
    counts = Counter()
    for r in store.values():
        counts["contracts"] += 1
        counts["amendments"] += len(r["amendments"])
        for k in ("atm", "son"):
            if r.get(k):
                counts[f"with_{k}"] += 1
        if str(r.get("consultancy") or "").lower() == "yes":
            counts["consultancies"] += 1
        if str(r.get("conf_contract") or "").lower() == "yes":
            counts["confidential_contract"] += 1
        if str(r.get("conf_outputs") or "").lower() == "yes":
            counts["confidential_outputs"] += 1
    write_json_if_changed(os.path.join(outdir, "index.json"), {
        "generated": generated,
        "note": "Enrichment from AusTender's weekly Contract Notice Export. Contracts are "
                "keyed on the parent CN ID and amendments on their own -A id, so a file "
                "captured twice or an amendment published in two weeks counts once. "
                "AusTender deletes these files after eighteen months; the store is "
                "permanent from first capture.",
        "fields": FIELDS,
        "amendment_fields": ["id", "published", "value", "reason"],
        "shards": shards,
        "sources": sources,
        "totals": dict(counts),
        "covers": {"from": min((s["from"] for s in sources), default=None),
                   "to": max((s["to"] for s in sources), default=None)},
    })
    return counts, shards


def main():
    ap = argparse.ArgumentParser(description="Capture AusTender weekly contract notice exports.")
    ap.add_argument("--out", default="data/export")
    ap.add_argument("--from-dir", help="read already-downloaded .xlsx files named <guid>.xlsx")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    store, idx = load_store(args.out)
    sources = list(idx.get("sources", []))
    done = {s["sha"] for s in sources}
    print(f"Store holds {len(store):,} contracts from {len(sources)} files.", file=sys.stderr)

    s = session()
    files = discover(s)
    # Oldest first, so later weeks overwrite descriptive fields in order.
    files.sort(key=lambda f: f["to"])
    print(f"AusTender lists {len(files)} weekly files, {files[0]['from']} to {files[-1]['to']}.",
          file=sys.stderr)

    added_files = 0
    for f in files:
        local = os.path.join(args.from_dir, f"{f['guid']}.xlsx") if args.from_dir else None
        if local and os.path.exists(local):
            blob = open(local, "rb").read()
        else:
            r = s.get(f"{SITE}/Reports/CnWeeklyExportDownload/{f['guid']}", timeout=TIMEOUT)
            r.raise_for_status()
            blob = r.content
            time.sleep(PAUSE)
        sha = hashlib.sha256(blob).hexdigest()[:16]
        if sha in done:
            continue
        rows, unknown = parse(blob)
        if unknown:
            print(f"  ? {f['to']}: unrecognised columns {unknown}", file=sys.stderr)
        nc, up, na = fold(store, rows, f["to"])
        sources = [x for x in sources if not (x["from"] == f["from"] and x["to"] == f["to"])]
        sources.append({"from": f["from"], "to": f["to"], "guid": f["guid"], "sha": sha,
                        "rows": len(rows), "new_contracts": nc, "new_amendments": na,
                        "captured": datetime.now(timezone.utc).date().isoformat()})
        done.add(sha)
        added_files += 1
        print(f"  {f['from']} → {f['to']}: {len(rows):,} rows, +{nc:,} contracts, "
              f"+{na:,} amendments", file=sys.stderr)

    sources.sort(key=lambda x: x["to"], reverse=True)
    counts, shards = save(store, args.out, sources,
                          datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print(f"{added_files} new files. Store: {counts['contracts']:,} contracts, "
          f"{counts['amendments']:,} amendments, {counts['with_atm']:,} with an ATM id, "
          f"{counts['with_son']:,} with a SON id, {counts['consultancies']:,} consultancies.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
