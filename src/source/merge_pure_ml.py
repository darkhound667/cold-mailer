# -*- coding: utf-8 -*-
"""Add the pure AI/ML professors to data/professors.json (Ruturva, 2026-10-21).

Inputs: data/pure_ml_candidates.json (pure_ml_pool.py), data/pure_ml_emails.json (emails read
off each professor's own homepage by scrape_homepage_emails.py) and data/verify/result-pm-*.json
(emails the Haiku agents found for the top-ranked professors the script could not read).

Per candidate: take the scraped email, else the agent's. Malformed or junk addresses,
company addresses (a professor's page can list a Meta/Microsoft mailbox) and addresses already
used by another row are dropped. Free-mail is allowed when the professor publishes it
(Phase 1 rule of 2026-10-20). Candidates are then taken best-ranked university first until
--count rows (default 500) are added, as new rows P2002..., marked track="pure_ml" so that
scripts/next_batch_ids.py can alternate them with the environmental professors.

Usage:  .venv/bin/python src/source/merge_pure_ml.py [--count 500]
"""
import argparse
import json
import re
import shutil
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import csr_rank  # noqa: E402
CHINA_CAP = 0.12  # share of the added rows that may be Chinese universities (same cap as pure_ml_pool.py)
EMAIL = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.([a-z]{2,})$")
TLDS = {"edu", "com", "org", "net", "io", "ch", "de", "nl", "uk", "kr", "au", "cn", "ca", "sg", "jp", "dk", "be",
        "nz", "se", "fi", "at", "fr", "it", "es", "ie", "no", "ai", "me", "xyz", "dev"}
COMPANY = re.compile(r"(^|\.)(meta|fb|microsoft|google|deepmind|openai|anthropic|amazon|apple|nvidia|ibm|adobe|"
                     r"krafton|samsung|naver|kakaocorp|tencent|alibaba|baidu|bytedance|huawei|sony|intel|qualcomm)\.")
FREEMAIL = re.compile(r"(^|\.)(gmail|googlemail|outlook|hotmail|yahoo|icloud|me|mac|protonmail|proton|163|126|qq)\.")
ACADEMIC = re.compile(r"\.(edu|ac\.[a-z]{2}|edu\.[a-z]{2})$|\.(ethz|epfl|uzh|unibe|unibas|unige|usi)\.ch$|\.(tum|lmu|kit)\.de$"
                      r"|uni-[a-z-]+\.de$|\.(tudelft|uva|vu|uu|leidenuniv|rug|tue)\.nl$|\.ku\.dk$|\.dtu\.dk$|\.kuleuven\.be$")
NO_TITLE_COUNTRIES = {"GB", "AU", "NZ", "IE"}  # lecturers there are "Dr.", so no assumed professor title


def ascii_low(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def clean_email(email: str) -> str:
    # An agent may list two addresses; one address per row (email_alt is not live), so take the first usable.
    for one in re.split(r"[,;\s]+", (email or "").strip().lower()):
        if _valid(one):
            return one
    return ""


def _valid(email: str) -> bool:
    if not EMAIL.match(email) or email.rsplit(".", 1)[1] not in TLDS or "-dot-" in email or COMPANY.search(email.split("@")[1]):
        return ""
    return email


def matches_name(email: str, name: str) -> bool:
    loc = re.sub(r"[^a-z]", "", email.split("@")[0])
    parts = [p for p in re.split(r"[\s\-]+", ascii_low(name)) if len(p) > 2]
    return any(p in loc for p in parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=500)
    args = ap.parse_args()

    cands = json.loads((DATA / "pure_ml_candidates.json").read_text())
    scraped = json.loads((DATA / "pure_ml_emails.json").read_text())
    agent = {}
    for f in sorted((DATA / "verify").glob("result-pm-*.json")):
        for r in json.loads(f.read_text()):
            agent[r["cid"]] = r
    ranks = csr_rank.rank_institutions()
    by_norm = {}
    for d, r in ranks.items():
        by_norm.setdefault(csr_rank.norm(d), r)

    db = json.loads((DATA / "professors.json").read_text())
    used = {r["email"].lower() for r in db if r.get("email")} | {a.lower() for r in db for a in r.get("email_alt", [])}
    start = max(int(r["id"][1:]) for r in db) + 1
    ready = []
    for c in cands:  # the university's CSRankings rank decides who is taken (and mailed) first
        c["csr_rank"] = csr_rank.match(c["institution"], ranks, by_norm)
    for c in sorted(cands, key=lambda c: (c["csr_rank"], c["univ_rank"], -c["ml_score"])):
        s, a = scraped.get(c["homepage"], {}), agent.get(c.get("cid"), {})
        email, src, how, title = clean_email(s.get("email")), s.get("source_url", ""), "homepage script", s.get("title", "")
        if not email or email in used:
            email, src, how, title = clean_email(a.get("email")), a.get("email_source_url", ""), "agent lookup", a.get("title", "")
        if not email or email in used:
            continue
        used.add(email)
        flags = []
        dom = email.split("@")[1]
        if not ACADEMIC.search(dom) and not FREEMAIL.search(dom):
            flags.append("email domain is not a recognisable university domain: check")
        if not matches_name(email, c["name"]):
            flags.append("email does not contain the name: check")
        if not title:
            title = "" if c["country"] in NO_TITLE_COUNTRIES else "Professor"
            flags.append("title not read from page" + ("" if title else " (assumed professor)") + ": CSRankings faculty list")
        ready.append((c, email, src, how, title, flags))

    pm = [r for r in db if r.get("track") == "pure_ml"]  # the cap counts the Chinese rows already added
    china_left = int(CHINA_CAP * (len(pm) + args.count)) - sum(1 for r in pm if r["country"] == "CN")
    chosen = []
    for item in ready:
        if item[0]["country"] == "CN":
            if china_left <= 0:
                continue
            china_left -= 1
        chosen.append(item)
        if len(chosen) == args.count:
            break
    shutil.copy(DATA / "professors.json", DATA / f"professors.backup-before-pure-ml-{date.today().isoformat()}.json")
    today = date.today().isoformat()
    for i, (c, email, src, how, title, flags) in enumerate(chosen):
        db.append({
            "id": f"P{start + i:04d}", "name": c["name"], "title": title, "department": "",
            "university": c["institution"], "country": c["country"], "timezone": c["timezone"],
            "env_domain": "pure AI/ML", "track": "pure_ml", "ml_area": c["area"], "ml_methods": [],
            "tier": "A", "n_works": round(c["ml_score"]), "n_last_author": 0, "fit_evidence": [],
            "univ_rank": c["univ_rank"], "csr_rank": c["csr_rank"], "scholarid": c["scholarid"],
            "homepage": c["homepage"], "email": email, "source_url": src or c["homepage"], "date_found": today,
            "confidence": "high" if how == "homepage script" else "medium",
            "flags": "; ".join([f"pure AI/ML pool {today}, email via {how}", *flags]),
            "verified": True, "openalex_ids": [], "status": "verified", "sendable": True,
        })
    DATA.joinpath("professors.json").write_text(json.dumps(db, indent=1, ensure_ascii=False))

    print(f"candidates with a usable email: {len(ready)}; added {len(chosen)} as P{start:04d}-P{start + len(chosen) - 1:04d}")
    print("worst CSRankings rank added:", max((x[0]["csr_rank"] for x in chosen), default=None))
    print("by country:", dict(Counter(c["country"] for c, *_ in chosen).most_common()))
    print("via:", dict(Counter(x[3] for x in chosen)), "| flagged:", sum(1 for x in chosen if x[5]))


if __name__ == "__main__":
    main()
