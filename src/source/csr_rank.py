# -*- coding: utf-8 -*-
"""CSRankings institution ranking, used to decide whose university is mailed first
(Ruturva, 2026-10-29: "mailed according to the CSR ranking of their respective university").

Computed from the CSRankings author data in data/cache/csrankings/ the way csrankings.org does:
an institution's score is the geometric mean, over the four AI areas, of its adjusted paper
counts since 2016:
    AI      aaai, ijcai
    Vision  cvpr, iccv, eccv
    ML      icml, nips, iclr, kdd
    NLP     acl, emnlp, naacl
Rank 1 = best. An institution is matched to a professor's `university` by an exact name, else by a
normalised name ("Univ." = "University", punctuation and "the" ignored). A university CSRankings
does not list gets UNKNOWN (mailed after every ranked one).

Usage:  .venv/bin/python src/source/csr_rank.py
Writes data/csr_rank.json ({institution: rank}) and sets `csr_rank` on every row of
data/professors.json.
"""
import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
AREAS = {"ai": {"aaai", "ijcai"}, "vision": {"cvpr", "iccv", "eccv"},
         "ml": {"icml", "nips", "iclr", "kdd"}, "nlp": {"acl", "emnlp", "naacl"}}
VENUE_AREA = {v: a for a, vs in AREAS.items() for v in vs}
SINCE = 2016
UNKNOWN = 9999
# Spellings that differ from CSRankings' (OpenAlex name -> CSRankings name). No fuzzy matching:
# it paired unrelated universities ("Ghent" with "Kent").
ALIAS = {"Technical University of Munich": "TU Munich", "Delft University of Technology": "TU Delft",
         "Purdue University West Lafayette": "Purdue University", "UNSW Sydney": "UNSW",
         "University of Padua": "University of Padova", "Universität Hamburg": "University of Hamburg",
         "Polytechnique Montréal": "Polytechnique Montreal", "University of Nebraska–Lincoln": "University of Nebraska",
         "Technische Universität Dresden": "TU Dresden", "Technische Universität Berlin": "TU Berlin",
         "Norwegian University of Science and Technology": "NTNU",
         "Christian-Albrechts-Universität zu Kiel": "University of Kiel",
         "Technische Universität München": "TU Munich", "KTH Royal Institute of Technology": "KTH Royal Inst. of Technology",
         "Hamburg University": "University of Hamburg", "Universiteit van Amsterdam": "University of Amsterdam"}


def norm(name: str) -> str:
    n = name.lower().replace("&", " and ")
    n = re.sub(r"\buniv\b\.?", "university", n)
    n = re.sub(r"\binst\b\.?", "institute", n)
    n = re.sub(r"\btech\b\.?", "technology", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b(the|of|at|in|and)\b", " ", n)
    return " ".join(sorted(n.split()))  # word order does not matter: "Berkeley University California"


def rank_institutions() -> dict:
    counts = defaultdict(lambda: defaultdict(float))
    for r in csv.DictReader(open(DATA / "cache" / "csrankings" / "generated-author-info.csv", encoding="utf-8")):
        area = VENUE_AREA.get(r["area"])
        if area and int(r["year"]) >= SINCE:
            counts[r["dept"]][area] += float(r["adjustedcount"])
    score = {d: math.prod(c[a] + 1 for a in AREAS) ** (1 / len(AREAS)) for d, c in counts.items()}
    ordered = sorted(score, key=lambda d: -score[d])
    return {d: i + 1 for i, d in enumerate(ordered)}


def match(university: str, ranks: dict, by_norm: dict) -> int:
    if university in ranks:
        return ranks[university]
    if ALIAS.get(university) in ranks:
        return ranks[ALIAS[university]]
    return by_norm.get(norm(university), UNKNOWN)


def main() -> None:
    ranks = rank_institutions()
    (DATA / "csr_rank.json").write_text(json.dumps(ranks, indent=1, ensure_ascii=False))
    by_norm = {}
    for d, r in ranks.items():
        by_norm.setdefault(norm(d), r)
    rows = json.loads((DATA / "professors.json").read_text())
    for row in rows:
        row["csr_rank"] = match(row["university"], ranks, by_norm)
    (DATA / "professors.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    env = [r for r in rows if r.get("track") != "pure_ml"]
    print(f"{len(ranks)} institutions ranked; matched {sum(r['csr_rank'] != UNKNOWN for r in rows)}/{len(rows)} "
          f"professors (environmental {sum(r['csr_rank'] != UNKNOWN for r in env)}/{len(env)})")
    miss = {}
    for r in env:
        if r["csr_rank"] == UNKNOWN:
            miss[r["university"]] = miss.get(r["university"], 0) + 1
    print("unmatched environmental universities (top):", sorted(miss.items(), key=lambda x: -x[1])[:15])
    print("top 10:", [d for d, _ in sorted(ranks.items(), key=lambda x: x[1])[:10]])


if __name__ == "__main__":
    main()
