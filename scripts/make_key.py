# -*- coding: utf-8 -*-
"""Create the DATA_KEY for encrypting private state (decision Q34).

Writes a random 32-byte key, base64-encoded, to .data_key (gitignored) and prints nothing
secret. Then, yourself:
    gh secret set DATA_KEY -R darkhound667/cold-mailer < .data_key
and keep a copy of .data_key somewhere offline (password manager): without it the encrypted
state in the repo can't be read.
"""
import base64
import os
import sys
from pathlib import Path

path = Path(__file__).resolve().parents[1] / ".data_key"
if path.exists():
    sys.exit(f"{path.name} already exists; not overwriting (delete it yourself if you really mean to)")
path.write_text(base64.b64encode(os.urandom(32)).decode() + "\n")
path.chmod(0o600)
print(f"wrote {path.name}. Next: gh secret set DATA_KEY -R darkhound667/cold-mailer < {path.name}")
