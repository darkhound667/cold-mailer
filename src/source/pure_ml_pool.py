# -*- coding: utf-8 -*-
"""Pure AI/ML professor pool (Ruturva, 2026-10-21): faculty whose work is general AI/ML, not
AI x environment, to alternate with the environmental professors already in professors.json.

Source: the public CSRankings faculty list (github.com/emeryberger/CSrankings), which gives
name, affiliation and the professor's own homepage URL, plus every paper they published at
the top venues. "Pure AI/ML" here means: since 2019 at least MIN_ML adjusted papers at the
ML/vision/NLP venues below, and at least MIN_SHARE of all their CSRankings output there.
Anyone already in professors.json (same normalised name) is skipped.

Order: world ranking of the university first (univ_ranks.py, which also gives country and
timezone), then ML paper count. Chinese universities are capped at CHINA_CAP of the pool.

Universities outside the table are taken after the ranked ones (see UNRANKED).

Usage (inputs are downloaded to --src, default data/cache/csrankings/):
    .venv/bin/python src/source/pure_ml_pool.py [--src DIR] [--total 1300]
Writes data/pure_ml_candidates.json.
"""
import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from openalex_candidates import COUNTRIES
from select_pool import name_key
from univ_ranks import RANKS

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = DATA / "pure_ml_candidates.json"

# CSRankings venue ids for the AI, vision, ML and NLP areas.
ML_VENUES = {"aaai", "ijcai", "icml", "nips", "iclr", "cvpr", "iccv", "eccv", "acl", "emnlp", "naacl", "kdd"}
SINCE, MIN_ML, MIN_SHARE, CHINA_CAP = 2019, 3.0, 0.6, 0.12
# Venue -> a plain research-area label for the email's interest line.
AREA_OF = {"cvpr": "computer vision", "iccv": "computer vision", "eccv": "computer vision",
           "acl": "natural language processing", "emnlp": "natural language processing",
           "naacl": "natural language processing", "icml": "machine learning", "nips": "machine learning",
           "iclr": "deep learning", "aaai": "artificial intelligence", "ijcai": "artificial intelligence",
           "kdd": "data mining and machine learning"}


# Universities missing from the rank table (univ_ranks.py) are still usable: CSRankings gives each
# institution's country and state/province (data/cache/csr_institutions.json). They come after
# every ranked university, then by ML paper count (Ruturva, 2026-10-29: "1000 more").
UNRANKED = 1000
ONE_ZONE = {"GB": "Europe/London", "SG": "Asia/Singapore", "JP": "Asia/Tokyo", "NZ": "Pacific/Auckland",
            "CN": "Asia/Shanghai", "KR": "Asia/Seoul", "CH": "Europe/Zurich", "DE": "Europe/Berlin",
            "NL": "Europe/Amsterdam", "FR": "Europe/Paris", "SE": "Europe/Stockholm", "DK": "Europe/Copenhagen",
            "NO": "Europe/Oslo", "FI": "Europe/Helsinki", "AT": "Europe/Vienna", "BE": "Europe/Brussels",
            "IE": "Europe/Dublin", "ES": "Europe/Madrid", "IT": "Europe/Rome"}
US_STATE_ZONE = {
    "America/New_York": {"Connecticut", "Delaware", "Florida", "Georgia", "Indiana", "Maine", "Maryland",
                         "Massachusetts", "Michigan", "New Hampshire", "New Jersey", "New York", "North Carolina",
                         "Ohio", "Pennsylvania", "Rhode Island", "South Carolina", "Vermont", "Virginia",
                         "West Virginia", "District of Columbia", "Kentucky"},
    "America/Chicago": {"Alabama", "Arkansas", "Illinois", "Iowa", "Kansas", "Louisiana", "Minnesota",
                        "Mississippi", "Missouri", "Nebraska", "North Dakota", "Oklahoma", "South Dakota",
                        "Tennessee", "Texas", "Wisconsin"},
    "America/Denver": {"Colorado", "Idaho", "Montana", "New Mexico", "Utah", "Wyoming"},
    "America/Los_Angeles": {"California", "Nevada", "Oregon", "Washington"},
    "America/Phoenix": {"Arizona"}, "Pacific/Honolulu": {"Hawaii"}, "America/Anchorage": {"Alaska"}}
CA_ZONE = {"British Columbia": "America/Vancouver", "Alberta": "America/Edmonton",
           "Saskatchewan": "America/Regina", "Manitoba": "America/Winnipeg", "Ontario": "America/Toronto",
           "Quebec": "America/Toronto", "Nova Scotia": "America/Halifax", "New Brunswick": "America/Halifax",
           "Newfoundland and Labrador": "America/St_Johns"}
AU_ZONE = {"New South Wales": "Australia/Sydney", "Australian Capital Territory": "Australia/Sydney",
           "Victoria": "Australia/Melbourne", "Queensland": "Australia/Brisbane", "Western Australia": "Australia/Perth",
           "South Australia": "Australia/Adelaide", "Tasmania": "Australia/Hobart", "Northern Territory": "Australia/Darwin"}


def locate(aff: str, insts: dict):
    """(country, timezone) for an institution outside the rank table, or None if unknown/out of scope."""
    i = insts.get(aff) or {}
    cc, region = i.get("country"), i.get("region") or ""
    if cc in ONE_ZONE:
        return cc, ONE_ZONE[cc]
    if cc == "US":
        zone = next((z for z, states in US_STATE_ZONE.items() if region in states), None)
    elif cc == "CA":
        zone = CA_ZONE.get(region)
    elif cc == "AU":
        zone = AU_ZONE.get(region)
    else:
        zone = None
    return (cc, zone) if zone else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DATA / "cache" / "csrankings"))
    ap.add_argument("--total", type=int, default=1300)
    ap.add_argument("--min-ml", type=float, default=MIN_ML, help="ML-venue adjusted papers since 2019 (default 3)")
    ap.add_argument("--min-share", type=float, default=MIN_SHARE, help="share of their output at ML venues (default 0.6)")
    args = ap.parse_args()
    src = Path(args.src)

    have = {name_key(r["name"]) for r in json.loads((DATA / "professors.json").read_text())}
    ml, tot, venues = defaultdict(float), defaultdict(float), defaultdict(Counter)
    for r in csv.DictReader(open(src / "generated-author-info.csv", encoding="utf-8")):
        if int(r["year"]) < SINCE:
            continue
        k, a = (r["name"], r["dept"]), float(r["adjustedcount"])
        tot[k] += a
        if r["area"] in ML_VENUES:
            ml[k] += a
            venues[k][r["area"]] += a

    people = {(r["name"], r["affiliation"]): r
              for r in csv.DictReader(open(src / "csrankings.csv", encoding="utf-8"))}
    cands = []
    for k, m in ml.items():
        if m < args.min_ml or m / tot[k] < args.min_share or k not in people or name_key(k[0]) in have:
            continue
        p = people[k]
        if not p["homepage"].startswith("http"):
            continue
        cands.append({"name": re.sub(r"\s+\d{4}$", "", k[0]), "affiliation": k[1], "homepage": p["homepage"],
                      "scholarid": p["scholarid"], "ml_score": round(m, 1), "ml_share": round(m / tot[k], 2),
                      "area": AREA_OF[venues[k].most_common(1)[0][0]],
                      "venues": {v: round(x, 1) for v, x in venues[k].most_common(3)}})
    print(f"pure-ML faculty not yet in the DB: {len(cands)}")

    # Only universities in the world-ranking table (src/source/univ_ranks.py); best-ranked first.
    insts = json.loads((DATA / "cache" / "csr_institutions.json").read_text())
    ranked = []
    for c in cands:
        if c["affiliation"] in RANKS:
            c["univ_rank"], c["country"], c["timezone"] = RANKS[c["affiliation"]]
        elif (loc := locate(c["affiliation"], insts)):
            c["univ_rank"], (c["country"], c["timezone"]) = UNRANKED, loc
        else:
            continue
        c["institution"] = c["affiliation"]
        if c["country"] in COUNTRIES:
            ranked.append(c)
    ranked.sort(key=lambda c: (c["univ_rank"], -c["ml_score"]))
    china_left = int(args.total * CHINA_CAP)
    chosen = []
    for c in ranked:
        if c["country"] == "CN":
            if china_left <= 0:
                continue
            china_left -= 1
        chosen.append(c)
        if len(chosen) == args.total:
            break
    OUT.write_text(json.dumps(chosen, indent=1, ensure_ascii=False))
    print(f"chosen {len(chosen)}; by country:", dict(Counter(c["country"] for c in chosen).most_common()))
    print("worst university rank taken:", chosen[-1]["univ_rank"], " areas:", dict(Counter(c["area"] for c in chosen).most_common()))


if __name__ == "__main__":
    main()
