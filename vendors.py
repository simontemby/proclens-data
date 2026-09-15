"""
Who is really behind a contract bought through an intermediary.

A notice names the supplier the agency paid. When that supplier is a reseller or
integrator, the vendor whose product was bought goes unrecorded: the ATO's $2.5
million "Graph Database Software and Services" contract names Intech Solutions,
not the database. This works out, where the evidence allows, whose product it
most likely was — and says exactly how sure it is and why.

Scope is software and IT: UNSPSC software and computer services codes, or a
description about software, licences, subscriptions or platforms. Outside that
the question does not arise in the same way.

Evidence, strongest first. Nothing below the last tier is guessed.

  stated    The contract's own description names the vendor.
  likely    A published announcement names this supplier, vendor and buyer; or
            a pattern rule whose measured accuracy is at least 90%.
  possible  A documented partner of the supplier with a matching keyword; or a
            pattern rule measured below 90%.

The two pattern rules — what other contracts between the same agency and
supplier name, and what the supplier's contracts name overall — are tested on
every build against contracts whose vendor IS named, and each presumption
carries that measured accuracy in its evidence.

A supplier counts as an intermediary when its software contracts name three or
more other vendors, when it is a known licensing channel, or when a documented
partnership says so. A vendor selling its own product is not an intermediary
sale: a contract with Palantir for Palantir is the one case where the vendor is
not in question.
"""
import json
import os
import re
from collections import Counter, defaultdict

import refresh

MIN_VENDORS_FOR_INTERMEDIARY = 3
# Thresholds were chosen by testing, not taste. Hiding the vendor on the 6,182
# contracts that name exactly one, then asking each rule to guess it: at 2 and
# 60% the agency-and-supplier rule was right 81.7% of the time; at 3 and 80% it
# is right 91.9%. The supplier rule at 8 and 90% is right 95.6%. The same test
# runs on every build and its current result is printed in the evidence, so the
# claim cannot drift away from the data.
PAIR_MIN_NAMED, PAIR_SHARE = 3, 0.8
SUPPLIER_MIN_NAMED, SUPPLIER_SHARE = 8, 0.9
LIKELY_FLOOR = 90.0   # a rule below this measured accuracy is called "possible"
GOVERNMENT = re.compile(r"\b(department|agency|commission|authority|office of|bureau|"
                        r"australian government|commonwealth)\b", re.I)


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").lower().replace("’", "'"))


def _pat(words):
    return re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(w) for w in words) + r")s?(?![a-z0-9])")


def load(data_dir):
    path = os.path.join(data_dir, "vendors.json")
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        lex = json.load(fh)
    lex["_alias"] = {v: _pat(d["aliases"]) for v, d in lex["vendors"].items()}
    lex["_self"] = {v: _pat(d["aliases"] + d.get("entity", []) + [v.lower()])
                    for v, d in lex["vendors"].items()}
    sc = lex.get("scope", {})
    lex["_scope_words"] = _pat(sc.get("words", []))
    lex["_scope_cats"] = tuple(sc.get("unspsc_prefixes", []))
    return lex


def in_scope(rec, lex):
    cat = str(rec.get("cat") or "")
    if cat.startswith(lex["_scope_cats"]):
        return True
    if "software" in _norm(rec.get("category")):
        return True
    return bool(lex["_scope_words"].search(_norm(rec.get("title"))))


def infer(corpus, data_dir, report):
    lex = load(data_dir)
    if not lex:
        report["vendors"] = "no data/vendors.json"
        return
    partners = defaultdict(list)
    for p in lex.get("partners", []):
        partners[re.sub(r"\D", "", p.get("abn") or "")].append(p)

    # Pass 1: what each in-scope contract names, and who is naming whom.
    facts = {}
    supplier_vendors = defaultdict(Counter)   # supplier -> third-party vendors named
    supplier_named = Counter()                # supplier -> contracts naming a vendor
    pair_vendors = defaultdict(lambda: defaultdict(list))  # (buyer, supplier) -> vendor -> CNs
    for key, rec in corpus.items():
        if ":" in key or not in_scope(rec, lex):
            continue
        title, sup = _norm(rec.get("title")), _norm(rec.get("supplier"))
        named = {v for v, p in lex["_alias"].items() if p.search(title)}
        own = {v for v in named if lex["_self"][v].search(sup)}
        third = named - own
        skey = rec.get("abn") or "name:" + sup
        facts[key] = (third, own, skey)
        if third:
            supplier_named[skey] += 1
            for v in third:
                supplier_vendors[skey][v] += 1
                pair_vendors[(rec.get("buyer"), skey)][v].append(key)

    # Measure each rule before using it: hide the vendor on every contract that
    # names exactly one, remove that contract from the counts it would inform,
    # and see whether the rule recovers it.
    def pair_guess(buyer, skey, exclude=None, truth=None):
        pv = {v: [c for c in cns if c != exclude] for v, cns in (pair_vendors.get((buyer, skey)) or {}).items()}
        total = sum(len(c) for c in pv.values())
        if total < PAIR_MIN_NAMED:
            return None
        v, cns = max(pv.items(), key=lambda kv: len(kv[1]))
        return (v, cns, total) if len(cns) >= PAIR_MIN_NAMED and len(cns) / total >= PAIR_SHARE else None

    def supplier_guess(skey, truth=None):
        c = supplier_vendors[skey].copy()
        n_named = supplier_named[skey]
        if truth:
            c[truth] -= 1
            n_named -= 1
        if n_named < SUPPLIER_MIN_NAMED or not c:
            return None
        v, n = c.most_common(1)[0]
        return (v, n, n_named) if n / n_named >= SUPPLIER_SHARE else None

    trial = Counter()
    for key, (third, own, skey) in facts.items():
        if len(third) != 1:
            continue
        truth = next(iter(third))
        g = pair_guess(corpus[key].get("buyer"), skey, exclude=key)
        if g:
            trial["pair_n"] += 1
            trial["pair_ok"] += g[0] == truth
        elif supplier_guess(skey, truth):
            g = supplier_guess(skey, truth)
            trial["sup_n"] += 1
            trial["sup_ok"] += g[0] == truth
    acc = {r: (round(100 * trial[r + "_ok"] / trial[r + "_n"], 1) if trial[r + "_n"] else 0.0, trial[r + "_n"])
           for r in ("pair", "sup")}
    report["vendor_rule_accuracy"] = {"agency_and_supplier": {"right_pct": acc["pair"][0], "tested_on": acc["pair"][1]},
                                      "supplier": {"right_pct": acc["sup"][0], "tested_on": acc["sup"][1]}}

    def tier(rule):
        return "likely" if acc[rule][0] >= LIKELY_FLOOR else "possible"

    def tested(rule):
        return (f" Tested on {acc[rule][1]:,} contracts whose vendor is named, this rule is right "
                f"{acc[rule][0]:g}% of the time.")

    def intermediary(skey, sup):
        if GOVERNMENT.search(sup or "") and not partners.get(skey):
            # An agency re-supplying another agency is coordinated procurement,
            # not a reseller; it is still noted where it names vendors below.
            return len(supplier_vendors[skey]) >= MIN_VENDORS_FOR_INTERMEDIARY
        return (len(supplier_vendors[skey]) >= MIN_VENDORS_FOR_INTERMEDIARY
                or bool(refresh.PLATFORM_RE.search(sup or ""))
                or bool(partners.get(skey)))

    tally = Counter()
    for key, (third, own, skey) in facts.items():
        rec = corpus[key]
        sup = rec.get("supplier") or ""
        if own and not third:
            continue                      # the vendor selling its own product
        if not intermediary(skey, sup):
            continue
        evidence = []
        n_vendors = len(supplier_vendors[skey])
        if n_vendors >= MIN_VENDORS_FOR_INTERMEDIARY:
            profile = (f"{sup} is a reseller or integrator: its software contracts name {n_vendors} "
                       f"other vendors across {supplier_named[skey]:,} contracts")
        elif partners.get(skey):
            profile = (f"{sup} is a documented partner of "
                       f"{', '.join(dict.fromkeys(p['vendor'] for p in partners[skey]))}")
        else:
            profile = f"{sup} is a software licensing reseller"

        if third:
            for v in sorted(third):
                evidence.append([v, "stated", "named", "The contract's description names " + v + ".", None, []])
        else:
            title = _norm(rec.get("title"))
            buyer = rec.get("buyer") or ""
            # documented partnership naming this buyer
            for p in partners.get(skey, []):
                kw = any(k in title for k in p.get("keywords", []))
                named_buyer = any(b.lower() == buyer.lower() for b in p.get("buyers", []))
                if kw and named_buyer:
                    evidence.append([p["vendor"], "likely", "partner",
                                     f"{sup} and {p['vendor']} publicly announced a deal with {buyer}, "
                                     f"and this contract's description matches: “{p['evidence']}”",
                                     p["source"], []])
            if not evidence:
                g = pair_guess(buyer, skey)
                if g:
                    v, cns, total = g
                    evidence.append([v, tier("pair"), "pattern",
                                     f"{len(cns)} of {total} other software contracts between {buyer} "
                                     f"and {sup} that name a vendor name {v}." + tested("pair"),
                                     None, sorted(cns)[-3:]])
            if not evidence:
                for p in partners.get(skey, []):
                    if any(k in title for k in p.get("keywords", [])):
                        evidence.append([p["vendor"], "possible", "partner",
                                         f"{sup} is a documented {p['vendor']} partner, and this contract's "
                                         f"description mentions {', '.join(p['keywords'])}.", p["source"], []])
            if not evidence:
                g = supplier_guess(skey)
                if g:
                    v, n, n_named = g
                    evidence.append([v, tier("sup"), "supplier",
                                     f"{n} of {n_named} of {sup}'s software contracts that name a "
                                     f"vendor name {v}." + tested("sup"), None, []])

        rec["intermediary"] = profile
        tally["intermediary_contracts"] += 1
        if evidence:
            order = {"stated": 0, "likely": 1, "possible": 2}
            evidence.sort(key=lambda e: order[e[1]])
            best = evidence[0][1]
            rec["vendor"] = " / ".join(dict.fromkeys(e[0] for e in evidence if e[1] == best))
            rec["vconf"] = best
            rec["vendor_evidence"] = evidence
            tally[best] += 1
        else:
            tally["vendor_unknown"] += 1
    tally["intermediary_suppliers"] = len({facts[k][2] for k in facts
                                          if corpus[k].get("intermediary")})
    report["vendors"] = dict(tally)
