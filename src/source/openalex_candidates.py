# -*- coding: utf-8 -*-
"""Phase 1, step 1: find candidate AI x environment researchers from OpenAlex.

Pulls recent works (2021+) that OpenAlex tags with a machine-learning concept AND whose
primary topic sits in an environmental subfield, restricted to institutions in the target
countries. Groups the works by author, so each candidate comes with the paper titles and
URLs that are their fit evidence (hard rule 9).

This finds *candidates*, not professors: OpenAlex knows nothing about job titles. Title,
department, current affiliation and email are checked on official pages afterwards.

Only filter queries are used (1 credit each; keyless budget is 1,000 credits/day). No
contact email is sent to OpenAlex (decision Q14). Raw pages are cached under
data/cache/openalex/ so re-runs cost nothing.

Usage:
    python src/source/openalex_candidates.py fetch      # download (resumable)
    python src/source/openalex_candidates.py build      # aggregate -> data/candidates.json
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "cache" / "openalex"
OUT = ROOT / "data" / "candidates.json"

YEARS = "2021-2026"
# Decision Q8 (2026-10-18).
COUNTRIES = ["US", "GB", "CA", "AU", "SG", "JP", "NZ", "CN", "KR",
             "CH", "DE", "NL", "FR", "SE", "DK", "NO", "FI", "AT", "BE", "IE", "ES", "IT"]
# OpenAlex concepts: Machine learning, Deep learning, Artificial neural network, Reinforcement learning.
ML_CONCEPTS = ["C119857082", "C108583219", "C50644808", "C97541855"]

# Environmental subfields -> the 11 agreed domains (decision Q3). A topic-name rule below
# refines the domain where a subfield is too broad.
SUBFIELD_DOMAIN = {
    "1902": "air quality & atmospheric science",
    "1904": "remote sensing & earth observation",
    "1908": "disaster & climate risk",
    "1910": "oceans & cryosphere",
    "2102": "energy systems, renewables & smart grids",
    "2105": "energy systems, renewables & smart grids",
    "2302": "ecology & biodiversity",
    "2303": "ecology & biodiversity",
    "2304": "pollution & environmental engineering",
    "2305": "pollution & environmental engineering",
    "2306": "climate modeling & weather forecasting",
    "2308": "carbon, emissions & sustainability",
    "2309": "ecology & biodiversity",
    "2310": "pollution & environmental engineering",
    "2311": "pollution & environmental engineering",
    "2312": "hydrology & water resources",
    "1102": "agriculture & food systems",
    "1104": "oceans & cryosphere",
    "1111": "agriculture & food systems",
}
# Topics outside those subfields that are still environmental.
EXTRA_TOPICS = {"T10689": "remote sensing & earth observation"}  # Remote-Sensing Image Classification

TOPIC_DOMAIN_RULES = [  # (regex on primary topic name, domain); first match wins
    (r"remote sensing|lidar|satellite|land use|land cover", "remote sensing & earth observation"),
    (r"flood|drought|landslide|earthquake|seismic|wildfire|fire|hazard|tsunami", "disaster & climate risk"),
    (r"hydrolog|streamflow|groundwater|rainfall|precipitation|water", "hydrology & water resources"),
    (r"air pollut|air quality|aerosol|atmospher", "air quality & atmospheric science"),
    (r"climate|weather|meteorolog", "climate modeling & weather forecasting"),
    (r"carbon|emission|co2|greenhouse", "carbon, emissions & sustainability"),
    (r"solar|wind|photovoltaic|grid|battery|energy|power", "energy systems, renewables & smart grids"),
    (r"crop|agricult|soil|plant", "agriculture & food systems"),
    (r"ocean|sea ice|marine|coastal|glacier|snow|ice", "oceans & cryosphere"),
    (r"species|ecolog|biodiversity|wildlife|animal|forest|vegetation", "ecology & biodiversity"),
]

METHOD_RULES = [  # (regex on title, label)
    (r"reinforcement learning|\bq-learning|\bpolicy (gradient|optimi)", "reinforcement learning"),
    (r"transformer|attention|large language model|\bllm", "transformers"),
    (r"graph neural|\bgnn\b|graph convolution", "graph neural networks"),
    (r"physics[- ]informed|physics[- ]guided|hybrid model", "physics-informed ML"),
    (r"\blstm\b|recurrent|\bgru\b", "recurrent networks"),
    (r"convolutional|\bcnn\b|u-net|unet|resnet", "convolutional networks"),
    (r"deep learning|deep neural|neural network", "deep learning"),
    (r"random forest|gradient boost|xgboost|lightgbm|decision tree", "tree ensembles"),
    (r"gaussian process|bayesian", "probabilistic ML"),
    (r"machine learning|data[- ]driven|artificial intelligence|\bai\b", "machine learning"),
]

NOT_A_PAPER = re.compile(r"^(comment on|reply on|reply to|response to|author response|"
                         r"referee comment|review of|correction to|erratum|corrigendum)\b", re.I)
REVIEW_DOI = re.compile(r"-(rc|cc|ac|ec)\d+$", re.I)
NOT_A_PERSON = re.compile(r"^\s*$|\bet al\b|consortium|collaboration|team\b|group\b", re.I)

SELECT = "id,doi,title,publication_year,authorships,primary_topic"
BASE_FILTER = f"publication_year:{YEARS},concepts.id:{'|'.join(ML_CONCEPTS)}"
QUERIES = {  # name -> filter; each is fetched page by page with a cursor
    "subfields": BASE_FILTER + f",primary_topic.subfield.id:{'|'.join(SUBFIELD_DOMAIN)}",
    "extra_topics": BASE_FILTER + f",primary_topic.id:{'|'.join(EXTRA_TOPICS)}",
}


def get(url: str, tries: int = 4) -> dict:
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # network hiccup or 429; back off and retry
            if attempt == tries - 1:
                raise
            print(f"  retry after {type(e).__name__}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))


def fetch() -> None:
    """Download every page of every query into the cache.

    Keyless OpenAlex allows plain `page=N` paging only up to 10,000 results per query
    (cursor paging needs an API key), so each query is split into one partition per
    country per year. Resumable: finished partitions leave a `done` marker."""
    years = range(int(YEARS[:4]), int(YEARS[-4:]) + 1)
    for name, flt in QUERIES.items():
        for cc in COUNTRIES:
            for year in years:
                pdir = CACHE / name / f"{cc}-{year}"
                if (pdir / "done").exists():
                    continue
                pdir.mkdir(parents=True, exist_ok=True)
                pflt = flt.replace(f"publication_year:{YEARS}", f"publication_year:{year}")
                pflt += f",institutions.country_code:{cc}"
                page, pages = 0, 1
                while page < pages:
                    page += 1
                    params = {"filter": pflt, "per_page": 200, "page": page, "select": SELECT}
                    data = get("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
                    count = data["meta"]["count"]
                    if count > 10_000:
                        sys.exit(f"{name} {cc}-{year}: {count} works, over the 10k paging cap; split further")
                    pages = -(-count // 200)
                    (pdir / f"page-{page:04d}.json").write_text(json.dumps(data["results"]))
                (pdir / "done").write_text("")
                print(f"{name} {cc}-{year}: {count} works, {pages} pages", flush=True)


def domain_of(work: dict) -> str:
    topic = work.get("primary_topic") or {}
    tname = (topic.get("display_name") or "").lower()
    for pattern, dom in TOPIC_DOMAIN_RULES:
        if re.search(pattern, tname):
            return dom
    tid = (topic.get("id") or "").rsplit("/", 1)[-1]
    if tid in EXTRA_TOPICS:
        return EXTRA_TOPICS[tid]
    sid = ((topic.get("subfield") or {}).get("id") or "").rsplit("/", 1)[-1]
    return SUBFIELD_DOMAIN.get(sid, "unmapped")


def methods_of(title: str) -> list:
    t = title.lower()
    return [label for pattern, label in METHOD_RULES if re.search(pattern, t)]


def build() -> None:
    """Group cached works by author (only authorships at a target-country institution)
    and write data/candidates.json, sorted by evidence strength."""
    works = {}
    for page in sorted(CACHE.glob("*/*/page-*.json")):
        for w in json.loads(page.read_text()):
            works[w["id"]] = w  # the two queries can overlap; dedupe by work id

    authors = defaultdict(lambda: {"works": [], "inst": Counter(), "positions": Counter(),
                                   "domains": Counter(), "methods": Counter(), "name": ""})
    for w in works.values():
        title = (w.get("title") or "").strip()
        # Peer-review comments and replies (e.g. EGUsphere "Comment on egusphere-2025-…",
        # DOIs ending -rc1/-cc1/-ac1) are indexed as works but are not research evidence.
        if not title or NOT_A_PAPER.search(title) or REVIEW_DOI.search(w.get("doi") or ""):
            continue
        dom, meths = domain_of(w), methods_of(title)
        for a in w.get("authorships") or []:
            aid = (a.get("author") or {}).get("id")
            insts = [i for i in a.get("institutions") or [] if i.get("country_code") in COUNTRIES]
            if not aid or not insts or NOT_A_PERSON.search((a.get("author") or {}).get("display_name") or ""):
                continue
            rec = authors[aid]
            rec["name"] = a["author"].get("display_name") or rec["name"]
            rec["works"].append({"title": title, "year": w.get("publication_year"),
                                 "url": w.get("doi") or w["id"], "position": a.get("author_position"),
                                 "domain": dom})
            rec["positions"][a.get("author_position")] += 1
            rec["domains"][dom] += 1
            rec["methods"].update(meths)
            for i in insts:
                rec["inst"][(i.get("id"), i.get("display_name"), i.get("country_code"))] += 1

    out = []
    for aid, r in authors.items():
        n, last = len(r["works"]), r["positions"]["last"]
        # Proposed Tier A rule (to confirm with Ruturva): >= 3 intersection works since
        # 2021, with >= 2 as last author (the usual PI position).
        tier = "A" if n >= 3 and last >= 2 else ("B" if n >= 2 else "-")
        (inst_id, inst_name, cc), _ = r["inst"].most_common(1)[0]
        recent = sorted(r["works"], key=lambda x: (x["position"] != "last", -(x["year"] or 0)))
        out.append({
            "openalex_id": aid, "name": r["name"], "tier": tier,
            "n_works": n, "n_last_author": last,
            "institution": inst_name, "institution_id": inst_id, "country": cc,
            "env_domain": r["domains"].most_common(1)[0][0],
            "env_domains": dict(r["domains"].most_common(3)),
            "ml_methods": [m for m, _ in r["methods"].most_common(3)],
            "fit_evidence": recent[:3],
        })
    out.sort(key=lambda x: (-x["n_last_author"], -x["n_works"]))
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False))

    tiers = Counter(x["tier"] for x in out)
    print(f"works: {len(works)}  authors: {len(out)}  tiers: {dict(tiers)}")
    by_country = Counter(x["country"] for x in out if x["tier"] == "A")
    print("Tier A by country:", dict(by_country.most_common()))


def institutions() -> None:
    """Add institution type, city, region and IANA timezone to every Tier A/B candidate,
    and keep only universities (OpenAlex type `education`): companies, government labs
    and hospitals are out of scope. 50 institutions per request, cached."""
    from timezones import timezone_for

    cands = json.loads(OUT.read_text())
    cache_file = CACHE.parent / "institutions.json"
    known = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    wanted = sorted({c["institution_id"] for c in cands if c["tier"] in "AB"} - set(known))
    for i in range(0, len(wanted), 50):
        ids = "|".join(w.rsplit("/", 1)[-1] for w in wanted[i:i + 50])
        params = {"filter": f"openalex_id:{ids}", "per_page": 50,
                  "select": "id,display_name,type,geo,summary_stats"}
        for inst in get("https://api.openalex.org/institutions?" + urllib.parse.urlencode(params))["results"]:
            geo = inst.get("geo") or {}
            known[inst["id"]] = {"type": inst.get("type"), "city": geo.get("city"),
                                 "region": geo.get("region"), "country": geo.get("country_code"),
                                 "h_index": (inst.get("summary_stats") or {}).get("h_index")}
        cache_file.write_text(json.dumps(known))

    kept = []
    for c in cands:
        inst = known.get(c["institution_id"])
        if c["tier"] not in "AB" or not inst or inst["type"] != "education":
            continue
        c.update({"city": inst["city"], "region": inst["region"],
                  "timezone": timezone_for(inst["country"], inst["region"]),
                  "institution_h_index": inst["h_index"]})
        kept.append(c)
    # Rank: intersection evidence first, institution research strength second.
    kept.sort(key=lambda x: (x["tier"], -x["n_last_author"], -x["n_works"], -(x["institution_h_index"] or 0)))
    (OUT.parent / "candidates_universities.json").write_text(json.dumps(kept, indent=1, ensure_ascii=False))

    for tier in "AB":
        rows = [c for c in kept if c["tier"] == tier]
        print(f"Tier {tier} at universities: {len(rows)}  "
              f"(no timezone: {sum(1 for c in rows if not c['timezone'])})")
        print("  by country:", dict(Counter(c["country"] for c in rows).most_common()))


if __name__ == "__main__":
    {"fetch": fetch, "build": build, "institutions": institutions}[
        sys.argv[1] if len(sys.argv) > 1 else "build"]()
