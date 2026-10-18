# -*- coding: utf-8 -*-
"""Reusable patterns for pulling faculty bios off university sites in bulk, instead of
one search/fetch per person. There's no single universal scraper — every university's
site is different — but in practice most fall into one of these three buckets. Find
which one your target site is, then adapt the matching function below.

Output schema everywhere in this pipeline: a list of
    {"name": str, "email": str | None, "title": str, "bio": str}
Write your adapted scraper to emit exactly this.

NEVER fabricate or pattern-guess an email address (e.g. assuming
firstname.lastname@department.edu). Only set `email` when you found it actually
published on the page. Leave it None otherwise — a blank field is honest, a guessed
one bounces.
"""
import json
import re
from typing import Optional
from urllib.request import Request, urlopen

USER_AGENT = "Mozilla/5.0 (compatible; research-outreach-scraper/1.0)"


def fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Pattern 1: WordPress REST API
# ---------------------------------------------------------------------------
# Many university department sites run WordPress. If a personal profile page loads
# at e.g. https://dept.university.edu/people/jane-doe/, check whether
# https://dept.university.edu/wp-json/wp/v2/<post_type>?slug=jane-doe returns JSON
# with a "content" field containing their bio — this is far faster than fetching
# rendered HTML one page at a time, and you can often list every slug in bulk from
# the same endpoint with per_page=100&_fields=title,slug (paginate with &page=2, etc).
#
# One caveat: some hosts silently truncate a request for many slugs at once
# (`?slug=a,b,c,d`) down to just 1-2 results with no error — always fetch one slug at
# a time in production, even though batching multiple slugs together looks tempting.

def scrape_wordpress_profile(base_url: str, post_type: str, slug: str) -> dict:
    url = f"{base_url}/wp-json/wp/v2/{post_type}?slug={slug}&_fields=title,content"
    data = json.loads(fetch(url))
    if not data:
        return {}
    entry = data[0]
    title = entry.get("title", {}).get("rendered", "")
    content_html = entry.get("content", {}).get("rendered", "")
    bio = re.sub(r"<[^>]+>", " ", content_html)
    bio = re.sub(r"\s{2,}", " ", bio).strip()
    emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", content_html)
    return {"name": title, "email": emails[0] if emails else None, "title": "", "bio": bio}


# ---------------------------------------------------------------------------
# Pattern 2: embedded JSON-LD / schema.org block
# ---------------------------------------------------------------------------
# Many modern academic sites embed a JSON-LD <script type="application/ld+json">
# block per person, often with an "identifier" (name) field immediately followed by
# a "description" field (their bio) — but the SAME page often also has an unrelated
# generic page-level meta description earlier in the HTML using a bare
# "description":"..." key. Anchor your regex to the field that comes right after
# "identifier" so you don't accidentally grab the wrong one.

def scrape_jsonld_profile(html: str) -> dict:
    m = re.search(r'"identifier":"([^"]*)","description":"((?:[^"\\]|\\.)*)"', html)
    if not m:
        return {}
    name = json.loads('"' + m.group(1) + '"')
    bio_raw = json.loads('"' + m.group(2) + '"')
    bio = re.sub(r'<a\s+href="mailto:[^"]*">[^<]*</a>', "", bio_raw)
    bio = re.sub(r"<[^>]+>", " ", bio)
    bio = re.sub(r"\s{2,}", " ", bio).strip()
    emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", html)
    return {"name": name, "email": emails[0] if emails else None, "title": "", "bio": bio}


# ---------------------------------------------------------------------------
# Pattern 3: static directory listing with inline "focus"/research-area tags
# ---------------------------------------------------------------------------
# Some department "faculty directory" pages render server-side (not a JS SPA) and
# list every person with a name, title, and a short research-area tag right there —
# no per-person page needed at all. This gives you thinner data (a tag, not a full
# bio paragraph) but in one fetch for the whole department. If a page like this
# exists for your target site, prefer it — it's both faster and more complete than
# chasing individual profile pages, several of which are often JS-rendered SPAs that
# a plain HTTP fetch can't see into at all.
#
# There's no generic regex for this one since every site's markup differs; fetch the
# page, inspect it, and write a one-off parser. If a handful of senior/chair
# professors on the page show their honorific title in the research-area slot instead
# of an actual research description (a real thing that happens — the field gets
# overwritten by whatever's most "important" about the person from the site's point
# of view), look those specific few up individually rather than leaving them with a
# meaningless "President's Chair Professor" as their stated research topic.


# ---------------------------------------------------------------------------
# Bot-protected sites
# ---------------------------------------------------------------------------
# Some university sites sit behind bot-protection (Incapsula and similar) that blocks
# plain automated fetches with a 403, even though the page loads fine in a real
# browser. If you hit this: open the page in your own logged-in browser, copy its
# session cookies, and pass them explicitly on the request (most languages' HTTP
# libraries support setting cookies on a session/request object) — see your HTTP
# client's docs for "session cookies" or "cookie jar". This is a workaround for
# *your own* legitimate access being blocked by an overzealous bot filter, not a way
# around any access control that isn't yours to bypass.


def resolve_optional(value: Optional[str]) -> Optional[str]:
    """Small helper: normalize an empty-string scrape result to None rather than "" ,
    so downstream code can rely on a single falsy check."""
    return value if value else None
