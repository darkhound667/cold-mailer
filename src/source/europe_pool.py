# -*- coding: utf-8 -*-
"""More professors in Europe, found and checked by script only (Ruturva, 2026-10-31: "add more europe
professors ... make sure that i'm not emailing some random professors ... but i don't want to use tokens
to check this, so make the python script properly").

Three sources, none of them guessed:
  A. CSRankings faculty at European institutions (data/cache/csrankings/institutions.csv) who publish
     mainly at the AI / vision / ML / NLP venues (>= MIN_ML adjusted papers since 2019 and >= MIN_SHARE
     of their output) and are not in professors.json yet  ->  track "pure_ml".
  B. OpenAlex ML x environment authors in Europe (data/candidates_universities.json): Tier A, plus Tier B
     with at least one last-author paper, not in professors.json yet  ->  environmental track.
  C. Rows already in professors.json for Europe that were verified as faculty but whose email was never
     found (status "verified", no email): the pages are read again by script.

How a candidate is checked (a row is added only when ALL of these hold):
  1. a page of the person's own (CSRankings homepage, or a homepage listed on their ORCID record) loads and
     prints their surname;
  2. for B and C, faculty status is evidenced: a professor / reader / lecturer title printed next to the
     name on that page, or as a current ORCID employment role (emeritus, honorary, visiting, adjunct,
     postdoc and researcher-only titles do not count); CSRankings people are faculty by definition;
  3. an email is printed on the page (or its contact page), or is public on their ORCID record, and it
     contains their surname / first name (scrape_homepage_emails.pick), is not a generic mailbox or a
     company address, and sits on an academic domain (same site as the page, a domain already used by a
     verified row, or an edu / ac.xx / uni-* style domain);
  4. the address is not used by any other row.
Nothing else is written: no guessed addresses, no subagents, no paid calls.

Usage (resumable; everything fetched is cached in data/cache/europe/):
    .venv/bin/python src/source/europe_pool.py [--limit N] [--workers 16] [--no-merge]
Writes data/europe_found.json (every candidate with what was read) and, unless --no-merge, adds the
accepted people to data/professors.json (backup first), which `scripts/next_batch_ids.py` then mails first.
"""
import argparse
import csv
import html as htmllib
import json
import re
import shutil
import sys
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
CACHE = DATA / "cache" / "europe"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import csr_rank  # noqa: E402
from pure_ml_pool import AREA_OF, ML_VENUES, SINCE  # noqa: E402
from scrape_homepage_emails import EMAIL, contact_links, emails_in, pick  # noqa: E402
from select_pool import name_key  # noqa: E402
from timezones import COUNTRY_TZ, EUROPE  # noqa: E402

MIN_ML, MIN_SHARE = 1.5, 0.4
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

# Faculty titles as printed in the languages of the countries in scope.
PROF_RX = re.compile(
    r"(?:(?:Assistant|Associate|Full|Senior|Distinguished|Junior|Tenured|Tenure[- ]Track|Chaired?)\s+)?"
    r"(?:Professor|Reader|Lecturer)(?:\s+(?:in|of|for)\s+[A-Z][\w&,\- ]{2,50})?"
    r"|Univ\.?-?\s?Prof\.?(?:\s*Dr\.?)?|Prof\.(?:\s*Dr\.?)?|Juniorprofessor(?:in)?|Universit[aä]tsprofessor(?:in)?"
    r"|Professor(?:in|a|e|essa)?\b|Professeur(?:e)?|Professore|Hoogleraar|Universitair (?:Hoofd)?docent"
    r"|Catedr[aá]tic[oa]|Profesor(?:a)?|Ma[iî]tre de conf[ée]rences|Dozent(?:in)?|Docent|Ricercatore|Lektor",
    re.I)
NOT_FACULTY = re.compile(r"emerit|retired|honorary|honorar|visiting|adjunct|former|post-?doc|research (?:associate|fellow|assistant)|"
                         r"doctoral (?:candidate|student)|ph\.?d\.? (?:student|candidate)|assistant to|secretar", re.I)
# Hosts that are not a person's own page: social networks, aggregators, publisher profiles.
SKIP_HOSTS = re.compile(r"(linkedin|twitter|x\.com|facebook|instagram|researchgate|scholar\.google|orcid\.org|mastodon|"
                        r"semanticscholar|dblp|wikipedia|youtube|academia\.edu|publons|scopus|webofscience|bsky|"
                        r"github\.com|gitlab|medium\.com|wikidata|loop\.frontiersin|ssrn|zotero|osf\.io)", re.I)
COMPANY = re.compile(r"(^|\.)(meta|fb|microsoft|google|deepmind|openai|anthropic|amazon|apple|nvidia|ibm|adobe|samsung|"
                     r"huawei|sony|intel|qualcomm|bosch|siemens|zalando|spotify)\.")
FREEMAIL = re.compile(r"(^|\.)(gmail|googlemail|outlook|hotmail|yahoo|icloud|me|mac|protonmail|proton|gmx|web|live|aol)\.")
ACAD_DOMAIN = re.compile(r"\.(edu|ac\.[a-z]{2}|edu\.[a-z]{2})$|(^|\.)(uni|univ|tu|hu|fu|rwth|kit|ethz|epfl|kth|ntnu|dtu|inria|"
                         r"cnrs|mpg|mpi|fraunhofer|csic|cwi|helmholtz|kuleuven|tudelft|tue|uva|vu|uu|rug|ku|au|aau|abo|"
                         r"unibe|unibas|unige|uzh|usi|polimi|polito|unimi|unipd|unibo|uniroma|sns|ist|upc|ugr|ucm|uam)"
                         r"[a-z0-9-]*\.", re.I)
TITLE_WINDOW = 600  # characters after the surname searched for a title


def ascii_low(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def surname_token(name: str) -> str:
    parts = [p for p in re.split(r"[\s\-]+", ascii_low(re.sub(r"\s+\d{4}$", "", name))) if len(p) > 1 and not p.endswith(".")]
    return parts[-1] if parts else ""


def registered(host: str) -> str:
    p = host.lower().strip(".").split(".")
    return ".".join(p[-3:] if len(p) >= 3 and p[-2] in {"ac", "edu", "co", "com", "org", "gov"} else p[-2:])


def fetch(url: str):
    """(html, final_url) or (None, url); normal TLS verification, https then the other scheme."""
    alt = ("http" if url.startswith("https") else "https") + url.split(":", 1)[1]
    for u in (url, alt):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept-Language": "en"})
            with urllib.request.urlopen(req, timeout=15) as r:
                if "html" not in (r.headers.get("Content-Type") or "html"):
                    return None, u
                raw = r.read(600_000)
                return raw.decode(r.headers.get_content_charset() or "utf-8", errors="replace"), r.geturl()
        except Exception:
            continue
    return None, url


def title_near(plain: str, surname: str):
    """First faculty title printed within a short window around the person's surname, or None."""
    low = ascii_low(plain)
    start = 0
    for _ in range(4):  # the first few mentions of the surname
        i = low.find(surname, start)
        if i < 0:
            return None
        window = plain[max(0, i - 160): i + TITLE_WINDOW]
        for m in PROF_RX.finditer(window):
            ctx = window[max(0, m.start() - 40): m.end() + 40]
            if not NOT_FACULTY.search(ctx):
                return re.sub(r"\s+", " ", m.group(0)).strip()[:60]
        start = i + len(surname)
    return None


def read_page(url: str, name: str) -> dict:
    page, final = fetch(url)
    if page is None:
        return {"ok": False}
    plain = htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", page))))
    sur = surname_token(name)
    out = {"ok": True, "final": final, "name_on_page": bool(sur) and sur in ascii_low(plain),
           "title": title_near(plain, sur) if sur else None, "email": "", "email_url": ""}
    host = urllib.parse.urlparse(final).netloc.lower()
    out["email"] = pick(emails_in(page), name, host) or ""
    out["email_url"] = final if out["email"] else ""
    if not out["email"] and out["name_on_page"]:
        for link in contact_links(page, final):
            sub, sub_final = fetch(link)
            if sub and (e := pick(emails_in(sub), name, host)):
                out["email"], out["email_url"] = e, sub_final
                break
    return out


def get_json(url: str, headers=None, tries: int = 3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception:
            if attempt == tries - 1:
                return None


# ---------------------------------------------------------------- ORCID
def orcid_info(orcid: str, cache: dict) -> dict:
    if orcid in cache:
        return cache[orcid]
    rec = get_json(f"https://pub.orcid.org/v3.0/{orcid}/record", {"Accept": "application/json"})
    info = {"urls": [], "emails": [], "roles": [], "ok": rec is not None}
    if rec:
        person = rec.get("person") or {}
        info["urls"] = [u["url"]["value"] for u in (person.get("researcher-urls") or {}).get("researcher-url", []) if u.get("url")]
        info["emails"] = [e["email"].lower() for e in (person.get("emails") or {}).get("email", [])]
        groups = ((rec.get("activities-summary") or {}).get("employments") or {}).get("affiliation-group", [])
        for g in groups:
            for s in g.get("summaries", []):
                e = s.get("employment-summary") or {}
                if not e.get("end-date"):
                    info["roles"].append({"role": e.get("role-title") or "", "org": (e.get("organization") or {}).get("name", "")})
    cache[orcid] = info
    return info


GENERIC_ORG = {"university", "universitat", "universiteit", "universite", "universita", "universidad", "college", "institute",
               "school", "department", "faculty", "technical", "technology", "national"}


def org_words(s: str) -> set:
    return {w for w in re.split(r"[^a-z]+", ascii_low(s)) if len(w) >= 4 and w not in GENERIC_ORG}


def role_title(info: dict, institution: str):
    """A current ORCID employment role with a faculty title at (a name-alike of) the institution."""
    want = org_words(institution)
    for r in info.get("roles", []):
        if r["role"] and PROF_RX.search(r["role"]) and not NOT_FACULTY.search(r["role"]) and (want & org_words(r["org"])):
            return re.sub(r"\s+", " ", r["role"]).strip()[:60]
    return None


def openalex_orcids(ids: list, cache: dict) -> None:
    todo = [i for i in ids if i not in cache]
    for k in range(0, len(todo), 50):
        chunk = todo[k:k + 50]
        q = urllib.parse.urlencode({"filter": "openalex_id:" + "|".join(i.rsplit("/", 1)[-1] for i in chunk),
                                    "per_page": 50, "select": "id,orcid"})
        data = get_json("https://api.openalex.org/authors?" + q)
        got = {a["id"]: (a.get("orcid") or "").rsplit("/", 1)[-1] for a in (data or {}).get("results", [])}
        for i in chunk:
            if data is not None:  # a failed request is left uncached and tried again next run
                cache[i] = got.get(i, "")
        print(f"  openalex orcids {min(k + 50, len(todo))}/{len(todo)}", flush=True)


# ---------------------------------------------------------------- candidates
def candidates_csrankings(have: set, insts: dict) -> list:
    src = DATA / "cache" / "csrankings"
    ml, tot, venues = defaultdict(float), defaultdict(float), defaultdict(Counter)
    eu = set(insts)
    for r in csv.DictReader(open(src / "generated-author-info.csv", encoding="utf-8")):
        if r["dept"] not in eu or int(r["year"]) < SINCE:
            continue
        k, a = (r["name"], r["dept"]), float(r["adjustedcount"])
        tot[k] += a
        if r["area"] in ML_VENUES:
            ml[k] += a
            venues[k][r["area"]] += a
    orcids = {r["name"]: r["orcid"] for r in csv.DictReader(open(src / "orcid.csv", encoding="utf-8"))
              if r["orcid"] != "0000-0000-0000-0000"}
    out = []
    for r in csv.DictReader(open(src / "csrankings.csv", encoding="utf-8")):
        k = (r["name"], r["affiliation"])
        if r["affiliation"] not in eu or ml[k] < MIN_ML or ml[k] / tot[k] < MIN_SHARE or not r["homepage"].startswith("http"):
            continue
        name = re.sub(r"\s+\d{4}$", "", r["name"])
        if name_key(name) in have:
            continue
        out.append({"src": "csrankings", "key": f"csr:{r['name']}|{r['affiliation']}", "name": name,
                    "institution": r["affiliation"], "country": insts[r["affiliation"]], "homepage": r["homepage"],
                    "orcid": orcids.get(r["name"], ""), "scholarid": r["scholarid"], "ml_score": round(ml[k], 1),
                    "area": AREA_OF[venues[k].most_common(1)[0][0]], "need_title": False})
    return out


def candidates_openalex(have: set) -> list:
    out = []
    for c in json.loads((DATA / "candidates_universities.json").read_text()):
        if c["country"] not in EUROPE or name_key(c["name"]) in have:
            continue
        if c["tier"] != "A" and not (c["tier"] == "B" and c["n_last_author"] >= 1):
            continue
        out.append({"src": "openalex", "key": "oa:" + c["openalex_id"], "name": c["name"], "institution": c["institution"],
                    "country": c["country"], "homepage": "", "orcid": None, "oa_id": c["openalex_id"], "tier": c["tier"],
                    "env_domain": c["env_domain"], "ml_methods": c["ml_methods"], "n_works": c["n_works"],
                    "n_last_author": c["n_last_author"], "timezone": c.get("timezone") or COUNTRY_TZ.get(c["country"]),
                    "fit_evidence": [{k: e[k] for k in ("title", "year", "url")} for e in c["fit_evidence"]],
                    "need_title": True})
    return out


def candidates_existing(db: list) -> list:
    out = []
    for r in db:
        if r["country"] in EUROPE and r.get("status") == "verified" and not r.get("email") and not r.get("sendable") \
                and (r.get("homepage") or r.get("openalex_ids")):
            out.append({"src": "existing", "key": "db:" + r["id"], "id": r["id"], "name": r["name"],
                        "institution": r["university"], "country": r["country"], "homepage": r.get("homepage", ""),
                        "orcid": None, "oa_id": (r.get("openalex_ids") or [""])[0], "need_title": False})
    return out


# ---------------------------------------------------------------- one candidate
def acceptable(email: str, page_url: str, known: set, used: set, from_page: bool) -> bool:
    if not EMAIL.fullmatch(email) or email in used:
        return False
    dom = email.split("@")[1]
    if COMPANY.search(dom) or (not from_page and FREEMAIL.search(dom)):
        return False
    if FREEMAIL.search(dom):  # printed on their own page: allowed (Phase 1 rule of 2026-10-20)
        return True
    host = urllib.parse.urlparse(page_url).netloc.lower() if page_url else ""
    return bool(registered(dom) in known or (host and registered(host) == registered(dom)) or ACAD_DOMAIN.search(dom))


def process(c: dict, orcid_cache: dict, known_domains: set, used: set) -> dict:
    res = {"email": "", "source_url": "", "title": "", "status": "", "how": ""}
    info = orcid_info(c["orcid"], orcid_cache) if c.get("orcid") else {"urls": [], "emails": [], "roles": [], "ok": False}
    role = role_title(info, c["institution"])
    urls = []
    for u in [c["homepage"], *info["urls"]]:
        if u and u.startswith("http") and not SKIP_HOSTS.search(urllib.parse.urlparse(u).netloc) and u not in urls:
            urls.append(u)
    title, email, src, reached, named = role, "", "", info["ok"], False
    for u in urls[:3]:
        p = read_page(u, c["name"])
        reached |= p["ok"]
        if not (p["ok"] and p["name_on_page"]):
            continue
        named = True
        title = title or p["title"]
        if p["email"] and not email and acceptable(p["email"], p["email_url"], known_domains, used, from_page=True):
            email, src, res["how"] = p["email"], p["email_url"], "own page"
        if email and title:
            break
    if not email:  # public address on the ORCID record
        e = pick([x for x in info["emails"] if not FREEMAIL.search(x.split("@")[1])], c["name"], "")
        if e and acceptable(e, "", known_domains, used, from_page=False):
            email, src, res["how"] = e, f"https://orcid.org/{c['orcid']}", "ORCID record"
    if c["need_title"] and not title:
        res["status"] = "no faculty title" if (named or info["roles"]) else ("no usable page" if reached else "pages did not load")
        return res
    if not email:
        res["status"] = "no email" if reached else "pages did not load"
        return res
    res.update(email=email, source_url=src, title=title or "", status="ok")
    return res


# ---------------------------------------------------------------- merge
def merge(cands: list, found: dict, db: list) -> None:
    ranks = csr_rank.rank_institutions()
    by_norm = {}
    for d, r in ranks.items():
        by_norm.setdefault(csr_rank.norm(d), r)
    today = date.today().isoformat()
    start = max(int(r["id"][1:]) for r in db) + 1
    by_id = {r["id"]: r for r in db}
    used = {r["email"].lower() for r in db if r.get("email")}
    added, updated = [], 0
    ok = [(c, found[c["key"]]) for c in cands if found.get(c["key"], {}).get("status") == "ok"]
    ok.sort(key=lambda t: (csr_rank.match(t[0]["institution"], ranks, by_norm), -t[0].get("ml_score", t[0].get("n_works", 0))))
    for c, f in ok:
        if f["email"] in used:
            continue
        used.add(f["email"])
        flags = f"Europe pool {today}: email on {f['how']}" + ("" if f["title"] else "; title not read from page")
        if c["src"] == "existing":
            r = by_id[c["id"]]
            r.update(email=f["email"], source_url=f["source_url"], title=r.get("title") or f["title"], sendable=True,
                     confidence="medium", date_found=today, flags=(r.get("flags", "") + "; " + flags).strip("; "))
            updated += 1
            continue
        row = {"id": f"P{start + len(added):04d}", "name": c["name"], "title": f["title"], "department": "",
               "university": c["institution"], "country": c["country"],
               "timezone": c.get("timezone") or COUNTRY_TZ.get(c["country"]), "homepage": c["homepage"] or f["source_url"],
               "email": f["email"], "source_url": f["source_url"], "date_found": today,
               "confidence": "high" if f["how"] == "own page" else "medium", "flags": flags, "verified": True,
               "status": "verified", "sendable": True,
               "csr_rank": csr_rank.match(c["institution"], ranks, by_norm)}
        if c["src"] == "csrankings":
            row.update(env_domain="pure AI/ML", track="pure_ml", ml_area=c["area"], ml_methods=[], tier="A",
                       n_works=round(c["ml_score"]), n_last_author=0, fit_evidence=[], univ_rank=1000,
                       scholarid=c.get("scholarid", ""), openalex_ids=[])
        else:
            row.update(env_domain=c["env_domain"], ml_methods=c["ml_methods"], tier=c["tier"], n_works=c["n_works"],
                       n_last_author=c["n_last_author"], fit_evidence=c["fit_evidence"], openalex_ids=[c["oa_id"]])
        added.append(row)
    shutil.copy(DATA / "professors.json", DATA / f"professors.backup-before-europe-{today}.json")
    db.extend(added)
    (DATA / "professors.json").write_text(json.dumps(db, indent=1, ensure_ascii=False))
    print(f"added {len(added)} new rows (P{start:04d}-P{start + len(added) - 1:04d}), recovered {updated} existing rows")
    print("new rows by country:", dict(Counter(r["country"] for r in added).most_common()))
    print("new rows by track:", dict(Counter("pure_ml" if r.get("track") == "pure_ml" else "environmental" for r in added)))


# ---------------------------------------------------------------- audit
def institution_in_text(inst: str, plain: str) -> bool:
    low = ascii_low(plain)
    return bool(any(w in low for w in org_words(inst)) or ascii_low(inst) in low)


EUROPE_TLDS = {c.lower() for c in EUROPE} | {"uk", "eu", "com", "org", "net", "int"}
AUDIT_MARK = re.compile(r";?\s*Europe audit:[^;]*")


def audit(db: list) -> None:
    """Second check on every row this script added: the email must belong to the institution the row names.
    A row passes when (a) the email's domain is already used by a verified row of that institution, or (b) the
    domain's name shares a word with the institution, or (c) for an ORCID address, a current ORCID employment
    is at the institution, or (d) the email sits on the same site as the page it was printed on and that page
    names the institution in its first 2,500 characters (its header). Anything else stays in the database but is
    marked not sendable with a note: a professor whose email is at another institution has probably moved
    or holds two posts, so their country and send time would be wrong. Safe to re-run."""
    orcid_cache = json.loads((CACHE / "orcid.json").read_text()) if (CACHE / "orcid.json").exists() else {}
    dom_inst = defaultdict(set)
    for r in db:
        if r.get("email") and r.get("sendable") and "Europe pool" not in r.get("flags", ""):
            dom_inst[registered(r["email"].split("@")[1])].add(r["university"])
    rows = [r for r in db if "Europe pool" in r.get("flags", "")]
    for r in rows:  # forget an earlier verdict
        if "held back" in r["flags"]:
            r["sendable"] = True
        r["flags"] = AUDIT_MARK.sub("", r["flags"])

    def check(r):
        dom = registered(r["email"].split("@")[1])
        if r["university"] in dom_inst.get(dom, ()):
            return True
        stem = re.sub(r"[^a-z]", "", dom.split(".")[0])
        tokens = [t for t in re.split(r"[^a-z]+", ascii_low(r["university"])) if t]
        initials = "".join(t[0] for t in tokens if t not in {"of", "the", "and", "de", "di", "du", "der", "van"})
        if any(len(w) >= 4 and (w[:4] in stem or stem[:4] in w) for w in org_words(r["university"])) \
                or (len(stem) >= 3 and (stem == initials or any(t == stem or t.startswith(stem) for t in tokens if len(t) >= 3))):
            return True
        if dom.split(".")[-1] not in EUROPE_TLDS:  # another country's or a US .edu address: held back
            return False
        if r["source_url"].startswith("https://orcid.org/"):
            info = orcid_cache.get(r["source_url"].rsplit("/", 1)[-1], {})
            want = org_words(r["university"])
            return any(want & org_words(x["org"]) for x in info.get("roles", []))
        page, final = fetch(r["source_url"])
        if page is None or registered(urllib.parse.urlparse(final).netloc) != dom:
            return False
        plain = htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", page))))
        return institution_in_text(r["university"], plain[:2500])

    with ThreadPoolExecutor(16) as ex:
        verdicts = list(ex.map(check, rows))
    bad = 0
    for r, ok in zip(rows, verdicts):
        r["flags"] += "; Europe audit: ok" if ok else "; Europe audit: email is not at the listed institution: check (held back)"
        if not ok:
            r["sendable"] = False
            bad += 1
    shutil.copy(DATA / "professors.json", DATA / f"professors.backup-before-europe-audit-{date.today().isoformat()}.json")
    (DATA / "professors.json").write_text(json.dumps(db, indent=1, ensure_ascii=False))
    print(f"audited {len(rows)} rows: {len(rows) - bad} pass, {bad} held back (sendable=False)")
    print("held back by country:", dict(Counter(r["country"] for r, ok in zip(rows, verdicts) if not ok).most_common()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only the first N candidates (testing)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--no-merge", action="store_true")
    ap.add_argument("--audit-only", action="store_true", help="only run the institution audit on rows already added")
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    if args.audit_only:
        audit(json.loads((DATA / "professors.json").read_text()))
        return

    db = json.loads((DATA / "professors.json").read_text())
    have = {name_key(r["name"]) for r in db if r.get("sendable") or r.get("status") in ("not_faculty", "retired", "moved")}
    inst_rows = list(csv.DictReader(open(DATA / "cache" / "csrankings" / "institutions.csv", encoding="utf-8")))
    insts = {r["institution"]: r["countryabbrv"].upper() for r in inst_rows if r["countryabbrv"].upper() in EUROPE}
    # Europeans already in the DB without a usable email are candidates C (read again), not skipped as "have".
    cands = candidates_existing(db) + candidates_csrankings(have, insts) + candidates_openalex({name_key(r["name"]) for r in db})
    seen, uniq = set(), []
    for c in cands:  # one entry per person
        nk = (name_key(c["name"]), c["country"])
        if nk not in seen:
            seen.add(nk)
            uniq.append(c)
    cands = uniq[: args.limit] if args.limit else uniq
    print(f"candidates: {len(cands)}", dict(Counter(c["src"] for c in cands)), flush=True)

    oa_cache_file = CACHE / "openalex_orcid.json"
    oa_cache = json.loads(oa_cache_file.read_text()) if oa_cache_file.exists() else {}
    openalex_orcids([c["oa_id"] for c in cands if c.get("oa_id")], oa_cache)
    oa_cache_file.write_text(json.dumps(oa_cache))
    for c in cands:
        if c.get("oa_id"):
            c["orcid"] = oa_cache.get(c["oa_id"]) or ""
    print("with an ORCID:", sum(bool(c.get("orcid")) for c in cands), flush=True)

    orcid_file = CACHE / "orcid.json"
    orcid_cache = json.loads(orcid_file.read_text()) if orcid_file.exists() else {}
    known = {registered(r["email"].split("@")[1]) for r in db if r.get("email") and r.get("sendable")}
    used = {r["email"].lower() for r in db if r.get("email")}
    found_file = CACHE / "results.json"
    done = json.loads(found_file.read_text()) if found_file.exists() else {}
    todo = [c for c in cands if c["key"] not in done]
    print(f"to read: {len(todo)} (cached {len(cands) - len(todo)})", flush=True)

    def work(c):
        try:
            return process(c, orcid_cache, known, used)
        except Exception as e:  # one broken page must not stop the run
            return {"email": "", "source_url": "", "title": "", "status": f"error {type(e).__name__}", "how": ""}

    with ThreadPoolExecutor(args.workers) as ex:
        for i, (c, r) in enumerate(zip(todo, ex.map(work, todo)), 1):
            done[c["key"]] = r
            if i % 100 == 0:
                found_file.write_text(json.dumps(done, ensure_ascii=False))
                orcid_file.write_text(json.dumps(orcid_cache, ensure_ascii=False))
                print(f"{i}/{len(todo)} read; accepted so far {sum(1 for v in done.values() if v['status'] == 'ok')}", flush=True)
    found_file.write_text(json.dumps(done, ensure_ascii=False))
    orcid_file.write_text(json.dumps(orcid_cache, ensure_ascii=False))
    (DATA / "europe_found.json").write_text(json.dumps(
        [{**c, **done[c["key"]]} for c in cands if c["key"] in done], indent=1, ensure_ascii=False))

    print("outcome by source/status:")
    for (src, st), n in sorted(Counter((c["src"], done[c["key"]]["status"]) for c in cands if c["key"] in done).items()):
        print(f"  {src:<10} {st:<22} {n}")
    if not args.no_merge:
        merge(cands, done, db)
        audit(json.loads((DATA / "professors.json").read_text()))


if __name__ == "__main__":
    main()
