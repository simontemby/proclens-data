#!/usr/bin/env python3
"""
Resolve suppliers into the organisations behind them, using a graph.

The archive keys suppliers on ABN, which splits companies apart: 23,446 of
72,095 ABNs are published under more than one name, and 5,918 normalised names
appear under more than one ABN — Hays Specialist Recruitment under sixteen. A
"top suppliers" list built on ABNs therefore understates whoever registers many
entities and overstates nobody.

This loads suppliers, the names they publish under, their postcodes and the
agencies they supply into Neo4j, then groups the ABNs that publish under the
same distinctive name.

One group is one name. It is deliberately NOT a transitive closure: the first
run of this took connected components, and chaining — A shares one name with B,
B shares a different name with C — welded 901 ABNs, 385,688 contracts and $628bn
into a single "organisation", because a handful of ABNs publish under names that
bridge unrelated companies. An ABN may therefore belong to more than one group,
which is the honest answer when the evidence says two things.

Four guards, because a careless merge is worse than none:

  * only a DISTINCTIVE name groups anything. A name counts as distinctive when
    it has at least two words left after company words are stripped and at
    least one of those words is rare — used by few suppliers. Rarity is counted
    over ABNs, not over names, because a supplier that misspells itself ninety
    ways would otherwise make its own name look common: "hays" appears in 90
    distinct names in this archive and belongs to one company.
    "hays specialist recruitment" groups; "consulting services" never does.
  * a shared name alone is a candidate, not a merge. Something the name did not
    supply has to agree: a shared postcode, or a shared customer agency.
  * an ABN joins on the name it PRINCIPALLY trades under — its most-used name,
    or one covering at least 40% of its contracts. Without this, sixteen ABNs
    join "hays specialist recruitment", of which fifteen are agencies that typed
    the supplier's name against their own ABN once; the Electoral Commission is
    not a recruiter. It also dissolves placeholder text such as "temporary
    record" sitting in a supplier field, and stops one company appearing as
    three groups because it spells itself three ways.
  * names that label a collection of independent traders rather than a company
    — a barrister's list or chambers — are never grouped, at any size, and a
    name covering an implausible number of ABNs is reported instead of merged.

Everything else is written out as a candidate for review. Nothing here renames
or rewrites a contract: the output is a grouping, and every group carries the
evidence that formed it.

Run by .github/workflows/graph.yml, which starts Neo4j beside it. Locally:

    docker run -d --name neo4j -p 7687:7687 -e NEO4J_AUTH=neo4j/testtest \
      -e NEO4J_PLUGINS='["graph-data-science"]' neo4j:5-community
    NEO4J_PASSWORD=testtest python graph.py

The grouping rules are pure Python and can be checked without any of that:

    python graph.py --dry-run
"""
import argparse, json, os, re, sys, time
from collections import Counter, defaultdict

import build

DATA = os.environ.get("PROCLENS_DATA", "data")
URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD", "testtest")

# Words that say nothing about which organisation this is.
COMPANY_WORDS = re.compile(
    r"\b(pty|ltd|limited|proprietary|p/l|pl|inc|incorporated|llc|plc|co|company|corp|"
    r"corporation|holdings?|group|australia|aust|australian|the|trustee|for|trust|unit|"
    r"family|t/a|t/as|trading|as|atf|a/c|acn|abn|services|service|solutions|consulting|"
    r"consultants?|enterprises?|international|national|and)\b")
RARE_WORD_MAX = 60       # a word used by more than this many suppliers is common
NAME_MAX_ABNS = 25       # a name over this many ABNs is a list, not a company
DOMINANT_SHARE = 0.4     # ... or account for this much of the ABN's work
# Labels shared by independent traders, and agency placeholder text. A
# barrister's list is not an organisation; "temporary record" is not a supplier.
COLLECTION_RE = re.compile(
    r"\b(list|lists|chambers|barristers?|counsel|temporary record|various|unknown|"
    r"not applicable|miscellaneous|confidential|redacted|withheld)\b")


def norm(name):
    s = str(name or "").lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", COMPANY_WORDS.sub(" ", s)).strip()


# ---------------------------------------------------------------- the facts

def gather():
    """Supplier-level facts from the built corpus."""
    abn = defaultdict(lambda: {"contracts": 0, "value": 0.0, "labels": Counter()})
    names = defaultdict(Counter)        # normalised name -> abn -> contracts
    agencies = defaultdict(Counter)     # abn -> agency -> contracts
    postcodes = defaultdict(Counter)    # abn -> postcode -> contracts
    idx = build.read_json(os.path.join(DATA, "corpus", "index.json"), {})
    for m in idx.get("months", []):
        month = m["month"]
        lst = build.decode(build.read_json(os.path.join(DATA, "corpus", "list", f"{month}.json"), {}))
        det = {d["cn"]: d for d in
               build.decode(build.read_json(os.path.join(DATA, "corpus", "detail", f"{month}.json"), {}))}
        for r in lst:
            a = str(r.get("abn") or "").strip()
            if not re.fullmatch(r"\d{11}", a):
                continue
            v = r["value"] if isinstance(r.get("value"), (int, float)) else 0.0
            rec = abn[a]
            rec["contracts"] += 1
            rec["value"] += v
            if r.get("supplier"):
                rec["labels"][r["supplier"].strip()] += 1
            n = norm(r.get("supplier"))
            if n:
                names[n][a] += 1
            if r.get("buyer"):
                agencies[a][r["buyer"]] += 1
            pc = str((det.get(r["cn"]) or {}).get("supplier_postcode") or "").strip()
            if re.fullmatch(r"\d{4}", pc):
                postcodes[a][pc] += 1
    return abn, names, agencies, postcodes


def distinctive(names):
    """A name is distinctive when it has two or more words and at least one of
    them is used by few suppliers. Counting suppliers rather than names matters:
    ninety spellings of Hays are ninety names but one company."""
    users = defaultdict(set)
    for n, members in names.items():
        for w in set(n.split()):
            users[w].update(members)
    freq = {w: len(a) for w, a in users.items()}
    out = {}
    for n in names:
        words = n.split()
        out[n] = len(words) >= 2 and min(freq[w] for w in words) <= RARE_WORD_MAX
    return out, freq


# ------------------------------------------------------------- the grouping

def group(abn, names, agencies, postcodes, distinct):
    """One distinctive name, one organisation — see the module docstring for why
    this does not chain. Returns the groups, the rejections by reason, and the
    name-sharing pairs nothing but the name confirms."""
    groups, rejected, candidates = [], Counter(), []
    top = {}                       # abn -> the name it publishes under most
    for n, members in names.items():
        for a, c in members.items():
            if c > top.get(a, (0, ""))[0]:
                top[a] = (c, n)

    def principal(a, name, c):
        return top[a][1] == name or c >= DOMINANT_SHARE * abn[a]["contracts"]

    for name, members in names.items():
        if len(members) < 2:
            continue
        if not distinct[name]:
            rejected["name is not distinctive"] += 1
            continue
        if COLLECTION_RE.search(name):
            rejected["a collection of traders, not an organisation"] += 1
            continue
        if len(members) > NAME_MAX_ABNS:
            rejected[f"covers more than {NAME_MAX_ABNS} ABNs"] += 1
            continue
        kept = sorted(a for a, c in members.items() if principal(a, name, c))
        if len(kept) < 2:
            rejected["not the name these ABNs principally trade under"] += 1
            continue
        pc, ag = Counter(), Counter()
        for a in kept:
            for p in postcodes[a]:
                pc[p] += 1
            for g in agencies[a]:
                ag[g] += 1
        shared_pc = sum(1 for _, c in pc.items() if c > 1)
        shared_ag = sum(1 for _, c in ag.items() if c > 1)
        if not shared_pc and not shared_ag:
            rejected["nothing but the name agrees"] += 1
            if len(candidates) < 500:
                candidates.append({"name": name, "abns": kept,
                                   "labels": [label(abn, a) for a in kept][:4]})
            continue
        groups.append({
            "name": name, "abns": kept,
            "labels": [label(abn, a) for a in sorted(kept, key=lambda a: -abn[a]["contracts"])][:6],
            "contracts": sum(abn[a]["contracts"] for a in kept),
            "value": round(sum(abn[a]["value"] for a in kept), 2),
            "shared_postcodes": shared_pc, "shared_agencies": shared_ag,
        })
    groups.sort(key=lambda g: -g["value"])
    return groups, dict(rejected.most_common()), candidates


def label(abn, a):
    d = abn[a]["labels"]
    return d.most_common(1)[0][0] if d else a


# ---------------------------------------------------------------- the graph

def load(session, abn, names, agencies, postcodes, distinct):
    session.run("MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 20000 ROWS")
    for stmt in ("CREATE CONSTRAINT abn IF NOT EXISTS FOR (a:Abn) REQUIRE a.abn IS UNIQUE",
                 "CREATE CONSTRAINT nm IF NOT EXISTS FOR (n:Name) REQUIRE n.norm IS UNIQUE",
                 "CREATE CONSTRAINT ag IF NOT EXISTS FOR (g:Agency) REQUIRE g.name IS UNIQUE",
                 "CREATE CONSTRAINT pc IF NOT EXISTS FOR (p:Postcode) REQUIRE p.code IS UNIQUE",
                 "CREATE CONSTRAINT org IF NOT EXISTS FOR (o:Org) REQUIRE o.name IS UNIQUE"):
        session.run(stmt)

    def batched(rows, cypher, size=5000):
        for i in range(0, len(rows), size):
            session.run(cypher, rows=rows[i:i + size])

    batched([{"abn": a, "contracts": d["contracts"], "value": round(d["value"], 2),
              "label": label(abn, a)} for a, d in abn.items()],
            "UNWIND $rows AS r MERGE (a:Abn {abn:r.abn}) "
            "SET a.contracts=r.contracts, a.value=r.value, a.label=r.label")
    batched([{"norm": n, "distinct": distinct[n], "abn": a, "contracts": c}
             for n, m in names.items() for a, c in m.items()],
            "UNWIND $rows AS r MERGE (n:Name {norm:r.norm}) SET n.distinctive=r.distinct "
            "WITH r, n MATCH (a:Abn {abn:r.abn}) MERGE (a)-[p:PUBLISHED_AS]->(n) SET p.contracts=r.contracts")
    batched([{"abn": a, "agency": g, "contracts": c} for a, m in agencies.items() for g, c in m.items()],
            "UNWIND $rows AS r MERGE (g:Agency {name:r.agency}) "
            "WITH r, g MATCH (a:Abn {abn:r.abn}) MERGE (a)-[s:SUPPLIED]->(g) SET s.contracts=r.contracts")
    batched([{"abn": a, "code": p, "contracts": c} for a, m in postcodes.items() for p, c in m.items()],
            "UNWIND $rows AS r MERGE (p:Postcode {code:r.code}) "
            "WITH r, p MATCH (a:Abn {abn:r.abn}) MERGE (a)-[l:AT]->(p) SET l.contracts=r.contracts")


def write_groups(session, groups):
    """Put the grouping in the graph, so it can be queried and argued with."""
    rows = [{k: g[k] for k in ("name", "abns", "contracts", "value",
                               "shared_postcodes", "shared_agencies")} for g in groups]
    for i in range(0, len(rows), 2000):
        session.run("""
            UNWIND $rows AS r
            MERGE (o:Org {name:r.name})
            SET o.contracts=r.contracts, o.value=r.value, o.abns=size(r.abns),
                o.shared_postcodes=r.shared_postcodes, o.shared_agencies=r.shared_agencies
            WITH o, r UNWIND r.abns AS abn
            MATCH (a:Abn {abn:abn}) MERGE (a)-[:PART_OF]->(o)""", rows=rows[i:i + 2000])


def review(session):
    """What the graph can now answer that a table of ABNs could not."""
    out = {}
    out["abns_in_more_than_one_org"] = session.run(
        "MATCH (a:Abn)-[:PART_OF]->(o:Org) WITH a, count(o) AS n WHERE n > 1 "
        "RETURN count(a) AS c").single()["c"]
    out["widest_customer_base"] = session.run("""
        MATCH (o:Org)<-[:PART_OF]-(:Abn)-[:SUPPLIED]->(g:Agency)
        WITH o, count(DISTINCT g) AS agencies
        RETURN o.name AS name, agencies, o.abns AS abns
        ORDER BY agencies DESC LIMIT 5""").data()
    out["top_before_grouping"] = session.run(
        "MATCH (a:Abn) RETURN a.label AS label, a.value AS value "
        "ORDER BY value DESC LIMIT 10").data()
    return out


def main():
    ap = argparse.ArgumentParser(description="Resolve suppliers into organisations with Neo4j.")
    ap.add_argument("--out", default=os.path.join(DATA, "supplier_groups.json"))
    ap.add_argument("--dry-run", action="store_true",
                    help="apply the grouping rules without Neo4j, and print what they do")
    args = ap.parse_args()

    t0 = time.time()
    abn, names, agencies, postcodes = gather()
    distinct, _ = distinctive(names)
    print(f"{len(abn):,} ABNs · {len(names):,} names ({sum(distinct.values()):,} distinctive) · "
          f"{sum(len(m) for m in agencies.values()):,} supplier-agency links", file=sys.stderr, flush=True)
    groups, rejected, candidates = group(abn, names, agencies, postcodes, distinct)

    graph = {}
    if not args.dry_run:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
        with driver.session() as s:
            load(s, abn, names, agencies, postcodes, distinct)
            print(f"loaded in {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
            write_groups(s, groups)
            graph = review(s)
        driver.close()

    grouped_abns = {a for g in groups for a in g["abns"]}
    payload = {
        "generated": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                       .isoformat(timespec="seconds"),
        "note": "Suppliers grouped into organisations. One group is one distinctive name that at "
                "least two ABNs trade under, where a postcode or a customer agency also agrees. "
                "Groups are not chained together, so an ABN can appear in more than one. Candidates "
                "share a name with nothing else to confirm them and are NOT grouped.",
        "rules": {"rare_word_max_suppliers": RARE_WORD_MAX, "max_abns_per_name": NAME_MAX_ABNS,
                  "principal_name_or_share": DOMINANT_SHARE},
        "totals": {"abns": len(abn), "groups": len(groups), "abns_grouped": len(grouped_abns),
                   "largest_group": max((len(g["abns"]) for g in groups), default=0),
                   "candidates_unconfirmed": len(candidates)},
        "rejected": rejected,
        "graph": graph,
        "groups": groups,
        "candidates": candidates,
    }
    if args.dry_run:
        print(json.dumps({k: payload[k] for k in ("totals", "rejected")}, indent=2))
        for g in groups[:15]:
            print(f"{g['value']:>14,.0f}  {len(g['abns']):>3} ABNs  {g['name'][:44]:<46} {g['labels'][0][:40]}")
        return
    build.write_json_if_changed(args.out, payload)

    summary = [f"**{len(groups):,} organisations** from {len(grouped_abns):,} ABNs "
               f"({len(abn):,} ABNs in all). Largest group {payload['totals']['largest_group']} ABNs. "
               f"{len(candidates):,} unconfirmed candidates.", "",
               "| Organisation | ABNs | Contracts | Value | Evidence |", "|---|---|---|---|---|"]
    for g in groups[:15]:
        summary.append(f"| {g['labels'][0][:40]} | {len(g['abns'])} | {g['contracts']:,} | "
                       f"${g['value']:,.0f} | {g['shared_agencies']} shared agencies, "
                       f"{g['shared_postcodes']} shared postcodes |")
    summary += ["", "**Top suppliers before grouping (by ABN)**", "", "| Supplier | Value |", "|---|---|"]
    for b in graph.get("top_before_grouping", []):
        summary.append(f"| {b['label'][:40]} | ${b['value']:,.0f} |")
    text = "\n".join(summary)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text + "\n")
    print(f"done in {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
