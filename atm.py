#!/usr/bin/env python3
"""
Approach-to-market ingest — the notices, before anyone has won anything.

Everything else in this archive is retrospective: a contract notice exists only
once the money is committed. This script watches the other end, the approaches
to market, so a reader can see what government is about to buy rather than only
what it has already bought.

Two things make this harder than it sounds.

The feed is a WINDOW, not a history. AusTender publishes the current ATM list as
75 RSS items and nothing else — no archive, no paging, no date query. An ATM that
opens and closes between two polls is gone for good, and a closed one is dropped
with no record it ever existed. So the store here is permanent and append-only:
the feed is treated as a sighting, never as the truth about what exists.

The feed is also thin. Each item carries a title, a link and a publish date, and
that is all — no agency, no category, no closing date, nothing to alert on. Those
live on the ATM's own page, so each notice is enriched once, on first sighting,
and never fetched again. That page also publishes three fields the OCDS API does
not expose anywhere: whether the approach is a panel arrangement, whether it is
open to multiple agencies, and whether it is multi-stage.

    python atm.py --out data/atm
    python atm.py --out data/atm --no-detail      # feed only, no page fetches
"""
import argparse, hashlib, html, json, os, re, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

FEED = os.environ.get("AUSTENDER_ATM_FEED",
                      "https://www.tenders.gov.au/public_data/rss/rss.xml")
SITE = "https://www.tenders.gov.au"
# tenders.gov.au refuses a plain requests user agent with a 403; the API host
# does not. Same organisation, different front door.
UA = os.environ.get("PROCLENS_BROWSER_UA",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 60
PAUSE = 0.8          # between detail pages; this is a courtesy, not a limit
RETRIES = 3

FIELDS = ["guid", "kind", "atm_id", "title", "agency", "cat", "cat_title", "atm_type",
          "location", "panel", "multi_agency", "multi_stage", "publish", "close",
          "desc", "first_seen", "closed", "detail", "open"]

DICT_FIELDS = ("kind", "agency", "atm_type", "location", "panel", "multi_agency",
               "multi_stage", "cat", "cat_title")

# The detail page renders every attribute the same way, so one pattern reads all
# of them: <label for="Agency">Agency</label>:</span><div class="list-desc-inner">…
# The label text is matched as [^<]* rather than .*? on purpose. With .*? the
# login form's <label for="EmailAddress"> at the top of every page opened a
# match that ran all the way down to the first real field and swallowed it, so
# the ATM ID silently came back empty on every notice.
DESC_RE = re.compile(
    r'<label for="(?P<key>\w+)">[^<]*</label>\s*:\s*</span>\s*'
    r'<div class="list-desc-inner">(?P<val>.*?)</div>', re.S)

DETAIL_KEYS = {"AtmId": "atm_id", "Agency": "agency", "Category": "_cat",
               "CloseDate": "close", "PublishDate": "publish",
               "Locations": "location", "Type": "atm_type",
               "MultiAgencyAccess": "multi_agency", "PanelArrangement": "panel",
               "MultiStage": "multi_stage", "Description": "desc"}

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get(url):
    last = None
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Referer": SITE + "/"},
                             timeout=TIMEOUT)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.text
        except Exception as e:                       # noqa: BLE001 - retry anything
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def strip_tags(s):
    s = re.sub(r"(?is)<br\s*/?>", " ", s or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def as_date(s):
    """AusTender writes dates as 4-Sep-2026, sometimes with a time after it."""
    if not s:
        return None
    m = re.match(r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})", s)
    if not m:
        return None
    d, mon, y = m.groups()
    if mon.title() not in MONTHS:
        return None
    return f"{y}-{MONTHS[mon.title()]:02d}-{int(d):02d}"


def close_time(s):
    m = re.search(r"(\d{1,2}:\d{2}\s*[ap]m)", s or "", re.I)
    return m.group(1).lower().replace(" ", "") if m else ""


# The feed carries two kinds of notice under one roof: ordinary approaches at
# /Atm/Show/ and adverts (grant-style opportunities from research corporations)
# at /Advert/Show/. Assuming everything was an ATM sent five of the seventy-five
# to a redirect and a not-found page, so the path is kept, not reconstructed.
LINK_RE = re.compile(r"/(Atm|Advert)/Show/([0-9a-f-]{16,})", re.I)


def looks_like_id(s):
    """Feed titles read 'ATM ID: subject', but plenty of subjects contain a colon
    of their own. Only treat the prefix as an identifier if it could be one."""
    s = s.strip()
    return bool(s) and len(s) <= 40 and (any(c.isdigit() for c in s)
                                         or s == s.upper())


def parse_feed(xml):
    out = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def tag(t):
            m = re.search(rf"<{t}>(.*?)</{t}>", block, re.S)
            return html.unescape(m.group(1)).strip() if m else ""
        m = LINK_RE.search(tag("link") or tag("guid"))
        if not m:
            continue
        kind, guid = m.group(1).title(), m.group(2)
        # Agencies paste tabs and stray form labels into these fields. Collapse
        # whitespace, which is presentation only, but do not otherwise rewrite
        # what was published — the archive records their text, not a tidied one.
        title = re.sub(r"\s+", " ", tag("title")).strip()
        atm_id, _, subject = title.partition(":")
        if not subject.strip() or not looks_like_id(atm_id):
            atm_id, subject = "", title
        pub = ""
        try:
            pub = parsedate_to_datetime(tag("pubDate")).date().isoformat()
        except Exception:                             # noqa: BLE001
            pass
        out.append({"guid": guid, "kind": kind, "atm_id": atm_id.strip(),
                    "title": subject.strip(), "desc": strip_tags(tag("description")),
                    "publish": pub})
    return out


def fetch_detail(guid, kind="Atm"):
    page = get(f"{SITE}/{kind}/Show/{guid}")
    return parse_detail(page) if page else {}


def parse_detail(page):
    found = {}
    for m in DESC_RE.finditer(page):
        key = DETAIL_KEYS.get(m.group("key"))
        if key and key not in found:
            found[key] = strip_tags(m.group("val"))
    out = {k: v for k, v in found.items() if not k.startswith("_")}
    # Category arrives as "83112200 - Enhanced telecommunications services".
    cat = found.get("_cat", "")
    cm = re.match(r"\s*(\d{6,8})\s*[-–]\s*(.*)", cat)
    if cm:
        out["cat"], out["cat_title"] = cm.group(1), cm.group(2).strip()
    elif cat:
        out["cat_title"] = cat
    if out.get("close"):
        out["close_time"] = close_time(out["close"])
        out["close"] = as_date(out["close"])
    if out.get("publish"):
        out["publish"] = as_date(out["publish"]) or out["publish"]
    # The page also carries a named contact and their email address. That is a
    # person, not procurement data, and nothing here needs it — so it is not
    # captured rather than captured and hidden.
    return out


def load_store(outdir):
    """Read every year shard back into one dict keyed by ATM guid."""
    store = {}
    idx = read_json(os.path.join(outdir, "index.json"), {})
    for sh in idx.get("shards", []):
        payload = read_json(os.path.join(outdir, sh["file"]), {})
        f, d = payload.get("fields", FIELDS), payload.get("dict", {})
        for row in payload.get("rows", []):
            r = {}
            for i, k in enumerate(f):
                v = row[i] if i < len(row) else None
                if k in d and isinstance(v, int):
                    v = d[k][v]
                r[k] = v
            if r.get("guid"):
                store[r["guid"]] = r
    return store


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
    out = []
    for r in rows:
        out.append([lookup[f][r.get(f)] if f in DICT_FIELDS else r.get(f)
                    for f in FIELDS])
    return dicts, out


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default if default is not None else {}


def write_json_if_changed(path, obj):
    """Returns (written, sha). The digest ignores `generated` so a run that
    changes nothing but the clock does not invalidate a cached shard."""
    blob = json.dumps(obj, separators=(",", ":"), default=str)
    stable = {k: v for k, v in obj.items() if k != "generated"}
    sha = hashlib.sha256(json.dumps(stable, separators=(",", ":"), sort_keys=True,
                                    default=str).encode()).hexdigest()[:12]
    try:
        with open(path) as fh:
            if {k: v for k, v in json.load(fh).items() if k != "generated"} == stable:
                return False, sha
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(blob)
    os.replace(tmp, path)
    return True, sha


# ---------------------------------------------------------------- watchlist

def load_watchlist(path):
    wl = read_json(path, {})
    return wl.get("watches", []) if isinstance(wl, dict) else []


# Agencies publish curly quotes, en dashes and non-breaking spaces. A watch term
# typed with a straight apostrophe would never match "Analyst's Notebook" as the
# page actually writes it, so both sides are flattened to the plain characters.
PUNCT = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                       "\u2013": "-", "\u2014": "-", "\u00a0": " "})


def norm(s):
    return re.sub(r"\s+", " ", str(s or "").translate(PUNCT)).lower()


def haystack(r):
    return norm(" ".join(str(r.get(k) or "") for k in
                         ("atm_id", "title", "desc", "agency", "cat", "cat_title",
                          "atm_type", "location")))


# Terms are matched on word boundaries, not as bare substrings. "ndia" inside
# "Indian Ocean Territories" raised a disability-services alert against a
# broadcasting tender; short acronyms make that failure the rule, not the
# exception. The lookarounds are fixed width so a term may contain punctuation.
_TERM_CACHE = {}


def term_re(t):
    t = norm(t)
    if t not in _TERM_CACHE:
        # A trailing "s" is allowed so a watch on "participant" still catches
        # "participants"; requiring an exact word missed the commoner spelling.
        _TERM_CACHE[t] = re.compile(r"(?<![a-z0-9])" + re.escape(t) + r"s?(?![a-z0-9])")
    return _TERM_CACHE[t]


def has_term(t, hay):
    return bool(term_re(t).search(hay))


def matches(watch, r, hay):
    """A watch fires when every condition it states is satisfied. A watch that
    states nothing matches nothing — an empty rule quietly alerting on all 75
    notices would be worse than no rule at all."""
    stated = False
    terms = [t.lower() for t in watch.get("any", []) if t]
    if terms:
        stated = True
        if not any(has_term(t, hay) for t in terms):
            return None
    for t in (watch.get("all") or []):
        stated = True
        if not has_term(t, hay):
            return None
    for t in (watch.get("none") or []):
        if has_term(t, hay):
            return None
    ag = watch.get("agency")
    if ag:
        stated = True
        if ag.lower() not in str(r.get("agency") or "").lower():
            return None
    for field in ("panel", "multi_agency", "multi_stage"):
        want = watch.get(field)
        if want is None:
            continue
        stated = True
        if str(r.get(field) or "").strip().lower() != str(want).strip().lower():
            return None
    if not stated:
        return None
    return [t for t in terms if has_term(t, hay)] or ["rule"]


# ---------------------------------------------------------------------- feed

def atom(alerts, generated, site):
    """A plain Atom file. There is no server here to send email from, and a feed
    is the one alerting mechanism a static host can actually deliver."""
    def esc(s):
        return html.escape(str(s or ""), quote=True)
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<feed xmlns="http://www.w3.org/2005/Atom">',
           "<title>Legal Tender — watchlist alerts</title>",
           f"<link href=\"{esc(site)}\"/>",
           f"<link rel=\"self\" href=\"{esc(site)}data/atm/alerts.xml\"/>",
           f"<id>tag:legaltender,2026:alerts</id>",
           f"<updated>{esc(generated)}</updated>",
           "<subtitle>New Commonwealth approaches to market matching a watchlist."
           " Publication is not an award; nothing here has been won by anyone."
           "</subtitle>"]
    for a in alerts[:200]:
        url = f"{SITE}/{a.get('kind') or 'Atm'}/Show/{a['guid']}"
        body = (f"Watch: {a['watch']}\nAgency: {a.get('agency') or 'not stated'}\n"
                f"Category: {a.get('cat_title') or a.get('cat') or 'not stated'}\n"
                f"ATM type: {a.get('atm_type') or 'not stated'}\n"
                f"Panel arrangement: {a.get('panel') or 'not stated'}\n"
                f"Closes: {a.get('close') or 'not stated'}\n\n{a.get('desc') or ''}")
        out += ["<entry>",
                f"<title>{esc(a['watch'])}: {esc(a['title'])}</title>",
                f"<link href=\"{esc(url)}\"/>",
                f"<id>tag:legaltender,2026:alert:{esc(a['watch'])}:{esc(a['guid'])}</id>",
                f"<updated>{esc(a['seen'])}</updated>",
                f"<summary>{esc(body)}</summary>",
                "</entry>"]
    out.append("</feed>")
    return "\n".join(out)


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Ingest AusTender approaches to market.")
    ap.add_argument("--out", default="data/atm")
    ap.add_argument("--watchlist", default="data/watchlist.json")
    ap.add_argument("--site", default="https://simontemby.github.io/proclens-data/")
    ap.add_argument("--no-detail", action="store_true",
                    help="skip per-notice page fetches (feed fields only)")
    ap.add_argument("--max-detail", type=int, default=150,
                    help="cap detail fetches in one run")
    ap.add_argument("--refresh-detail", action="store_true",
                    help="re-fetch detail for notices already enriched")
    ap.add_argument("--rematch", action="store_true",
                    help="re-evaluate every stored notice against the watchlist, "
                         "for after the watchlist itself has changed")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    store = load_store(args.out)
    print(f"Store holds {len(store):,} notices.", file=sys.stderr)

    try:
        items = parse_feed(get(FEED))
    except Exception as e:                            # noqa: BLE001
        sys.exit(f"! cannot read the ATM feed: {e}")
    if not items:
        sys.exit("! feed parsed to zero items — refusing to mark everything closed")
    print(f"Feed carries {len(items)} current notices.", file=sys.stderr)

    seen = now_iso()
    new_guids = []
    for it in items:
        r = store.get(it["guid"])
        if r is None:
            r = dict(it)
            r["first_seen"] = seen
            r["detail"] = 0
            store[it["guid"]] = r
            new_guids.append(it["guid"])
        else:
            # The feed is a sighting. Keep the earliest first_seen and never let
            # a thinner later reading overwrite an enriched field with "".
            for k, v in it.items():
                if v and not r.get(k):
                    r[k] = v
        # A notice that reappears after dropping out is open again.
        r["closed"] = None

    # Enrich currently-open notices first: a page for a closed ATM may already
    # be gone, and an open one is what a reader is actually waiting on.
    live = {i["guid"] for i in items}
    todo = sorted((g for g in store
                   if args.refresh_detail or not store[g].get("detail")),
                  key=lambda g: (g not in live, store[g].get("publish") or ""))
    fetched, enriched_guids = 0, []
    if not args.no_detail:
        for guid in todo[:args.max_detail]:
            try:
                d = fetch_detail(guid, store[guid].get("kind") or "Atm")
            except Exception as e:                    # noqa: BLE001
                print(f"  ! {guid}: {e}", file=sys.stderr)
                continue
            if d:
                d.pop("close_time", None)
                store[guid].update({k: v for k, v in d.items() if v})
                store[guid]["detail"] = 1
                enriched_guids.append(guid)
                fetched += 1
            time.sleep(PAUSE)
        if len(todo) > args.max_detail:
            print(f"  {len(todo) - args.max_detail} notices left to enrich next run",
                  file=sys.stderr)
    print(f"  {len(new_guids)} new, {fetched} enriched", file=sys.stderr)

    # -------- alerts. Only ever raised for notices first seen in THIS run, so a
    # new watchlist entry does not replay years of history into the feed at once.
    watches = load_watchlist(args.watchlist)
    prior = read_json(os.path.join(args.out, "alerts.json"), {})
    # Alerts raised by a watch that has since been deleted or renamed are dropped.
    # Leaving them would show results under a rule that no longer exists, and the
    # chip filtering them on the site would not appear at all.
    live_names = {w.get("name") or "watch" for w in watches}
    alerts = [a for a in prior.get("alerts", []) if a.get("watch") in live_names]
    dropped = len(prior.get("alerts", [])) - len(alerts)
    if dropped:
        print(f"  {dropped} alerts dropped: their watch is no longer in the watchlist",
              file=sys.stderr)
    raised = {(a["watch"], a["guid"]) for a in alerts}
    fresh = 0
    # Enriched notices are re-checked as well as new ones. A notice seen from the
    # feed alone has no agency, category or panel flag, so a watch that depends
    # on those could never fire for it; if the enrichment lands on a later run,
    # matching only what is new would miss it permanently. The (watch, guid) set
    # keeps the second look from raising anything twice.
    # --rematch exists because a watch normally fires only forward. That is right
    # by default — a new rule should not replay years of history into the feed —
    # but after editing the watchlist you want to know what it would have caught.
    candidates = list(store) if args.rematch else dict.fromkeys(new_guids + enriched_guids)
    for guid in candidates:
        r = store[guid]
        hay = haystack(r)
        for w in watches:
            name = w.get("name") or "watch"
            if (name, guid) in raised:
                continue
            hit = matches(w, r, hay)
            if not hit:
                continue
            raised.add((name, guid))
            alerts.append({"watch": name, "guid": guid, "kind": r.get("kind"),
                           "atm_id": r.get("atm_id"),
                           "title": r.get("title"), "agency": r.get("agency"),
                           "cat": r.get("cat"), "cat_title": r.get("cat_title"),
                           "atm_type": r.get("atm_type"), "panel": r.get("panel"),
                           "close": r.get("close"), "desc": (r.get("desc") or "")[:400],
                           "hit": hit, "seen": seen})
            fresh += 1
    alerts.sort(key=lambda a: a.get("seen") or "", reverse=True)
    alerts = alerts[:2000]
    write_json_if_changed(os.path.join(args.out, "alerts.json"),
                          {"generated": seen, "watches": [w.get("name") for w in watches],
                           "alerts": alerts})
    feed_xml = atom(alerts, (alerts[0]["seen"] if alerts else seen), args.site)
    path = os.path.join(args.out, "alerts.xml")
    try:
        unchanged = open(path).read() == feed_xml
    except OSError:
        unchanged = False
    if not unchanged:
        with open(path, "w") as fh:
            fh.write(feed_xml)
    print(f"  {fresh} new alerts ({len(alerts)} in the feed)", file=sys.stderr)

    # -------- shards, by publish year, so the front end's shard loader can read
    # them with no new code.
    by_year = defaultdict(list)
    today = seen[:10]
    for r in store.values():
        was_open = r.get("open")
        r["open"] = 1 if r["guid"] in live else 0
        # Stamp the closing date once, on the run that first misses the notice.
        # Writing a "last seen" on every row every poll would rewrite every
        # shard twice a day: a commit and a fresh cache-bust digest for browsers
        # to re-download, all to record that nothing happened.
        if not r["open"] and was_open is not False and not r.get("closed"):
            r["closed"] = today if was_open is not None else r.get("first_seen", "")[:10]
        y = (r.get("publish") or r.get("first_seen") or "")[:4] or "unknown"
        by_year[y].append(r)
    shards = []
    for year in sorted(by_year):
        rows = sorted(by_year[year], key=lambda r: r.get("publish") or "", reverse=True)
        name = f"atm-{year}.json"
        p = os.path.join(args.out, name)
        dicts, enc = encode(rows)
        _, sha = write_json_if_changed(p, {"year": year, "fields": FIELDS,
                                           "dict": dicts, "rows": enc})
        shards.append({"year": year, "file": name, "count": len(rows),
                       "bytes": os.path.getsize(p), "sha": sha})
    shards.sort(key=lambda s: s["year"], reverse=True)

    closing = sum(1 for r in store.values() if r.get("open"))
    write_json_if_changed(os.path.join(args.out, "index.json"), {
        "generated": seen,
        "note": "Approaches to market, captured from AusTender's current-ATM feed. "
                "The feed holds only what is open right now, so this archive begins "
                "at first capture and cannot recover notices that closed before it. "
                "An approach is not an award: nothing here has been won by anyone.",
        "fields": FIELDS,
        "shards": shards,
        "totals": {"notices": len(store), "open": closing,
                   "new_this_run": len(new_guids), "alerts": len(alerts)},
        "capture_start": min((r.get("first_seen") or "" for r in store.values()),
                             default=seen)[:10],
    })
    print(f"Total {len(store):,} notices, {closing} currently open, "
          f"{len(shards)} shards.", file=sys.stderr)


if __name__ == "__main__":
    main()
