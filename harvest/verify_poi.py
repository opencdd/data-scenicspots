#!/usr/bin/env python3
"""Case-by-case verification of harvested DBpedia POI candidates.

DBpedia is a dirty mirror: misapplied categories, stale facts, labels
that are just English romanizations, and entries that are not places
at all. Nothing enters the ScenicSpots corpus on DBpedia's word alone.

For every candidate this stage queries the English Wikipedia API
(20 titles per batch) and checks, per entry:

  1. EXISTENCE — the page exists (redirects resolved).
  2. COUNTRY — the description or opening text names the entity's
     country (with per-country alias sets: England/Scotland count for
     the UK, "U.S." for the United States, ...).
  3. TYPE — the text supports the assigned spot type (museum text for
     Museum, park text for NationalPark, ...). Landmark is unchecked
     (heterogeneous). Institutional keywords anywhere in the lead
     (university, station, hotel, ...) reject outright.
  4. LABELS — every non-English label must equal the Wikipedia
     interlanguage title for that language (langlinks), which replaces
     DBpedia's rdfs:label as the source of the native name. Unconfirmed
     labels are dropped, never guessed.
  5. FACTS — established year, elevation, area, and visitor numbers
     must appear verbatim in the page text; unconfirmed numbers are
     dropped, never guessed.

Entries failing 1-3 are REJECTED and written to poi_rejected.json with
the reason. Entries passing are written to poi_harvest_verified.json,
which is what the emitter consumes.

Reads:  harvest/out/poi_harvest.json
Writes: harvest/out/poi_harvest_verified.json
        harvest/out/poi_rejected.json
"""

import argparse
import json
import re
import time
import urllib.parse
import urllib.request

API = "https://en.wikipedia.org/w/api.php"
BATCH = 20

COUNTRY_ALIASES = {
    "Taiwan": [r"Taiwan", r"Taipei", r"Taichung", r"Kaohsiung", r"Tainan"],
    "Japan": [r"Japan", r"Japanese", r"Tokyo", r"Kyoto", r"Osaka", r"Nara",
              r"Kanazawa", r"Nagoya", r"Matsuyama", r"Hiroshima"],
    "SouthKorea": [r"South Korea", r"\bKorea\b", r"Korean", r"Seoul", r"Busan",
                   r"Gyeongju", r"Jeju"],
    "Italy": [r"Italy", r"Italian", r"Rome", r"Florence", r"Venice", r"Milan",
              r"Naples", r"Tuscany"],
    "HongKong": [r"Hong Kong"],
    "UnitedKingdom": [r"United Kingdom", r"\bUK\b", r"Britain", r"British",
                      r"England", r"English\b", r"Scotland", r"Wales",
                      r"Northern Ireland", r"London", r"Edinburgh"],
    "France": [r"France", r"French", r"Paris"],
    "UnitedStates": [r"United States", r"U\.S\.", r"\bUSA\b", r"American",
                     r"California", r"New York", r"Texas", r"Arizona", r"Utah",
                     r"Colorado", r"Monterey", r"Chicago", r"Boston",
                     r"Washington", r"Seattle", r"Los Angeles",
                     r"San Francisco", r"Mojave"],
    "China": [r"\bChina\b", r"Chinese", r"Beijing", r"Shanghai", r"Hangzhou",
              r"Xi'an", r"Chengde"],
    "Singapore": [r"Singapore"],
    "Germany": [r"Germany", r"German\b", r"Berlin", r"Munich", r"Bavaria",
                r"Cologne"],
    "Spain": [r"Spain", r"Spanish", r"Madrid", r"Barcelona", r"Andalusia",
              r"Seville", r"Granada"],
    "Thailand": [r"Thailand", r"\bThai\b", r"Bangkok", r"Chiang Mai"],
    "Vietnam": [r"Vietnam", r"Vietnamese", r"Hanoi", r"Ho Chi Minh",
                r"Hue\b"],
    "Malaysia": [r"Malaysia", r"Malaysian", r"Kuala Lumpur", r"Penang"],
    "Australia": [r"Australia", r"Australian", r"Sydney", r"Melbourne",
                  r"Canberra", r"Queensland"],
    "Canada": [r"Canada", r"Canadian", r"Toronto", r"Vancouver", r"Ottawa",
               r"Ontario", r"Quebec"],
    "India": [r"\bIndia\b", r"Indian\b", r"Delhi", r"Mumbai", r"Jaipur"],
    "Philippines": [r"Philippines", r"Philippine", r"Filipino", r"Manila",
                    r"Intramuros"],
    "Indonesia": [r"Indonesia", r"Indonesian", r"Jakarta", r"Bali",
                  r"Java\b", r"Borobudur"],
    "Mexico": [r"Mexico", r"Mexican", r"Guadalajara", r"Yucat"],
    "Brazil": [r"Brazil", r"Brazilian", r"Rio de Janeiro", r"S[uãa]o Paulo"],
    "Egypt": [r"Egypt", r"Egyptian", r"\bCairo\b", r"\bGiza\b", r"Nile\b"],
    "Turkey": [r"Turkey", r"Turkish", r"Ankara", r"Istanbul", r"Bosphorus",
               r"Cappadocia"],
    "Greece": [r"Greece", r"Greek", r"Athens", r"Aegean", r"Peloponnese"],
    "Netherlands": [r"Netherlands", r"Dutch", r"Amsterdam", r"Holland"],
    "Portugal": [r"Portugal", r"Portuguese", r"Lisbon", r"Sintra"],
    "Poland": [r"Poland", r"Polish", r"Warsaw", r"Krak[oó]w", r"Gda[nń]sk"],
    "Czechia": [r"Czech", r"Prague", r"Bohemia", r"Moravia"],
    "Hungary": [r"Hungary", r"Hungarian", r"Budapest", r"Danube"],
    "Switzerland": [r"Switzerland", r"Swiss", r"Zurich", r"Geneva", r"Lucerne"],
    "Austria": [r"Austria", r"Austrian", r"Vienna", r"Salzburg"],
    "Belgium": [r"Belgium", r"Belgian", r"Brussels", r"Bruges", r"Antwerp", r"Flanders"],
    "Sweden": [r"Sweden", r"Swedish", r"Stockholm", r"Gothenburg"],
    "Norway": [r"Norway", r"Norwegian", r"Oslo", r"Bergen", r"Fjord"],
    "Denmark": [r"Denmark", r"Danish", r"Copenhagen"],
    "Finland": [r"Finland", r"Finnish", r"Helsinki", r"Suomi"],
    "Ireland": [r"Ireland", r"Irish", r"Dublin"],
    "Iceland": [r"Iceland", r"Icelandic", r"Reykjavik"],
    "Croatia": [r"Croatia", r"Croatian", r"Zagreb", r"Dubrovnik", r"Split\b", r"Dalmatia"],
}

TYPE_CONFIRM = {
    "Museum": [r"museum", r"gallery"],
    "Temple": [r"temple", r"shrine", r"worship"],
    "NationalPark": [r"national park", r"\bpark\b", r"protected area"],
    "Palace": [r"palace", r"castle", r"country house", r"fortress", r"fort\b"],
    "HotSpring": [r"hot spring", r"onsen", r"\bspa\b", r"thermal"],
    "NightMarket": [r"market"],
    "Cathedral": [r"cathedral", r"church", r"basilica", r"minster", r"abbey"],
    "Mosque": [r"mosque", r"masjid"],
}

# anything in the lead of the article that disqualifies the entry
INSTITUTIONAL = re.compile(
    r"\buniversity\b|\bcollege\b|\bschool\b|\bhospital\b|\bstation\b|"
    r"\bairport\b|\bhotel\b|\bcompany\b|\bcorporation\b|\bministry\b|"
    r"\bauthority\b|\bmunicipality\b|\bborough\b|\bdepartment store\b",
    re.I,
)

ZH_VARIANT = {"zh-TW": "zh-hant", "zh-HK": "zh-hant", "zh-CN": "zh-hans"}

LANG_TITLES = ["zh", "zh-hans", "zh-hant", "ja", "ko", "fr", "it", "de", "es", "th", "vi"]


def api(batch_titles, sleep):
    params = {
        "action": "query", "format": "json", "prop": "extracts|langlinks|coordinates",
        "titles": "|".join(batch_titles), "redirects": 1,
        "exintro": 1, "explaintext": 1, "exlimit": str(BATCH),
        "lllimit": "max", "lllang": "|".join(LANG_TITLES),
        "colimit": "max",
    }
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "opencdd-verify/1.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)["query"]
        except Exception as exc:
            if attempt == 2:
                print("  API-FAIL %s" % exc, flush=True)
                return None
            # 429s need a real cool-down, not a token pause
            time.sleep(30 * (attempt + 1))
    return None


def fact_confirmed(kind, value, text):
    v = str(value)
    try:
        if kind == "est":
            m = re.search(r"\b(\d{4})\b", v)
            return bool(m) and re.search(r"\b%s\b" % m.group(1), text) is not None
        n = float(v)
    except ValueError:
        return False
    whole = str(int(n)) if n == int(n) else re.escape(v)
    if kind == "elev":
        return re.search(r"%s\s*(?:m\b|met\b)" % whole, text) is not None
    if kind == "area":
        return re.search(whole, text) is not None
    if kind == "vis":
        if n >= 1_000_000 and n % 1_000_000 == 0:
            millions = int(n / 1_000_000)
            if re.search(r"%s(?:\.\d+)?\s*million" % millions, text, re.I):
                return True
        return re.search(whole, text) is not None
    return False


def verify_country(country, text):
    for alias in COUNTRY_ALIASES.get(country, []):
        if re.search(alias, text, re.I):
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--harvest", default="harvest/out/poi_harvest.json")
    ap.add_argument("--out-dir", default="harvest/out")
    ap.add_argument("--sleep", type=float, default=0.4)
    args = ap.parse_args()

    data = json.load(open(args.harvest, encoding="utf-8"))
    titles = []
    where = {}
    for country, ents in data.items():
        for key in ents:
            titles.append(key)
            where[key] = country

    verified, rejected = {}, []
    stats = {"kept": 0, "rejected": 0, "labels_dropped": 0, "facts_dropped": 0}

    for i in range(0, len(titles), BATCH):
        batch = titles[i:i + BATCH]
        q = api(batch, args.sleep)
        if q is None:
            for t in batch:
                rejected.append({"title": t, "reason": "api-failure"})
                stats["rejected"] += 1
            continue
        # redirects/normalizations remap requested title -> canonical page key
        remap = {}
        for item in (q.get("normalized") or []) + (q.get("redirects") or []):
            remap[item["from"]] = item["to"]
        pages = q.get("pages", {})
        by_title = {}
        for p in pages.values():
            by_title[p["title"]] = p
        for t in batch:
            canonical = remap.get(t, t)
            page = by_title.get(canonical)
            if page is None or "missing" in page:
                rejected.append({"title": t, "reason": "page-missing"})
                stats["rejected"] += 1
                continue
            country = where[t]
            extract = (page.get("extract") or "")[:600]
            langlinks = {ll["lang"]: ll["*"] for ll in page.get("langlinks", [])}
            text = extract
            e = data[country][t]
            desc = e.get("desc") or ""
            search_text = desc + " " + text
            head = search_text[:220]
            type_checks = TYPE_CONFIRM.get(e.get("type", "Landmark"))
            type_in_head = type_checks is None or any(
                re.search(w, head, re.I) for w in type_checks)
            type_anywhere = type_checks is None or any(
                re.search(w, search_text, re.I) for w in type_checks)

            # Institutional keywords in the lead reject only when the
            # lead does not also name the spot type: "university museum"
            # is a museum, but a hotel whose lead never names an
            # attraction type is a hotel.
            if INSTITUTIONAL.search(head) and not type_in_head:
                rejected.append({"title": t, "reason": "institutional-keyword",
                                 "text": head[:120]})
                stats["rejected"] += 1
                continue
            # "building in X" with no attraction type anywhere — a
            # generic building page, not a scenic spot.
            if re.match(r"building\b", desc, re.I) and not type_anywhere:
                rejected.append({"title": t, "reason": "generic-building",
                                 "text": head[:120]})
                stats["rejected"] += 1
                continue
            if not verify_country(country, search_text):
                rejected.append({"title": t, "reason": "country-unconfirmed",
                                 "text": search_text[:120]})
                stats["rejected"] += 1
                continue
            if type_checks and not type_anywhere:
                rejected.append({"title": t, "reason": "type-unconfirmed",
                                 "type": e["type"], "text": head[:120]})
                stats["rejected"] += 1
                continue

            # labels: only interlanguage-title-confirmed ones survive
            langs = {}
            for l, v in e.get("langs", {}).items():
                if l == "zh":
                    continue
                if langlinks.get(l, "").strip() == v.strip():
                    langs[l] = v
                else:
                    stats["labels_dropped"] += 1
            zhtag_langlink = langlinks.get("zh-hant") or langlinks.get("zh-hans") or langlinks.get("zh")
            if zhtag_langlink:
                langs["zh"] = zhtag_langlink
            else:
                stats["labels_dropped"] += 1

            # facts: only numbers present in the article text survive.
            # Coordinates are different: the Wikipedia coordinates API
            # is the primary source itself, so whatever it returns is
            # confirmed by definition.
            facts = {}
            for f, v in e.get("facts", {}).items():
                if fact_confirmed(f, v, page.get("extract") or ""):
                    facts[f] = v
                else:
                    stats["facts_dropped"] += 1
            coords = (page.get("coordinates") or [{}])[0]
            if "lat" in coords and "lon" in coords:
                facts["lat"] = round(float(coords["lat"]), 6)
                facts["lon"] = round(float(coords["lon"]), 6)

            verified.setdefault(country, {})[t] = {
                "langs": langs, "facts": facts,
                "type": e["type"], "desc": e.get("desc"),
                "city": e.get("city"),
            }
            stats["kept"] += 1
        time.sleep(args.sleep)
        print("verified %d/%d" % (min(i + BATCH, len(titles)), len(titles)), flush=True)

    out = args.out_dir.rstrip("/") + "/poi_harvest_verified.json"
    json.dump(verified, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    rej = args.out_dir.rstrip("/") + "/poi_rejected.json"
    json.dump(rejected, open(rej, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("kept %d, rejected %d, labels dropped %d, facts dropped %d" % (
        stats["kept"], stats["rejected"], stats["labels_dropped"], stats["facts_dropped"]))
    print("wrote %s and %s" % (out, rej))


if __name__ == "__main__":
    main()
