# -*- coding: utf-8 -*-
"""Classify a person's research bio into one of the domain buckets defined in config.yaml,
and extract a short (1-2 topic) phrase from their bio to use in the email's opening line.

IMPORTANT — read before editing the regex/trim logic below:
Text-trimming bugs here are the single most common source of corrupted output in this
pipeline, and they don't throw exceptions — they just silently mangle a fraction of your
emails. Two real bugs hit in production use of this pattern, both worth knowing about:

  1. A naive `re.sub(r"[^a-zA-Z0-9]", "", word)` to find a word's "core" for casing
     purposes strips internal hyphens too, not just leading/trailing punctuation, e.g.
     "Cross-language" -> "crosslanguage". If you need to normalize casing or punctuation,
     prefer `re.sub(r"[A-Za-z]+", repl, text)` (transform only alphabetic runs, leave
     everything else in place) over any approach that rebuilds the string from a
     stripped "core" plus reassembled prefix/suffix — the reassembly step is exactly
     where duplication bugs like "Tan Lip-Bu" -> "Lip-Bulipbu" come from.
  2. A word-by-word `.strip(punctuation)` pass silently deletes commas and semicolons
     that were acting as separators between topic items, turning
     "computational intelligence, multi-agent systems" into a run-on
     "computational intelligence multi-agent systems" with no punctuation at all.

After changing anything here, run it against a sample of real scraped bios and read the
output by eye — don't just check that it runs without error.
"""
import re


def score_bio(bio: str, domains: dict) -> dict:
    """Return {domain_name: hit_count} for every domain whose keywords appear in bio."""
    b = " " + bio.lower() + " "
    scores = {}
    for name, cfg in domains.items():
        c = sum(b.count(kw.lower()) for kw in cfg.get("keywords", []))
        if c:
            scores[name] = c
    return scores


def classify(bio: str, domains: dict, default: str = "Other") -> str:
    """Pick the domain bucket with the most keyword hits. Ties break in config order."""
    scores = score_bio(bio, domains)
    if not scores:
        return default
    return max(scores, key=scores.get)


CANONICAL_ACRONYMS = {
    "ai": "AI", "nlp": "NLP", "hci": "HCI", "iot": "IoT", "bci": "BCI", "llm": "LLM",
    "llms": "LLMs", "3d": "3D", "vlm": "VLM", "gnn": "GNN", "dsp": "DSP", "eeg": "EEG",
}


def sentence_case(topic: str) -> str:
    """Lowercase a scraped topic phrase for mid-sentence use, preserving known acronyms
    and leaving all punctuation (hyphens, commas, parentheses) untouched in place."""
    def repl(m: re.Match) -> str:
        word = m.group(0)
        return CANONICAL_ACRONYMS.get(word.lower(), word.lower())
    return re.sub(r"[A-Za-z]+", repl, topic)


def trim_topic(topic: str, max_items: int = 2) -> str:
    """Collapse a comma/semicolon/and-separated list of research areas down to the
    first `max_items`, joined with 'and'. Leaves short (already <= max_items) phrases
    untouched so their original punctuation is preserved."""
    items = re.split(r";\s*|,\s*(?:and\s+)?|\s+and\s+", topic)
    items = [i.strip() for i in items if i.strip()]
    if len(items) <= max_items:
        return topic.replace(";", ",")
    return " and ".join(items[:max_items])


TOPIC_PATTERNS = [
    r"research interests?\s+(?:span|spans|include|are)\s+(.*?)(?:\.\s|,\s+including|,\s+with|$)",
    r"research focuses? on\s+(.*?)(?:\.\s|$)",
    r"research\s+(?:lies|lie)\s+at the intersection of\s+(.*?)(?:\.\s|,\s+with|$)",
    r"research interests? include\s+(.*?)(?:\.\s|,\s+including|$)",
    r"is broadly interested in\s+(.*?)(?:\.\s|,\s+with|$)",
    r"is interested in\s+(.*?)(?:\.\s|$)",
]


def extract_topic(bio: str, fallback: str, max_items: int = 2) -> str:
    """Pull a short topic phrase directly out of a bio using common self-description
    patterns ("research interests include X, Y and Z", "focuses on X"). Falls back to
    a generic domain-name phrase if nothing matches — better an honest generic line
    than a mangled one stitched from a pattern that didn't actually fit."""
    for pat in TOPIC_PATTERNS:
        m = re.search(pat, bio, re.IGNORECASE)
        if m:
            phrase = m.group(1).strip().rstrip(",.; ")
            if 15 < len(phrase) < 140:
                return sentence_case(trim_topic(phrase, max_items))
    return fallback
