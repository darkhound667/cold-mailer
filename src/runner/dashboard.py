# -*- coding: utf-8 -*-
"""Phase 4: build the passphrase-locked dashboard data (decision Q12/Q33).

Reads the public state and writes site/data.enc.json, encrypted with DASHBOARD_PASSPHRASE
(crypto.encrypt_for_browser), which site/index.html decrypts in the browser. The runner commits
the file with the state; the page (on GitHub Pages) fetches the latest copy from the repo, so a
redeploy is only needed when the page itself changes. Without the passphrase it shows only a lock. No email addresses
are included, only the public-safe fields plus every status transition (UTC + local).

Usage (in the workflow, after tick.py):
    DASHBOARD_PASSPHRASE=... python src/runner/dashboard.py
"""
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner import crypto  # noqa: E402
from runner.state import STATE_DIR, iso  # noqa: E402

SITE = Path(__file__).resolve().parents[2] / "site"
COLUMNS = ("id", "name", "university", "country", "env_domain", "tier", "subject", "status",
           "reply_label", "send_local", "send_utc", "timezone", "body", "events")


def build(state_dir: Path = STATE_DIR) -> dict:
    emails = json.loads((state_dir / "emails.json").read_text()) if (state_dir / "emails.json").exists() else []
    hb = json.loads((state_dir / "heartbeat.json").read_text()) if (state_dir / "heartbeat.json").exists() else {}
    return {"generated_utc": iso(datetime.now(timezone.utc)), "last_poll_utc": hb.get("last_poll_utc"),
            "counts": dict(Counter(e["status"] for e in emails)),
            "labels": dict(Counter(e.get("reply_label") or "none" for e in emails)),
            "rows": [{c: e.get(c) for c in COLUMNS} for e in emails]}


def write(state_dir: Path = STATE_DIR) -> bool:
    """Encrypt the current state into site/data.enc.json (committed with the state). The page
    reads it from the repo, so the dashboard is current after every commit without a redeploy."""
    passphrase = os.environ.get("DASHBOARD_PASSPHRASE", "")
    if not passphrase:
        print("DASHBOARD_PASSPHRASE not set: dashboard not updated")  # never publish it unlocked
        return False
    SITE.mkdir(exist_ok=True)
    (SITE / "data.enc.json").write_text(json.dumps(crypto.encrypt_for_browser(build(state_dir), passphrase)))
    return True


def main() -> None:
    if not write():
        sys.exit(1)
    print("wrote site/data.enc.json")


if __name__ == "__main__":
    main()
