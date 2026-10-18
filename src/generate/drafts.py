# -*- coding: utf-8 -*-
"""Phase 2: write one reviewable draft per professor into review/ (local, gitignored).

The template follows Ruturva's sample email (SPEC.md amendment 14). Nothing refers to the
professor's papers. What varies is the salutation, the research area named in the interest
line (from env_domain + ML methods), the subject, and the second project:
  - Direct Air Capture traineeship for carbon / energy professors;
  - the Chennai air-quality group project for everyone else.
The first project is always the wildlife-detection ML project.
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
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA, REVIEW = ROOT / "data", ROOT / "review"
ATTACHMENT = "Ruturva_Vaghasia_Resume.pdf"

INTRO = ("My name is Ruturva Vaghasia, an Environmental Science and Engineering undergraduate with a "
         "minor in Artificial Intelligence and Data Science at IIT Bombay, where I hold a CPI of 9.28. "
         "I am writing to express my interest in working with you on research in {area}, an area I "
         "would like to go considerably deeper into.")
WILDLIFE = ("I built a wildlife-detection model for image grids using handcrafted texture features and "
            "gradient-boosted trees.")
CHENNAI = ("I also analysed, in a group project, air-quality monitoring data from a station in Chennai, "
           "tracing pollution spikes to their likely causes and flagging faulty sensor readings.")
DAC = ("I also worked as an R&D trainee at Chemeca on screening adsorbent materials for direct air "
       "capture of CO2.")
ASK = ("I would be grateful for any opportunity to contribute to your group's work, and I would be "
       "glad to discuss how I could be of help.")
CLOSING = ("My resume is attached for your reference. Thank you for your time and consideration. "
           "I look forward to hearing from you.\n\nWarm regards,\nRuturva Vaghasia\n"
           "B.Tech. Environmental Science and Engineering, Minor in Artificial Intelligence and Data Science\n"
           "Indian Institute of Technology Bombay")
# Research area named in the interest line: built only from the professor's domain and the ML
# methods in their paper titles, never from a specific paper (Ruturva's sample, 2026-10-18).
AREA = {
    "remote sensing & earth observation": "remote sensing and Earth observation",
    "hydrology & water resources": "hydrology and water resources",
    "ecology & biodiversity": "ecology and biodiversity conservation",
    "climate modeling & weather forecasting": "climate and weather modelling",
    "air quality & atmospheric science": "air quality and atmospheric science",
    "disaster & climate risk": "natural hazards and disaster risk",
    "energy systems, renewables & smart grids": "energy systems and renewables",
    "pollution & environmental engineering": "pollution and environmental engineering",
    "agriculture & food systems": "agriculture and food systems",
    "oceans & cryosphere": "ocean and cryosphere science",
    "carbon, emissions & sustainability": "carbon emissions and sustainability",
}
# Second project: the Direct Air Capture traineeship for carbon and energy professors, the
# Chennai air-quality group project for everyone else (Ruturva, 2026-10-18).
DAC_DOMAINS = {"carbon, emissions & sustainability", "energy systems, renewables & smart grids"}
# Professor titles as printed on official pages, in the languages seen so far.
PROF_TITLE = re.compile(r"(?i)prof|教授|교수|hoogleraar|catedr[aá]tic|ordinari[oa]|associate dean|dean\b")
# Verification notes that should block a quick approve (the rest are informational).
RISKY_NOTE = re.compile(r"(?i)check|moved|identity|conflict|differs|deceas|possibl|unconfirm|unverif|"
                        r"not confirmed|snippet|fixed-term|rtd|research-track|adjunct|on leave|stale|old|mailto")
PARTICLES = {"van", "von", "de", "del", "della", "der", "den", "da", "di", "du", "le", "la", "al", "el", "dos", "das"}


def clean_name(name: str) -> str:
    """Drop invisible format characters (e.g. U+202C direction marks) that scraped names carry."""
    return "".join(ch for ch in name if unicodedata.category(ch) != "Cf")


def surname(name: str) -> tuple:
    """(surname, unusual?) from a 'Given Family' display name, keeping particles like 'van'."""
    clean = re.sub(r"\s+", " ", clean_name(name).replace("‐", "-")).strip()
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
    return f"Dear Prof. {last}," if PROF_TITLE.search(r.get("title") or "") else f"Dear Dr. {last},"


def research_area(r: dict) -> str:
    methods = set(r.get("ml_methods") or [])
    if "reinforcement learning" in methods:
        method = "reinforcement learning"
    elif methods & {"deep learning", "convolutional networks", "transformers", "graph neural networks", "recurrent networks"}:
        method = "deep learning"
    else:
        method = "machine learning"
    return f"{method} for {AREA.get(r['env_domain'], 'environmental problems')}"


def subject(r: dict) -> str:
    return f"Research internship inquiry: {research_area(r)}"


def body(r: dict) -> str:
    second = DAC if r["env_domain"] in DAC_DOMAINS else CHENNAI
    parts = [salutation(r), "I hope this message finds you well.", INTRO.format(area=research_area(r)),
             f"{WILDLIFE} {second}", ASK, CLOSING]
    return "\n\n".join(parts) + "\n"


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


def batch_from_header(path: Path) -> int:
    """Batch number written in a draft's header; drafts from before batches existed are batch 1."""
    for line in path.read_text().split("\n")[:12]:
        if line.startswith("batch: "):
            return int(line.split(": ", 1)[1])
    return 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100, help="how many NEW drafts to write (one batch)")
    args = ap.parse_args()

    REVIEW.mkdir(exist_ok=True)
    sendable = [r for r in json.loads((DATA / "professors.json").read_text()) if r.get("sendable")]
    batch_of = {p.stem: batch_from_header(p) for p in REVIEW.glob("P*.md")}
    batch = max(batch_of.values(), default=0) + 1
    # Never overwrite an existing draft (it may have hand edits): only new professors get one.
    for r in [r for r in sendable if r["id"] not in batch_of][: args.limit]:
        header = (f"---\nid: {r['id']}\nbatch: {batch:02d}\nstatus: draft\nto: {r['email']}\nname: {clean_name(r['name'])}\n"
                  f"university: {r['university']}\nsubject: {subject(r)}\nattachment: {ATTACHMENT}\n---\n\n")
        (REVIEW / f"{r['id']}.md").write_text(header + body(r))
        batch_of[r["id"]] = batch

    index = []
    for r in (r for r in sendable if r["id"] in batch_of):  # one index across all batches
        path = REVIEW / f"{r['id']}.md"
        index.append({"batch": f"{batch_of[r['id']]:02d}", "id": r["id"], "name": r["name"], "title": r.get("title", ""),
                      "university": r["university"], "country": r["country"], "timezone": r["timezone"],
                      "env_domain": r["env_domain"], "email": r["email"], "email_confidence": r.get("confidence", ""),
                      "subject": subject(r), "file": path.name, "risk_flags": " | ".join(risk_flags(r))})
    with open(REVIEW / "INDEX.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(index[0]))
        w.writeheader()
        w.writerows(index)
    new = [i for i in index if i["batch"] == f"{batch:02d}"]
    print(f"batch {batch:02d}: {len(new)} new drafts (flagged: {sum(1 for i in new if i['risk_flags'])}); "
          f"{len(index)} drafts in review/ in total")


if __name__ == "__main__":
    main()
