#!/usr/bin/env python3
"""Wikidata (CC0) enrichment for the verified POI harvest.

Runs after the Wikipedia verification stage and upgrades
poi_harvest_verified.json in place. Wikidata is CC0 and community-
curated from official statistics, so where it disagrees with the
DBpedia mirror it wins; where it is silent the verified Wikipedia
values stand.

Uses the standard MediaWiki action APIs rather than the WDQS SPARQL
endpoint (which is aggressively rate-limited during outages):

  resolve   en.wikipedia prop=pageprops (wikibase_item) — the same
            API the verification stage already queries — 50 titles
            per call
  fetch     www.wikidata.org action=wbgetentities with labels and
            claims — 50 entities per call

Per spot:

  labels     native labels for every configured language; Chinese is
             resolved per-country into the region-coded BCP-47 tag
             (zh-hant → zh-TW/zh-HK, zh-hans → zh-CN). Wikidata labels
             replace DBpedia rdfs:label values; a label equal to the
             English name is dropped (same noise rule as the harvest).
  coords     P625 fills spots whose Wikipedia article carried no
             coordinates.
  visitors   the latest P3872 (visitors per year) with its P585
             point-in-time replaces the unqualified DBpedia figure,
             and the year lands in visitor_data_year. (P3872 is
             currently filtered from the Wikidata API — dormant.)
  grade      a P757 claim (UNESCO World Heritage Site ID) marks the
             spot WorldHeritage on the official-grade dimension.

Writes: poi_harvest_verified.json (in place) + wikidata_stats.json.
"""

import argparse
import json
import time
import urllib.parse
import urllib.request

WIKI_API = "https://en.wikipedia.org/w/api.php"
WD_API = "https://www.wikidata.org/w/api.php"
BATCH = 50

ZH_TAG_FROM_HANT = {"zh-TW": ["zh-hant", "zh-tw", "zh"],
                    "zh-HK": ["zh-hant", "zh-hk", "zh"],
                    "zh-CN": ["zh-hans", "zh-cn", "zh"]}
PLAIN_LANGS = ["ja", "ko", "fr", "it", "de", "es", "th", "vi",
               "pt", "ar", "tr", "el", "nl", "pl", "cs", "hu"]
ALL_LANGS = ["zh-hant", "zh-hans", "zh", "zh-tw", "zh-hk", "zh-cn"] + PLAIN_LANGS


def api(url, params, attempts=3):
    full = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                full, headers={"User-Agent": "opencdd-harvest/2.0 (verified POI pipeline)"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as exc:
            if attempt == attempts - 1:
                print("  API-FAIL %s" % exc, flush=True)
                return {}
            time.sleep(20 * (attempt + 1))
    return {}


def resolve_qids(titles, sleep):
    """en-wikipedia title → Wikidata QID via pageprops."""
    out = {}
    for i in range(0, len(titles), BATCH):
        batch = titles[i:i + BATCH]
        d = api(WIKI_API, {"action": "query", "format": "json",
                           "prop": "pageprops", "ppprop": "wikibase_item",
                           "redirects": 1, "titles": "|".join(batch)})
        remap = {}
        for item in (d.get("query", {}).get("normalized") or []) + \
                    (d.get("query", {}).get("redirects") or []):
            remap[item["from"]] = item["to"]
        for page in d.get("query", {}).get("pages", {}).values():
            qid = (page.get("pageprops") or {}).get("wikibase_item")
            if qid:
                out[page.get("title", "")] = qid
        time.sleep(sleep)
    return out


def fetch_entities(qids, sleep):
    """QID → labels, coordinates, latest visitor count+year."""
    items = {}
    for i in range(0, len(qids), BATCH):
        batch = qids[i:i + BATCH]
        d = api(WD_API, {"action": "wbgetentities", "format": "json",
                         "ids": "|".join(batch),
                         "props": "labels|aliases|claims",
                         # "en" too: the aliases fetch filters by this
                         # list, and English aliases are the synonyms
                         "languages": "|".join(["en"] + ALL_LANGS)})
        for qid, ent in (d.get("entities") or {}).items():
            item = {"labels": {}, "aliases": [], "lat": None, "lon": None,
                    "vis": None, "vis_year": None, "whs": False}
            for lang, lab in (ent.get("labels") or {}).items():
                item["labels"][lang] = lab.get("value")
            en_aliases = []
            for al in (ent.get("aliases") or {}).get("en") or []:
                v = (al.get("value") or "").strip()
                if 2 < len(v) <= 60 and not v.isupper() and v not in en_aliases:
                    en_aliases.append(v)
                if len(en_aliases) >= 5:
                    break
            item["aliases"] = en_aliases
            claims = ent.get("claims") or {}
            p625 = (claims.get("P625") or [{}])[0]
            v = (((p625.get("mainsnak") or {}).get("datavalue") or {})
                 .get("value") or {})
            if "latitude" in v:
                item["lat"] = float(v["latitude"])
                item["lon"] = float(v["longitude"])
            if claims.get("P757"):
                item["whs"] = True
            for stmt in claims.get("P3872") or []:
                snak = ((stmt.get("mainsnak") or {}).get("datavalue") or {})
                amount = snak.get("value", {}).get("amount")
                quals = (stmt.get("qualifiers") or {}).get("P585") or []
                if amount and quals:
                    t = quals[0].get("datavalue", {}).get("value", {}).get("time", "")
                    year = int(t[1:5]) if len(t) > 5 else None
                    if year and (item["vis_year"] is None or year > item["vis_year"]):
                        item["vis_year"] = year
                        item["vis"] = float(amount.lstrip("+"))
            items[qid] = item
        time.sleep(sleep)
    return items


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verified", default="harvest/out/poi_harvest_verified.json")
    ap.add_argument("--config", default="harvest/dbpedia_poi_categories.json")
    ap.add_argument("--sleep", type=float, default=0.8)
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    data = json.load(open(args.verified, encoding="utf-8"))

    spots = []
    for country, ents in data.items():
        for title, e in ents.items():
            spots.append((country, title, e))
    print("resolving %d titles against Wikidata..." % len(spots), flush=True)
    qid_of = resolve_qids([t for _, t, _ in spots], args.sleep)
    print("matched %d/%d" % (len(qid_of), len(spots)), flush=True)

    qids = sorted(set(qid_of.values()))
    print("fetching entity data for %d items..." % len(qids), flush=True)
    item_data = fetch_entities(qids, args.sleep)

    stats = {"label_upgrades": 0, "coords_filled": 0,
             "visitors_with_year": 0, "items_matched": len(qid_of)}
    for country, title, e in spots:
        qid = qid_of.get(title.replace("_", " "))
        if not qid:
            continue
        d = item_data.get(qid)
        if not d:
            continue
        en_name = title.replace("_", " ").strip().lower()
        labels = d["labels"]
        for lang in PLAIN_LANGS:
            v = labels.get(lang)
            if v and v.strip().lower() != en_name:
                if e["langs"].get(lang) != v:
                    stats["label_upgrades"] += 1
                e["langs"][lang] = v
        zh_tag = cfg["countries"][country]["zh_tag"]
        for cand in ZH_TAG_FROM_HANT.get(zh_tag, []):
            v = labels.get(cand)
            if v:
                e["langs"]["zh"] = v
                break
        if "lat" not in e["facts"] and d["lat"] is not None:
            e["facts"]["lat"] = round(d["lat"], 6)
            e["facts"]["lon"] = round(d["lon"], 6)
            stats["coords_filled"] += 1
        en_lower = title.replace("_", " ").strip().lower()
        kept = [a for a in d.get("aliases", []) if a.lower() != en_lower][:3]
        if kept:
            e["aliases"] = kept
            stats["alias_sets"] = stats.get("alias_sets", 0) + 1
        if d.get("whs"):
            e["facts"]["whs"] = True
            stats["world_heritage"] = stats.get("world_heritage", 0) + 1
        if d["vis"] is not None and d["vis"] >= 1000:
            vis = int(d["vis"]) if d["vis"] == int(d["vis"]) else d["vis"]
            e["facts"]["vis"] = vis
            e["facts"]["vis_year"] = d["vis_year"]
            stats["visitors_with_year"] += 1

    json.dump(data, open(args.verified, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(stats, open("harvest/out/wikidata_stats.json", "w"), indent=1)
    print("labels upgraded: %d, coords filled: %d, visitors with year: %d, "
          "world heritage graded: %d, alias sets: %d"
          % (stats["label_upgrades"], stats["coords_filled"],
             stats["visitors_with_year"], stats.get("world_heritage", 0), stats.get("alias_sets", 0)))


if __name__ == "__main__":
    main()
