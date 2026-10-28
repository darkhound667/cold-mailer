# -*- coding: utf-8 -*-
"""Read each pure-ML professor's own homepage and take the email printed on it (Ruturva,
2026-10-21: "just try to get their mails from their LinkedIn/college website/own websites").

No searching, no guessing: only an address that is printed on the professor's own page (or
its contact page one click away) counts. Handles mailto: links, "[at]"/"(at)"/" at " and
"[dot]" spellings, HTML entities and Cloudflare-protected addresses. An address must look
like the professor's (surname or first name in the local part) and not be a generic
mailbox, so a department's webmaster@ never becomes the recipient.

Usage:  .venv/bin/python src/source/scrape_homepage_emails.py [--workers 24]
Reads data/pure_ml_candidates.json, writes data/pure_ml_emails.json (resumable, keyed by homepage).
"""
import argparse
import html
import json
import re
import ssl
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = DATA / "pure_ml_emails.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
GENERIC = re.compile(r"^(info|webmaster|admin|support|contact|noreply|no-reply|office|secretary|dept|"
                     r"department|help|hello|mail|postmaster|enquir|inquir|press|media|hr|jobs|careers|"
                     r"recruit|admissions|grad|phd|students|registrar|web|feedback|sales|team|lab)\b", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
# "name [at] cs [dot] uni [dot] edu", "name (at) uni.edu", "name AT uni DOT edu"
AT_DOT = re.compile(r"([A-Za-z0-9._\-]+)\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+|\s*&#64;\s*)"
                    r"\s*([A-Za-z0-9\-]+(?:\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+|\.)\s*[A-Za-z0-9\-]+)+)", re.I)
TITLE = re.compile(r"\b((?:Assistant|Associate|Full|Distinguished|Emeritus|Visiting|Adjunct)\s+Professor|"
                   r"Professor(?:\s+of\s+[A-Z][\w &]+)?|Reader|Senior Lecturer|Lecturer)\b")
CTX = ssl.create_default_context()
CTX.check_hostname, CTX.verify_mode = False, ssl.CERT_NONE


def fetch(url: str):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
            raw = r.read(600_000)
            enc = r.headers.get_content_charset() or "utf-8"
            return raw.decode(enc, errors="replace"), r.geturl()
    except Exception:
        return None, url


def cf_decode(hexstr: str) -> str:
    key = int(hexstr[:2], 16)
    return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))


def ascii_low(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def emails_in(page: str) -> list:
    found = []
    for m in re.finditer(r'data-cfemail="([0-9a-f]+)"', page):
        found.append(cf_decode(m.group(1)))
    text = html.unescape(urllib.parse.unquote(page))
    found += [m.group(0) for m in EMAIL.finditer(text)]
    plain = re.sub(r"<[^>]+>", " ", text)  # tag-free text for the spelled-out forms
    for m in AT_DOT.finditer(plain):
        dom = re.sub(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+)\s*", ".", m.group(2), flags=re.I)
        found.append(m.group(1) + "@" + re.sub(r"\s+", "", dom))
    clean = []
    for e in found:
        e = e.strip(".,;:()<>[]\"'").lower()
        if EMAIL.fullmatch(e) and not e.endswith((".png", ".jpg", ".gif", ".svg", ".css", ".js")) and e not in clean:
            clean.append(e)
    return clean


def pick(cands: list, name: str, home_host: str):
    """The address that looks like this professor's: surname/first name in the local part."""
    parts = [p for p in re.split(r"[\s\-]+", ascii_low(name)) if len(p) > 1]
    if not parts:
        return None
    first, last = parts[0], parts[-1]
    best = None
    for e in cands:
        local, dom = e.split("@")
        if GENERIC.match(local) or dom.endswith(("example.com", "sentry.io", "wixpress.com")):
            continue
        loc = re.sub(r"[^a-z]", "", local)
        score = 0
        if len(last) >= 3 and last in loc:
            score += 3
        if len(first) >= 3 and first in loc:
            score += 2
        if len(parts) >= 2 and loc[:1] == first[:1] and len(last) >= 3 and last[:4] in loc:
            score += 2
        if loc and loc in (first[:1] + last, first + last[:1]):
            score += 2
        if score and (dom.split(".")[-1] in home_host or home_host.endswith(dom) or dom in home_host
                      or re.search(r"\.(edu|ac\.[a-z]{2}|edu\.[a-z]{2})$", dom)):
            score += 1
        if score >= 3 and (best is None or score > best[0]):
            best = (score, e)
    return best[1] if best else None


def contact_links(page: str, base: str) -> list:
    links = []
    for m in re.finditer(r'<a\s[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', page, re.I | re.S):
        href, label = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip().lower()
        if re.search(r"contact|about|home", label) or re.search(r"contact", href, re.I):
            links.append(urllib.parse.urljoin(base, html.unescape(href)))
    return links[:2]


def work(c: dict) -> dict:
    page, final = fetch(c["homepage"])
    if page is None:  # many pages answer on the other scheme only (http vs https)
        alt = ("http" if c["homepage"].startswith("https") else "https") + c["homepage"].split(":", 1)[1]
        page, final = fetch(alt)
    res = {"email": "", "source_url": "", "title": "", "fetched": page is not None}
    if page is None:
        return res
    host = urllib.parse.urlparse(final).netloc.lower()
    plain = re.sub(r"<[^>]+>", " ", html.unescape(page))
    t = TITLE.search(plain)
    res["title"] = t.group(1).strip() if t else ""
    email = pick(emails_in(page), c["name"], host)
    src = final
    if not email:
        for link in contact_links(page, final):
            sub, subfinal = fetch(link)
            if sub and (email := pick(emails_in(sub), c["name"], host)):
                src = subfinal
                break
    if email:
        res.update(email=email, source_url=src)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--retry-unreachable", action="store_true", help="fetch the homepages that failed last time again")
    args = ap.parse_args()
    cands = json.loads((DATA / "pure_ml_candidates.json").read_text())
    done = json.loads(OUT.read_text()) if OUT.exists() else {}
    if args.retry_unreachable:
        done = {h: v for h, v in done.items() if v["fetched"]}
    todo = [c for c in cands if c["homepage"] not in done]
    with ThreadPoolExecutor(args.workers) as ex:
        for i, (c, r) in enumerate(zip(todo, ex.map(work, todo)), 1):
            done[c["homepage"]] = r
            if i % 100 == 0:
                OUT.write_text(json.dumps(done, ensure_ascii=False))
                print(f"{i}/{len(todo)} done; emails so far {sum(1 for v in done.values() if v['email'])}", flush=True)
    OUT.write_text(json.dumps(done, ensure_ascii=False))
    got = sum(1 for c in cands if done[c["homepage"]]["email"])
    print(f"emails found for {got} of {len(cands)} homepages; unreachable: "
          f"{sum(1 for c in cands if not done[c['homepage']]['fetched'])}")


if __name__ == "__main__":
    main()
