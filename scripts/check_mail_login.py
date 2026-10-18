# -*- coding: utf-8 -*-
"""Phase 0 feasibility check: can this machine authenticate to the sender's SMTP and IMAP
servers? Logs in and out of each. It sends nothing, reads no mail and prints no credentials.

Environment (GitHub Secrets in Actions, or a local .env):
    SENDER_EMAIL       the mailbox address (required)
    SENDER_APP_TOKEN   password / token for that mailbox (required)
    SMTP_USER          login name if it differs from SENDER_EMAIL (optional)
    SMTP_SERVER        default smtp-auth.iitb.ac.in
    SMTP_PORT          default 587 (STARTTLS)
    IMAP_SERVER        default imap.iitb.ac.in
    IMAP_PORT          default 993 (TLS)

Exit code 0 only if both logins succeed.
"""
import imaplib
import os
import smtplib
import ssl
import sys


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing required environment variable: {name}")
    return val


def check_smtp(host: str, port: int, user: str, token: str) -> bool:
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            code, _ = smtp.login(user, token)
            print(f"SMTP {host}:{port}  login ok (code {code})")
        return True
    except Exception as e:  # report the failure type only; server text may echo the username
        print(f"SMTP {host}:{port}  FAILED: {type(e).__name__}")
        return False


def check_imap(host: str, port: int, user: str, token: str) -> bool:
    try:
        imap = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)
        imap.login(user, token)
        # EXAMINE opens INBOX read-only; only the status word is printed, never counts or mail.
        status, _ = imap.select("INBOX", readonly=True)
        imap.logout()
        print(f"IMAP {host}:{port}  login ok, INBOX read-only select: {status}")
        return status == "OK"
    except Exception as e:
        print(f"IMAP {host}:{port}  FAILED: {type(e).__name__}")
        return False


def main() -> None:
    email = require_env("SENDER_EMAIL")
    token = require_env("SENDER_APP_TOKEN")
    user = os.environ.get("SMTP_USER") or email

    smtp_ok = check_smtp(os.environ.get("SMTP_SERVER") or "smtp-auth.iitb.ac.in",
                         int(os.environ.get("SMTP_PORT") or 587), user, token)
    imap_ok = check_imap(os.environ.get("IMAP_SERVER") or "imap.iitb.ac.in",
                         int(os.environ.get("IMAP_PORT") or 993), user, token)

    print(f"RESULT smtp={'pass' if smtp_ok else 'fail'} imap={'pass' if imap_ok else 'fail'}")
    sys.exit(0 if smtp_ok and imap_ok else 1)


if __name__ == "__main__":
    main()
