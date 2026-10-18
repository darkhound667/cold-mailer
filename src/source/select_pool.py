# -*- coding: utf-8 -*-
"""Phase 1, step 2: turn Tier A candidates into the ~2,000-professor pool.

Rules agreed with Ruturva (SPEC.md amendments 9 and 13):
  - Tier A only, universities only (already filtered by openalex_candidates.py).
  - Merge duplicate OpenAlex author records (same normalised name at the same institution).
  - China capped at CHINA_CAP; everyone else ranked by intersection evidence first
    (last-author papers, then all papers), institution research strength second, up to TOTAL.
  - Fill missing timezones (US/CA/AU institutions without a state in OpenAlex) from the
    institution's coordinates with timezonefinder.
  - Carry over the hand-verified fields of the 20-row sample (data/sample20.json).

Title, department, homepage and email stay blank here: they come only from official pages
(hard rule 1), filled in by the verification step.

Usage (needs the project venv for timezonefinder):
    .venv/bin/python src/source/select_pool.py
"""
import json
import re
import unicodedata
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

from openalex_candidates import get

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
TOTAL, CHINA_CAP = 2000, 400


def name_key(name: str) -> str:
    """Accent-, case- and order-insensitive name, e.g. 'Li, Jonathan' == 'Jonathan Li'."""
    spaced = re.sub(r"[^\w\s]", " ", name)  # Unicode hyphens etc. become separators first
    ascii_name = unicodedata.normalize("NFKD", spaced).encode("ascii", "ignore").decode().lower()
    return " ".join(sorted(re.sub(r"[^a-z ]", " ", ascii_name).split()))


def merge_duplicates(rows: list) -> list:
    """Collapse records of the same person split across several OpenAlex author ids."""
    groups = defaultdict(list)
    for r in rows:
        groups[(name_key(r["name"]), r["institution_id"])].append(r)
    merged = []
    for recs in groups.values():
        recs.sort(key=lambda r: -r["n_works"])
        main = dict(recs[0])  # the richest record keeps its name, domain and evidence
        main["n_works"] = sum(r["n_works"] for r in recs)
        main["n_last_author"] = sum(r["n_last_author"] for r in recs)
        main["openalex_ids"] = [r["openalex_id"] for r in recs]
        merged.append(main)
    return merged


def fill_timezones(rows: list) -> None:
    """Look up coordinates for institutions with no timezone and resolve them offline."""
    from timezonefinder import TimezoneFinder

    missing = sorted({r["institution_id"] for r in rows if not r.get("timezone")})
    coords = {}
    for i in range(0, len(missing), 50):
        ids = "|".join(m.rsplit("/", 1)[-1] for m in missing[i:i + 50])
        params = {"filter": f"openalex_id:{ids}", "per_page": 50, "select": "id,geo"}
        for inst in get("https://api.openalex.org/institutions?" + urllib.parse.urlencode(params))["results"]:
            geo = inst.get("geo") or {}
            coords[inst["id"]] = (geo.get("latitude"), geo.get("longitude"))
    tf = TimezoneFinder()
    for r in rows:
        if not r.get("timezone"):
            lat, lng = coords.get(r["institution_id"], (None, None))
            r["timezone"] = tf.timezone_at(lat=lat, lng=lng) if lat is not None else None


def main() -> None:
    cands = [c for c in json.loads((DATA / "candidates_universities.json").read_text()) if c["tier"] == "A"]
    pool = merge_duplicates(cands)
    rank = lambda r: (-r["n_last_author"], -r["n_works"], -(r.get("institution_h_index") or 0))
    china = sorted((r for r in pool if r["country"] == "CN"), key=rank)[:CHINA_CAP]
    rest = sorted((r for r in pool if r["country"] != "CN"), key=rank)[:TOTAL - CHINA_CAP]
    chosen = sorted(china + rest, key=rank)
    fill_timezones(chosen)

    verified = {name_key(v["name"]): v for v in json.loads((DATA / "sample20.json").read_text())}
    out = []
    for i, r in enumerate(chosen, 1):
        v = verified.get(name_key(r["name"]), {})
        out.append({
            "id": f"P{i:04d}", "name": r["name"], "title": v.get("title", ""),
            "department": v.get("department", ""), "university": r["institution"],
            "country": r["country"], "timezone": r["timezone"], "env_domain": r["env_domain"],
            "ml_methods": r["ml_methods"], "tier": r["tier"],
            "n_works": r["n_works"], "n_last_author": r["n_last_author"],
            "fit_evidence": [{k: e[k] for k in ("title", "year", "url")} for e in r["fit_evidence"]],
            "homepage": v.get("homepage", ""), "email": v.get("email", ""),
            "source_url": v.get("source_url", ""), "date_found": v.get("date_found", ""),
            "confidence": v.get("confidence", ""), "flags": v.get("flags", ""),
            "verified": bool(v), "openalex_ids": r.get("openalex_ids", [r["openalex_id"]]),
        })
    (DATA / "professors.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))

    avail = Counter(r["country"] for r in pool)
    picked = Counter(r["country"] for r in out)
    print(f"Tier A pool after merging duplicates: {len(pool)} (from {len(cands)} records)")
    print(f"selected: {len(out)}  verified so far: {sum(r['verified'] for r in out)}  "
          f"no timezone: {sum(1 for r in out if not r['timezone'])}")
    print("country  selected / Tier A available")
    for cc, n in avail.most_common():
        print(f"  {cc}  {picked.get(cc, 0):5d} / {n}")
    print("by domain:", dict(Counter(r["env_domain"] for r in out).most_common()))
    if rest:
        print(f"cut-off outside China: >= {rest[-1]['n_last_author']} last-author papers; "
              f"China cut-off: >= {china[-1]['n_last_author']}")


if __name__ == "__main__":
    main()
