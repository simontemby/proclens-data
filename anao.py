#!/usr/bin/env python3
"""
What the Auditor-General has already examined.

This archive records what was bought. It cannot say whether a procurement was
run well, whether a contract delivered, or what happened inside an arrangement —
and for those questions somebody has often already done the work. The
Auditor-General has published 1,484 performance audits, many of them on
procurement, and they carry findings and figures that appear in no register:
what a system actually cost, why a tender was re-run, what a panel was used for.

This indexes them: report number, title, publication date, the audit's own stated
objective, and the entities it names. It does not try to read the findings. An
audit's conclusions are argument, not data, and summarising them automatically
would put words in the Auditor-General's mouth. What it does is let a reader
looking at an agency see that its procurement has been audited, and go and read
it.

The index is public and plainly served — anao.gov.au answers an ordinary client,
paginates with a query parameter and will return 120 records a page, so the whole
catalogue is about a dozen requests.

    python anao.py
"""
import argparse
import html
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import build

# anao.gov.au hangs — not refuses, hangs until the client gives up — when reached
# from a GitHub runner. A hang rather than a refusal, from a cloud network only,
# is the signature of a host that advertises IPv6 and does not answer on it, so
# the crawl is pinned to IPv4. Their robots.txt permits /pubs/ and /work/; this
# is accommodating broken plumbing, not working around a decision.
def _ipv4_only():
    import socket
    orig = socket.getaddrinfo

    def only_v4(host, port, family=0, *a, **kw):
        return orig(host, port, socket.AF_INET, *a, **kw)
    socket.getaddrinfo = only_v4


_ipv4_only()

DATA = os.environ.get("PROCLENS_DATA", "data")
INDEX = "https://www.anao.gov.au/pubs/performance-audit"
SITE = "https://www.anao.gov.au"
# No URL in here, deliberately. anao.gov.au hangs — not refuses, hangs until the
# client times out — on any User-Agent containing one, which is a WAF rule against
# a common bot signature. The client still says plainly what it is.
UA = "Mozilla/5.0 (compatible; legal-tender-archive/1.0; procurement transparency research)"
PER_PAGE = 120
PAUSE = 1.0


def text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


def as_date(s):
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(2).lower())
    return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}" if mon else None


def page(s, n):
    r = s.get(INDEX, params={"items_per_page": PER_PAGE, "page": n}, timeout=120)
    r.raise_for_status()
    return r.text


def audits_on(page_html):
    """One record per audit.

    The list is markup, not data, and an audit's report number and publication
    date are printed BEFORE the link that carries its title. So each record is
    assembled from the text leading up to its own link, not the text after it —
    reading forward found four report numbers out of 1,481 and no dates at all."""
    out = []
    marker = 'href="/work/performance-audit/'
    parts = page_html.split(marker)
    # Each audit is linked twice — once from a block with no text, once from its
    # title — and its report number and date are printed once, before the first
    # of them. Reading only the text immediately before the link we keep found
    # the number for none of them, so the metadata is carried forward from
    # wherever it appears to the next record that has a title.
    pending = {}
    for i in range(1, len(parts)):
        before = text(parts[i - 1][-2500:])
        num = re.search("Auditor-General Report No\\.?\\s*(\\d+)\\s*of\\s*"
                        "(\\d{4}[\u2013\u2014-]\\d{2})", before)
        pub = re.search(r"Published:\s*(?:[A-Za-z]+\s+)?(\d{1,2}\s+[A-Za-z]+\s+\d{4})", before)
        # Merged, not replaced: "Published:" is printed again closer to the title,
        # and replacing the whole block there threw the report number away.
        if num:
            pending["report_no"] = f"{num.group(1)} of {num.group(2)}"
        if pub:
            pending["published"] = as_date(pub.group(1))
        m = re.match(r'([^"#?]+)"', parts[i])
        if not m:
            continue
        path = "/work/performance-audit/" + m.group(1)
        title_m = re.search(r">([^<>]{12,200})</a>", parts[i][:1200])
        title = text(title_m.group(1)) if title_m else ""
        if not title:
            continue
        obj = re.search(r"(The audit objective[^|]{10,600})", text(parts[i][:2600]))
        out.append({
            "url": SITE + path,
            "slug": path.rsplit("/", 1)[-1],
            "title": title,
            "report_no": pending.get("report_no"),
            "published": pending.get("published"),
            "objective": obj.group(1)[:600] if obj else None,
        })
        pending = {}
    return out


FIELD = re.compile(r'field--name-field-report-(entity|portfolio|sector)\b(.{0,400}?)'
                   r'(?=field--name-|</article>)', re.S)


def detail(s, url):
    """Who an audit examined, read from the report rather than guessed from its
    title.

    Matching an agency to an audit by the words in its name looked plausible and
    was not: "Australian National Audit Office" took 338 audits because every
    audit mentions auditing, and "System Administration" took 46 on the strength
    of two ordinary words. The ANAO tags each report with the entity and
    portfolio it covers, so the join is an exact name rather than an inference."""
    out = {}
    try:
        t = s.get(url, timeout=120).text
    except Exception:                                  # noqa: BLE001
        return out
    for name, blob in FIELD.findall(t):
        parts = [x.strip() for x in
                 html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", blob))).split("|")
                 if x.strip()]
        # The block reads: css classes, the label, then the value.
        vals = [x for x in parts if not x.startswith(("field--", "<", "class=")) 
                and x.lower() not in ("entity", "portfolio", "sector") and len(x) > 3]
        if vals:
            out[name] = vals[0]
    # An audit that covers the whole service is not an audit of one agency. The
    # ANAO writes that a dozen ways — "Across Agency", "Across entities (listed
    # below)", "NO-DEPTS-LISTED" — and attributing any of them to a single
    # agency would put an audit on a page it does not belong to.
    e = out.get("entity") or ""
    if re.match(r"^\s*across\s|^no-depts-listed", e, re.I):
        out["entity"] = None
        out["scope"] = "Across agencies"
    return out


def collect(s):
    found, n = {}, 0
    while n < 40:
        try:
            htm = page(s, n)
        except Exception as e:                        # noqa: BLE001
            print(f"  ! page {n}: {str(e)[:90]}", file=sys.stderr)
            break
        rows = audits_on(htm)
        fresh = [r for r in rows if r["slug"] not in found]
        for r in rows:
            found.setdefault(r["slug"], r)
        print(f"  page {n}: {len(rows)} listed, {len(found):,} distinct", file=sys.stderr, flush=True)
        if not fresh:
            break
        n += 1
        time.sleep(PAUSE)
    return found


def main():
    ap = argparse.ArgumentParser(description="Index Auditor-General performance audits.")
    ap.add_argument("--out", default=os.path.join(DATA, "anao", "audits.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--entities", action="store_true",
                    help="also read each audit's own page for the entity and portfolio it "
                         "covers. Only audits that do not already have one are fetched.")
    args = ap.parse_args()

    s = requests.Session()
    s.headers["User-Agent"] = UA
    retry = Retry(total=3, connect=3, read=3, backoff_factor=2.0,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    found = collect(s)
    if len(found) < 100:
        sys.exit(f"anao: only {len(found)} audits found; refusing to replace the store on "
                 f"what looks like a failed crawl")

    today = datetime.now(timezone.utc).date().isoformat()
    existing = build.read_json(args.out, {})
    store = {r["slug"]: r for r in existing.get("audits", [])}
    new = 0
    for slug, r in found.items():
        r["first_seen"] = (store.get(slug) or {}).get("first_seen") or today
        if slug not in store:
            new += 1
        store[slug] = r
    if args.entities:
        need = [r for r in store.values() if not r.get("entity")]
        print(f"reading {len(need):,} audit pages for the entity they cover", file=sys.stderr)
        for i, r in enumerate(need, 1):
            d = detail(s, r["url"])
            r.update({k: v for k, v in d.items() if v})
            if i % 50 == 0:
                print(f"  {i}/{len(need)}", file=sys.stderr, flush=True)
            time.sleep(PAUSE / 2)

    rows = sorted(store.values(), key=lambda r: (r.get("published") or "", r["slug"]), reverse=True)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Performance audits published by the Auditor-General. An index only: report "
                "number, title, date and the audit's own stated objective. The findings are "
                "not summarised here — an audit's conclusions are argument, not data.",
        "source": INDEX,
        "totals": {"audits": len(rows), "new_this_run": new,
                   "with_a_report_number": sum(1 for r in rows if r.get("report_no")),
                   "with_a_date": sum(1 for r in rows if r.get("published")),
                   "with_an_entity": sum(1 for r in rows if r.get("entity"))},
        "audits": rows,
    }
    if args.dry_run:
        import json
        print(json.dumps(payload["totals"], indent=1))
        for r in rows[:5]:
            print(f"  {str(r.get('published')):<12} {str(r.get('report_no') or ''):<14} {r['title'][:58]}")
        return
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    build.write_json_if_changed(args.out, payload)
    summary = f"**{len(rows):,} Auditor-General performance audits** ({new:,} new this run)."
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(summary + "\n")


if __name__ == "__main__":
    main()
