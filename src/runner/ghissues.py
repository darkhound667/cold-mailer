# -*- coding: utf-8 -*-
"""GitHub Issues through the `gh` CLI (preinstalled on Actions runners, authenticated by GH_TOKEN).

Issues are public, so titles and bodies never name a professor or quote a reply:
  - "New reply #N" (label `reply`): Ruturva reads the reply in his inbox, then adds the label
    `positive` or `negative`; the next tick copies that label onto the email (decision Q4/Q10).
  - `needs-ruturva`: anything the runner must not decide alone (ask_protocol).
"""
import json
import subprocess

LABELS = {"reply": "0e8a16", "positive": "1d76db", "negative": "b60205", "needs-ruturva": "d93f0b"}


class GhIssues:
    def __init__(self, repo: str):
        self.repo = repo
        self._labels_ready = False

    def _gh(self, *args) -> str:
        return subprocess.run(["gh", *args, "-R", self.repo], check=True, capture_output=True, text=True).stdout

    def _ensure_labels(self) -> None:
        if not self._labels_ready:
            for name, color in LABELS.items():
                subprocess.run(["gh", "label", "create", name, "--color", color, "--force", "-R", self.repo],
                               capture_output=True, text=True)
            self._labels_ready = True

    def open(self, title: str, body: str, labels: list) -> int:
        self._ensure_labels()
        url = self._gh("issue", "create", "--title", title, "--body", body, *sum((["--label", l] for l in labels), []))
        return int(url.strip().rsplit("/", 1)[-1])

    def previous_run_utc(self):
        """When the previous run of this workflow last updated, i.e. finished (None if none yet)."""
        from datetime import datetime
        out = self._gh("run", "list", "--workflow", "tick.yml", "--limit", "2", "--json", "updatedAt")
        runs = json.loads(out)
        return datetime.fromisoformat(runs[1]["updatedAt"].replace("Z", "+00:00")) if len(runs) > 1 else None

    def labelled(self, label: str) -> list:
        """[{number, labels: [names]}] for all issues (open or closed) with `label`."""
        out = self._gh("issue", "list", "--label", label, "--state", "all", "--limit", "1000",
                       "--json", "number,labels")
        return [{"number": i["number"], "labels": [l["name"] for l in i["labels"]]} for i in json.loads(out)]
