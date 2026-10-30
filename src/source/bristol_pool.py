# -*- coding: utf-8 -*-
"""University of Bristol academics who work in Ruturva's domain, from Bristol's own staff directory
(Ruturva, 2026-10-30: "add bristol university professors ... only the ones doing research ... in my
domain", mailed first in the next batch).

Source: www.bristol.ac.uk/people/ (the university's staff pages; its sitemap lists every page). Each
page prints the person's position, school, expertise tags, biography and official email, so the
email is "as published on their pages" (Phase 1 rule). Nothing is searched or guessed.

Selection:
  - academic staff only: position contains Professor, Reader or Lecturer (not honorary, visiting or
    emeritus), in a technical school (computer science, engineering, mathematics, earth/geographical
    sciences, physics, biological sciences);
  - domain: AI/ML named in their expertise tags or job title, at least three distinct AI/ML terms in
    the biography, or papers at the CSRankings ML venues since 2016. With an environmental term as well (climate, ecology, hydrology, ...) they join the
    environmental track (env_domain from the first matching term); otherwise the pure AI/ML track;
  - anyone already in professors.json (same email or name) is not added twice, but every Bristol
    row that is sendable gets `priority = 1`, which scripts/next_batch_ids.py puts first.
Whether a group takes interns is not published in a form a script can read, so it is not a filter.

Usage:  .venv/bin/python src/source/bristol_pool.py [--workers 12] [--dry-run]
Fetched pages are cached in data/cache/bristol_people.json (resumable).
"""
import argparse
import html
import json
import re
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import csr_rank  # noqa: E402

SITEMAP = "https://www.bristol.ac.uk/people/sitemap/sitemap-0.xml"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}
ACADEMIC = re.compile(r"\b(Professor|Reader|Lecturer|Chair)\b", re.I)
NOT_STAFF = re.compile(r"\b(Honorary|Visiting|Emeritus|Proleptic|Research Fellow|Teaching Fellow)\b", re.I)
# Schools whose work can be AI/ML research; law, business, philosophy, medicine and the like are out
# even when a biography mentions AI (they write about it, they do not build it).
TECH_SCHOOL = re.compile(r"School of (Computer Science|Engineering|Electrical|Mechanical|Civil|Aerospace|Mathematics|"
                         r"Earth Sciences|Geographical Sciences|Physics|Biological Sciences)", re.I)
ML_TERMS = {
    "machine learning": r"machine[- ]learning", "deep learning": r"deep[- ]learning",
    "artificial intelligence": r"artificial[- ]intelligence|\bAI\b", "neural networks": r"neural[- ]network",
    "computer vision": r"computer[- ]vision", "natural language processing": r"natural[- ]language|\bNLP\b|language model",
    "reinforcement learning": r"reinforcement[- ]learning", "data science": r"data[- ]science",
    "generative models": r"generative (model|AI)|diffusion model",
    "statistical learning": r"statistical (machine )?learning|probabilistic (model|machine)|Bayesian (inference|learning|model)|Gaussian process",
}
ENV_DOMAINS = [  # first match wins
    ("remote sensing & earth observation", r"remote sensing|earth observation|satellite"),
    ("hydrology & water resources", r"hydrolog|flood|water resource|river|catchment"),
    ("ecology & biodiversity", r"ecolog|biodiversity|wildlife|conservation"),
    ("climate modeling & weather forecasting", r"climate|weather|meteorolog"),
    ("air quality & atmospheric science", r"atmospher|air quality|aerosol"),
    ("disaster & climate risk", r"natural hazard|disaster|volcan|earthquake|landslide"),
    ("energy systems, renewables & smart grids", r"renewable|smart grid|energy system|wind energy|solar"),
    ("oceans & cryosphere", r"ocean|glaci|ice sheet|cryosphere|marine"),
    ("carbon, emissions & sustainability", r"carbon|emission|sustainab|net zero"),
    ("agriculture & food systems", r"agricultur|crop|food system"),
    ("pollution & environmental engineering", r"pollution|environmental engineering|environmental monitoring|waste"),
]
ML_AREA = [("computer vision", "computer vision"), ("natural language processing", "natural language processing"),
           ("reinforcement learning", "reinforcement learning"), ("deep learning", "deep learning"),
           ("artificial intelligence", "artificial intelligence"), ("machine learning", "machine learning")]
NAME_PREFIX = re.compile(r"^(Professor|Prof\.?|Dr\.?|Doctor|Mr\.?|Ms\.?|Mrs\.?|Miss|Reader|Lecturer)\s+", re.I)
DEGREE = re.compile(r"\s+(?=(?:PhD|Ph\.D|DPhil|MSc|M\.Sc|BSc|B\.Sc|MSci|BA|B\.A|MA|M\.A|MEng|MRes|MPhil|M\.Phil|BEng|LL|MMath|MBA|MPP|MPA|MBChB|BMBCh|MS|BS|BE|MASt|PGCert|MPharm|MD|MB|M\.Eng|BScEng|FIAPR|DF-BMVA|FHEA|SFHEA|FRS|FREng|CEng|FIET|FIEEE|MIEEE)\b)")


def get(url: str) -> str:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40).read().decode("utf-8", "ignore")


def parse(page: str) -> dict:
    txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<script.*?</script>|<style.*?</style>|<[^>]+>", " ", page)))
    m = re.search(r"Back (?P<name>.+?) (?:Expertise (?P<exp>.*?) )?Current positions (?P<pos>.*?) Contact (?P<contact>.*?) Press and media", txt)
    if not m:
        return {}
    emails = [e for e in re.findall(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", m["contact"])
              if not e.lower().startswith(("press-office", "media"))]
    bio = re.search(r"Biography (.*?)(?: Research interests| Publications| Projects| Teaching|$)", txt)
    return {"name": m["name"].strip(), "position": m["pos"].strip(), "expertise": (m["exp"] or "").strip(),
            "email": emails[0] if emails else "", "bio": (bio.group(1) if bio else "")[:1500]}


def fetch_one(url: str) -> dict:
    try:
        return parse(get(url))
    except Exception as e:  # a page that will not load is skipped, no retry (Phase 1 rule)
        return {"error": type(e).__name__}


def csr_ml_people() -> set:
    """(surname, first initial) of Bristol people with ML-venue papers since 2016 (CSRankings)."""
    import csv
    ml = {"aaai", "ijcai", "icml", "nips", "iclr", "cvpr", "iccv", "eccv", "acl", "emnlp", "naacl", "kdd"}
    score = {}
    for r in csv.DictReader(open(DATA / "cache" / "csrankings" / "generated-author-info.csv", encoding="utf-8")):
        if r["dept"] == "University of Bristol" and r["area"] in ml and int(r["year"]) >= 2016:
            score[r["name"]] = score.get(r["name"], 0) + float(r["adjustedcount"])
    out = set()
    for name, v in score.items():
        parts = re.sub(r"\s+\d{4}$", "", name).split()
        if v >= 0.3 and len(parts) >= 2:
            out.add((re.sub(r"[^a-z]", "", parts[-1].lower()), parts[0][0].lower()))
    return out


def person_key(name: str):
    parts = name.split()
    return re.sub(r"[^a-z]", "", parts[-1].lower()), parts[0][0].lower()


def classify(p: dict, csr: set = frozenset()):
    """(track, env_domain, ml_area, methods) or None when the person is not in the domain."""
    if (not p.get("email") or not ACADEMIC.search(p["position"]) or NOT_STAFF.search(p["position"])
            or not TECH_SCHOOL.search(p["position"])):
        return None
    exp_hits = [k for k, rx in ML_TERMS.items() if re.search(rx, p["expertise"] + " " + p["position"], re.I)]
    bio_hits = [k for k, rx in ML_TERMS.items() if re.search(rx, p["bio"], re.I)]
    published = person_key(NAME_PREFIX.sub("", p["name"]).strip()) in csr  # papers at ML venues
    if not exp_hits and len(bio_hits) < 3 and not published:
        return None
    text = " ".join([p["expertise"], p["position"], p["bio"]])
    methods = sorted(set(exp_hits + bio_hits)) or ["machine learning"]
    area = next((a for k, a in ML_AREA if k in methods), "machine learning")
    domain = next((d for d, rx in ENV_DOMAINS if re.search(rx, text, re.I)), None)
    return ("env", domain, area, methods) if domain else ("pure_ml", "pure AI/ML", area, methods)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cache_file = DATA / "cache" / "bristol_people.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    urls = re.findall(r"<loc>([^<]+)</loc>", get(SITEMAP))
    todo = [u for u in urls if u not in cache]
    with ThreadPoolExecutor(args.workers) as ex:
        for i, (u, r) in enumerate(zip(todo, ex.map(fetch_one, todo)), 1):
            cache[u] = r
            if i % 500 == 0:
                cache_file.write_text(json.dumps(cache, ensure_ascii=False))
                print(f"{i}/{len(todo)} pages", flush=True)
    cache_file.write_text(json.dumps(cache, ensure_ascii=False))

    db = json.loads((DATA / "professors.json").read_text())
    have_email = {r["email"].lower() for r in db if r.get("email")}
    have_name = {re.sub(r"[^a-z]", "", r["name"].lower()) for r in db if "bristol" in r["university"].lower()}
    ranks = csr_rank.rank_institutions()
    bristol_rank = ranks.get("University of Bristol", csr_rank.UNKNOWN)
    start = max(int(r["id"][1:]) for r in db) + 1
    today = date.today().isoformat()
    csr = csr_ml_people()
    new = []
    for url, p in cache.items():
        c = classify(p, csr) if p.get("name") else None
        if c is None:
            continue
        name = DEGREE.split(NAME_PREFIX.sub("", p["name"]).strip(), maxsplit=1)[0].strip(" ,")
        if p["email"].lower() in have_email or re.sub(r"[^a-z]", "", name.lower()) in have_name:
            continue
        have_email.add(p["email"].lower())
        track, domain, area, methods = c
        row = {"id": f"P{start + len(new):04d}", "name": name, "title": p["position"][:120], "department": "",
               "university": "University of Bristol", "country": "GB", "timezone": "Europe/London",
               "env_domain": domain, "ml_methods": methods, "tier": "A", "n_works": 0, "n_last_author": 0,
               "fit_evidence": [], "homepage": url, "email": p["email"], "source_url": url, "date_found": today,
               "confidence": "high", "flags": f"Bristol staff page {today}; expertise: {p['expertise'][:150] or 'from biography'}",
               "verified": True, "openalex_ids": [], "status": "verified", "sendable": True,
               "csr_rank": bristol_rank, "priority": 1}
        if track == "pure_ml":
            row.update(track="pure_ml", ml_area=area, univ_rank=53, scholarid="")
        new.append(row)
    print("staff pages:", len(cache), "| usable:", sum(1 for p in cache.values() if p.get("email")),
          "| new in-domain academics:", len(new), dict(Counter(r.get("track", "env") for r in new)))
    for r in new[:60]:
        print(f"  {r['id']} {r['name']:<28} {r.get('track', 'env'):<8} {r['env_domain'][:28]:<28} {r['title'][:50]}")
    if args.dry_run:
        return
    for r in db:  # every sendable Bristol row, old or new, goes first
        if "bristol" in r["university"].lower() and r.get("sendable"):
            r["priority"] = 1
    db.extend(new)
    (DATA / "professors.json").write_text(json.dumps(db, indent=1, ensure_ascii=False))
    print("added", len(new), "rows; Bristol rows with priority:", sum(1 for r in db if r.get("priority")))


if __name__ == "__main__":
    main()
