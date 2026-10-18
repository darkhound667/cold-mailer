# -*- coding: utf-8 -*-
"""Phase 2: write one reviewable draft per professor into review/ (local, gitignored).

The template is fixed (SPEC.md Phase 2 + amendments): no sentence about the professor's
own work. What varies is only the salutation, the subject line, and the ORDER of the two
resume projects, chosen from the professor's env_domain:
  - Direct Air Capture screening first for carbon / air / climate / energy / pollution;
  - wildlife-detection ML first for everything else.
Both project lines restate the resume in plain words, with no numbers or results.
The noisy-label project is not cited (decision Q13), and nor is the Deep RL trading project.

Each review/P####.md starts with a small header (id, status, to, subject, attachment) and
then the exact body. review/INDEX.csv lists every draft with risk flags for review:
missing title, medium-confidence email, weak fit evidence, unusual name, and any note
carried over from verification.

Usage:
    python src/generate/drafts.py [--limit 100]
"""
import argparse
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA, REVIEW = ROOT / "data", ROOT / "review"
ATTACHMENT = "Ruturva_Vaghasia_Resume.pdf"

INTRO = ("I hope this message finds you well. My name is Ruturva Vaghasia, and I am a second-year "
         "B.Tech student in Environmental Science and Engineering at IIT Bombay. I am interested in "
         "applying AI, machine learning and reinforcement learning to environmental problems.")
WILDLIFE = ("In a course project, I built a machine-learning pipeline that detects animals in images, "
            "using texture features I designed and a gradient-boosted classifier.")
DAC = ("As an R&D trainee at Chemeca, I worked on screening adsorbent materials for direct air capture "
       "of CO2, building a framework to judge which sorbents are viable and comparing frameworks to "
       "find what drives their uptake.")
CLOSING = "Looking forward to hearing from you.\n\nRegards,\nRuturva Vaghasia"
DAC_FIRST = {"carbon, emissions & sustainability", "air quality & atmospheric science",
             "climate modeling & weather forecasting", "energy systems, renewables & smart grids",
             "pollution & environmental engineering"}
# Verification notes that should block a quick approve (the rest are informational).
RISKY_NOTE = re.compile(r"(?i)check|moved|identity|conflict|differs|deceas|possibl|unconfirm|unverif|"
                        r"not confirmed|snippet|fixed-term|rtd|research-track|adjunct|on leave|stale|old|mailto")
PARTICLES = {"van", "von", "de", "del", "della", "der", "den", "da", "di", "du", "le", "la", "al", "el", "dos", "das"}


def surname(name: str) -> tuple:
    """(surname, unusual?) from a 'Given Family' display name, keeping particles like 'van'."""
    clean = re.sub(r"\s+", " ", name.replace("‐", "-")).strip()
    tokens = clean.split(" ")
    unusual = (len(tokens) == 1 or len(tokens) > 3 or clean.isupper()
               or bool(re.search(r"(^|\s)[A-Z]\.?$", clean))  # trailing initial, e.g. "Shutao Li A"
               or any(t.lower() in PARTICLES for t in tokens[:-1])
               or bool(re.search(r"[^\x00-\x7F]", clean)))
    while len(tokens) > 1 and re.fullmatch(r"[A-Z]\.?", tokens[-1]):  # "Yifang Ban A" -> "Ban"
        tokens = tokens[:-1]
    last = tokens[-1]
    if len(tokens) >= 3 and tokens[-2].lower() in PARTICLES:
        last = " ".join(tokens[-2:])
    return last, unusual


def salutation(r: dict) -> str:
    last, _ = surname(r["name"])
    return f"Dear Professor {last}," if "prof" in (r.get("title") or "").lower() else f"Dear Dr. {last},"


def subject(r: dict) -> str:
    return f"Prospective research intern: machine learning for {r['env_domain'].replace(' & ', ' and ')}"


def body(r: dict) -> str:
    first, second = (DAC, WILDLIFE) if r["env_domain"] in DAC_FIRST else (WILDLIFE, DAC)
    return "\n\n".join([salutation(r), INTRO, first, second, CLOSING]) + "\n"


def risk_flags(r: dict) -> list:
    flags = []
    if not r.get("title") or "not verified" in r["title"].lower():
        flags.append("missing title")
    if r.get("confidence") == "medium":
        flags.append("medium-confidence email")
    if r["n_works"] < 5 or r["n_last_author"] < 3:
        flags.append("weak fit evidence")
    if surname(r["name"])[1]:
        flags.append("unusual name")
    if r.get("flags") and RISKY_NOTE.search(r["flags"]):  # informational notes stay in professors.json
        flags.append("check note: " + r["flags"])
    return flags


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()

    rows = [r for r in json.loads((DATA / "professors.json").read_text()) if r.get("sendable")][: args.limit]
    REVIEW.mkdir(exist_ok=True)
    index = []
    for r in rows:
        path = REVIEW / f"{r['id']}.md"
        if not path.exists():  # never overwrite a draft that may already have hand edits
            header = (f"---\nid: {r['id']}\nstatus: draft\nto: {r['email']}\nname: {r['name']}\n"
                      f"university: {r['university']}\nsubject: {subject(r)}\nattachment: {ATTACHMENT}\n---\n\n")
            path.write_text(header + body(r))
        index.append({"id": r["id"], "name": r["name"], "title": r.get("title", ""),
                      "university": r["university"], "country": r["country"], "timezone": r["timezone"],
                      "env_domain": r["env_domain"], "email": r["email"], "email_confidence": r.get("confidence", ""),
                      "subject": subject(r), "file": path.name, "risk_flags": " | ".join(risk_flags(r))})
    with open(REVIEW / "INDEX.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(index[0]))
        w.writeheader()
        w.writerows(index)
    flagged = sum(1 for i in index if i["risk_flags"])
    print(f"drafts: {len(index)} in review/  (flagged for review: {flagged})")


if __name__ == "__main__":
    main()
