#!/usr/bin/env python3
"""DBpedia POI harvest for the ScenicSpots bulk registry.

Reads the category sidecar (dbpedia_poi_categories.json), queries the
DBpedia SPARQL endpoint once per category, and writes:

  <out-dir>/poi_harvest.json  raw per-country entity data
  <out-dir>/poi_bulk.cddal    the generated registry section, wrapped in
                              BEGIN/END markers for idempotent splicing

Code stability: identifiers already present in the fixture (via
--fixture) keep their existing codes; only new identifiers are
allocated the next free sequence number in their country block. Codes
are never renumbered or reused. The Rakefile task browser:harvest_poi
splices the emitted section into the fixture and rebuilds the demo
dictionaries.

Data source: DBpedia (Wikipedia, CC BY-SA) — facts and multilingual
labels only; attribution is carried in the fixture header. Polite
fetching: default 2.6 s between queries, 3 attempts, visible failures.
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request

BEGIN = "# ==== BEGIN DBPEDIA BULK REGISTRY (generated - do not hand-edit) ===="
END = "# ==== END DBPEDIA BULK REGISTRY ===="

LANG_TAGS = {"zh": None, "ja": "ja", "ko": "ko", "fr": "fr", "it": "it",
             "de": "de", "es": "es", "th": "th", "vi": "vi"}


def query(endpoint, category, langs, sleep, attempts=3):
    label_opts = []
    for l in langs:
        if l == "zh":
            label_opts.append("OPTIONAL { ?res rdfs:label ?zh . FILTER(strstarts(lang(?zh),'zh')) } ")
        else:
            label_opts.append("OPTIONAL { ?res rdfs:label ?%s . FILTER(lang(?%s)='%s') } " % (l, l, l))
    sparql = ("SELECT DISTINCT ?res ?en ?desc ?est ?elev ?area ?vis "
              + " ".join("?" + l for l in langs) + " WHERE { "
              # Full IRI, not dbc: prefixed name — prefixed names cannot
              # contain commas (Museums_in_Washington,_D.C. 400s).
              "?res dct:subject <http://dbpedia.org/resource/Category:"
              + urllib.parse.quote(category) + "> . "
              "?res rdfs:label ?en . FILTER(lang(?en)='en') "
              + " ".join(label_opts)
              + "OPTIONAL { ?res dbo:description ?desc . FILTER(lang(?desc)='en') } "
              "OPTIONAL { ?res dbp:established ?est } "
              "OPTIONAL { ?res dbo:elevation ?elev } "
              "OPTIONAL { ?res dbp:areaKm ?area } "
              "OPTIONAL { ?res dbp:visitors ?vis } } LIMIT 400")
    url = endpoint + "?" + urllib.parse.urlencode(
        {"query": sparql, "format": "application/sparql-results+json"})
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "opencdd-harvest/2.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)["results"]["bindings"]
        except Exception as exc:
            if attempt == attempts - 1:
                print("  QUERY-FAIL %s (%s)" % (category, exc), flush=True)
                return []
            time.sleep(sleep * 3)
    return []


def to_year(v):
    m = re.search(r"\b(\d{3,4})\b", str(v))
    y = int(m.group(1)) if m else None
    return y if y and 500 <= y <= 2026 else None


def to_num(v):
    s = str(v).replace(",", "").strip()
    m = re.match(r"-?\d+(\.\d+)?", s)
    if not m:
        return None
    f = float(m.group(0))
    if re.search(r"million", s, re.I):
        f *= 1000000
    if f <= 0 or f >= 1e10:
        return None
    # whole values emit as integers, never "769000.0"
    return int(f) if f == int(f) else f


# Non-place pages that leak through mapping categories: settlements,
# administrative units, organizations, transit infrastructure, and
# generic concept pages ("National parks of X" as a page). A scenic
# spot registry must contain places of interest, not these. Patterns
# are identifier-shaped (CamelCase, no underscores) and applied
# case-sensitively to the identifier suffix.
NON_PLACE_NAME = re.compile(
    r"(District|Authority|Borough|Municipality|Province|Prefecture|Region|Ward|"
    r"Line|Station|Metro|Subway|Airport|Railway|Cemetery|University|College|"
    r"Hospital|School|Bank|Hotel|Company)$|^(List_of|"
    r"National_parks_of_the_United_Kingdom)$"
)
NON_PLACE_DESC = re.compile(
    r"^(a district|district in|district of|an? (urban|residential) (district|area)|"
    r"area of relatively|uk authority|authority (that|responsible)|"
    r"municipality|borough of|railway station|metro station|underground station)",
    re.I,
)


def is_non_place(key, desc):
    if NON_PLACE_NAME.search(key):
        return True
    if desc and NON_PLACE_DESC.match(desc):
        return True
    return False


def spot_type(category):
    if "Cathedrals" in category or "Churches" in category:
        return "Cathedral"
    if "Mosques" in category:
        return "Mosque"
    if "National_parks" in category or "National_park" in category:
        return "NationalPark"
    if "Museums" in category:
        return "Museum"
    if "Archaeological_sites" in category:
        return "ArchaeologicalSite"
    if "Temples" in category or "Shrines" in category:
        return "Temple"
    if "Palaces" in category or "Castles" in category or "Country_houses" in category:
        return "Palace"
    if "Hot_springs" in category:
        return "HotSpring"
    if "Night_markets" in category:
        return "NightMarket"
    return "Landmark"


def load_used_codes(fixture):
    """Every identifier already coded in the fixture (curated + bulk)."""
    used = {}
    if not fixture:
        return used
    text = open(fixture, encoding="utf-8").read()
    for ident, code in re.findall(
            r"instance ([A-Za-z][A-Za-z0-9_]*) < MDC_C[0-9]+ \{\n  code: ([A-Za-z]+\d+)", text):
        used[ident] = code
    return used


def existing_seq_maxes(fixture, stem="POI"):
    """Highest sequence number already allocated per SPI country block."""
    maxes = {}
    if not fixture:
        return maxes
    text = open(fixture, encoding="utf-8").read()
    for code in re.findall(r"code: SPI(\d{2})(\d{2})\b", text):
        maxes[code[0]] = max(maxes.get(code[0], 0), int(code[1]))
    return maxes


def harvest(cfg, countries, limit, sleep, outdir):
    import os
    os.makedirs(outdir, exist_ok=True)
    endpoint = cfg["sparql_endpoint"]
    langs = cfg["languages"]
    curated = set(cfg["curated_skip"])
    curated_ids = set(re.sub(r"[^A-Za-z0-9]", "", k) for k in curated)
    city_by_category = cfg.get("city_by_category", {})
    # Incremental: a subset run merges into the previous harvest instead
    # of replacing it, so `--countries UnitedKingdom` refreshes one
    # country and leaves the rest of the registry intact.
    out_path = os.path.join(outdir, "poi_harvest.json")
    if os.path.exists(out_path):
        out = json.load(open(out_path, encoding="utf-8"))
    else:
        out = {}
    for country in countries:
        c = cfg["countries"][country]
        ents = {}
        for cat in c["categories"]:
            t = spot_type(cat)
            city = city_by_category.get(cat)
            for row in query(endpoint, cat, langs, sleep):
                key = row["res"]["value"].rsplit("/", 1)[-1]
                if key in curated or ":" in key or key.startswith("List_of"):
                    continue
                if not re.match(r"^[A-Za-z0-9_().'()’-]+$", key):
                    continue
                desc = row.get("desc", {}).get("value") if "desc" in row else None
                if is_non_place(key, desc):
                    continue
                e = ents.setdefault(key, {"langs": {}, "facts": {}, "type": t, "desc": None,
                                          "city": city})
                if t != "Landmark":
                    e["type"] = t
                en_label = row.get("en", {}).get("value", key.replace("_", " "))
                for l in langs:
                    if l in row and l not in e["langs"]:
                        v = row[l]["value"]
                        # DBpedia serves English romanizations as fr/de/es
                        # labels for many topics; a "translation" identical
                        # to the English name is noise, not data.
                        if l != "zh" and v.strip().lower() == en_label.strip().lower():
                            continue
                        e["langs"][l] = v
                if desc and not e["desc"]:
                    e["desc"] = desc
                for f in ("est", "elev", "area", "vis"):
                    if f in row and f not in e["facts"]:
                        e["facts"][f] = row[f]["value"]
            time.sleep(sleep)
        rich = {k: v for k, v in ents.items() if v["facts"] or v["langs"]}
        rich = dict(sorted(rich.items())[:limit])
        out[country] = rich
        print("%s: %d kept (of %d)" % (country, len(rich), len(ents)), flush=True)
    with open(os.path.join(outdir, "poi_harvest.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    return out


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').strip()


def emit(cfg, harvest_data, fixture, today):
    stem = cfg.get("code_stem", "SPI")
    used = load_used_codes(fixture)
    seq_max = existing_seq_maxes(fixture, stem)
    head = [
        BEGIN, "#",
        "# Registered individuals - DBpedia-sourced, Wikipedia-verified registry",
        "# (bulk, generated)",
        "#",
        "# Candidates sourced from DBpedia (Wikipedia, CC BY-SA), retrieved %s." % today,
        "# Every entry was then individually verified against the English",
        "# Wikipedia article before admission: page existence, country, spot",
        "# type, native labels via interlanguage titles, and facts (years,",
        "# elevations, areas, visitor numbers) confirmed verbatim in the",
        "# article text. Unverified labels and numbers are dropped, never",
        "# guessed; rejected candidates are logged in poi_rejected.json.",
        "#",
        "# Country code blocks: two country digits + two sequence digits",
        "# (SPI00nn TW ... SPI09nn SG, SPI10nn DE, SPI11nn ES, SPI12nn TH,",
        "# SPI13nn VN, SPI14nn MY, SPI15nn AU, SPI16nn CA, SPI17nn IN,",
        "# POI18nn PH, POI19nn ID, POI20nn+ per config). Identifiers beginning",
        "# prefixed N. Existing codes are never renumbered; regenerate via",
        "# `rake browser:harvest_poi` in data-private.",
        "#",
        "# Edit curated entries above this block only.",
        "",
    ]
    body = []
    total_new = 0
    total_kept = 0
    emitted = set()
    for country, c in cfg["countries"].items():
        if harvest_data is not None and country not in harvest_data:
            continue
        ents = (harvest_data or {}).get(country, {})
        for key in sorted(ents):
            e = ents[key]
            ident = re.sub(r"[^A-Za-z0-9]", "", key)
            if not ident:
                continue
            if ident[0].isdigit():
                ident = "N" + ident
            if ident in emitted:
                continue
            emitted.add(ident)
            if ident in used:
                code = used[ident]
                total_kept += 1
            else:
                seq = seq_max.get(c["block"], 0) + 1
                seq_max[c["block"]] = seq
                code = stem + c["block"] + ("%02d" % seq)
                used[ident] = code
                total_new += 1
            en = key.replace("_", " ")
            body.append("instance " + ident + " < MDC_C002 {")
            body.append("  code: " + code)
            body.append('  preferred_name.en: "' + esc(en) + '"')
            zhtag = c["zh_tag"]
            for l in cfg["languages"]:
                tag = zhtag if l == "zh" else LANG_TAGS.get(l, l)
                if e["langs"].get(l) and tag:
                    body.append('  preferred_name.' + tag + ': "' + esc(e["langs"][l]) + '"')
            if e.get("desc"):
                body.append('  definition.en: "' + esc(e["desc"])[:200] + '"')
            t = e.get("type", "Landmark")
            body.append("  superclass: " + t)
            body.append("  class_type: ITEM_CLASS")
            body.append("  country: " + c["class"])
            body.append("  spot_type: " + t)
            if e.get("city"):
                body.append("  city: " + e["city"])
            f = e.get("facts", {})
            if f.get("est") and to_year(f["est"]):
                body.append("  established_year: " + str(to_year(f["est"])))
            elev = to_num(f.get("elev")) if f.get("elev") else None
            if elev is not None and 0 < elev < 9000:
                body.append("  elevation: " + str(elev))
            if f.get("lat") is not None and f.get("lon") is not None:
                body.append("  latitude: " + str(f["lat"]))
                body.append("  longitude: " + str(f["lon"]))
            # DBpedia's areaKm is unreliable for buildings — a temple
            # comes back as "870" km². Areas are only plausible for
            # extensive landscapes (parks); drop them elsewhere.
            area = to_num(f.get("area")) if f.get("area") else None
            if area is not None and (t == "NationalPark" or area <= 5):
                body.append("  area: " + str(area))
            if f.get("whs"):
                body.append("  official_grade: WorldHeritage")
            vis = to_num(f.get("vis")) if f.get("vis") else None
            # fewer than 1,000 visitors a year is a parsing artifact,
            # not an attendance figure
            if vis is not None and vis >= 1000:
                body.append("  annual_visitors: " + str(vis))
            if f.get("vis_year") and vis is not None and vis >= 1000:
                body.append("  visitor_data_year: " + str(int(f["vis_year"])))
            body.append("}")
            body.append("")
    section = "\n".join(head + body).rstrip("\n") + "\n" + END + "\n"
    return section, total_new, total_kept


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="harvest/dbpedia_poi_categories.json")
    ap.add_argument("--fixture",
                    default="reference-docs/examples/scenicspots.cddal")
    ap.add_argument("--out-dir", default="harvest/out")
    ap.add_argument("--countries", help="comma-separated subset of country keys")
    ap.add_argument("--limit-per-country", type=int, default=None)
    ap.add_argument("--sleep", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="probe category counts only; no harvest, no emission")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    limit = args.limit_per_country or cfg["default_limit_per_country"]
    sleep = args.sleep if args.sleep is not None else cfg["query_sleep_seconds"]
    countries = (args.countries.split(",") if args.countries
                 else list(cfg["countries"]))
    for c in countries:
        if c not in cfg["countries"]:
            sys.exit("unknown country key: " + c)

    if args.dry_run:
        for country in countries:
            for cat in cfg["countries"][country]["categories"]:
                rows = query(cfg["sparql_endpoint"], cat, cfg["languages"], sleep)
                print("%-14s %-45s %d" % (country, cat, len(rows)), flush=True)
                time.sleep(sleep)
        return

    import datetime
    import subprocess
    import os
    data = harvest(cfg, countries, limit, sleep, args.out_dir)

    # Nothing enters the corpus unverified: run the Wikipedia
    # verification stage and emit from its output, never from the raw
    # DBpedia harvest.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    subprocess.run(
        ["python3", os.path.join(script_dir, "verify_poi.py"),
         "--harvest", args.out_dir.rstrip("/") + "/poi_harvest.json",
         "--out-dir", args.out_dir],
        check=True,
    )
    verified_path = args.out_dir.rstrip("/") + "/poi_harvest_verified.json"

    # Wikidata (CC0) enrichment: native-label upgrades, coordinate
    # fills, year-qualified visitor counts.
    subprocess.run(
        ["python3", os.path.join(script_dir, "wikidata_enrich.py"),
         "--verified", verified_path],
        check=True,
    )
    data = json.load(open(verified_path, encoding="utf-8"))

    today = str(datetime.date.today())
    section, new_n, kept_n = emit(cfg, data, args.fixture, today)
    out = args.out_dir.rstrip("/") + "/poi_bulk.cddal"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(section)
    print("emitted %s: %d new codes, %d kept codes" % (out, new_n, kept_n))


if __name__ == "__main__":
    main()
