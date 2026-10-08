#!/usr/bin/env python3
"""
Senate Order 13 ingest — contracts from entities that do not report to AusTender.

Corporate Commonwealth entities are not bound by the Commonwealth Procurement
Rules' reporting requirements, so their contracts never appear in AusTender. The
NDIA is the significant example: it spends billions and publishes nothing to the
OCDS feed. Under Senate Continuing Order 13 those entities must instead publish,
twice yearly, every contract of $100,000 or more (GST inclusive) on their own
website, as a spreadsheet.

This ingests those listings.

Most entities do not publish a spreadsheet. The NDIA, the National Library and the
Reserve Bank do; CSIRO publishes nineteen PDFs and no spreadsheet at all, and so
do Defence Housing Australia, the Clean Energy Finance Corporation, the National
Reconstruction Fund and the Australian Reinsurance Pool Corporation. So this reads
both, and reads PDFs by their ruled table structure rather than by scraping text:
pypdf returns a CSIRO header split across eleven lines, where pdfplumber returns
nine columns with the rows intact.

A row that does not parse is dropped and counted, never half-stored. A column
misread puts a wrong supplier against a real amount, which is worse than a gap.

Two things make this data BETTER than AusTender for value questions:
  * it carries "Original Contract Value" alongside the current consideration,
    which is the amendment history the OCDS API refuses to give; and
  * it states explicitly whether a contract carries confidentiality provisions,
    and why.

    python so13.py --out data/so13
"""
import argparse, io, json, os, re, sys
from datetime import datetime, timezone

import requests

try:
    import openpyxl
except ImportError:
    sys.exit("so13.py needs openpyxl: pip install openpyxl")

try:
    import pdfplumber
except ImportError:
    pdfplumber = None     # only needed for the entities that publish PDFs

UA = os.environ.get("PROCLENS_UA", "LegalTender/1.0 (procurement transparency research)")
TIMEOUT = 90

# Entities that publish Senate Order 13 listings instead of reporting to
# AusTender. Add to this list as more are identified; the parser is generic.
SOURCES = [
    {
        "entity": "National Disability Insurance Agency",
        "short": "ndia",
        "page": "https://www.ndis.gov.au/policies-rules-and-legal/legal/senate-order-13-entity-contracts",
        "base": "https://www.ndis.gov.au",
    },
    # Finance rates these Material and none of them reported anywhere in this
    # archive before. CSIRO is the largest: nineteen listings back to 2016-17.
    {
        "entity": "Commonwealth Scientific and Industrial Research Organisation",
        "short": "csiro",
        "page": "https://www.csiro.au/en/about/corporate-governance/access-to-information/contracts",
        "base": "https://www.csiro.au",
    },
    {
        "entity": "Defence Housing Australia",
        "short": "dha",
        "page": "https://www.dha.gov.au/about-us/planning-and-reporting/procurement-and-consultancies",
        "base": "https://www.dha.gov.au",
    },
    {
        "entity": "Clean Energy Finance Corporation",
        "short": "cefc",
        "page": "https://www.cefc.com.au/who-we-are/governance/compliance/",
        "base": "https://www.cefc.com.au",
    },
    # Not reachable this way, and left here so the gap stays visible rather than
    # being forgotten. Both publish listings — the files exist and are public —
    # but build their index pages in the browser: the NRF's governance page links
    # none of its PDFs in the served HTML, and ARPC's publications page renders a
    # "Senate Orders (0)" filter whose contents arrive by script. Reaching them
    # needs a browser, which is not something a scheduled job should be doing.
    #   National Reconstruction Fund Corporation — nrf.gov.au/who-we-are/our-governance
    #   Australian Reinsurance Pool Corporation — arpc.gov.au/publications/
    {
        "entity": "Reserve Bank of Australia",
        "short": "rba",
        "page": "https://www.rba.gov.au/about-rba/entity-contracts",
        "base": "https://www.rba.gov.au",
    },
    {
        "entity": "National Library of Australia",
        "short": "nla",
        "page": "https://nla.gov.au/about-us/tenders-and-contracts/contracts-by-reporting-period",
        "base": "https://nla.gov.au",
    },
]

# Column headings vary between entities and between periods, so match on intent
# rather than on an exact string.
COLMAP = [
    ("supplier",      r"contractor|supplier|vendor|company"),
    ("abn",           r"\babn\b"),
    ("title",         r"subject matter|description|subject"),
    ("start",         r"commencement|start date"),
    ("end",           r"anticipated end|end date|expiry"),
    ("confidential",  r"provisions requiring the parties to maintain"),
    ("conf_reason",   r"^reason ?\(s\)$"),
    ("conf_other",    r"other requirements of confidentiality"),
    ("value",         r"consideration"),
    ("cn",            r"contract id|contract number"),
    ("ctype",         r"contract type"),
    ("method",        r"procurement method"),
    ("approached",    r"suppliers approached"),
    ("value_first",   r"original contract value"),
    ("variations",    r"number of variation"),
]

FIELDS = ["entity", "supplier", "abn", "title", "value", "value_first", "variations",
          "start", "end", "method", "ctype", "approached", "confidential",
          "conf_reason", "cn", "period", "source_url"]


def get(url, binary=False):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content if binary else r.text


def discover(src):
    """Find the listings linked from an entity's Senate Order page.

    Two shapes: a direct link to a file, which is what most entities publish, and
    a content-management id whose visible text carries the period, which is what
    the NDIA publishes."""
    html = get(src["page"])
    out, seen = [], set()
    for m in re.finditer(r'href="([^"]+\.(xlsx|xls|csv|pdf))"([^>]*)>(.{0,400}?)</a>',
                         html, re.S | re.I):
        href, ext, _, label = m.groups()
        url = href if href.startswith("http") else src["base"] + ("" if href.startswith("/") else "/") + href
        if url in seen:
            continue
        seen.add(url)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", label)).strip()
        # Governance pages carry board charters, privacy policies and investment
        # mandates beside the listings. Only take what says it is a listing: a
        # contract listing, or a Senate Order, or a reporting period.
        hay = (text + " " + href).lower()
        if not re.search(r"senate\s*order|entity\s*contract|contract.{0,20}listing|"
                         r"listing.{0,20}contract|murray motion|contract register", hay):
            continue
        out.append({"url": url, "label": (text or href.rsplit("/", 1)[-1])[:120],
                    "period": period_of(text + " " + href),
                    "xlsx": ext.lower() in ("xlsx", "xls")})
    if out:
        return out
    # Links are media ids; the visible text carries the reporting period.
    for m in re.finditer(r'href="(/media/(\d+)/download[^"]*)"([^>]*)>(.{0,400}?)</a>',
                         html, re.S | re.I):
        href, mid, _, label = m.groups()
        if mid in seen:
            continue
        seen.add(mid)
        text = re.sub(r"<[^>]+>", " ", label)
        text = re.sub(r"\s+", " ", text).strip()
        if not re.search(r"xlsx|excel|csv", text, re.I) and not re.search(r"20\d\d", text):
            continue
        out.append({"url": src["base"] + href, "label": text[:120],
                    "period": period_of(text), "xlsx": bool(re.search(r"xlsx|excel", text, re.I))})
    return out


MONTHS = ("january|february|march|april|may|june|july|august|september|october|"
          "november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec")


def period_of(text):
    """Which reporting period a listing covers.

    Read from the two dates that bound it, because the file names do not agree on
    a convention: "1 July 2024 to 30 June 2025" and "1-Jan-2024-31-Dec-2024" and
    "2023-24" all appear, and matching digits out of a name turned the first into
    "2024-30". A period that runs July to June is the financial year; one that
    runs January to December is the calendar year."""
    t = text.lower()
    dates = re.findall(r"(?:\d{1,2}\s*[-\s]\s*)?(" + MONTHS + r")\w*\s*[-\s]\s*(20\d\d)", t)
    if len(dates) >= 2:
        (m1, y1), (m2, y2) = dates[0], dates[-1]
        if m1.startswith("jul") and m2.startswith("jun") and y2 == str(int(y1) + 1):
            return f"{y1}-{y2[2:]}"
        if m1.startswith("jan") and m2.startswith("dec") and y1 == y2:
            return y1
        return f"{y1}-{y2[2:]}" if y1 != y2 else y1
    m = re.search(r"(20\d\d)\s*[-–—/]\s*(?:20)?(\d\d)\b", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.search(r"(20\d\d)", t)
    return m.group(1) if m else "unknown"


def header_row(rows):
    for i, r in enumerate(rows[:25]):
        if r and sum(1 for c in r if c) > 4:
            return i
    return None


def map_columns(cols):
    idx = {}
    for i, c in enumerate(cols):
        name = re.sub(r"\s+", " ", str(c or "")).strip()
        if not name:
            continue
        for key, pat in COLMAP:
            if key in idx:
                continue
            if re.search(pat, name, re.I):
                idx[key] = i
                break
    return idx


def as_money(v):
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    if not v:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(v))
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def as_date(v):
    if isinstance(v, datetime):
        return v.date().isoformat()
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s[:20], fmt).date().isoformat()
        except ValueError:
            pass
    return s[:10] or None


def parse(blob, entity, period, url):
    """A listing, whichever way the entity chose to publish it."""
    if blob[:4] == b"%PDF":
        return parse_pdf(blob, entity, period, url)
    return parse_xlsx(blob, entity, period, url)


def parse_pdf(blob, entity, period, url):
    """PDF listings, read by their ruled table structure.

    Tables run across pages and the header is repeated on each, so every table is
    taken and any row that repeats the header is dropped. A row with no supplier
    or no readable amount is dropped too, and counted: a misread column would put
    a wrong supplier against a real amount, which is worse than a missing row."""
    if pdfplumber is None:
        sys.exit("so13.py needs pdfplumber for PDF listings: pip install pdfplumber")
    out, idx, dropped = [], None, 0
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue
                rows = [[("" if c is None else str(c).replace("\n", " ").strip()) for c in r]
                        for r in table]
                h = next((i for i, r in enumerate(rows[:3]) if map_columns(r).get("supplier") is not None), None)
                if h is not None:
                    idx = map_columns(rows[h])
                    body = rows[h + 1:]
                elif idx is None:
                    continue              # a table before any header is not a listing
                else:
                    body = rows
                for r in body:
                    row = build_row(r, idx, entity, period, url)
                    if row is None:
                        dropped += 1
                    else:
                        out.append(row)
    if dropped:
        print(f"    {dropped} row(s) dropped: no readable amount", file=sys.stderr)
    return out


def build_row(r, idx, entity, period, url):
    """One contract, or None when the row carries nothing to record.

    A blank contractor is not a reason to discard a row. CSIRO's listing names no
    contractor for a run of its contracts — one of them worth $27,025,790.75,
    several marked confidential for costing or profit reasons — and those are
    disclosed contracts with an unnamed counterparty, which is a fact about the
    listing rather than a parsing failure. The amount is what a row must have."""
    def cell(k):
        i = idx.get(k)
        v = r[i] if i is not None and i < len(r) else None
        return v
    supplier = str(cell("supplier") or "").strip()
    if re.match(r"^(total|contractor|contractor name|supplier|vendor)$", supplier, re.I):
        return None                                  # a repeated header
    value = as_money(cell("value"))
    if value is None:
        return None                                  # nothing to record
    return {
        "entity": entity, "supplier": supplier or "(contractor not named in the listing)",
        "abn": re.sub(r"\D", "", str(cell("abn") or "")) or "",
        "title": str(cell("title") or "").strip(),
        "value": value, "value_first": as_money(cell("value_first")),
        "variations": cell("variations"),
        "start": as_date(cell("start")), "end": as_date(cell("end")),
        "method": str(cell("method") or "").strip(),
        "ctype": str(cell("ctype") or "").strip(),
        "approached": cell("approached"),
        "confidential": str(cell("confidential") or "").strip(),
        "conf_reason": str(cell("conf_reason") or "").strip(),
        "cn": str(cell("cn") or "").strip(),
        "period": period, "source_url": url,
    }


def parse_xlsx(blob, entity, period, url):
    wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    sheet = next((n for n in wb.sheetnames if re.search(r"contract|listing", n, re.I)),
                 wb.sheetnames[-1])
    rows = [r for r in wb[sheet].iter_rows(values_only=True)]
    h = header_row(rows)
    if h is None:
        return []
    idx = map_columns(rows[h])
    if "supplier" not in idx:
        return []
    out = []
    for r in rows[h + 1:]:
        if not r or not any(r):
            continue
        def cell(k):
            i = idx.get(k)
            return r[i] if i is not None and i < len(r) else None
        supplier = str(cell("supplier") or "").strip()
        if not supplier or re.match(r"^(total|contractor)$", supplier, re.I):
            continue
        out.append({
            "entity": entity,
            "supplier": supplier,
            "abn": re.sub(r"\D", "", str(cell("abn") or "")) or "",
            "title": str(cell("title") or "").strip(),
            "value": as_money(cell("value")),
            "value_first": as_money(cell("value_first")),
            "variations": cell("variations"),
            "start": as_date(cell("start")),
            "end": as_date(cell("end")),
            "method": str(cell("method") or "").strip(),
            "ctype": str(cell("ctype") or "").strip(),
            "approached": cell("approached"),
            "confidential": str(cell("confidential") or "").strip(),
            "conf_reason": str(cell("conf_reason") or "").strip(),
            "cn": str(cell("cn") or "").strip(),
            "period": period,
            "source_url": url,
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="Ingest Senate Order 13 contract listings.")
    ap.add_argument("--out", default="data/so13")
    ap.add_argument("--only", help="restrict to one entity short name, e.g. ndia")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    manifest, total = [], 0
    for src in SOURCES:
        if args.only and src["short"] != args.only:
            continue
        try:
            files = discover(src)
        except Exception as e:
            print(f"! {src['short']}: cannot read page ({e})", file=sys.stderr)
            continue
        print(f"{src['short']}: {len(files)} listing files found", file=sys.stderr)
        for f in files:
            # Both formats are read now. An entity that publishes the same period
            # as a spreadsheet and a PDF is read from the spreadsheet, which needs
            # no table reconstruction.
            if not f["xlsx"] and any(g["xlsx"] and g["period"] == f["period"] for g in files):
                print(f"  skip (spreadsheet exists for {f['period']}): {f['label'][:60]}",
                      file=sys.stderr)
                continue
            try:
                rows = parse(get(f["url"], binary=True), src["entity"], f["period"], f["url"])
            except Exception as e:
                print(f"  ! {f['label']}: {e}", file=sys.stderr)
                continue
            if not rows:
                print(f"  ! {f['label']}: no rows parsed", file=sys.stderr)
                continue
            name = f"{src['short']}-{f['period']}.json"
            path = os.path.join(args.out, name)
            payload = {"entity": src["entity"], "period": f["period"],
                       "source_url": f["url"], "label": f["label"],
                       "fields": FIELDS,
                       "rows": [[r.get(k) for k in FIELDS] for r in rows]}
            blob = json.dumps(payload, separators=(",", ":"), default=str)
            old = None
            try:
                with open(path) as fh:
                    old = fh.read()
            except OSError:
                pass
            if old != blob:
                with open(path, "w") as fh:
                    fh.write(blob)
            val = sum(r["value"] for r in rows if r.get("value"))
            manifest.append({"entity": src["entity"], "short": src["short"],
                             "period": f["period"], "file": name, "count": len(rows),
                             "total": round(val, 2), "source_url": f["url"]})
            total += len(rows)
            print(f"  {f['period']}: {len(rows)} contracts, ${val:,.0f}", file=sys.stderr)

    manifest.sort(key=lambda m: (m["short"], m["period"]), reverse=True)
    with open(os.path.join(args.out, "index.json"), "w") as fh:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "Senate Order 13 listings from entities that do not report to "
                    "AusTender. Values are GST inclusive and cover contracts of "
                    "$100,000 or more only, so smaller spend is absent by design.",
            "fields": FIELDS,
            "listings": manifest,
        }, fh, separators=(",", ":"))
    print(f"Total {total} contracts across {len(manifest)} listings.", file=sys.stderr)


if __name__ == "__main__":
    main()
