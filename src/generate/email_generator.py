# -*- coding: utf-8 -*-
"""Generate a personalized email + subject line for each scraped person, using the
background/projects/domain config in config.yaml.

Usage:
    python email_generator.py --input professors.json --config config.yaml --output emails.json

Input schema (professors.json): a JSON list of
    {"name": str, "email": str | null, "title": str, "bio": str}

Output schema (emails.json): the same records plus
    {"domain_bucket": str, "topic": str, "subject": str, "email_draft": str}

NEVER fabricate or pattern-guess an email address upstream of this script (e.g.
firstname.lastname@domain). If a scraper couldn't find a real published address, leave
`email: null` — a blank field is honest, a guessed one bounces and looks careless.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from classify.domain_classifier import classify, extract_topic

TITLE_PREFIXES = ("Sir", "Dame", "Dr.", "Dr", "Lord", "Prince")


def salutation(name: str) -> str:
    """Strip any parenthetical disambiguator (e.g. two people sharing a name, tagged
    "Jane Doe (Dept A)" vs "Jane Doe (Dept B)" upstream) before it leaks into the
    greeting — keep it in the spreadsheet's Name column, not in "Dear Prof. X (Dept A),"."""
    clean = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    parts = clean.split(" ", 1)
    if len(parts) == 2 and parts[0] in TITLE_PREFIXES:
        return f"Dear {clean},"
    return f"Dear Prof. {clean},"


def build_background(domain_name: str, domain_cfg: dict, cfg: dict) -> str:
    projects = {p["id"]: p["text"].strip() for p in cfg["flagship_projects"]}
    order = domain_cfg.get("lead_order") or list(projects.keys())
    lead_text = projects[order[0]]
    follow_text = projects[order[1]] if len(order) > 1 else ""

    parts = [lead_text]
    if follow_text:
        parts.append(f"Separately, {follow_text[0].lower()}{follow_text[1:]}")

    sp_id = domain_cfg.get("secondary_project")
    if sp_id:
        sp_text = cfg["secondary_projects"].get(sp_id, "").strip()
        if sp_text:
            parts.append(sp_text)

    return " ".join(parts)


def build_subject(domain_name: str, domain_cfg: dict, topic: str) -> str:
    if domain_cfg.get("weak_fit"):
        return "Interest in collaborating with your research group"
    return f"Interest in your research on {topic}"


def build_email(name: str, domain_name: str, domain_cfg: dict, topic: str, cfg: dict) -> str:
    s = cfg["sender"]
    opening = cfg["templates"]["opening"].format(
        name=s["name"], degree=s["degree"], institution=s["institution"],
        headline_metric=s.get("headline_metric", ""),
    ).strip()

    if domain_cfg.get("weak_fit"):
        interest_line = (
            f"I am writing to introduce myself and express interest in the research your "
            f"group conducts in {topic}, and to explore whether there might be an "
            f"opportunity for me to contribute."
        )
    else:
        interest_line = (
            f"I am writing to express my interest in working with you on research in "
            f"{topic}, an area I would like to go considerably deeper into."
        )

    background = build_background(domain_name, domain_cfg, cfg)
    closing = cfg["templates"]["closing"].strip()

    body = (
        f"{salutation(name)}\n\n"
        f"I hope this message finds you well.\n\n"
        f"{opening} {interest_line}\n\n"
        f"{background}\n\n"
        f"{closing}\n\n"
        f"Warm regards,\n{s['name']}\n{s['degree']}\n{s['institution']}"
    )
    # never let an em/en dash slip into generated text (a small tell of AI-written mail)
    return body.replace("—", ", ").replace("–", ", ")


def process(people: list, cfg: dict) -> list:
    domains = cfg["domains"]
    results = []
    for p in people:
        bio = p.get("bio") or ""
        domain_name = classify(bio, domains, default=next(iter(domains))) if bio else next(iter(domains))
        domain_cfg = domains[domain_name]
        fallback = f"{domain_name.lower()} research"
        topic = fallback if domain_cfg.get("weak_fit") else extract_topic(bio, fallback)
        results.append({
            **p,
            "domain_bucket": domain_name,
            "topic": topic,
            "subject": build_subject(domain_name, domain_cfg, topic),
            "email_draft": build_email(p["name"], domain_name, domain_cfg, topic, cfg),
        })
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, help="scraped professors JSON")
    ap.add_argument("--config", required=True, help="config.yaml")
    ap.add_argument("--output", required=True, help="output emails JSON")
    args = ap.parse_args()

    people = json.loads(Path(args.input).read_text(encoding="utf-8"))
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    results = process(people, cfg)
    Path(args.output).write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")

    with_email = sum(1 for r in results if r.get("email"))
    print(f"Processed {len(results)} people ({with_email} with a verified email).")


if __name__ == "__main__":
    main()
