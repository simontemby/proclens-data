#!/usr/bin/env python3
"""
Resolve suppliers into the organisations behind them, using a graph.

The archive keys suppliers on ABN, which splits companies apart: 23,446 of
72,095 ABNs are published under more than one name, and 5,918 normalised names
appear under more than one ABN — Hays Specialist Recruitment under sixteen. A
"top suppliers" list built on ABNs therefore understates whoever registers many
entities and overstates nobody.

This loads suppliers, the names they publish under, their postcodes and the
agencies they supply into Neo4j, links ABNs that share a distinctive name, and
takes the connected components as organisations.

Two guards, because a careless merge is worse than none:

  * only a DISTINCTIVE name links anything. A name counts as distinctive when
    it has at least two words left after company words are stripped and at
    least one of those words is rare across every supplier name in the archive.
    "hays specialist recruitment" links; "consulting services" never does.
  * a shared name alone is a candidate, not a merge. Something the name did not
    supply has to agree: a shared postcode, or a shared customer agency.

Everything else is written out as a candidate for review. Nothing here renames
or rewrites a contract: the output is a grouping, and every group carries the
evidence that formed it.

Run by .github/workflows/graph.yml, which starts Neo4j beside it. Locally:

    docker run -d --name neo4j -p 7687:7687 -e NEO4J_AUTH=neo4j/testtest \
      -e NEO4J_PLUGINS='["graph-data-science"]' neo4j:5-community
    NEO4J_PASSWORD=testtest python graph.py
"""
import argparse, json, os, re, sys, time
from collections import Counter, defaultdict

from neo4j import GraphDatabase

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
RARE_NAME_MAX = 60       # a word in more than this many distinct names is common
GROUP_REVIEW_SIZE = 12   # a component larger than this is reported, not trusted


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
    them is rare across every supplier name in the archive."""
    freq = Counter()
    for n in names:
        for w in set(n.split()):
            freq[w] += 1
    out = {}
    for n in names:
        words = n.split()
        out[n] = len(words) >= 2 and min(freq[w] for w in words) <= RARE_NAME_MAX
    return out, freq


# ---------------------------------------------------------------- the graph

def load(session, abn, names, agencies, postcodes, distinct):
    session.run("MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 20000 ROWS")
    for stmt in ("CREATE CONSTRAINT abn IF NOT EXISTS FOR (a:Abn) REQUIRE a.abn IS UNIQUE",
                 "CREATE CONSTRAINT nm IF NOT EXISTS FOR (n:Name) REQUIRE n.norm IS UNIQUE",
                 "CREATE CONSTRAINT ag IF NOT EXISTS FOR (g:Agency) REQUIRE g.name IS UNIQUE",
                 "CREATE CONSTRAINT pc IF NOT EXISTS FOR (p:Postcode) REQUIRE p.code IS UNIQUE"):
        session.run(stmt)

    def batched(rows, cypher, size=5000):
        for i in range(0, len(rows), size):
            session.run(cypher, rows=rows[i:i + size])

    batched([{"abn": a, "contracts": d["contracts"], "value": round(d["value"], 2),
              "label": d["labels"].most_common(1)[0][0] if d["labels"] else a} for a, d in abn.items()],
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


def resolve(session):
    """Candidates from shared distinctive names; merges only where a postcode or
    an agency agrees."""
    session.run("""
        MATCH (a:Abn)-[:PUBLISHED_AS]->(n:Name {distinctive:true})<-[:PUBLISHED_AS]-(b:Abn)
        WHERE a.abn < b.abn
        MERGE (a)-[c:SHARES_NAME]->(b) SET c.name = n.norm""")
    session.run("""
        MATCH (a:Abn)-[c:SHARES_NAME]->(b:Abn)
        OPTIONAL MATCH (a)-[:AT]->(p:Postcode)<-[:AT]-(b)
        OPTIONAL MATCH (a)-[:SUPPLIED]->(g:Agency)<-[:SUPPLIED]-(b)
        WITH a, b, c, count(DISTINCT p) AS pc, count(DISTINCT g) AS ag
        SET c.postcodes = pc, c.agencies = ag
        FOREACH (_ IN CASE WHEN pc > 0 OR ag > 0 THEN [1] ELSE [] END |
                 MERGE (a)-[m:SAME_ORG]->(b) SET m.postcodes = pc, m.agencies = ag, m.name = c.name)""")
    # Connected components over the confirmed links only.
    session.run("CALL gds.graph.exists('org') YIELD exists "
                "WITH exists WHERE exists CALL gds.graph.drop('org') YIELD graphName RETURN graphName")
    session.run("""
        CALL gds.graph.project('org', 'Abn',
            {SAME_ORG: {orientation: 'UNDIRECTED'}})""")
    session.run("CALL gds.wcc.write('org', {writeProperty: 'org'})")


def report(session):
    groups = session.run("""
        MATCH (a:Abn) WITH a.org AS org, collect(a) AS members
        WHERE size(members) > 1
        RETURN org,
               [m IN members | m.abn] AS abns,
               [m IN members | m.label] AS labels,
               reduce(s = 0, m IN members | s + m.contracts) AS contracts,
               reduce(s = 0.0, m IN members | s + m.value) AS value
        ORDER BY value DESC""").data()
    for g in groups:
        ev = session.run("""
            MATCH (a:Abn)-[m:SAME_ORG]-(b:Abn) WHERE a.org = $org AND b.org = $org
            RETURN collect(DISTINCT m.name)[0..4] AS names,
                   sum(m.postcodes) AS pc, sum(m.agencies) AS ag""", org=g["org"]).single()
        g["names"], g["shared_postcodes"], g["shared_agencies"] = ev["names"], ev["pc"], ev["ag"]
    candidates = session.run("""
        MATCH (a:Abn)-[c:SHARES_NAME]->(b:Abn)
        WHERE c.postcodes = 0 AND c.agencies = 0
        RETURN c.name AS name, a.abn AS a, b.abn AS b, a.label AS al, b.label AS bl
        ORDER BY name LIMIT 500""").data()
    before = session.run("MATCH (a:Abn) RETURN a.label AS label, a.abn AS abn, a.value AS value "
                         "ORDER BY value DESC LIMIT 10").data()
    return groups, candidates, before


def main():
    ap = argparse.ArgumentParser(description="Resolve suppliers into organisations with Neo4j.")
    ap.add_argument("--out", default=os.path.join(DATA, "supplier_groups.json"))
    args = ap.parse_args()

    t0 = time.time()
    abn, names, agencies, postcodes = gather()
    distinct, _ = distinctive(names)
    print(f"{len(abn):,} ABNs · {len(names):,} names ({sum(distinct.values()):,} distinctive) · "
          f"{sum(len(m) for m in agencies.values()):,} supplier-agency links", file=sys.stderr, flush=True)

    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    with driver.session() as s:
        load(s, abn, names, agencies, postcodes, distinct)
        print(f"loaded in {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
        resolve(s)
        groups, candidates, before = report(s)
    driver.close()

    big = [g for g in groups if len(g["abns"]) > GROUP_REVIEW_SIZE]
    payload = {
        "generated": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                       .isoformat(timespec="seconds"),
        "note": "Suppliers grouped into organisations. Two ABNs are grouped only when they publish "
                "under the same distinctive name AND share a postcode or a customer agency. Groups "
                "larger than %d ABNs are listed for review rather than trusted. Candidates are pairs "
                "sharing a name with nothing else to confirm them; they are NOT grouped." % GROUP_REVIEW_SIZE,
        "rules": {"rare_word_max_names": RARE_NAME_MAX, "review_size": GROUP_REVIEW_SIZE},
        "totals": {"abns": len(abn), "groups": len(groups),
                   "abns_grouped": sum(len(g["abns"]) for g in groups),
                   "needs_review": len(big), "candidates_unconfirmed": len(candidates)},
        "groups": [{"abns": g["abns"], "names": g["names"], "labels": sorted(set(g["labels"]))[:6],
                    "contracts": g["contracts"], "value": round(g["value"], 2),
                    "shared_postcodes": g["shared_postcodes"], "shared_agencies": g["shared_agencies"],
                    "review": len(g["abns"]) > GROUP_REVIEW_SIZE} for g in groups],
        "candidates": candidates,
    }
    build.write_json_if_changed(args.out, payload)

    summary = [f"**{len(groups):,} organisations** from {payload['totals']['abns_grouped']:,} ABNs "
               f"({len(abn):,} ABNs in all). {len(candidates):,} unconfirmed candidates. "
               f"{len(big)} group(s) over {GROUP_REVIEW_SIZE} ABNs need review.", "",
               "| Organisation | ABNs | Contracts | Value | Evidence |", "|---|---|---|---|---|"]
    for g in payload["groups"][:15]:
        summary.append(f"| {g['labels'][0][:40]} | {len(g['abns'])} | {g['contracts']:,} | "
                       f"${g['value']:,.0f} | {g['shared_agencies']} shared agencies, "
                       f"{g['shared_postcodes']} shared postcodes |")
    summary += ["", "**Top suppliers before grouping (by ABN)**", "", "| Supplier | Value |", "|---|---|"]
    for b in before:
        summary.append(f"| {b['label'][:40]} | ${b['value']:,.0f} |")
    text = "\n".join(summary)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text + "\n")
    print(f"done in {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
