> **cold-mailer** — research-internship outreach system, built in gated phases on top of
> [Accayo14/Cold-mail-outreach-automation](https://github.com/Accayo14/Cold-mail-outreach-automation)
> (MIT, see `LICENSE`). Phase 0: only `scripts/check_mail_login.py` and its manual
> `check-mail-login` workflow are new; nothing here sends email yet.

# Cold Mail Outreach Automation

A pipeline for running a personalized, research-fit-aware cold email campaign to professors —
built for reaching out about research opportunities, but the pattern generalizes to any
"find a list of people, learn what each of them actually does, write each of them a
genuinely relevant email, then send in reviewable batches" workflow.

It does **not** try to blast generic mail-merge messages. The whole point is that every email
is written using the recipient's actual, current research description (not a job title, not a
department label), and every batch is meant to be reviewed by a human before anything sends.

## What it does

1. **Scrape** — pull a faculty list plus each person's real research bio from a university's
   own site (see `src/scrape/` for worked examples against a few common site patterns).
2. **Classify** — bucket each person into a research domain (ML, NLP, Computer Vision, ...)
   based on the *content* of their bio, not just their job title — titles lie more often than
   you'd expect (`src/classify/domain_classifier.py`).
3. **Generate** — write a personalized email per person: an opening that names 1-2 of *their*
   actual topics (never their whole research portfolio — that reads as "I didn't really read
   your bio"), a background paragraph built from your own project blurbs (ordered by relevance
   to their domain), and a consistent closing (`src/generate/email_generator.py`).
4. **Export** — a multi-sheet Excel workbook, one sheet per domain, for human review
   (`src/export/build_workbook.py`).
5. **Send** — a batch mailer (Jupyter notebook for supervised runs, or a standalone script for
   scheduled/unattended runs) that sends a reviewed CSV batch over SMTP, with per-send delay and
   a persistent sent-log so re-running never double-sends (`src/send/`).

## Why it's structured this way

This came out of actually running the campaign, not from a design doc. A few of the choices
exist specifically because of mistakes made along the way:

- **Domain classification is checked against bio content, not just trusted from the person's
  title.** Titles like "Professor of Computer Science" tell you nothing about whether someone
  works on vision, security, or theory. Classifying by title alone silently sent the most
  specific, most reusable version of the flagship-project pitch to the wrong bucket for a
  meaningful fraction of people — including, in one run, two of the most prominent researchers
  at the target institution, who ended up with a generic email instead of a personalized one.
- **Every batch is written to a plain-text review file before anything is queued to send.**
  No email goes out without a human reading it first.
- **The mailer never fabricates a recipient's email address.** If it wasn't found published on
  an official page, the field stays blank. Pattern-guessing addresses (`firstname.lastname@...`)
  produces bounces and looks careless.
- **Text-processing helpers that trim or re-case scraped text are the single most bug-prone
  part of this pipeline.** A naive word-by-word regex trim silently dropped internal
  hyphens and duplicated fragments of text (`"Cross-language"` → `"crosslanguage"`,
  `"Tan Lip-Bu"` → `"Lip-Bulipbu"`). If you touch `domain_classifier.py`'s topic-trimming
  logic, re-run it against real scraped text afterward and diff a sample by eye — this class of
  bug doesn't throw an exception, it just quietly corrupts a fraction of your output.
- **Credentials are never hardcoded.** The mailer reads `SENDER_EMAIL` / `SENDER_APP_TOKEN` /
  `SENDER_NAME` from the environment (see `.env.example`). Don't put real values in any tracked
  file.

## Setup

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # fill in your own background/projects
cp .env.example .env                 # fill in your own SMTP credentials
```

Edit `config.yaml`:
- `sender`: your name, degree, institution — used in the signature and salutation logic.
- `flagship_projects`: 1-3 of your strongest projects, written as ready-to-paste sentences.
  These appear in every email, ordered by relevance to the recipient's domain.
- `secondary_projects`: shorter, domain-specific blurbs (e.g. an NLP project, a CV project) —
  only the one matching the recipient's bucket gets appended.
- `domains`: the keyword lists used to classify bios. Tune these to your field.

## Usage

```bash
# 1. Scrape (site-specific — see src/scrape/ for patterns to adapt)
python src/scrape/your_university_scraper.py --out data/professors.json

# 2. Classify + generate emails
python src/generate/email_generator.py \
    --input data/professors.json \
    --config config.yaml \
    --output data/emails.json

# 3. Export for human review
python src/export/build_workbook.py --input data/emails.json --output Professors.xlsx

# 4. Review the .xlsx, then build a send batch (CSV) for whichever rows you approve.

# 5. Send (interactively, via the notebook) or schedule (via the standalone script)
python src/send/send_batch.py --batch data/batch_01.csv
```

## Repo layout

```
config.example.yaml        # background/projects/domain config template
.env.example                # required environment variables for sending mail
src/
  scrape/                   # example scrapers for common site patterns (WordPress REST API,
                             # embedded JSON-LD, generic HTML directory listing)
  classify/
    domain_classifier.py    # keyword-based bio -> domain-bucket classifier
  generate/
    email_generator.py      # config-driven personalized email + subject generator
  export/
    build_workbook.py       # multi-sheet Excel builder (one sheet per domain)
  send/
    mailer_notebook.ipynb   # interactive, supervised batch sender (dry-run + send + dashboard)
    send_batch.py           # standalone unattended sender (for scheduled/cron runs)
examples/
  sample_professors.json    # a handful of fictional example records (schema reference)
  sample_batch.csv          # a fictional example send-batch (schema reference)
docs/
  PIPELINE.md                # longer write-up of the methodology and lessons learned
```

## What's *not* in this repo

No real scraped data, no real email addresses, no credentials, no resume, no identifying
information about who originally built this. `examples/` uses entirely fictional names. If
you use this, your own scraped data and your own `config.yaml`/`.env` should stay local
(both are gitignored).

## License

MIT — see `LICENSE`.
