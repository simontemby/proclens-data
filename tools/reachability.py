#!/usr/bin/env python3
"""
Which sources answer this machine, and which refuse it.

Three sources this archive wants are collected by hand because they refuse
GitHub's runners: the Auditor-General's publication index, the Victorian
contract register and buy.nsw. The usual explanation is that they block
anything outside Australia, and the usual remedy is an Australian VM. That is
worth testing before anybody rents one, because the three do not refuse for the
same reason:

                            an Australian         a GitHub runner,
                            datacentre address    US, HTTP / browser
        ANAO                403 Akamai            403 Akamai / 403 Akamai
        Victorian register  200                   403 Cloudflare / 403
        buy.nsw             200                   200 / 200
        AusTender (control) 200                   200 / 200

Three things that were assumed, and are not so:

  - buy.nsw answers a GitHub runner perfectly well. Whatever keeps that
    collection manual, it is not the site refusing.
  - The Victorian register refuses the United States and answers Australia, so
    for that one source an Australian machine is the whole answer.
  - The Auditor-General refuses both, and refuses a real Chromium from the same
    runner exactly as it refuses a plain client. It is reading the address, not
    the client, so a browser is not the way in. Whether it would accept a
    dedicated Australian address is open: the one tested here was a shared
    consumer VPN exit, and Akamai's own words were "a high volume of
    simultaneous submissions from your network", which is what a shared address
    earns. That is the question to settle by running this on the machine
    itself, before trusting it with a collection.

Run it anywhere — a laptop, a VM, a GitHub runner:

    python3 tools/reachability.py

It fetches one representative page per source, reports the status and what the
refusal was, and exits 1 if anything refused, so a workflow can use it as a
probe. Nothing is written and nothing is ingested.
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

SOURCES = [
    ("ANAO publications", "https://www.anao.gov.au/publications/all?f%5B0%5D=type%3Aaudit_report",
     "Auditor-General performance audits — anao.py"),
    ("Victorian register", "https://www.tenders.vic.gov.au/contract/search",
     "Victorian contract register — vic.py"),
    ("buy.nsw", "https://buy.nsw.gov.au/",
     "NSW contract awards — nsw.py reads exports taken from here"),
    ("AusTender OCDS", "https://api.tenders.gov.au/ocds/findByDates/contractPublished/"
                       "2026-01-01T00:00:00Z/2026-01-02T00:00:00Z",
     "the API the archive already collects on GitHub — a control"),
]

# What a refusal is, in the words of the thing refusing. A 403 from Akamai and a
# 403 from Cloudflare call for different answers, and "403" alone calls for none.
TELLS = [
    (r"Akamai|Reference Number: \d+\.\w+", "Akamai bot management — judging the "
     "address, not the client: a real browser from the same machine is refused too"),
    (r"x-amzn-waf-action|awswaf", "AWS WAF challenge — needs a browser to solve it"),
    (r"Just a moment|cf-mitigated|Cloudflare", "Cloudflare interstitial — needs a "
     "browser to solve it"),
    (r"high volume of simultaneous submissions", "rate limit attributed to the whole "
     "network, which a shared address will keep hitting"),
    (r"robots|not permitted|denied", "an explicit refusal rather than a challenge"),
]


def probe(url, timeout=45):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(40000).decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(40000).decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", {}


def why(body, headers):
    hay = body + "\n" + "\n".join(f"{k}: {v}" for k, v in headers.items())
    for pat, said in TELLS:
        if re.search(pat, hay, re.I):
            return said
    return ""


def probe_browser(url, timeout=45):
    """The same question asked by a real browser.

    A bot manager has two things to judge: where the request came from and what
    sent it. A plain HTTP client loses on the second whatever its User-Agent
    says, because the TLS handshake and the order of the headers give it away.
    If a browser on this same machine is let through, the address was never the
    problem and no machine needs to be rented."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(locale="en-AU", user_agent=UA)
        try:
            r = page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            body = page.content()
            return (r.status if r else None), body[:40000], dict(r.headers if r else {})
        except Exception as e:
            return None, f"{type(e).__name__}: {e}", {}
        finally:
            b.close()


def main():
    ap = argparse.ArgumentParser(description="Which sources answer this machine.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--browser", action="store_true",
                    help="ask with a real browser instead of an HTTP client")
    args = ap.parse_args()

    where = {}
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=20) as r:
            where = json.load(r)
    except Exception:
        pass
    here = (f"{where.get('ip', '?')}  {where.get('country', '?')}  "
            f"{where.get('org', 'network unknown')}")
    how = "a real browser" if args.browser else "an HTTP client"
    print(f"From {here}\nAsking as {how}\n", file=sys.stderr)

    rows, refused = [], 0
    for name, url, what in SOURCES:
        status, body, headers = (probe_browser(url) if args.browser else probe(url))
        ok = status == 200 and len(body) > 2000
        # No status at all means nothing answered: a timeout or a reset, which
        # is its own kind of refusal and the one that says least. The exception
        # is the only thing there is to report, so report it.
        said = "" if ok else (why(body, headers) or
                              (body.strip() if status is None else
                               "no explanation given"))
        rows.append({"source": name, "url": url, "for": what, "status": status,
                     "bytes": len(body), "ok": ok, "refusal": said})
        if not ok:
            refused += 1
        mark = "answers" if ok else f"REFUSES ({status})"
        print(f"  {name:20} {mark:16} {len(body):>7,}b  {said}", file=sys.stderr)

    print("", file=sys.stderr)
    if refused:
        print(f"{refused} of {len(SOURCES)} refuse this machine. A source whose refusal "
              f"names a bot manager is judging the network, not the country, and moving "
              f"to another datacentre will not change its mind.", file=sys.stderr)
    else:
        print("Every source answers this machine. It can run the collection that "
              "GitHub's runners cannot.", file=sys.stderr)
    if args.json:
        print(json.dumps({"from": where, "results": rows}, indent=1))
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
