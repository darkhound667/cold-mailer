# -*- coding: utf-8 -*-
"""Phase 1, step 3: fold official-page verification results into data/professors.json.

Reads data/verify/result-*.json (written by the verification agents, one object per
professor: id, name, status, current_university, title, department, homepage, email,
email_source_url, confidence, notes) and updates the matching rows.

Sanity checks, because an email must come from an official page (hard rule 1):
  - malformed addresses and free-mail domains (gmail, qq, 163, ...) are dropped and flagged;
  - an email whose domain shares nothing with its source page's domain is kept but flagged
    for a manual look (e.g. a department page on one domain listing a university address);
  - "moved" rows are flagged: their timezone was computed for the old institution.

Only status "verified" (or "moved" with an email) counts as sendable.

Usage:
    python src/source/merge_verified.py
"""
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
FREEMAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com",
            "qq.com", "163.com", "126.com", "foxmail.com", "icloud.com", "protonmail.com"}


def registered_domain(host: str) -> str:
    """Rough 'last two labels' (three for e.g. ac.uk, edu.cn, ac.jp) to compare domains."""
    parts = host.lower().strip(".").split(".")
    keep = 3 if len(parts) >= 3 and parts[-2] in {"ac", "edu", "co", "com", "org", "gov"} else 2
    return ".".join(parts[-keep:])


def main() -> None:
    rows = json.loads((DATA / "professors.json").read_text())
    by_id = {r["id"]: r for r in rows}
    results = []
    for f in sorted((DATA / "verify").glob("result-*.json")):
        results += json.loads(f.read_text())

    today = date.today().isoformat()
    issues = Counter()
    for res in results:
        r = by_id.get(res.get("id"))
        if r is None or r["verified"]:  # unknown id, or hand-verified sample row: leave alone
            continue
        flags = [res.get("notes", "").strip()] if res.get("notes") else []
        email = (res.get("email") or "").strip()
        if email and (not EMAIL.match(email) or email.split("@")[1].lower() in FREEMAIL):
            flags.append(f"dropped non-official/malformed email {email!r}")
            issues["dropped email"] += 1
            email = ""
        src = res.get("email_source_url") or ""
        if email and src:
            if registered_domain(email.split("@")[1]) != registered_domain(urlparse(src).hostname or ""):
                flags.append("email domain differs from source page domain: check")
                issues["domain mismatch"] += 1
        status = res.get("status", "not_found")
        if status == "moved":
            flags.append(f"moved to {res.get('current_university') or '?'}: timezone/country need recheck")
        r.update({
            "status": status,
            "title": res.get("title", ""), "department": res.get("department", ""),
            "homepage": res.get("homepage", ""), "email": email,
            "source_url": src if email else "", "date_found": today if email else "",
            "confidence": res.get("confidence", "") if email else "",
            "flags": "; ".join(f for f in flags if f), "verified": True,
        })
        if status == "moved":
            r["current_university"] = res.get("current_university", "")
    for r in rows:  # the 20-row sample was verified by hand earlier
        r.setdefault("status", "verified" if r["verified"] else "pending")
        r["sendable"] = bool(r["email"]) and r["status"] in ("verified", "moved")

    (DATA / "professors.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    print(f"results merged: {len(results)}  rows: {len(rows)}  still pending: "
          f"{sum(1 for r in rows if r['status'] == 'pending')}")
    print("status:", dict(Counter(r["status"] for r in rows).most_common()))
    print("sendable (official email + current faculty):", sum(r["sendable"] for r in rows),
          "| confidence:", dict(Counter(r["confidence"] for r in rows if r["sendable"])))
    print("checks:", dict(issues))
    print("sendable by country:", dict(Counter(r["country"] for r in rows if r["sendable"]).most_common()))


if __name__ == "__main__":
    main()
