#!/usr/bin/env python3
"""
build.py — one deduplicated corpus from every source, and the index that lets a
browser search it without downloading it.

The ingest scripts each keep their own store in their own shape: refresh.py the
OCDS API, export.py the weekly Contract Notice Export, historical.py the
data.gov.au extracts, senate.py the Senate Order snapshots, so13.py entities
outside AusTender altogether. The front end used to fetch all of them and merge
them itself, which meant roughly 290,000 contracts from 2014 to 2020 were
downloaded twice and reconciled on every page load by code no one could test.

This does the reconciling once, here, where it can be counted. Each contract
appears exactly once, keyed on its CN ID. What each source adds is kept beside
the core record, and wherever two Commonwealth publications give different
values the both of them are kept, not averaged or chosen between.

Output, under data/corpus/:

  list/YYYY-MM.json    what the results table, filters, totals and CSV need
  detail/YYYY-MM.json  everything else, fetched only when a contract is opened
  updates.json         records that changed after their month file was written
  terms/<key>.json     token -> months containing it, so a search fetches only
                       the months that can match
  index.json           months, digests, totals, agencies, and the build report

Month files follow the archive's existing rule: written once, and anything that
changes afterwards goes to updates.json, so a week of amendments to contracts
from every year does not rewrite a hundred files in git. --compact folds the
updates back in.

    python build.py
    python build.py --compact
"""
import argparse, hashlib, json, os, re, sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

import refresh
import vendors

DATA = os.environ.get("PROCLENS_DATA", "data")

LIST_FIELDS = ["cn", "title", "buyer", "supplier", "abn", "value", "value_first", "cur",
               "pub", "start", "end", "amendments", "flags", "src", "vendor", "vconf"]
LIST_DICT = ("buyer", "supplier", "abn", "cur", "flags", "src", "vendor", "vconf")

DETAIL_FIELDS = [
    "cn", "signed", "method", "cat", "category", "agency_ref", "amended", "trail",
    "first_seen", "last_seen",
    "atm", "son", "panel", "conf_contract", "conf_contract_reason", "conf_outputs",
    "conf_outputs_reason", "consultancy", "consultancy_reason",
    "agency_branch", "agency_division", "supplier_city", "supplier_postcode",
    "supplier_country", "amend_trail", "hist_amendments",
    "senate_obs", "senate_mismatch", "v_api", "v_export", "v_hist", "v_senate",
    "so13_entity", "so13_period", "so13_type", "so13_variations", "so13_approached",
    "so13_source", "intermediary", "vendor_evidence", "value_concat"]
DETAIL_DICT = ("method", "category", "conf_contract", "conf_outputs", "consultancy",
               "consultancy_reason", "conf_contract_reason", "conf_outputs_reason",
               "agency_branch", "agency_division", "supplier_city", "supplier_country",
               "panel", "so13_entity", "so13_period", "so13_type", "so13_source",
               "first_seen", "last_seen", "intermediary")

# Senate Order reports list contracts current at the end of their period.
PERIOD_END = {"CY": "12-31", "FY": "06-30"}
# Values are published to the cent. A difference below a dollar is rounding.
TOLERANCE = 1.0
# Two sources can differ honestly: a historical extract is a snapshot, so a
# contract that later grew by amendment reads low there. A hundredfold gap is
# not that. The Treasury records a $123bn contract with Hays for "Human
# Resource Services" where the extract records nothing; one of the two is a
# typing mistake, and the archive does not get to decide which by itself.
CONTRADICTION = 100.0


def concatenated(value, trail):
    """Some implausible values are two amendments typed into one field.
    CN3491208's $123,000,198,000 is its $123,000 amendment followed by its
    $198,000 amendment, digit for digit, and the historical extract records
    $198,000. Where that is what happened, the archive can say so exactly
    instead of only noting that the sources differ."""
    if not isinstance(value, (int, float)) or value < 1e6 or not trail:
        return None
    whole = f"{int(round(value))}"
    parts = sorted({int(round(t[1])) for t in trail
                    if isinstance(t[1], (int, float)) and 1000 <= t[1] < value / CONTRADICTION})
    for a in parts:
        for b in parts:
            if f"{a}{b}" == whole:
                return [a, b]
    return None
# The Commonwealth Procurement Rules allow 42 days to report a contract or an
# amendment to it on AusTender.
PUBLISH_DAYS = 42
# Past this many changed records, fold updates back into the month files.
AUTO_COMPACT_ROWS = 40_000

CN_RE = re.compile(r"^(CN\d+)(?:-A\d+)?$", re.I)


# ---------------------------------------------------------------- helpers

def cn_key(v):
    s = re.sub(r"\s+", "", str(v or "")).upper()
    m = CN_RE.match(s)
    return m.group(1) if m else s


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default if default is not None else {}


def write_json_if_changed(path, obj):
    blob = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)
    sha = hashlib.sha256(blob.encode()).hexdigest()[:12]
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == blob:
                return False, sha
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(blob)
    os.replace(tmp, path)
    return True, sha


def decode(payload):
    f, d = payload.get("fields", []), payload.get("dict") or {}
    out = []
    for row in payload.get("rows", []):
        o = {}
        for i, k in enumerate(f):
            v = row[i] if i < len(row) else None
            if k in d and isinstance(v, int) and not k.startswith("_") and 0 <= v < len(d[k]):
                v = d[k][v]
            o[k] = v
        out.append(o)
    return out


def encode(rows, fields, dict_fields):
    dicts, lookup = {}, {}
    for f in dict_fields:
        vals, seen = [], {}
        for r in rows:
            v = r.get(f)
            if v not in seen:
                seen[v] = len(vals)
                vals.append(v)
        dicts[f], lookup[f] = vals, seen
    enc = [[lookup[f][r.get(f)] if f in dict_fields else r.get(f) for f in fields] for r in rows]
    # Trailing nulls cost bytes on every row and carry nothing.
    for row in enc:
        while row and row[-1] is None:
            row.pop()
    return {"fields": fields, "dict": dicts, "rows": enc}


def blank(v):
    return v is None or v == "" or v == []


def value_as_at(rec, day):
    """The API's value for a contract as it stood on `day`, or None if the
    contract had not been published by then."""
    if not rec.get("pub") or rec["pub"] > day:
        return None
    trail = rec.get("trail")
    if not trail:
        return rec.get("value")
    val = None
    for d, v in trail:
        if d and d <= day:
            val = v
    return val if val is not None else rec.get("value_first")


def period_end(label):
    """CY2024 -> 2024-12-31; FY2024-25 -> 2025-06-30."""
    m = re.match(r"(CY|FY)(\d{4})", label or "")
    if not m:
        return None
    kind, y = m.group(1), int(m.group(2))
    return f"{y}-12-31" if kind == "CY" else f"{y + 1}-06-30"


# ---------------------------------------------------------------- sources

def load_api():
    store, _ = refresh.load_store(DATA)
    by_cn, collisions = {}, []
    for r in store.values():
        k = cn_key(r.get("cn"))
        if not k:
            continue
        if k in by_cn:
            # Two contracting processes publishing under one CN ID. Neither is
            # discarded silently: the later publication is kept as the record and
            # the collision is reported.
            other = by_cn[k]
            collisions.append([k, other.get("ocid"), r.get("ocid")])
            if (r.get("pub") or "") < (other.get("pub") or ""):
                continue
        by_cn[k] = r
    return by_cn, collisions


def load_export():
    out = {}
    idx = read_json(os.path.join(DATA, "export", "index.json"), {})
    for sh in idx.get("shards", []):
        for r in decode(read_json(os.path.join(DATA, "export", sh["file"]), {})):
            k = cn_key(r.get("cn"))
            if k.startswith("CN"):
                out[k] = r
    return out


def load_hist(report):
    out, skipped = {}, Counter()
    idx = read_json(os.path.join(DATA, "historical", "index.json"), {})
    for sh in idx.get("shards", []):
        for r in decode(read_json(os.path.join(DATA, "historical", sh["file"]), {})):
            k = cn_key(r.get("cn"))
            # Standing offer notices share the extracts with contract notices but
            # are not contracts; counting them would inflate every total.
            if not k.startswith("CN"):
                skipped[k[:3] or "blank"] += 1
                continue
            out[k] = r
    report["historical_non_contract_ids_skipped"] = dict(skipped)
    return out


def load_senate():
    p = read_json(os.path.join(DATA, "senate", "contracts.json"), {})
    labels = (p.get("dict") or {}).get("_periods") or []
    out = {}
    for r in decode(p):
        k = cn_key(r.get("cn"))
        if not k.startswith("CN"):
            continue
        r["periods"] = [labels[i] if isinstance(i, int) and i < len(labels) else i
                        for i in (r.get("periods") or [])]
        r["observations"] = [[labels[o[0]] if isinstance(o[0], int) and o[0] < len(labels)
                              else o[0], o[1]] for o in (r.get("observations") or [])]
        out[k] = r
    return out


def load_so13():
    """One record per contract: listings repeat a contract in every period it is
    current, sometimes with its supplier's name spelt differently."""
    out = {}
    for name in sorted(os.listdir(os.path.join(DATA, "so13"))) if os.path.isdir(
            os.path.join(DATA, "so13")) else []:
        if not name.endswith(".json") or name == "index.json":
            continue
        p = read_json(os.path.join(DATA, "so13", name), {})
        short = name.split("-")[0]
        for row in p.get("rows", []):
            r = dict(zip(p["fields"], row))
            ident = str(r.get("cn") or "").strip()
            if not ident:
                continue
            k = f"{short.upper()}:{ident}"
            if k not in out or str(r.get("period") or "") >= str(out[k].get("period") or ""):
                out[k] = r
    return out


# ---------------------------------------------------------------- merge

def merge_all(report):
    api, collisions = load_api()
    exp, hist, sen, so13 = load_export(), load_hist(report), load_senate(), load_so13()
    report["inputs"] = {"api": len(api), "export": len(exp), "historical": len(hist),
                        "senate": len(sen), "so13": len(so13)}
    report["cn_collisions_in_api"] = len(collisions)
    report["cn_collision_sample"] = collisions[:20]

    corpus = {}
    only = Counter()

    # 1. The API is the record wherever it has the contract.
    for k, a in api.items():
        rec = {f: a.get(f) for f in ("ocid", "title", "buyer", "supplier", "abn", "value",
                                     "value_first", "cur", "pub", "signed", "start", "end",
                                     "method", "cat", "amendments", "amended", "trail",
                                     "first_seen", "last_seen")}
        rec["cn"] = k
        rec["v_api"] = a.get("value")
        rec["_src"] = {"api"}
        corpus[k] = rec

    # 2. Contracts the API does not have take their core from the next source
    #    that does, in order of how current that source is.
    def core_from_export(k, e):
        return {"cn": k, "title": e.get("title"), "buyer": e.get("agency"),
                "supplier": e.get("supplier"), "abn": e.get("abn"), "value": e.get("value"),
                "value_first": None, "cur": "AUD", "pub": e.get("pub"),
                "start": e.get("start"), "end": e.get("end"), "method": e.get("method"),
                "amendments": len(e.get("amendments") or []), "_src": set()}

    def core_from_hist(k, h):
        return {"cn": k, "title": h.get("title"), "buyer": h.get("agency"),
                "supplier": h.get("supplier"), "abn": h.get("abn"), "value": h.get("value"),
                "value_first": h.get("value_first"), "cur": "AUD", "pub": h.get("pub"),
                "start": h.get("start"), "end": h.get("end"), "method": h.get("method"),
                "cat": h.get("unspsc") or h.get("cat"),
                "amendments": len(h.get("amendments") or []), "_src": set()}

    def core_from_senate(k, s):
        return {"cn": k, "title": s.get("title"), "buyer": s.get("agency"),
                "supplier": s.get("supplier"), "abn": s.get("abn"), "value": s.get("value"),
                "value_first": s.get("value_first"), "cur": "AUD", "pub": s.get("pub"),
                "start": s.get("start"), "end": s.get("end"), "category": s.get("cat"),
                "agency_ref": s.get("agency_ref"), "amendments": 0, "_src": set()}

    for k, e in exp.items():
        if k not in corpus:
            corpus[k] = core_from_export(k, e)
            only["export"] += 1
    for k, h in hist.items():
        if k not in corpus:
            corpus[k] = core_from_hist(k, h)
            only["historical"] += 1
    for k, s in sen.items():
        if k not in corpus:
            corpus[k] = core_from_senate(k, s)
            only["senate"] += 1

    # 3. Enrichment. A field another source already supplied is never
    #    overwritten; each source's own value is kept under its own name.
    for k, e in exp.items():
        rec = corpus[k]
        rec["_src"].add("export")
        rec["v_export"] = e.get("value")
        for src_f, dst_f in (("atm", "atm"), ("son", "son"), ("category", "category"),
                             ("agency_ref", "agency_ref"),
                             ("conf_contract", "conf_contract"),
                             ("conf_contract_reason", "conf_contract_reason"),
                             ("conf_outputs", "conf_outputs"),
                             ("conf_outputs_reason", "conf_outputs_reason"),
                             ("consultancy", "consultancy"),
                             ("consultancy_reason", "consultancy_reason"),
                             ("agency_branch", "agency_branch"),
                             ("agency_division", "agency_division"),
                             ("supplier_city", "supplier_city"),
                             ("supplier_postcode", "supplier_postcode"),
                             ("supplier_country", "supplier_country"),
                             ("method", "method")):
            if blank(rec.get(dst_f)) and not blank(e.get(src_f)):
                rec[dst_f] = e[src_f]
        if e.get("amendments"):
            rec["amend_trail"] = e["amendments"]

    for k, h in hist.items():
        rec = corpus[k]
        rec["_src"].add("historical")
        rec["v_hist"] = h.get("value")
        for src_f, dst_f in (("son", "son"), ("atm", "atm"), ("panel", "panel"),
                             ("conf", "conf_contract"), ("conf_reason", "conf_contract_reason"),
                             ("consult", "consultancy"), ("consult_reason", "consultancy_reason"),
                             ("agency_ref", "agency_ref"), ("country", "supplier_country"),
                             ("method", "method")):
            if blank(rec.get(dst_f)) and not blank(h.get(src_f)):
                rec[dst_f] = h[src_f]
        if blank(rec.get("cat")) and h.get("unspsc"):
            rec["cat"] = h["unspsc"]
        if h.get("amendments"):
            rec["hist_amendments"] = h["amendments"]

    late_n = late_contracts = differs_n = 0
    for k, s in sen.items():
        rec = corpus[k]
        rec["_src"].add("senate")
        rec["v_senate"] = s.get("value")
        rec["senate_obs"] = s.get("observations") or None
        for src_f, dst_f in (("conf_contract", "conf_contract"),
                             ("conf_reason", "conf_contract_reason"),
                             ("conf_outputs", "conf_outputs"),
                             ("conf_out_reason", "conf_outputs_reason"),
                             ("agency_ref", "agency_ref")):
            if blank(rec.get(dst_f)) and not blank(s.get(src_f)):
                rec[dst_f] = s[src_f]
        if "api" not in rec["_src"]:
            continue
        # The fair comparison: what the snapshot said at the end of its period
        # against what the API's own history says the contract was worth on that
        # same day. Comparing a 2024 snapshot with today's API value would call
        # every later amendment a disagreement.
        #
        # Most differences that survive that are not disagreements at all. The
        # snapshot carries a value AusTender published only LATER: in 17,925 of
        # the first 19,233 mismatches, the snapshot's exact figure appears in the
        # API's own history after the period ended, a median of 175 days on.
        # The agency reported the amendment to the Senate before it published
        # it. That is recorded as "later", with the delay. Only a value that never
        # appears in the API's history at all is a disagreement.
        bad = []
        trail = rec.get("trail") or []
        for label, sv in (s.get("observations") or []):
            day = period_end(label)
            av = value_as_at(rec, day) if day else None
            if sv is None or av is None or abs(sv - av) <= TOLERANCE:
                continue
            tol = max(TOLERANCE, abs(sv) * 1e-6)
            later = [t for t in trail if t[0] and t[0] > day and t[1] is not None
                     and abs(t[1] - sv) <= tol]
            if later:
                lag = (date.fromisoformat(later[0][0]) - date.fromisoformat(day)).days
                bad.append([label, sv, av, "later", lag])
                late_n += 1
            else:
                bad.append([label, sv, av, "differs", None])
        if bad:
            rec["senate_mismatch"] = bad
            if any(b[3] == "differs" for b in bad):
                differs_n += 1
            if any(b[3] == "later" and b[4] > PUBLISH_DAYS for b in bad):
                late_contracts += 1
    report["senate_vs_api"] = {"observations_published_later": late_n,
                               "contracts_amendment_published_late": late_contracts,
                               "contracts_value_never_in_api": differs_n}

    # 4. Entities outside AusTender. Never merged with an AusTender CN.
    for k, s in so13.items():
        corpus[k] = {"cn": k, "title": s.get("title"), "buyer": s.get("entity"),
                     "supplier": s.get("supplier"), "abn": s.get("abn"),
                     "value": s.get("value"), "value_first": s.get("value_first"),
                     "cur": "AUD", "pub": None, "start": s.get("start"), "end": s.get("end"),
                     "method": s.get("method"), "conf_contract": s.get("confidential"),
                     "conf_contract_reason": s.get("conf_reason"),
                     "so13_entity": s.get("entity"), "so13_period": s.get("period"),
                     "so13_type": s.get("ctype"), "so13_variations": s.get("variations"),
                     "so13_approached": s.get("approached"),
                     "so13_source": s.get("source_url"), "amendments": 0, "_src": {"so13"}}
    only["so13"] = len(so13)
    report["only_in"] = dict(only)

    # 5. Whose product an intermediary sold, where the evidence allows.
    vendors.infer(corpus, DATA, report)

    # 6. Flags, source label, and the export/API comparison for the report.
    export_diff = 0
    for rec in corpus.values():
        f = [x for x in refresh.flag(rec).split(",") if x] if rec.get("value") is not None or rec.get("pub") else []
        # The supplier-name rule is superseded by vendors.infer, which also uses
        # what each supplier's contracts name.
        f = [x for x in f if x != "platform_or_reseller"]
        if rec.get("intermediary"):
            f.append("reseller_sale")
        others = [rec[k] for k in ("v_export", "v_hist", "v_senate")
                  if isinstance(rec.get(k), (int, float))]
        v = rec.get("value")
        if isinstance(v, (int, float)) and others and abs(v) > 0:
            lo, hi = min(others), max(others)
            if abs(lo) * CONTRADICTION <= abs(v) or abs(v) * CONTRADICTION <= abs(hi):
                f.append("value_contradicted")
        cc = concatenated(v, rec.get("trail"))
        if cc:
            rec["value_concat"] = cc
        mm = rec.get("senate_mismatch") or []
        if any(b[3] == "differs" for b in mm):
            f.append("source_disagreement")
        if any(b[3] == "later" and (b[4] or 0) > PUBLISH_DAYS for b in mm):
            f.append("late_amendment")
        # Facts rather than review flags, carried in the same list so they can be
        # filtered on: confidentiality is the single most useful filter for spend
        # that is hard to see.
        if any(str(rec.get(k) or "").strip().lower() in ("y", "yes")
               for k in ("conf_contract", "conf_outputs")):
            f.append("confidential")
        if str(rec.get("consultancy") or "").strip().lower() in ("y", "yes"):
            f.append("consultancy")
        rec["flags"] = ",".join(f)
        rec["src"] = ",".join(sorted(rec.pop("_src")))
        if rec.get("v_api") is not None and rec.get("v_export") is not None and \
                abs(rec["v_api"] - rec["v_export"]) > TOLERANCE:
            export_diff += 1
    report["api_vs_export_current_value_differs"] = export_diff
    report["value_contradicted"] = sum(1 for r in corpus.values()
                                       if "value_contradicted" in (r.get("flags") or ""))
    report["value_concatenated"] = sum(1 for r in corpus.values() if r.get("value_concat"))
    report["contracts"] = len(corpus)
    return corpus


def month_of(rec):
    """SO13 listings carry no publication date, so they file under their start."""
    p = rec.get("pub") or (rec.get("start") if str(rec.get("cn", "")).find(":") > 0 else None)
    return p[:7] if p and len(p) >= 7 and p[:4].isdigit() else "unknown"


# ---------------------------------------------------------------- search terms

TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens(rec):
    """Must match tokenize() in index.html exactly, or a search would skip a
    month that holds a match."""
    text = " ".join(str(rec.get(k) or "") for k in ("title", "buyer", "supplier", "abn", "cn", "vendor"))
    out = set()
    for t in TOKEN_RE.findall(text.lower()):
        if len(t) < 2 or len(t) > 40:
            continue
        if t.isdigit() and len(t) > 4 and len(t) != 11:
            continue  # purchase-order and reference numbers; ABNs are 11 digits
        out.add(t)
    return out


def term_shard(t):
    # CN ids are 800,000 distinct tokens under one prefix, so they get a finer key.
    if t.startswith("cn") and len(t) > 4 and t[2:].isdigit():
        return "cn" + t[2:5]
    return t[:2]


# ---------------------------------------------------------------- write

def write_corpus(corpus, out, compact):
    old_index = read_json(os.path.join(out, "index.json"), {})
    compact = compact or not old_index.get("months")

    by_month = defaultdict(list)
    for rec in corpus.values():
        by_month[month_of(rec)].append(rec)

    base = {}
    if not compact:
        for m in old_index.get("months", []):
            L = decode(read_json(os.path.join(out, "list", f"{m['month']}.json"), {}))
            D = {d["cn"]: d for d in decode(read_json(os.path.join(out, "detail", f"{m['month']}.json"), {}))}
            for l in L:
                base[l["cn"]] = (m["month"], l, D.get(l["cn"]) or {"cn": l["cn"]})

    def canon(d, fields):
        return json.dumps({f: d.get(f) for f in fields}, sort_keys=True, default=str)

    months, upd_l, upd_d = [], [], []
    for month in sorted(by_month):
        recs = sorted(by_month[month], key=lambda r: (r.get("pub") or "", r["cn"]), reverse=True)
        rows_l, rows_d = [], []
        for r in recs:
            lr = {f: r.get(f) for f in LIST_FIELDS}
            # Stored only when it differs: a missing first value means unchanged,
            # which is true of three contracts in four and saves the bytes.
            if lr.get("value_first") == lr.get("value"):
                lr["value_first"] = None
            dr = {f: r.get(f) for f in DETAIL_FIELDS}
            b = base.get(r["cn"])
            if compact or b is None or b[0] != month:
                # New to the archive, or filed under a different month than
                # before: the month file is being rewritten regardless, so it
                # carries the current record directly.
                rows_l.append(lr); rows_d.append(dr)
                continue
            _, bl, bd = b
            rows_l.append(bl); rows_d.append(bd)
            if canon(bl, LIST_FIELDS) != canon(lr, LIST_FIELDS) or \
                    canon(bd, DETAIL_FIELDS) != canon(dr, DETAIL_FIELDS):
                upd_l.append(lr); upd_d.append(dr)
        path_l = os.path.join(out, "list", f"{month}.json")
        _, lsha = write_json_if_changed(path_l, dict(encode(rows_l, LIST_FIELDS, LIST_DICT), month=month))
        _, dsha = write_json_if_changed(os.path.join(out, "detail", f"{month}.json"),
                                        dict(encode(rows_d, DETAIL_FIELDS, DETAIL_DICT), month=month))
        months.append({"month": month, "count": len(recs), "list": lsha, "detail": dsha,
                       "bytes": os.path.getsize(path_l)})

    # A month that no longer holds any contract must not linger as a stale file.
    live = {m["month"] for m in months}
    for sub in ("list", "detail"):
        d = os.path.join(out, sub)
        for n in os.listdir(d) if os.path.isdir(d) else []:
            if n.endswith(".json") and n[:-5] not in live:
                os.remove(os.path.join(d, n))

    _, usha = write_json_if_changed(os.path.join(out, "updates.json"), {
        "list": encode(upd_l, LIST_FIELDS, LIST_DICT),
        "detail": encode(upd_d, DETAIL_FIELDS, DETAIL_DICT)})
    return months, {"rows": len(upd_l), "sha": usha}, compact


def main():
    ap = argparse.ArgumentParser(description="Build the deduplicated corpus and search index.")
    ap.add_argument("--out", default=os.path.join(DATA, "corpus"))
    ap.add_argument("--compact", action="store_true", help="fold updates into month files")
    args = ap.parse_args()

    report = {}
    corpus = merge_all(report)
    out = args.out

    months, updates, compacted = write_corpus(corpus, out, args.compact)
    if not compacted and updates["rows"] > AUTO_COMPACT_ROWS:
        print(f"{updates['rows']:,} changed records; compacting.", file=sys.stderr)
        months, updates, compacted = write_corpus(corpus, out, True)
    report["compacted"] = compacted
    report["updates"] = updates["rows"]

    # Search terms over the CURRENT record of every contract.
    month_ix = {m["month"]: i for i, m in enumerate(months)}
    postings = defaultdict(lambda: defaultdict(set))
    for rec in corpus.values():
        mi = month_ix[month_of(rec)]
        for t in tokens(rec):
            postings[term_shard(t)][t].add(mi)
    tdir = os.path.join(out, "terms")
    existing = {n[:-5] for n in os.listdir(tdir) if n.endswith(".json")} if os.path.isdir(tdir) else set()
    term_shas = {}
    for key, toks in postings.items():
        _, sha = write_json_if_changed(os.path.join(tdir, f"{key}.json"),
                                       {k: sorted(v) for k, v in sorted(toks.items())})
        term_shas[key] = sha
    for stale in existing - set(postings):
        os.remove(os.path.join(tdir, f"{stale}.json"))

    # The page labels UNSPSC codes from this. It lives beside the corpus because
    # the historical store it comes from is not published.
    codes = read_json(os.path.join(DATA, "historical", "unspsc.json"), {})
    if codes:
        write_json_if_changed(os.path.join(out, "unspsc.json"), codes)

    agencies = Counter(r.get("buyer") for r in corpus.values() if r.get("buyer"))
    totals = {"contracts": len(corpus),
              "value_aud": round(sum(r["value"] for r in corpus.values()
                                     if isinstance(r.get("value"), (int, float))
                                     and (r.get("cur") or "AUD") == "AUD"), 2),
              "by_year": dict(sorted(Counter((r.get("pub") or "")[:4] or "none"
                                             for r in corpus.values()).items()))}
    # How much of the headline figure rests on a contract another source
    # contradicts. It is small in count and large in money, so the page says so
    # rather than leaving a reader to assume the total is all of a piece.
    contra = [r for r in corpus.values() if "value_contradicted" in (r.get("flags") or "")
              and isinstance(r.get("value"), (int, float))]
    totals["contradicted"] = {"contracts": len(contra),
                              "value_aud": round(sum(r["value"] for r in contra), 2)}
    flag_counts = Counter(f for r in corpus.values() for f in (r.get("flags") or "").split(",") if f)
    report["terms"] = sum(len(v) for v in postings.values())
    report["term_shards"] = len(postings)
    write_json_if_changed(os.path.join(out, "index.json"), {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "list_fields": LIST_FIELDS, "detail_fields": DETAIL_FIELDS,
        "months": months, "updates": updates,
        "terms": term_shas, "totals": totals, "flag_counts": dict(flag_counts),
        "agencies": agencies.most_common(), "report": report,
    })
    print(json.dumps({k: v for k, v in report.items() if k != "cn_collision_sample"}, indent=1),
          file=sys.stderr)


if __name__ == "__main__":
    main()
