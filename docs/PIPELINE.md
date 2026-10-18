# Pipeline methodology

This is a longer write-up of how each stage works and why, for anyone adapting this to
their own outreach campaign.

## 1. Scrape

The goal is a real, current research description per person — not their job title, and
not a category label someone else assigned them. Titles like "Professor of Computer
Science" say nothing about whether someone works on vision, security, or theory; two
people with the identical title can have zero research overlap.

In practice, most university sites fall into one of three buckets (see
`src/scrape/scraping_patterns.py` for the matching code):

- **WordPress-backed sites** often expose a REST API (`/wp-json/wp/v2/<type>?slug=...`)
  that returns a person's full bio as clean-ish HTML, much faster than fetching and
  parsing rendered pages one at a time.
- **Sites with embedded JSON-LD** (`<script type="application/ld+json">`) frequently put
  a person's bio right in the page source, keyed by an `"identifier"` field with their
  name, immediately followed by a `"description"` field with their bio. Watch out: the
  same page often *also* has an unrelated, generic page-level meta description earlier
  in the HTML using a bare `"description":"..."` key with no identifier nearby — anchor
  your regex to the identifier so you don't grab the wrong one.
- **Static server-rendered directory listings** sometimes put a short research-area tag
  inline for every person on one page, with no separate profile page needed at all. This
  is thinner data (a tag, not a paragraph) but can cover an entire department in a single
  fetch, versus 100+ individual requests to a per-person page — several of which, in
  practice, turn out to be a client-side SPA a plain HTTP fetch can't see into at all.

A recurring quality issue on the static-directory pattern: some professors' entries have
their research-area field overwritten by their honorific title ("President's Chair
Professor and Dean") instead of an actual description — usually the more senior/famous
faculty, ironically. If you see this on a handful of entries, look those specific people
up individually rather than leaving "Dean" as their stated research area.

**Never fabricate or pattern-guess an email.** Only set it when actually found published
on an official page. A blank field is honest; a guessed `firstname.lastname@domain.edu`
produces bounces and reads as careless if it lands on a monitored catch-all inbox.

## 2. Classify

`src/classify/domain_classifier.py` scores a bio's lowercase text against each domain's
keyword list and picks the highest-scoring bucket. It's crude on purpose — a full ML
classifier is overkill for a few hundred people you're going to review by eye anyway —
but crude scoring has a real failure mode worth knowing: **keywords that are substrings
of each other double-count a single mention.** `"medical"` is a substring of
`"biomedical"`, so a bio that says "biomedical" once scores 2 for a domain that lists
both, potentially outweighing a genuine match elsewhere. Keep each domain's keyword list
to genuinely distinct terms, or account for the overlap deliberately.

Buckets with no real project overlap for your background should be marked `weak_fit:
true` in config — they get an honest, openly generic email ("interested in your group's
work on X") rather than a forced, over-specific pitch stitched from a thin match.

**Sanity-check the classifier against real content, not titles**, once you have a batch
of real scraped data. In production use of this pattern, roughly a quarter of people at
one target institution turned out to be misclassified when checked against their actual
bio instead of trusted from their department label — including, in one case, two of the
most prominent researchers at the institution, who would otherwise have received the
generic weak-fit email instead of a properly personalized one.

## 3. Generate

`src/generate/email_generator.py` builds each email from `config.yaml`: an opening line
naming 1-2 of the recipient's *actual* topics (not their whole research portfolio — that
reads as "I copy-pasted your bio"), a background paragraph assembled from your flagship
project blurbs (ordered by relevance to their bucket) plus one domain-specific secondary
blurb, and a fixed closing.

Topic-phrase extraction (`extract_topic` in the classifier module) tries a handful of
common self-description patterns first ("research interests include X, Y and Z") and
falls back to a generic domain-name phrase if nothing matches cleanly — deliberately,
since a bad regex match stitched into a sentence produces something worse than an honest
generic line. Expect roughly 15-25% of real bios to need a hand-written topic override
even with a decent pattern list; that's normal, not a sign something's broken.

**The single most bug-prone part of this pipeline is text normalization** — anything that
trims, re-cases, or reformats scraped text. Two real corruption bugs hit in production
use, neither of which threw an exception:

- A naive approach to stripping a word's punctuation for casing purposes
  (`re.sub(r"[^a-zA-Z0-9]", "", word)`) strips *internal* hyphens too, not just
  leading/trailing punctuation — `"Cross-language"` silently became `"crosslanguage"`.
  A more broken version of the same idea, which tried to reassemble the original
  punctuation around a stripped "core", instead duplicated fragments of text:
  `"Tan Lip-Bu"` became `"Lip-Bulipbu"`.
- A word-by-word punctuation strip deleted commas that were acting as separators
  between list items, turning `"computational intelligence, multi-agent systems"` into
  a run-on `"computational intelligence multi-agent systems"` with no punctuation
  between the two ideas at all.

The fix in both cases was the same: don't rebuild strings from stripped fragments.
Transform only the alphabetic runs in place (`re.sub(r"[A-Za-z]+", repl, text)`) and
leave every other character exactly where it was. If you modify this logic, run it
against real scraped text afterward and read a sample of the output by eye — these bugs
are invisible to `python script.py` running cleanly; they only show up when you actually
read what got generated.

## 4. Export

One Excel sheet per domain bucket, largest first, for human review. Nothing here should
be surprising — it's a straightforward `openpyxl` workbook builder
(`src/export/build_workbook.py`).

## 5. Send

Two sender implementations, same underlying logic:

- `src/send/mailer_notebook.ipynb` — interactive, for supervised runs where you want to
  watch each send happen (dry run first, review the output, then send).
- `src/send/send_batch.py` — a standalone script with no notebook/kernel dependency, for
  scheduled/unattended runs (cron, Windows Task Scheduler).

Both read every credential from environment variables (`.env` — see `.env.example`),
never hardcoded. Both track sent addresses in a per-batch log CSV so re-running a batch
never double-sends. Both cap the send rate (60s between sends by default) — a burst of
sends in a few minutes on a shared institutional relay is what gets accounts
rate-limited or suspended, and the account behind that relay is often also your primary
login.

**A note on IMAP "Sent" copies:** SMTP only relays a message — it doesn't store a copy
anywhere on your end. If you want proof in your own Sent folder, the mailer can `APPEND`
a copy via IMAP after each send. Some institutional networks restrict IMAP access to
on-campus/VPN connections even when SMTP sending works fine from anywhere; if IMAP login
fails, sending itself still works, you just won't get the automatic Sent-folder mirror.

**Always send one test email to yourself first**, with the real credentials and the
real resume attachment, before touching a real batch. Confirm it arrived, the resume
opened correctly, and both the plain-text and HTML rendering look right.
